from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
import re, os, httpx, json, sqlite3, hashlib, secrets, time

app = FastAPI(title="QuickPrint Payment Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION ---
ADMIN_SECRET     = os.getenv("ADMIN_SECRET",     "change-this-admin-secret")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8472478534:AAFFmBnSmYtFveznUFRxyrF0NbbIRMe1mDU")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6509973320")
DB_PATH          = os.getenv("DB_PATH",           "/app/quickprint.db")

TXN_EXPIRY_HOURS = 24
transaction_store: dict = {}

SMS_PATTERNS = [
    r"TxnID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"TrxID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"Transaction\s*ID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"TID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"ID\s*[:\-]?\s*([A-Za-z0-9]{8,20})",
]
AMOUNT_PATTERN = r"(?:Tk|BDT)\s*([\d,]+(?:\.\d+)?)"

# --- MODELS ---

class RegisterPayload(BaseModel):
    phone: str
    password: str
    name: str

class LoginPayload(BaseModel):
    phone: str
    password: str

class BuyCreditsPayload(BaseModel):
    txnId: str
    amount: float
    method: str
    token: str

class UseCreditsPayload(BaseModel):
    amount: float
    token: str
    description: Optional[str] = "Print job"

class VerifyPayload(BaseModel):
    txnId: str
    method: str
    amount: float
    token: Optional[str] = None

class AdminCreditPayload(BaseModel):
    phone: str
    amount: float
    note: Optional[str] = "Manual top-up"

class SMSPayload(BaseModel):
    sender: str
    message: str
    method: Optional[str] = None

# --- DATABASE SETUP ---

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        credits REAL DEFAULT 0,
        token TEXT,
        is_admin INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS credit_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_phone TEXT NOT NULL,
        type TEXT NOT NULL,
        amount REAL NOT NULL,
        txn_id TEXT,
        method TEXT,
        description TEXT,
        balance_after REAL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_phone TEXT,
        txn_id TEXT,
        amount REAL,
        method TEXT,
        files INTEGER,
        pages INTEGER,
        copies INTEGER,
        location TEXT,
        print_mode TEXT,
        paper_size TEXT,
        paid_with TEXT DEFAULT 'txn',
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()
    conn.close()
    print("DEBUG: Database initialized")

init_db()

# --- HELPERS ---

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def generate_token() -> str:
    return secrets.token_hex(32)

def get_user_by_token(token: str):
    if not token:
        return None
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE token = ?", (token,)).fetchone()
    conn.close()
    return user

def extract_txn_id(message: str) -> Optional[str]:
    for pattern in SMS_PATTERNS:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None

def extract_amount(message: str) -> Optional[float]:
    match = re.search(AMOUNT_PATTERN, message, re.IGNORECASE)
    return float(match.group(1).replace(",", "")) if match else None

def detect_method(sender: str, message: str) -> str:
    combined = (sender + message).lower()
    if "bkash" in combined: return "bkash"
    if "nagad" in combined: return "nagad"
    return "unknown"

def clean_expired():
    cutoff = datetime.utcnow() - timedelta(hours=TXN_EXPIRY_HOURS)
    expired = [k for k, v in transaction_store.items() if v["received_at"] < cutoff]
    for k in expired:
        del transaction_store[k]

def store_txn(txn_id: str, amount, method: str, sender: str, raw: str):
    transaction_store[txn_id] = {
        "method": method, "amount": amount, "sender": sender,
        "received_at": datetime.utcnow(), "used": False, "raw": raw[:200],
    }

async def send_telegram(chat_id: str, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )

async def process_sms_text(text: str, chat_id: str):
    txn_id = extract_txn_id(text)
    if not txn_id:
        await send_telegram(chat_id, "❌ No transaction ID found in message.")
        return {"ok": True, "txn_found": False}
    amount = extract_amount(text)
    method = detect_method("", text)
    clean_expired()
    store_txn(txn_id, amount, method, "telegram", text)
    await send_telegram(chat_id, f"✅ TXN saved!\nID: {txn_id}\nAmount: {amount or 'Unknown'} TK\nMethod: {method}")
    return {"ok": True, "txn_found": True, "txn_id": txn_id}

# --- ROUTES ---

@app.get("/")
def root():
    conn = get_db()
    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return {"status": "QuickPrint server is running", "users": user_count, "txns_in_memory": len(transaction_store)}

# ── AUTH ──────────────────────────────────────────────────────

@app.post("/api/register")
def register(payload: RegisterPayload):
    phone = payload.phone.strip()
    if not re.match(r'^01[3-9]\d{8}$', phone):
        raise HTTPException(status_code=400, detail="Invalid Bangladeshi phone number")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(status_code=409, detail="Phone number already registered")
    token = generate_token()
    conn.execute(
        "INSERT INTO users (phone, name, password_hash, token) VALUES (?, ?, ?, ?)",
        (phone, payload.name.strip(), hash_password(payload.password), token)
    )
    conn.commit()
    conn.close()
    return {"ok": True, "token": token, "name": payload.name.strip(), "phone": phone, "credits": 0}

@app.post("/api/login")
def login(payload: LoginPayload):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE phone = ?", (payload.phone.strip(),)).fetchone()
    if not user or user["password_hash"] != hash_password(payload.password):
        conn.close()
        raise HTTPException(status_code=401, detail="Invalid phone or password")
    token = generate_token()
    conn.execute("UPDATE users SET token = ? WHERE phone = ?", (token, payload.phone.strip()))
    conn.commit()
    conn.close()
    return {"ok": True, "token": token, "name": user["name"], "phone": user["phone"], "credits": user["credits"], "is_admin": bool(user["is_admin"])}

@app.get("/api/profile")
def get_profile(x_token: Optional[str] = Header(None)):
    user = get_user_by_token(x_token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    conn = get_db()
    history = conn.execute(
        "SELECT * FROM credit_transactions WHERE user_phone = ? ORDER BY created_at DESC LIMIT 20",
        (user["phone"],)
    ).fetchall()
    orders = conn.execute(
        "SELECT * FROM orders WHERE user_phone = ? ORDER BY created_at DESC LIMIT 10",
        (user["phone"],)
    ).fetchall()
    conn.close()
    return {
        "ok": True,
        "name": user["name"],
        "phone": user["phone"],
        "credits": user["credits"],
        "is_admin": bool(user["is_admin"]),
        "credit_history": [dict(h) for h in history],
        "orders": [dict(o) for o in orders],
    }

@app.post("/api/logout")
def logout(x_token: Optional[str] = Header(None)):
    user = get_user_by_token(x_token)
    if user:
        conn = get_db()
        conn.execute("UPDATE users SET token = NULL WHERE phone = ?", (user["phone"],))
        conn.commit()
        conn.close()
    return {"ok": True}

# ── CREDITS ───────────────────────────────────────────────────

@app.post("/api/credits/buy")
async def buy_credits(payload: BuyCreditsPayload):
    user = get_user_by_token(payload.token)
    if not user:
        raise HTTPException(status_code=401, detail="Please log in to buy credits")
    clean_expired()
    txn = transaction_store.get(payload.txnId.upper())
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction ID not found. Make sure SMS was received.")
    if txn["used"]:
        raise HTTPException(status_code=409, detail="Transaction already used")
    if txn["amount"] is None:
        raise HTTPException(status_code=400, detail="Could not verify transaction amount")
    if txn["amount"] != payload.amount:
        raise HTTPException(status_code=400, detail=f"Amount mismatch: SMS={txn['amount']} TK, entered={payload.amount} TK")
    transaction_store[payload.txnId.upper()]["used"] = True
    conn = get_db()
    new_credits = user["credits"] + payload.amount
    conn.execute("UPDATE users SET credits = ? WHERE phone = ?", (new_credits, user["phone"]))
    conn.execute(
        "INSERT INTO credit_transactions (user_phone, type, amount, txn_id, method, description, balance_after) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["phone"], "credit", payload.amount, payload.txnId.upper(), payload.method, "Credit purchase", new_credits)
    )
    conn.commit()
    conn.close()
    await send_telegram(TELEGRAM_CHAT_ID, f"💳 Credits purchased!\nUser: {user['name']} ({user['phone']})\nAmount: {payload.amount} TK\nTxnID: {payload.txnId.upper()}\nNew Balance: {new_credits} TK")
    return {"ok": True, "credits": new_credits, "message": f"{payload.amount} TK credits added successfully!"}

@app.post("/api/credits/use")
def use_credits(payload: UseCreditsPayload):
    user = get_user_by_token(payload.token)
    if not user:
        raise HTTPException(status_code=401, detail="Please log in")
    if user["credits"] < payload.amount:
        raise HTTPException(status_code=400, detail=f"Insufficient credits. You have {user['credits']} TK, need {payload.amount} TK")
    conn = get_db()
    new_credits = user["credits"] - payload.amount
    conn.execute("UPDATE users SET credits = ? WHERE phone = ?", (new_credits, user["phone"]))
    conn.execute(
        "INSERT INTO credit_transactions (user_phone, type, amount, description, balance_after) VALUES (?, ?, ?, ?, ?)",
        (user["phone"], "debit", payload.amount, payload.description, new_credits)
    )
    conn.commit()
    conn.close()
    return {"ok": True, "credits": new_credits, "verified": True}

# ── VERIFY (guest or logged in) ───────────────────────────────

@app.post("/api/verify")
async def verify_payment(payload: VerifyPayload):
    clean_expired()

    # If user is logged in and wants to pay with credits
    if payload.token:
        user = get_user_by_token(payload.token)
        if user and user["credits"] >= payload.amount:
            conn = get_db()
            new_credits = user["credits"] - payload.amount
            conn.execute("UPDATE users SET credits = ? WHERE phone = ?", (new_credits, user["phone"]))
            conn.execute(
                "INSERT INTO credit_transactions (user_phone, type, amount, description, balance_after) VALUES (?, ?, ?, ?, ?)",
                (user["phone"], "debit", payload.amount, "Print job payment", new_credits)
            )
            conn.commit()
            conn.close()
            return {"ok": True, "verified": True, "paid_with": "credits", "credits_remaining": new_credits}

    # Guest or insufficient credits — verify by TXN ID
    txn = transaction_store.get(payload.txnId.upper())
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction ID not found. Please check and try again.")
    if txn["used"]:
        raise HTTPException(status_code=409, detail="Transaction already used")
    if txn["amount"] is None:
        raise HTTPException(status_code=400, detail="Could not extract amount from SMS")
    if txn["amount"] != payload.amount:
        raise HTTPException(status_code=400, detail=f"Amount mismatch: SMS={txn['amount']} TK, Order={payload.amount} TK")
    transaction_store[payload.txnId.upper()]["used"] = True
    return {"ok": True, "verified": True, "paid_with": "txn"}

# ── TELEGRAM WEBHOOK ──────────────────────────────────────────

@app.post("/api/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    print(f"DEBUG: Full Update Received: {data}")
    try:
        message_obj = data.get("message") or data.get("channel_post")
        if not message_obj:
            return {"ok": True, "note": "No message in update"}
        if isinstance(message_obj, str):
            try:
                message_obj = json.loads(message_obj)
                text = message_obj.get("text", "")
                chat_id = str(message_obj.get("chat", {}).get("id", TELEGRAM_CHAT_ID))
            except (json.JSONDecodeError, AttributeError):
                text = message_obj
                chat_id = TELEGRAM_CHAT_ID
        else:
            text = message_obj.get("text", "")
            chat_id = str(message_obj["chat"]["id"])
        print(f"DEBUG: From Chat ID: {chat_id} | Text: {text}")
        if chat_id != TELEGRAM_CHAT_ID:
            return {"ok": True, "note": "Ignored — unknown chat"}
        return await process_sms_text(text, chat_id)
    except Exception as e:
        print(f"ERROR: Telegram webhook crashed: {e}")
        return {"ok": False, "error": str(e)}

# ── ADMIN ─────────────────────────────────────────────────────

@app.get("/api/admin/users")
def admin_get_users(x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    users = conn.execute("SELECT id, phone, name, credits, is_admin, created_at FROM users ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"ok": True, "users": [dict(u) for u in users]}

@app.get("/api/admin/orders")
def admin_get_orders(x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    orders = conn.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()
    return {"ok": True, "orders": [dict(o) for o in orders]}

@app.get("/api/admin/transactions")
def admin_get_transactions(x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    txns = conn.execute("SELECT * FROM credit_transactions ORDER BY created_at DESC LIMIT 100").fetchall()
    conn.close()
    return {"ok": True, "transactions": [dict(t) for t in txns]}

@app.post("/api/admin/credits/add")
async def admin_add_credits(payload: AdminCreditPayload, x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE phone = ?", (payload.phone,)).fetchone()
    if not user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found")
    new_credits = user["credits"] + payload.amount
    conn.execute("UPDATE users SET credits = ? WHERE phone = ?", (new_credits, payload.phone))
    conn.execute(
        "INSERT INTO credit_transactions (user_phone, type, amount, description, balance_after) VALUES (?, ?, ?, ?, ?)",
        (payload.phone, "credit", payload.amount, payload.note, new_credits)
    )
    conn.commit()
    conn.close()
    await send_telegram(TELEGRAM_CHAT_ID, f"👑 Admin added {payload.amount} TK credits to {payload.phone}\nNote: {payload.note}\nNew Balance: {new_credits} TK")
    return {"ok": True, "phone": payload.phone, "credits": new_credits}

@app.get("/api/admin/txns")
def admin_get_txns(x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean_expired()
    return {"count": len(transaction_store), "transactions": {k: {**v, "received_at": v["received_at"].isoformat()} for k, v in transaction_store.items()}}

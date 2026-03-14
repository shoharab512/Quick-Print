from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
import re, os, httpx, json

from database import SessionLocal, engine
from models import Base, User, CreditTransaction, Order


# ---------- DATABASE INIT ----------
Base.metadata.create_all(bind=engine)


# ---------- FASTAPI APP ----------
app = FastAPI(title="QuickPrint Payment Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- CONFIG ----------
SMS_PUSH_SECRET  = os.getenv("SMS_PUSH_SECRET",  "change-this-device-secret")
ADMIN_SECRET     = os.getenv("ADMIN_SECRET",     "change-this-admin-secret")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

transaction_store: dict = {}
TXN_EXPIRY_HOURS = 24


# ---------- REQUEST MODELS ----------
class RegisterRequest(BaseModel):
    name: str
    phone: str
    password: str


class LoginRequest(BaseModel):
    phone: str
    password: str


class BuyCreditsRequest(BaseModel):
    user_id: int
    amount: float
    method: str
    txn_id: str


class CreateOrderRequest(BaseModel):
    user_id: int
    files: int
    pages: int
    copies: int
    amount: float


class VerifyPayload(BaseModel):
    txnId: str
    method: str
    amount: float


# ---------- SMS / PAYMENT PARSER ----------
SMS_PATTERNS = [
    r"TxnID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"TrxID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"Transaction\s*ID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
]

AMOUNT_PATTERN = r"(?:Tk|BDT)\s*([\d,]+(?:\.\d+)?)"


def extract_txn_id(message: str):
    for pattern in SMS_PATTERNS:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None


def extract_amount(message: str):
    match = re.search(AMOUNT_PATTERN, message, re.IGNORECASE)
    return match.group(1).replace(",", "") if match else None


def detect_method(sender: str, message: str):
    combined = (sender + message).lower()
    if "bkash" in combined:
        return "bkash"
    if "nagad" in combined:
        return "nagad"
    return "unknown"


def clean_expired():
    cutoff = datetime.utcnow() - timedelta(hours=TXN_EXPIRY_HOURS)
    expired = [k for k, v in transaction_store.items() if v["received_at"] < cutoff]
    for k in expired:
        del transaction_store[k]


def store_txn(txn_id: str, amount, method: str, sender: str, raw: str):
    transaction_store[txn_id] = {
        "method": method,
        "amount": amount,
        "sender": sender,
        "received_at": datetime.utcnow(),
        "used": False,
        "raw": raw[:200],
    }


# ---------- TELEGRAM ----------
async def send_telegram(chat_id: str, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )


async def process_text(text: str, chat_id: str):

    txn_id = extract_txn_id(text)

    if not txn_id:
        await send_telegram(chat_id, "❌ No transaction ID found.")
        return {"ok": True}

    amount = extract_amount(text)
    method = detect_method("", text)

    clean_expired()
    store_txn(txn_id, amount, method, "telegram", text)

    await send_telegram(chat_id, f"✅ TXN saved\nID: {txn_id}\nAmount: {amount}")
    return {"ok": True}


# ---------- ROUTES ----------
@app.get("/")
def root():
    return {"status": "QuickPrint server running"}


@app.post("/api/telegram")
async def telegram_webhook(request: Request):

    data = await request.json()

    message_obj = data.get("message") or data.get("channel_post")

    if not message_obj:
        return {"ok": True}

    text = message_obj.get("text", "")
    chat_id = str(message_obj["chat"]["id"])

    if chat_id != TELEGRAM_CHAT_ID:
        return {"ok": True}

    return await process_text(text, chat_id)


@app.post("/api/verify")
async def verify_payment(payload: VerifyPayload):

    clean_expired()

    txn = transaction_store.get(payload.txnId.upper())

    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if txn["used"]:
        raise HTTPException(status_code=409, detail="Transaction already used")

    if txn["amount"] and float(txn["amount"]) < payload.amount:
        raise HTTPException(status_code=400, detail="Amount mismatch")

    transaction_store[payload.txnId.upper()]["used"] = True

    return {"verified": True}


# ---------- USER SYSTEM ----------
@app.post("/register")
def register(data: RegisterRequest):

    db = SessionLocal()

    user = db.query(User).filter(User.phone == data.phone).first()

    if user:
        raise HTTPException(status_code=400, detail="Phone already registered")

    new_user = User(
        name=data.name,
        phone=data.phone,
        password=data.password
    )

    db.add(new_user)
    db.commit()

    return {"message": "User created"}


@app.post("/login")
def login(data: LoginRequest):

    db = SessionLocal()

    user = db.query(User).filter(User.phone == data.phone).first()

    if not user or user.password != data.password:
        raise HTTPException(status_code=401, detail="Invalid login")

    return {
        "user_id": user.id,
        "name": user.name,
        "credits": user.credits
    }


# ---------- BUY CREDITS ----------
@app.post("/buy-credits")
def buy_credits(data: BuyCreditsRequest):

    db = SessionLocal()

    user = db.query(User).filter(User.id == data.user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.credits += data.amount

    txn = CreditTransaction(
        user_id=data.user_id,
        amount=data.amount,
        type="add",
        method=data.method,
        txn_id=data.txn_id
    )

    db.add(txn)
    db.commit()

    return {"credits": user.credits}


# ---------- CREATE PRINT ORDER ----------
@app.post("/create-order")
def create_order(data: CreateOrderRequest):

    db = SessionLocal()

    user = db.query(User).filter(User.id == data.user_id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.credits < data.amount:
        raise HTTPException(status_code=400, detail="Not enough credits")

    user.credits -= data.amount

    order = Order(
        user_id=data.user_id,
        files=data.files,
        pages=data.pages,
        copies=data.copies,
        amount=data.amount,
        status="paid"
    )

    db.add(order)
    db.commit()

    return {"remaining_credits": user.credits}


# ---------- ADMIN ----------
@app.get("/api/txns")
async def list_transactions(x_admin_secret: Optional[str] = Header(None)):

    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    clean_expired()

    return transaction_store

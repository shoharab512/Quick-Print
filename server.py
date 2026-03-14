from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
import re, os, httpx, json

app = FastAPI(title="QuickPrint Payment Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION ---
SMS_PUSH_SECRET  = os.getenv("SMS_PUSH_SECRET",  "change-this-device-secret")
ADMIN_SECRET     = os.getenv("ADMIN_SECRET",     "change-this-admin-secret")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8472478534:AAFFmBnSmYtFveznUFRxyrF0NbbIRMe1mDU")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6509973320")

transaction_store: dict = {}
TXN_EXPIRY_HOURS = 24

SMS_PATTERNS = [
    r"TxnID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"TrxID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"Transaction\s*ID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"TID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"ID\s*[:\-]?\s*([A-Za-z0-9]{8,20})",
]

AMOUNT_PATTERN = r"(?:Tk|BDT)\s*([\d,]+(?:\.\d+)?)"

# --- HELPERS ---

def extract_txn_id(message: str) -> Optional[str]:
    for pattern in SMS_PATTERNS:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None

def extract_amount(message: str) -> Optional[str]:
    match = re.search(AMOUNT_PATTERN, message, re.IGNORECASE)
    return match.group(1).replace(",", "") if match else None

def detect_method(sender: str, message: str) -> str:
    combined = (sender + message).lower()
    if "bkash" in combined or "01711" in combined: return "bkash"
    if "nagad" in combined or "01711310000" in combined: return "nagad"
    return "unknown"

def clean_expired():
    cutoff = datetime.utcnow() - timedelta(hours=TXN_EXPIRY_HOURS)
    expired = [k for k, v in transaction_store.items() if v["received_at"] < cutoff]
    for k in expired:
        del transaction_store[k]

def store_txn(txn_id: str, amount, method: str, sender: str, raw: str):
    transaction_store[txn_id] = {
        "method":      method,
        "amount":      amount,
        "sender":      sender,
        "received_at": datetime.utcnow(),
        "used":        False,
        "raw":         raw[:200],
    }

async def process_text(text: str, chat_id: str):
    """Extract TXN from text, store it, and send Telegram reply."""
    print(f"DEBUG: Processing text: {text}")

    txn_id = extract_txn_id(text)
    if not txn_id:
        print("DEBUG: No TXN ID matched in this text.")
        await send_telegram(chat_id, "❌ No transaction ID found in message.")
        return {"ok": True, "txn_found": False}

    amount = extract_amount(text)
    method = detect_method("", text)

    clean_expired()
    store_txn(txn_id, amount, method, "telegram", text)
    print(f"DEBUG: Successfully stored TXN: {txn_id}")

    await send_telegram(chat_id, f"✅ TXN saved!\nID: {txn_id}\nAmount: {amount or 'Unknown'} TK\nMethod: {method}")
    return {"ok": True, "txn_found": True, "txn_id": txn_id}

async def send_telegram(chat_id: str, text: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
        print(f"DEBUG: Telegram Reply Status: {resp.status_code}")

# --- ROUTES ---

@app.get("/")
def root():
    return {"status": "QuickPrint server is running", "memory_count": len(transaction_store)}

@app.post("/api/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    print(f"DEBUG: Full Update Received: {data}")

    try:
        message_obj = data.get("message") or data.get("channel_post")
        if not message_obj:
            return {"ok": True, "note": "No message in update"}

        # Handle SMS Forwarder: message is a raw string
        if isinstance(message_obj, str):
            # Try to parse it as JSON string
            try:
                message_obj = json.loads(message_obj)
                text = message_obj.get("text", "")
                chat_id = str(message_obj.get("chat", {}).get("id", TELEGRAM_CHAT_ID))
            except (json.JSONDecodeError, AttributeError):
                # It's just a plain SMS text string
                text = message_obj
                chat_id = TELEGRAM_CHAT_ID
        else:
            text = message_obj.get("text", "")
            chat_id = str(message_obj["chat"]["id"])

        print(f"DEBUG: From Chat ID: {chat_id} | Text: {text}")

        if chat_id != TELEGRAM_CHAT_ID:
            print(f"DEBUG: ID Mismatch! Expected {TELEGRAM_CHAT_ID}, got {chat_id}")
            return {"ok": True, "note": "Ignored — unknown chat"}

        return await process_text(text, chat_id)

    except Exception as e:
        print(f"ERROR: Telegram webhook crashed: {e}")
        return {"ok": False, "error": str(e)}

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
    return {"ok": True, "verified": True, "txn": txn}

@app.get("/api/txns")
async def list_transactions(x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean_expired()
    return {"count": len(transaction_store), "transactions": transaction_store}

# --- MODELS ---

class VerifyPayload(BaseModel):
    txnId:  str
    method: str
    amount: int

class SMSPayload(BaseModel):
    sender:  str
    message: str
    method:  Optional[str] = None

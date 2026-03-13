from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
import re, os, httpx

app = FastAPI(title="QuickPrint Payment Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURATION ---
# These will use the Railway Variables if set, otherwise defaults
SMS_PUSH_SECRET  = os.getenv("SMS_PUSH_SECRET",  "change-this-device-secret")
ADMIN_SECRET     = os.getenv("ADMIN_SECRET",     "change-this-admin-secret")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "8472478534:AAFFmBnSmYtFveznUFRxyrF0NbbIRMe1mDU")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "6509973320")

transaction_store: dict = {}
TXN_EXPIRY_HOURS = 24

# Improved Regex: More flexible for bKash/Nagad/Manual formats
SMS_PATTERNS = [
    r"TxnID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"TrxID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"Transaction\s*ID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"TID\s*[:\-]?\s*([A-Za-z0-9]{6,20})",
    r"ID\s*[:\-]?\s*([A-Za-z0-9]{8,20})",
]

AMOUNT_PATTERN = r"(?:Tk|BDT)\s*([\d,]+(?:\.\d+)?)"

# --- MODELS ---
class VerifyPayload(BaseModel):
    txnId:  str
    method: str
    amount: int

class SMSPayload(BaseModel):
    sender:  str
    message: str
    method:  Optional[str] = None

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

# --- ROUTES ---
@app.get("/")
def root():
    return {
        "status": "QuickPrint server is running", 
        "memory_count": len(transaction_store),
        "debug_mode": True
    }

@app.post("/api/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    
    # CRITICAL DEBUG: This shows exactly what is arriving in Railway Logs
    print(f"DEBUG: Full Update Received: {data}")

    try:
        message_obj = data.get("message") or data.get("channel_post")
        if not message_obj:
            return {"ok": True, "note": "No message in update"}

        chat_id = str(message_obj["chat"]["id"])
        text = message_obj.get("text", "")
        
        print(f"DEBUG: From Chat ID: {chat_id} | Text: {text}")

        # Security Check
        if chat_id != TELEGRAM_CHAT_ID:
            print(f"DEBUG: ID Mismatch! Expected {TELEGRAM_CHAT_ID}, got {chat_id}")
            return {"ok": True, "note": "Ignored — unknown chat"}

        txn_id = extract_txn_id(text)
        if not txn_id:
            print("DEBUG: No TXN ID matched in this text.")
            return {"ok": True, "txn_found": False}

        amount = extract_amount(text)
        method = detect_method("", text)

        clean_expired()
        store_txn(txn_id, amount, method, "telegram", text)
        print(f"DEBUG: Successfully stored TXN: {txn_id}")

        # Send Success Reply via Telegram
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": f"✅ TXN saved!\nID: {txn_id}\nAmount: {amount or 'Unknown'} TK\nMethod: {method}"
                }
            )
            print(f"DEBUG: Telegram Reply Status: {resp.status_code}")

        return {"ok": True, "txn_found": True, "txn_id": txn_id}

    except Exception as e:
        print(f"ERROR: Telegram webhook crashed: {e}")
        return {"ok": False, "error": str(e)}

@app.post("/api/verify")
async def verify_payment(payload: VerifyPayload):
    clean_expired()
    txn = transaction_store.get(payload.txnId.upper())
    
    if not txn:
        return {"status": "not_found", "message": "Transaction ID not found."}
    
    if txn["used"]:
        return {"status": "used", "message": "This ID has already been used."}
    
    # Basic verification (Amount check if provided)
    if payload.amount > 0 and txn["amount"]:
        if float(txn["amount"]) < float(payload.amount):
            return {"status": "insufficient", "message": "Amount mismatch."}

    txn["used"] = True
    return {"status": "success", "data": txn}

@app.get("/api/txns")
async def list_transactions(x_admin_secret: Optional[str] = Header(None)):
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    clean_expired()
    return {"count": len(transaction_store), "transactions": transaction_store}

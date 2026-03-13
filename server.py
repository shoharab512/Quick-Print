"""
QuickPrint Payment Verification Server
=======================================
Run with:  uvicorn server:app --host 0.0.0.0 --port 8000
Install:   pip install fastapi uvicorn

Endpoints:
  POST /api/sms     — Android/Tasker pushes incoming SMS here
  POST /api/verify  — Frontend checks if a TXN ID is valid
  GET  /api/txns    — (Admin) View all received TXN IDs
"""

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
import re, os, httpx

app = FastAPI(title="QuickPrint Payment Server")

# ── CORS — allow your HTML frontend to call this server ────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Lock this down to your domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Secret keys — change these before deploying ────────────────
# The Android device must send this key in the header when pushing SMS
SMS_PUSH_SECRET = os.getenv("SMS_PUSH_SECRET", "change-this-device-secret")

# Optional: protect the admin /api/txns endpoint
ADMIN_SECRET    = os.getenv("ADMIN_SECRET",    "change-this-admin-secret")

# ── In-memory store (replace with a database for production) ───
# Structure: { "TXN_ID": { "method": "bkash", "amount": "150", "received_at": datetime, "used": False } }
transaction_store: dict = {}

# ── How long a TXN ID stays valid after being received (hours) ─
TXN_EXPIRY_HOURS = 24

# ── Regex patterns to extract TXN ID from bKash / Nagad SMS ───
# bKash example: "TxnID 8K3B2A1C9F"  or  "transaction ID AB12CD34EF"
# Nagad example: "Transaction ID 9X2Y3Z4W1V"
# Adjust these if your SMS format differs
SMS_PATTERNS = [
    r"TxnID\s+([A-Z0-9]{8,20})",               # bKash
    r"transaction\s+ID\s+([A-Z0-9]{8,20})",     # bKash / Nagad
    r"Transaction\s+ID\s*[:\-]?\s*([A-Z0-9]{8,20})",  # Nagad
    r"\bTID\s*[:\-]?\s*([A-Z0-9]{8,20})\b",
    r"TrxID\s+([A-Za-z0-9]{6,20})",    # bKash received
]

# Amount extraction (e.g. "Tk 150.00" or "BDT 200")
AMOUNT_PATTERN = r"(?:Tk|BDT)\s*([\d,]+(?:\.\d+)?)"


# ── Models ─────────────────────────────────────────────────────

class SMSPayload(BaseModel):
    """
    Sent by the Android device (via Tasker or SMS forwarder app).
    """
    sender:  str            # Phone number that sent the SMS e.g. "+8801711..."
    message: str            # Full raw SMS text
    method:  Optional[str] = None  # "bkash" or "nagad" (Tasker can detect from sender)

class VerifyPayload(BaseModel):
    """
    Sent by the frontend when user submits their TXN ID.
    """
    txnId:  str
    method: str             # "bkash" or "nagad"
    amount: int             # Expected amount in TK


# ── Helpers ────────────────────────────────────────────────────

def extract_txn_id(message: str) -> Optional[str]:
    """Try each regex pattern and return the first TXN ID found."""
    for pattern in SMS_PATTERNS:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return None

def extract_amount(message: str) -> Optional[str]:
    """Extract the payment amount from the SMS text."""
    match = re.search(AMOUNT_PATTERN, message, re.IGNORECASE)
    return match.group(1).replace(",", "") if match else None

def detect_method(sender: str, message: str) -> str:
    """Guess the payment method from sender number or message content."""
    combined = (sender + message).lower()
    if "bkash" in combined or "01711" in combined or "01712" in combined:
        return "bkash"
    if "nagad" in combined or "01711310000" in combined:
        return "nagad"
    return "unknown"

def clean_expired():
    """Remove TXN IDs older than TXN_EXPIRY_HOURS."""
    cutoff = datetime.utcnow() - timedelta(hours=TXN_EXPIRY_HOURS)
    expired = [k for k, v in transaction_store.items() if v["received_at"] < cutoff]
    for k in expired:
        del transaction_store[k]


# ── Routes ─────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "QuickPrint server is running"}


@app.post("/api/sms")
async def receive_sms(payload: SMSPayload, x_device_secret: Optional[str] = Header(None)):
    """
    Called by the Android device whenever a bKash/Nagad SMS arrives.

    Required header:  X-Device-Secret: <SMS_PUSH_SECRET>

    Tasker HTTP Post action should set:
      URL:     http://YOUR-SERVER-IP:8000/api/sms
      Header:  X-Device-Secret: your-secret
      Body:    { "sender": "%SMSRF", "message": "%SMSRB" }
    """

    # Authenticate the device
    if x_device_secret != SMS_PUSH_SECRET:
        raise HTTPException(status_code=401, detail="Invalid device secret")

    # Extract TXN ID from the SMS
    txn_id = extract_txn_id(payload.message)
    if not txn_id:
        # SMS received but no TXN ID found — log it but don't fail
        return {
            "received": True,
            "txn_found": False,
            "note": "No transaction ID pattern matched in this SMS",
            "raw_preview": payload.message[:80]
        }

    amount  = extract_amount(payload.message)
    method  = payload.method or detect_method(payload.sender, payload.message)

    clean_expired()

    # Store the TXN ID
    transaction_store[txn_id] = {
        "method":      method,
        "amount":      amount,
        "sender":      payload.sender,
        "received_at": datetime.utcnow(),
        "used":        False,
        "raw":         payload.message[:200],  # store first 200 chars for debugging
    }

    return {
        "received":  True,
        "txn_found": True,
        "txn_id":    txn_id,
        "method":    method,
        "amount":    amount,
    }


@app.post("/api/verify")
async def verify_payment(payload: VerifyPayload):
    """
    Called by the frontend when a user submits their transaction ID.
    Checks if the TXN ID exists, matches the method, and hasn't been used.
    """
    clean_expired()

    txn_id = payload.txnId.strip().upper()
    record = transaction_store.get(txn_id)

    # Not found
    if not record:
        return {
            "verified": False,
            "message":  "Transaction ID not found. Please check the ID and try again, or contact us on WhatsApp."
        }

    # Already used (prevent reuse)
    if record["used"]:
        return {
            "verified": False,
            "message":  "This transaction ID has already been used for a previous order."
        }

    # Method mismatch (optional strictness — comment out if too strict)
    if record["method"] != "unknown" and record["method"] != payload.method:
        return {
            "verified": False,
            "message":  f"Transaction ID found but it belongs to a {record['method'].title()} payment, not {payload.method.title()}."
        }

    # Amount mismatch (optional — checks if SMS amount matches expected)
    if record["amount"]:
        sms_amount = float(record["amount"])
        if abs(sms_amount - payload.amount) > 0:   # allow ±1 TK tolerance
            return {
                "verified": False,
                "message":  f"Transaction found but amount doesn't match. Expected {payload.amount} TK, SMS shows {record['amount']} TK."
            }

    # ✅ All checks passed — mark as used
    record["used"] = True
    record["verified_at"] = datetime.utcnow()

    return {
        "verified": True,
        "message":  "Payment verified successfully!",
        "txn_id":   txn_id,
        "method":   record["method"],
        "amount":   record["amount"],
    }


@app.get("/api/txns")
async def list_transactions(x_admin_secret: Optional[str] = Header(None)):
    """
    Admin endpoint — view all stored TXN IDs.
    Header: X-Admin-Secret: your-admin-secret
    """
    if x_admin_secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

    clean_expired()
    return {
        "count": len(transaction_store),
        "transactions": {
            k: {**v, "received_at": v["received_at"].isoformat()}
            for k, v in transaction_store.items()
        }
    }


@app.post("/api/telegram")
async def telegram_webhook(request: Request):
    data = await request.json()
    try:
        message_obj = data.get("message") or data.get("channel_post")
        if not message_obj:
            return {"ok": True, "note": "No message in update"}
        chat_id = str(message_obj["chat"]["id"])
        text = message_obj.get("text", "")
        if chat_id != TELEGRAM_CHAT_ID:
            return {"ok": True, "note": "Ignored — unknown chat"}
        txn_id = extract_txn_id(text)
        if not txn_id:
            return {"ok": True, "txn_found": False}
        amount = extract_amount(text)
        method = detect_method("", text)
        clean_expired()
        transaction_store[txn_id] = {
            "method": method, "amount": amount, "sender": "telegram",
            "received_at": datetime.utcnow(), "used": False, "raw": text[:200],
        }
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": f"✅ TXN saved!\nID: {txn_id}\nAmount: {amount} TK\nMethod: {method}"}
            )
        return {"ok": True, "txn_found": True, "txn_id": txn_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}
# last_update: Fri Mar 13 20:28:41 +06 2026
# Force Rebuild 101

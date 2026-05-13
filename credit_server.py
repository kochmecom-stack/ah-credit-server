"""
credit_server.py
=================
Server quan ly credit + nhan webhook SePay IPN.
"""

import hashlib
import hmac
import json
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify

# ─── SePay config ─────────────────────────────────────────────────────────────
SEPAY_MERCHANT_ID  = os.environ.get("SEPAY_MERCHANT_ID",  "SP-TEST-LH678847")
SEPAY_SECRET_KEY   = os.environ.get("SEPAY_SECRET_KEY",   "spsk_test_HyqNdk6AHrB66eg3cX3rbKi37yWmJZdj")
SEPAY_ENV          = os.environ.get("SEPAY_ENV",          "sandbox")

# ─── Bank info ────────────────────────────────────────────────────────────────
BANK_ACCOUNT_NO    = "8867286256"
BANK_ACCOUNT_NAME  = "LA QUI HA"
BANK_NAME          = "BIDV"

# ─── Credit pricing ──────────────────────────────────────────────────────────
VND_PER_CREDIT     = 1000
COST_IMAGE_FAST    = 1
COST_IMAGE_QUALITY = 3

# ─── Data files (ho tro persistent storage) ──────────────────────────────────
_DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp"))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
CREDITS_FILE = _DATA_DIR / "user_credits.json"
PAYMENT_LOG  = _DATA_DIR / "payment_log.json"

def _load_credits():
    if CREDITS_FILE.exists():
        try: return json.loads(CREDITS_FILE.read_text("utf-8"))
        except: pass
    return {}

def _save_credits(data):
    CREDITS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_user_credits(user_code):
    return int(_load_credits().get(user_code.upper(), {}).get("credits", 0))

def add_credits(user_code, amount_vnd, txn_id=""):
    user_code = user_code.upper().strip()
    new_credit = max(1, amount_vnd // VND_PER_CREDIT)
    credits = _load_credits()
    if user_code not in credits:
        credits[user_code] = {"credits": 0, "total_paid_vnd": 0, "history": []}
    credits[user_code]["credits"] += new_credit
    credits[user_code]["total_paid_vnd"] += amount_vnd
    credits[user_code].setdefault("history", []).append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "vnd": amount_vnd, "credit": new_credit, "txn_id": txn_id
    })
    _save_credits(credits)
    return credits[user_code]["credits"]

def _verify_sepay_signature(payload, signature):
    if not signature: return False
    try:
        sorted_fields = sorted([(k, v) for k, v in payload.items() if k != "signature"], key=lambda x: x[0])
        message = "&".join(f"{k}={v}" for k, v in sorted_fields)
        expected = hmac.new(SEPAY_SECRET_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
    except: return False

def _extract_user_code(text):
    if not text: return None
    upper = text.strip().upper()
    # 1. 10 alphanumeric chars
    m = re.search(r'(?<![A-Z0-9])([A-Z0-9]{10})(?![A-Z0-9])', upper)
    if m: return m.group(1)
    # 2. Legacy patterns
    legacy_patterns = [
        r'\b(TEST-[A-Z0-9]{5})\b', # <--- FIX for TEST-XXXXX
        r'\b([A-Z]{2,4}\d{2,5})\b',
        r'CODE[:\s]+([A-Z0-9]{4,10})',
    ]
    for pat in legacy_patterns:
        m = re.search(pat, upper)
        if m: return m.group(1)
    return None

def handle_sepay_ipn(payload, raw_signature=""):
    txn_id = str(payload.get("id") or "")
    amount = int(payload.get("transferAmount") or 0)
    content = str(payload.get("content") or "").strip()
    sig = payload.get("signature") or raw_signature
    if SEPAY_ENV != "sandbox" and not _verify_sepay_signature(payload, sig):
        return {"code": "1", "message": "invalid_signature"}
    user_code = _extract_user_code(content)
    if not user_code: return {"code": "1", "message": "no_user_code_found"}
    total = add_credits(user_code, amount, txn_id)
    return {"code": "00", "message": "success", "user_code": user_code, "credits_total": total}

def _register_routes(app):
    @app.route("/health")
    def health():
        return jsonify({"status": "ok", "merchant_id": SEPAY_MERCHANT_ID, "bank": f"{BANK_NAME} {BANK_ACCOUNT_NO}", "env": SEPAY_ENV})

    @app.route("/sepay/ipn", methods=["POST"])
    def sepay_ipn():
        payload = request.get_json(force=True) or {}
        sig = request.headers.get("X-Signature", "")
        return jsonify(handle_sepay_ipn(payload, sig))

    @app.route("/credits/<user_code>")
    def api_get_credits(user_code):
        return jsonify({"user_code": user_code.upper(), "credits": get_user_credits(user_code)})

def create_app():
    app = Flask(__name__)
    _register_routes(app)
    return app

if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

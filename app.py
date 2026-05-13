"""
app.py
======
Entry point cho Render.com / gunicorn.
Chay: gunicorn app:app --bind 0.0.0.0:$PORT
"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

# Import Flask truoc, sau do chay run_server de lay app object
from flask import Flask, request, jsonify

# Import toan bo logic tu credit_server
import credit_server as cs

app = Flask(__name__)

# ── Keep-alive: tu ping de Render khong ngu (free tier) ──────────────────────
import threading, time, urllib.request as _ureq

def _keep_alive():
    """Ping chinh minh moi 10 phut de tranh Render ngu."""
    time.sleep(60)   # doi 1 phut sau khi khoi dong
    while True:
        try:
            port = os.environ.get("PORT", "10000")
            _ureq.urlopen(f"http://localhost:{port}/health", timeout=5)
        except Exception:
            pass
        time.sleep(600)  # 10 phut

threading.Thread(target=_keep_alive, daemon=True, name="keep-alive").start()

# ── Health check ──────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":      "ok",
        "merchant_id": cs.SEPAY_MERCHANT_ID,
        "env":         cs.SEPAY_ENV,
        "bank":        f"{cs.BANK_NAME} {cs.BANK_ACCOUNT_NO}",
        "data_dir":    str(cs._DATA_DIR),
    })

# ── SePay IPN ─────────────────────────────────────────────────────────────────
@app.route("/sepay/ipn", methods=["POST"])
def sepay_ipn():
    payload = request.get_json(force=True) or {}
    sig     = request.headers.get("X-Signature", "")
    result  = cs.handle_sepay_ipn(payload, sig)
    return jsonify(result), 200

# ── Credit check API ──────────────────────────────────────────────────────────
@app.route("/credits/<user_code>", methods=["GET"])
def api_get_credits(user_code):
    c = cs.get_user_credits(user_code)
    return jsonify({
        "user_code": user_code.upper(),
        "credits":   c,
        "can_use_fast":    c >= cs.COST_IMAGE_FAST,
        "can_use_quality": c >= cs.COST_IMAGE_QUALITY,
    })

# ── Deduct credits ────────────────────────────────────────────────────────────
@app.route("/credits/deduct", methods=["POST"])
def api_deduct():
    body      = request.get_json(force=True) or {}
    user_code = body.get("user_code", "")
    model     = body.get("model", "fast")
    if not user_code:
        return jsonify({"ok": False, "error": "missing user_code"}), 400
    ok = cs.check_and_deduct(user_code, model)
    return jsonify({
        "ok":      ok,
        "credits": cs.get_user_credits(user_code),
        "error":   "" if ok else "insufficient_credits",
    })

# ── Add credits (manual/webhook) ──────────────────────────────────────────────
@app.route("/credits/add", methods=["POST"])
def api_add():
    admin_token = (
        request.headers.get("X-Admin-Token") or
        request.args.get("admin_token") or ""
    )
    if admin_token != os.environ.get("ADMIN_TOKEN", "ahstudio2026"):
        return jsonify({"error": "unauthorized"}), 401
    body      = request.get_json(force=True) or {}
    user_code = body.get("user_code", "")
    amount    = int(body.get("amount_vnd", 0))
    if not user_code or amount <= 0:
        return jsonify({"ok": False, "error": "missing user_code or amount_vnd"}), 400
    total = cs.add_credits(user_code, amount, body.get("txn_id", "manual"))
    return jsonify({"ok": True, "credits": total})

# ── All users (admin) ─────────────────────────────────────────────────────────
@app.route("/users", methods=["GET"])
def api_all_users():
    admin_token = (
        request.headers.get("X-Admin-Token") or
        request.args.get("admin_token") or ""
    )
    if admin_token != os.environ.get("ADMIN_TOKEN", "ahstudio2026"):
        return jsonify({"error": "unauthorized"}), 401
    data = cs._load_credits()
    return jsonify(data)

# ── Entry point local ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

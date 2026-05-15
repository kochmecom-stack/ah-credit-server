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

# ── In-memory log: luu 50 request cuoi de debug ───────────────────────────────
from collections import deque
_REQUEST_LOG = deque(maxlen=50)

# ── Keep-alive: tu ping de Render khong ngu (free tier) ──────────────────────
import threading, time, urllib.request as _ureq

def _keep_alive():
    """Ping chinh minh moi 10 phut de tranh Render ngu."""
    time.sleep(60)
    while True:
        try:
            port = os.environ.get("PORT", "10000")
            _ureq.urlopen(f"http://localhost:{port}/health", timeout=5)
        except Exception:
            pass
        time.sleep(600)

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
        "requests_logged": len(_REQUEST_LOG),
    })

# ── Debug: xem 50 IPN request cuoi ───────────────────────────────────────────
@app.route("/debug/requests", methods=["GET"])
def debug_requests():
    return jsonify(list(_REQUEST_LOG))

# ── SePay IPN ─────────────────────────────────────────────────────────────────
@app.route("/sepay/ipn", methods=["POST"])
def sepay_ipn():
    import datetime as _dt
    raw_body  = request.get_data(as_text=True)
    payload   = request.get_json(force=True) or {}
    sig       = request.headers.get("X-Signature", "")
    all_hdrs  = dict(request.headers)
    result    = cs.handle_sepay_ipn(payload, sig)

    # Luu vao in-memory log
    _REQUEST_LOG.append({
        "time":    _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ip":      request.remote_addr,
        "headers": {k: v for k, v in all_hdrs.items() if k.lower() in
                    ("content-type","x-signature","user-agent","x-forwarded-for")},
        "body":    raw_body[:500],
        "result":  result,
    })
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
    # Neu co field "amount" (raw credits) -> tru dung so luong do
    raw_amount = body.get("amount")
    if raw_amount is not None:
        try:
            cost = max(1, int(raw_amount))
        except (TypeError, ValueError):
            cost = 1
        ok = cs.deduct_credits(user_code, cost)
    else:
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

# ── Set credits (admin) — dung tu Google Sheet ────────────────────────────────
@app.route("/credits/set", methods=["POST"])
def api_set_credits():
    """
    Dat so credit chinh xac cho 1 user.
    Dung boi Google Sheet admin khi chinh sua cot 'Credit Hien Tai'.
    Body: { "user_code": "4DD3636D8D", "credits": 100, "admin_token": "ahstudio2026" }
    """
    admin_token = (
        request.headers.get("X-Admin-Token") or
        request.args.get("admin_token") or ""
    )
    body        = request.get_json(force=True) or {}
    # Admin token co the o header hoac trong body
    if not admin_token:
        admin_token = body.get("admin_token", "")
    if admin_token != os.environ.get("ADMIN_TOKEN", "ahstudio2026"):
        return jsonify({"error": "unauthorized"}), 401

    user_code = str(body.get("user_code", "")).upper().strip()
    new_cred  = body.get("credits", None)
    if not user_code or new_cred is None:
        return jsonify({"error": "missing user_code or credits"}), 400
    new_cred = max(0, int(new_cred))

    import datetime as _dt
    data = cs._load_credits()
    if user_code not in data:
        data[user_code] = {
            "credits": 0, "total_paid_vnd": 0,
            "top_up_count": 0, "history": [],
            "first_seen": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": "created_by_sheet_admin",
        }
    old_cred = int(data[user_code].get("credits", 0))
    data[user_code]["credits"] = new_cred
    data[user_code].setdefault("history", []).append({
        "time":        _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action":      "admin_set",
        "old_credits": old_cred,
        "new_credits": new_cred,
    })
    cs._save_credits(data)
    print(f"[Admin/Set] {user_code}: {old_cred} -> {new_cred}")
    return jsonify({"ok": True, "user_code": user_code, "credits": new_cred})

# ── Groq Key endpoint — cung cap Groq key cho user co credit ─────────────────
@app.route("/groq/key", methods=["GET", "POST"])
def api_groq_key():
    """
    Tra ve Groq API key (cua admin) cho user da co credit > 0.
    Key duoc luu tren Render env var GROQ_API_KEY (khong bao gio lo trong code).

    GET  /groq/key?user_code=XXXX
    POST /groq/key  body: {"user_code": "XXXX"}
    """
    if request.method == "POST":
        body = request.get_json(force=True) or {}
        user_code = str(body.get("user_code", "")).upper().strip()
    else:
        user_code = str(request.args.get("user_code", "")).upper().strip()

    if not user_code:
        return jsonify({"ok": False, "error": "missing user_code"}), 400

    # Kiem tra credits — chi cap key neu user con credit
    credits = cs.get_user_credits(user_code)
    if credits <= 0:
        return jsonify({
            "ok":      False,
            "error":   "insufficient_credits",
            "credits": 0,
        }), 403

    # Lay Groq key tu env var tren Render (an toan, khong hardcode)
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not groq_key:
        return jsonify({"ok": False, "error": "groq_not_configured"}), 503

    return jsonify({
        "ok":      True,
        "key":     groq_key,
        "credits": credits,
    })

# ── Entry point local ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

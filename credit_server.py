"""
credit_server.py
=================
Server quan ly credit + nhan webhook SePay IPN.

Thong tin SePay (SANDBOX):
  Merchant ID : SP-TEST-LH678847
  Secret Key  : spsk_test_HyqNdk6AHrB66eg3cX3rbKi37yWmJZdj
  IPN URL     : http://YOUR_PUBLIC_IP:5000/sepay/ipn

Luong thanh toan:
  1. Khach nhan ma user (VD: VH001)
  2. Khach chuyen khoan BIDV 8867286256 noi dung: "VH001"
  3. SePay phat hien giao dich → POST IPN toi server nay
  4. Server xac minh chu ky → cong credit → log

Gia credit: 1.000 VND = 1 credit = 1 anh Imagen 4 Fast ($0.02)
"""

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

# ─── SePay config ─────────────────────────────────────────────────────────────
SEPAY_MERCHANT_ID  = os.environ.get("SEPAY_MERCHANT_ID",  "SP-PROD-AH")
SEPAY_SECRET_KEY   = os.environ.get("SEPAY_SECRET_KEY",   "whsec_S6ClPG5FeNJtypyk7e1Co2p3GIu0a2sb")
SEPAY_ENV          = os.environ.get("SEPAY_ENV",          "production")  # production = xac minh chu ky

# ─── Bank info ────────────────────────────────────────────────────────────────
BANK_ACCOUNT_NO    = "8867286256"
BANK_ACCOUNT_NAME  = "LA QUI HA"
BANK_NAME          = "BIDV"

# ─── Credit pricing ──────────────────────────────────────────────────────────
VND_PER_CREDIT     = 1000   # 1.000 VND = 1 credit
COST_IMAGE_FAST    = 1      # Imagen 4 Fast:    1 credit/anh
COST_IMAGE_QUALITY = 3      # Imagen 4 Quality: 3 credit/anh

# ─── Data storage: GitHub Gist (persistent) + local cache (fallback) ─────────
_DATA_DIR    = Path(os.environ.get("DATA_DIR", "."))
_DATA_DIR.mkdir(parents=True, exist_ok=True)
CREDITS_FILE = _DATA_DIR / "user_credits.json"
PAYMENT_LOG  = _DATA_DIR / "payment_log.json"

# GitHub Repo file storage (khong can gist scope)
_GH_TOKEN  = os.environ.get("GIST_TOKEN", "")  # reuse same env var name
_GH_REPO   = os.environ.get("GH_REPO", "kochmecom-stack/ah-credit-server")
_GH_FILE   = "data/credits.json"   # file trong repo
_GIST_ID   = ""   # khong dung

# In-memory cache
_credits_cache: dict | None = None


# ═══════════════════════════════════════════════════════════════════════════════
# GitHub Repo file storage helpers
# ═══════════════════════════════════════════════════════════════════════════════

_GH_FILE_SHA = ""   # cache SHA de update

def _gh_load() -> dict:
    """Doc credits.json tu GitHub repo."""
    global _GH_FILE_SHA
    if not _GH_TOKEN:
        return {}
    try:
        import urllib.request as _ur
        req = _ur.Request(
            f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_FILE}",
            headers={
                "Authorization": f"token {_GH_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "AH-Credit-Server/1.0",
            }
        )
        with _ur.urlopen(req, timeout=10) as r:
            d = json.loads(r.read())
            _GH_FILE_SHA = d.get("sha", "")
            import base64 as _b64
            raw = _b64.b64decode(d["content"].replace("\n", "")).decode("utf-8")
            return json.loads(raw)
    except Exception as e:
        print(f"[GH] Load error: {e}")
        return {}


def _gh_save(data: dict):
    """Luu credits.json vao GitHub repo."""
    global _GH_FILE_SHA
    if not _GH_TOKEN:
        return
    try:
        import urllib.request as _ur, base64 as _b64
        content = _b64.b64encode(
            json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        ).decode()
        body = json.dumps({
            "message": "chore: update credits",
            "content": content,
            "sha":     _GH_FILE_SHA,
        }).encode()
        req = _ur.Request(
            f"https://api.github.com/repos/{_GH_REPO}/contents/{_GH_FILE}",
            data=body,
            headers={
                "Authorization": f"token {_GH_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "AH-Credit-Server/1.0",
            },
            method="PUT"
        )
        with _ur.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            _GH_FILE_SHA = result["content"]["sha"]
            print(f"[GH] Saved credits -> {result['commit']['sha'][:8]}")
    except Exception as e:
        print(f"[GH] Save error: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Credit management (GitHub repo-backed)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_credits() -> dict:
    global _credits_cache
    if _credits_cache is None:
        # Load tu GitHub repo truoc
        gh_data = _gh_load()
        if gh_data:
            _credits_cache = gh_data
            print(f"[GH] Loaded {len(gh_data)} users from repo")
        elif CREDITS_FILE.exists():
            try:
                _credits_cache = json.loads(CREDITS_FILE.read_text("utf-8"))
            except Exception:
                _credits_cache = {}
        else:
            _credits_cache = {}
    return _credits_cache


def _save_credits(data: dict):
    global _credits_cache
    _credits_cache = data
    # Luu local
    CREDITS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    # Luu len GitHub repo
    _gh_save(data)


def get_user_credits(user_code: str) -> int:
    """Tra ve so credit hien tai."""
    return int(_load_credits().get(user_code.upper(), {}).get("credits", 0))


def create_user_code(user_code: str, note: str = "") -> dict:
    """Tao user moi neu chua ton tai."""
    user_code = user_code.upper().strip()
    credits   = _load_credits()
    if user_code not in credits:
        credits[user_code] = {
            "credits": 0,
            "total_paid_vnd": 0,
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
            "history": [],
        }
        _save_credits(credits)
    return credits[user_code]


def add_credits(user_code: str, amount_vnd: int, txn_id: str = "") -> int:
    """Cong credit theo so tien VND. Tra ve tong credit sau khi cong."""
    user_code  = user_code.upper().strip()
    new_credit = max(1, amount_vnd // VND_PER_CREDIT)

    credits = _load_credits()
    if user_code not in credits:
        credits[user_code] = {"credits": 0, "total_paid_vnd": 0, "top_up_count": 0, "history": []}

    credits[user_code]["credits"]        += new_credit
    credits[user_code]["total_paid_vnd"] += amount_vnd
    credits[user_code]["top_up_count"]    = credits[user_code].get("top_up_count", 0) + 1
    credits[user_code].setdefault("history", []).append({
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "vnd":    amount_vnd,
        "credit": new_credit,
        "txn_id": txn_id,
    })
    _save_credits(credits)
    return credits[user_code]["credits"]


def deduct_credits(user_code: str, amount: int = 1) -> bool:
    """Tru credit khi dung API. Tra ve True neu du credit."""
    user_code = user_code.upper().strip()
    credits   = _load_credits()
    current   = int(credits.get(user_code, {}).get("credits", 0))
    if current < amount:
        return False
    credits[user_code]["credits"] -= amount
    _save_credits(credits)
    return True


def check_and_deduct(user_code: str, model: str = "fast") -> bool:
    """
    Kiem tra va tru credit truoc khi tao anh.
    model: "fast" (1 credit) hoac "quality" (3 credits)
    """
    cost = COST_IMAGE_QUALITY if model == "quality" else COST_IMAGE_FAST
    return deduct_credits(user_code, cost)


# ═══════════════════════════════════════════════════════════════════════════════
# SePay IPN verification
# ═══════════════════════════════════════════════════════════════════════════════

def _verify_sepay_signature(payload: dict, signature: str) -> bool:
    """
    Xac minh chu ky SePay IPN.
    SePay ky bang HMAC-SHA256: secret_key lam key, query_string sap xep lam message.
    """
    if not signature:
        return False
    try:
        # Sap xep cac field theo thu tu abc, bo qua field 'signature'
        sorted_fields = sorted(
            [(k, v) for k, v in payload.items() if k != "signature"],
            key=lambda x: x[0]
        )
        message = "&".join(f"{k}={v}" for k, v in sorted_fields)
        expected = hmac.new(
            SEPAY_SECRET_KEY.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        print(f"[SePay] Loi xac minh chu ky: {e}")
        return False


def _normalize_name(name: str) -> str:
    """
    Nguyen Van A → NGUYENVANA
    Khop voi logic normalize ben tab_product_to_video.py UI.
    """
    import unicodedata
    if not name.strip():
        return ""
    nfkd   = unicodedata.normalize("NFKD", name.strip())
    ascii_ = "".join(c for c in nfkd if not unicodedata.combining(c))
    return "".join(c for c in ascii_.upper() if c.isalnum())[:20]


def _extract_user_code(text: str) -> str | None:
    """
    Tim ma khach hang trong noi dung chuyen tien.
    Ho tro cac kieu:
      1) User code 10 ky tu alphanumeric: "85CCFCEA2A", "0TEST6682F"
      2) FORMAT KEY TOOL: "TEST-6682F", "TEST-R3C44" (PREFIX-5KT)
      3) Ma cu kieu VH001, CODE:VH001 (legacy)
      4) Ten normalize thuan chu cai: NGUYENVANA (5-20 chars)
    """
    if not text:
        return None
    upper = text.strip().upper()

    # --- Uu tien 1: User code 10 ky tu alphanumeric (format tool 10 char) ---
    m = re.search(r'(?<![A-Z0-9])([A-Z0-9]{10})(?![A-Z0-9])', upper)
    if m:
        return m.group(1)

    # --- Uu tien 1.5: Format key tool "PREFIX-SUFFIX" (VD: TEST-6682F) ---
    # Khop: 2-8 chu cai/so + dau - + 3-8 chu cai/so
    # Tra ve toan bo key (TEST-6682F) de dung lam user_code
    m = re.search(r'\b([A-Z0-9]{2,8}-[A-Z0-9]{3,8})\b', upper)
    if m:
        candidate = m.group(1)
        # Bo qua cac cum pho bien khong phai key
        _NOT_KEY = {"BIDV-", "MB-", "VCB-", "TCB-", "ACB-", "VPB-"}
        if not any(candidate.startswith(x) for x in _NOT_KEY):
            return candidate

    # --- Uu tien 2: Ma cu (VH001, KH1234, CODE:VH001) ---
    legacy_patterns = [
        r'\b([A-Z]{2,4}\d{2,5})\b',
        r'CODE[:\s]+([A-Z0-9]{4,10})',
        r'MA[:\s]+([A-Z0-9]{4,10})',
        r'USER[:\s]+([A-Z0-9]{4,10})',
    ]
    for pat in legacy_patterns:
        m = re.search(pat, upper)
        if m:
            return m.group(1)

    # --- Uu tien 3: Ten normalize thuan chu cai dai 5-20 (chi noi dung don) ---
    stripped = upper.strip()
    if ' ' not in stripped and re.fullmatch(r'[A-Z]{5,20}', stripped):
        return stripped

    # Tim cum chu cai dai nhat (bo qua tu pho bien)
    _SKIP = {"NAPTHE", "NAPTIEP", "THANHTOAN", "CHUYENKHOAN", "NAPTIEN",
             "NAPKREDIT", "CREDIT", "NAPQUA", "VIETQR", "VPBANK", "BIDV",
             "MOMO", "ZALOPAY", "SEPAY", "SHOPEE", "LAZADA", "TIKI",
             "GRAB", "BEBANK", "MBBANK", "TECHCOM", "ACBANK", "SACOMBANK"}
    chunks = re.findall(r'[A-Z]{5,20}', upper)
    for chunk in sorted(chunks, key=len, reverse=True):
        if chunk not in _SKIP:
            return chunk


    return None




def _log_payment(txn: dict):
    logs = []
    if PAYMENT_LOG.exists():
        try:
            logs = json.loads(PAYMENT_LOG.read_text("utf-8"))
        except Exception:
            pass
    logs.append(txn)
    PAYMENT_LOG.write_text(
        json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# SePay IPN handler
# ═══════════════════════════════════════════════════════════════════════════════

def handle_sepay_ipn(payload: dict, raw_signature: str = "") -> dict:
    """
    Xu ly SePay Webhook IPN.

    SePay THUC TE gui payload nhu sau (Bank Hub / Webhook):
    {
      "id":              123456,
      "gateway":         "BIDV",
      "transactionDate": "2024-05-11 12:00:00",
      "accountNumber":   "8867286256",
      "subAccount":      null,
      "code":            "FT24001XXXXXXX",    ← ma giao dich ngan hang (KHONG phai user code)
      "content":         "85CCFCEA2A",        ← NOI DUNG chuyen khoan (user code o day!)
      "transferType":    "in",
      "transferAmount":  50000,               ← so tien VND
      "accumulated":     50000,
      "referenceCode":   "FT...",
      "description":     "BIDV ...",
      "type":            "in"
    }
    """
    # ── Doc cac truong co fallback cho ca 2 format ────────────────────
    txn_id = str(
        payload.get("id") or
        payload.get("transaction_id") or
        payload.get("txn_id") or ""
    )

    # So tien: SePay dung "transferAmount", generic dung "amount"
    amount = int(
        payload.get("transferAmount") or
        payload.get("amount") or 0
    )

    # Noi dung chuyen khoan: SePay dung "content", generic dung "description"
    content = str(payload.get("content") or "").strip()
    desc    = str(payload.get("description") or payload.get("order_description") or "").strip()

    # Loai giao dich: SePay dung "transferType"/"type" = "in", generic dung "status"
    transfer_type = str(payload.get("transferType") or payload.get("type") or "").lower()
    status        = str(payload.get("status") or "").lower()

    order_no = str(payload.get("order_invoice_number") or "")
    sig      = payload.get("signature") or raw_signature

    # Log raw payload de debug
    print(f"[SePay] RAW payload: amount={amount} content={content!r} type={transfer_type!r} status={status!r}")

    # 1. Xac minh chu ky (bo qua trong sandbox)
    if SEPAY_ENV != "sandbox" and not _verify_sepay_signature(payload, sig):
        print(f"[SePay] Chu ky sai! txn={txn_id}")
        return {"code": "1", "message": "invalid_signature"}

    # 2. Chi xu ly tien VAO (transferType=="in") hoac status thanh cong
    is_incoming = transfer_type in ("in",)
    is_success  = status in ("success", "completed", "paid", "00")

    if not is_incoming and not is_success:
        _log_payment({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "txn_id": txn_id, "type": transfer_type, "status": status,
            "amount": amount, "content": content, "result": "skipped_not_incoming",
        })
        return {"code": "00", "message": "not_incoming_transfer"}

    # 3. Kiem tra so tien
    if amount < VND_PER_CREDIT:
        return {"code": "1", "message": f"amount_too_small ({amount} VND)"}

    # 4. Tim ma khach hang
    # Thu theo thu tu: content → desc → order_no
    user_code = (
        _extract_user_code(content) or
        _extract_user_code(desc)    or
        _extract_user_code(order_no)
    )

    if not user_code:
        _log_payment({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "txn_id": txn_id, "amount": amount,
            "content": content, "desc": desc, "order_no": order_no,
            "result": "no_user_code",
        })
        print(f"[SePay] Khong tim user code trong: content={content!r} desc={desc!r}")
        return {"code": "1", "message": "no_user_code_found"}

    # 5. Cong credit
    total = add_credits(user_code, amount, txn_id)
    added = amount // VND_PER_CREDIT

    _log_payment({
        "time":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_code":     user_code,
        "amount_vnd":    amount,
        "credits_added": added,
        "credits_total": total,
        "desc":          desc,
        "txn_id":        txn_id,
        "result":        "success",
    })

    print(f"[SePay] OK {user_code} +{added} credits (tong={total}) | {amount:,}VND | txn={txn_id}")

    # 6. SePay yeu cau tra ve code=00 la thanh cong
    return {
        "code":          "00",
        "message":       "success",
        "user_code":     user_code,
        "credits_added": added,
        "credits_total": total,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Flask server
# ═══════════════════════════════════════════════════════════════════════════════

def run_server(host: str = "0.0.0.0", port: int = 5000):
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        print("❌ pip install flask")
        return

    app = Flask(__name__)

    # ── Health check ──────────────────────────────────────────────────────────
    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status":      "ok",
            "merchant_id": SEPAY_MERCHANT_ID,
            "env":         SEPAY_ENV,
            "bank":        f"{BANK_NAME} {BANK_ACCOUNT_NO}",
        })

    # ── SePay IPN ─────────────────────────────────────────────────────────────
    @app.route("/sepay/ipn", methods=["POST"])
    def sepay_ipn():
        """
        SePay Instant Payment Notification.
        Cau hinh tai: https://merchant.sepay.vn → Tich hop → IPN URL
        URL: http://YOUR_PUBLIC_IP:5000/sepay/ipn
        """
        payload = request.get_json(force=True) or {}
        sig     = request.headers.get("X-Signature", "")
        result  = handle_sepay_ipn(payload, sig)
        # SePay can nhan HTTP 200 + {"code":"00"} la thanh cong
        return jsonify(result), 200

    # ── Credit check API ──────────────────────────────────────────────────────
    @app.route("/credits/<user_code>", methods=["GET"])
    def api_get_credits(user_code):
        c = get_user_credits(user_code)
        return jsonify({
            "user_code": user_code.upper(),
            "credits":   c,
            "can_use_fast":    c >= COST_IMAGE_FAST,
            "can_use_quality": c >= COST_IMAGE_QUALITY,
        })

    # ── Deduct credits (goi tu Shopee workflow) ───────────────────────────────
    @app.route("/credits/deduct", methods=["POST"])
    def api_deduct():
        body      = request.get_json(force=True) or {}
        user_code = body.get("user_code", "")
        model     = body.get("model", "fast")  # "fast" or "quality"
        if not user_code:
            return jsonify({"ok": False, "error": "missing user_code"}), 400
        ok = check_and_deduct(user_code, model)
        return jsonify({
            "ok":      ok,
            "credits": get_user_credits(user_code),
            "error":   "" if ok else "insufficient_credits",
        })

    # ── Create user (admin) ───────────────────────────────────────────────────
    @app.route("/users/create", methods=["POST"])
    def api_create_user():
        body      = request.get_json(force=True) or {}
        user_code = body.get("user_code", "")
        note      = body.get("note", "")
        if not user_code:
            return jsonify({"error": "missing user_code"}), 400
        data = create_user_code(user_code, note)
        return jsonify({"user_code": user_code.upper(), "data": data})

    # ── Admin token check helper ──────────────────────────────────────
    _ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "ahstudio2026")

    def _check_admin(req) -> bool:
        token = req.args.get("admin_token") or req.headers.get("X-Admin-Token", "")
        return token == _ADMIN_TOKEN

    # ── All users (admin) ─────────────────────────────────────────────
    @app.route("/users", methods=["GET"])
    def api_all_users():
        if not _check_admin(request):
            return jsonify({"error": "unauthorized"}), 401
        data = _load_credits()
        # Backfill top_up_count tu history neu chua co
        for code, info in data.items():
            if "top_up_count" not in info:
                info["top_up_count"] = len(info.get("history", []))
        return jsonify(data)

    # ── Payment log (admin) ───────────────────────────────────────────
    @app.route("/logs", methods=["GET"])
    def api_logs():
        if not _check_admin(request):
            return jsonify({"error": "unauthorized"}), 401
        logs = []
        if PAYMENT_LOG.exists():
            try:
                logs = json.loads(PAYMENT_LOG.read_text("utf-8"))
            except Exception:
                pass
        return jsonify({"total": len(logs), "logs": logs[-100:]})

    # ── Manual add credits (admin/test) ──────────────────────────────────────
    @app.route("/credits/add", methods=["POST"])
    def api_add():
        body      = request.get_json(force=True) or {}
        user_code = body.get("user_code", "")
        amount    = int(body.get("amount_vnd", 0))
        txn_id    = body.get("txn_id", f"manual_{int(time.time())}")
        if not user_code or amount <= 0:
            return jsonify({"error": "missing params"}), 400
        total = add_credits(user_code, amount, txn_id)
        return jsonify({"user_code": user_code, "credits_total": total})

    env_label = "🧪 SANDBOX" if SEPAY_ENV == "sandbox" else "🚀 PRODUCTION"
    print(f"""
╔═══════════════════════════════════════════════════════════╗
║           CREDIT SERVER  {env_label:<30}║
╠═══════════════════════════════════════════════════════════╣
║  Merchant  : {SEPAY_MERCHANT_ID:<46}║
║  Bank      : {BANK_NAME} - {BANK_ACCOUNT_NO:<36}║
║  Port      : {port:<46}║
╠═══════════════════════════════════════════════════════════╣
║  IPN URL (dat trong SePay dashboard):                    ║
║  http://YOUR_IP:{port}/sepay/ipn                          ║
╠═══════════════════════════════════════════════════════════╣
║  Pricing: 1.000 VND = 1 credit                           ║
║    Imagen 4 Fast    = 1 credit ($0.02/anh)               ║
║    Imagen 4 Quality = 3 credits ($0.06/anh)              ║
╚═══════════════════════════════════════════════════════════╝
""")

    app.run(host=host, port=port, debug=False)


def create_app():
    """
    Factory function cho gunicorn (Render.com).
    Usage: gunicorn 'credit_server:create_app()' --bind 0.0.0.0:$PORT
    """
    try:
        from flask import Flask, request, jsonify
    except ImportError:
        raise RuntimeError("pip install flask")

    app = Flask(__name__)
    _register_routes(app)
    return app


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]

    if "test" in args:
        # Gia lap thanh toan 50,000 VND cho VH001
        print("[TEST] Gia lap SePay IPN - chuyen 50.000 VND ma VH001...")
        fake_ipn = {
            "transaction_id":       "SP-TXN-TEST-001",
            "merchant_id":          SEPAY_MERCHANT_ID,
            "order_invoice_number": "VH001-TEST",
            "amount":               50000,
            "currency":             "VND",
            "status":               "success",
            "payment_method":       "BANK_TRANSFER",
            "description":          "VH001 nap tien test",
            "created_at":           datetime.now().isoformat(),
        }
        result = handle_sepay_ipn(fake_ipn)
        print(f"  Ket qua : {result}")
        print(f"  Credits : VH001 hien co {get_user_credits('VH001')} credits")

    elif "credits" in args:
        # Xem tat ca credits
        data = _load_credits()
        if not data:
            print("Chua co user nao.")
        else:
            print(f"{'Ma KH':<12} {'Credits':>8} {'Da nap (VND)':>14}")
            print("-" * 38)
            for code, info in sorted(data.items()):
                print(f"{code:<12} {info.get('credits',0):>8,} {info.get('total_paid_vnd',0):>14,}")

    elif "add" in args:
        # Thu cong tay: python credit_server.py add VH001 50000
        try:
            code   = args[args.index("add") + 1].upper()
            amount = int(args[args.index("add") + 2])
            total  = add_credits(code, amount, "cli_manual")
            print(f"[OK] Da cong {amount // VND_PER_CREDIT} credits cho {code}. Tong: {total}")
        except (IndexError, ValueError):
            print("Cu phap: python credit_server.py add <USER_CODE> <AMOUNT_VND>")

    else:
        port = int(os.environ.get("PORT", 5000))
        run_server(port=port)

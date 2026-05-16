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

# ── Version check API (dung cho auto-update notification trong client) ────────
@app.route("/api/version", methods=["GET"])
def api_version():
    """
    Tra ve thong tin phien ban moi nhat.
    Admin chi can cap nhat env vars tren Render dashboard, khong can rebuild client.
    ENV: LATEST_VERSION, LATEST_URL, LATEST_NOTE, LATEST_SIZE_MB
    """
    return jsonify({
        "version":  os.environ.get("LATEST_VERSION", "VS6.62"),
        "url":      os.environ.get("LATEST_URL", ""),
        "note":     os.environ.get("LATEST_NOTE", "Cap nhat moi nhat tu server"),
        "size_mb":  int(os.environ.get("LATEST_SIZE_MB", "355")),
    }), 200


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

# ── KIE API Proxy — key chi ton tai tren server, EXE khong biet ──────────────
_KIE_KEY = os.environ.get("KIE_API_KEY", "").strip()

@app.route("/api/kie/image", methods=["POST"])
def api_kie_image():
    """
    Proxy tao anh (nano-banana-2). Key KIE chi co tren server.
    Body: {user_code, prompt, aspect_ratio, model, image_url (optional)}
    Returns: {ok, image_base64, credits}
    """
    import requests as _req, base64 as _b64, json as _jj
    body       = request.get_json(force=True) or {}
    user_code  = str(body.get("user_code", "")).upper().strip()
    prompt     = body.get("prompt", "")
    aspect     = body.get("aspect_ratio", "9:16")
    model      = body.get("model", "nano-banana-2")
    img_url    = body.get("image_url", "")

    if not user_code or not prompt:
        return jsonify({"ok": False, "error": "missing_params"}), 400
    if not _KIE_KEY:
        return jsonify({"ok": False, "error": "service_unavailable"}), 503

    if not cs.deduct_credits(user_code, 1):
        return jsonify({"ok": False, "error": "insufficient_credits",
                        "credits": cs.get_user_credits(user_code)}), 402

    hdrs = {"Authorization": f"Bearer {_KIE_KEY}", "Content-Type": "application/json"}
    inp  = {"prompt": prompt, "aspect_ratio": aspect}
    if img_url:
        inp["image_url"] = img_url

    try:
        r = _req.post("https://api.kie.ai/api/v1/jobs/createTask",
                      headers=hdrs, json={"model": model, "input": inp}, timeout=30)
        if r.status_code != 200:
            cs.add_credits(user_code, 1000, "refund_kie_fail")
            return jsonify({"ok": False, "error": f"upstream_{r.status_code}"}), 502

        task_id = (r.json().get("data") or {}).get("taskId", "")
        if not task_id:
            cs.add_credits(user_code, 1000, "refund_no_task")
            return jsonify({"ok": False, "error": "no_task_id"}), 502

        # Poll toi da 120s — KIE dung state+resultJson
        import time as _t
        for _ in range(24):
            _t.sleep(5)
            pr = _req.get(f"https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}",
                          headers=hdrs, timeout=15)
            if pr.status_code != 200:
                continue
            d     = (pr.json().get("data") or {})
            state = str(d.get("state") or d.get("status") or "").lower()
            if state == "success":
                try:
                    rj  = _jj.loads(d.get("resultJson") or "{}")
                    url = (rj.get("resultUrls") or [""])[0]
                except Exception:
                    url = ""
                if not url:
                    url = ((d.get("works") or [{}])[0]).get("url", "")
                if url:
                    img = _req.get(url, timeout=30)
                    if img.ok:
                        return jsonify({"ok": True,
                                        "image_base64": _b64.b64encode(img.content).decode(),
                                        "credits": cs.get_user_credits(user_code)})
            elif state in ("fail", "failed", "error"):
                cs.add_credits(user_code, 1000, "refund_gen_failed")
                return jsonify({"ok": False, "error": "generation_failed"}), 502

        return jsonify({"ok": False, "error": "timeout", "task_id": task_id}), 504

    except Exception as e:
        cs.add_credits(user_code, 1000, "refund_exception")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/kie/video_task", methods=["POST"])
def api_kie_video_task():
    """
    Tao task video/anh (Kling, Seedance, TryOn). Tra ve task_id, client tu poll.
    Body: {user_code, model, input, credits}
    """
    import requests as _req
    body      = request.get_json(force=True) or {}
    user_code = str(body.get("user_code", "")).upper().strip()
    model     = body.get("model", "")
    inp       = body.get("input", {})
    credits   = max(1, int(body.get("credits", 1)))

    if not user_code or not model:
        return jsonify({"ok": False, "error": "missing_params"}), 400
    if not _KIE_KEY:
        return jsonify({"ok": False, "error": "service_unavailable"}), 503

    if not cs.deduct_credits(user_code, credits):
        return jsonify({"ok": False, "error": "insufficient_credits",
                        "credits": cs.get_user_credits(user_code)}), 402

    hdrs = {"Authorization": f"Bearer {_KIE_KEY}", "Content-Type": "application/json"}
    try:
        r = _req.post("https://api.kie.ai/api/v1/jobs/createTask",
                      headers=hdrs, json={"model": model, "input": inp}, timeout=30)
        if r.status_code != 200:
            cs.add_credits(user_code, credits * 1000, "refund_kie_fail")
            return jsonify({"ok": False, "error": f"upstream_{r.status_code}"}), 502

        task_id = (r.json().get("data") or {}).get("taskId", "")
        if not task_id:
            cs.add_credits(user_code, credits * 1000, "refund_no_task")
            return jsonify({"ok": False, "error": "no_task_id"}), 502

        return jsonify({"ok": True, "task_id": task_id,
                        "credits": cs.get_user_credits(user_code)})
    except Exception as e:
        cs.add_credits(user_code, credits * 1000, "refund_exception")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/kie/poll/<task_id>", methods=["GET"])
def api_kie_poll(task_id):
    """Proxy kiem tra trang thai task — khong lo key, khong tru credit."""
    import requests as _req, json as _jj
    if not _KIE_KEY:
        return jsonify({"ok": False, "error": "service_unavailable"}), 503
    try:
        hdrs = {"Authorization": f"Bearer {_KIE_KEY}"}
        pr   = _req.get(f"https://api.kie.ai/api/v1/jobs/recordInfo?taskId={task_id}",
                        headers=hdrs, timeout=15)
        if pr.status_code != 200:
            return jsonify({"ok": False, "status": "unknown"}), pr.status_code
        d     = pr.json().get("data") or {}
        state = str(d.get("state") or d.get("status") or "").lower()
        # Lay URL — ho tro ca 2 format KIE
        urls = []
        try:
            rj   = _jj.loads(d.get("resultJson") or "{}")
            urls = rj.get("resultUrls") or []
        except Exception:
            pass
        if not urls:
            urls = [w.get("url", "") for w in (d.get("works") or []) if w.get("url")]
        done    = state in ("success", "fail", "failed", "error")
        success = (state == "success")
        return jsonify({"ok": True, "status": state, "urls": urls,
                        "done": done, "success": success})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/kie/upload", methods=["POST"])
def api_kie_upload():
    """
    Proxy upload file (anh/video) len KIE CDN.
    Client gui base64, server dung key de upload, tra ve downloadUrl.
    Body: {user_code, file_base64, filename, mime_type}
    Returns: {ok, url, error}
    """
    import requests as _req, base64 as _b64, io as _io
    body      = request.get_json(force=True) or {}
    user_code = str(body.get("user_code", "")).upper().strip()
    file_b64  = body.get("file_base64", "")
    filename  = body.get("filename", "upload.jpg")
    mime_type = body.get("mime_type", "image/jpeg")

    if not user_code or not file_b64:
        return jsonify({"ok": False, "error": "missing_params"}), 400
    if not _KIE_KEY:
        return jsonify({"ok": False, "error": "service_unavailable"}), 503

    # Kiem tra credits (upload khong tru credit, chi can > 0)
    if cs.get_user_credits(user_code) <= 0:
        return jsonify({"ok": False, "error": "insufficient_credits"}), 402

    try:
        file_bytes = _b64.b64decode(file_b64)
        hdrs = {"Authorization": f"Bearer {_KIE_KEY}"}
        r = _req.post(
            "https://kieai.redpandaai.co/api/file-stream-upload",
            headers=hdrs,
            files={"file": (filename, _io.BytesIO(file_bytes), mime_type)},
            data={"uploadPath": "uploads/"},
            timeout=120,
        )
        data = r.json() if r.status_code == 200 else {}
        url  = (data.get("data") or {}).get("downloadUrl", "")
        if url:
            return jsonify({"ok": True, "url": url})
        # Fallback: thu base64 upload endpoint
        payload_b64 = {
            "fileBase64": file_b64,
            "fileName":   filename,
            "mimeType":   mime_type,
        }
        r2 = _req.post(
            "https://kieai.redpandaai.co/api/file/upload/base64",
            headers={**hdrs, "Content-Type": "application/json"},
            json=payload_b64,
            timeout=60,
        )
        data2 = r2.json() if r2.status_code == 200 else {}
        url2  = (data2.get("data") or {}).get("fileUrl", "") or (data2.get("data") or {}).get("downloadUrl", "")
        if url2:
            return jsonify({"ok": True, "url": url2})
        return jsonify({"ok": False, "error": f"upload_failed_http_{r.status_code}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/kie/upload_mp", methods=["POST"])
def api_kie_upload_mp():
    """
    Proxy upload file len KIE CDN bang multipart (nhanh hon base64 ~30%).
    Client gui multipart: user_code (form field) + file (file field).
    Returns: {ok, url, error}
    """
    import requests as _req, io as _io
    user_code = request.form.get("user_code", "").upper().strip()
    f         = request.files.get("file")

    if not user_code or not f:
        return jsonify({"ok": False, "error": "missing_params"}), 400
    if not _KIE_KEY:
        return jsonify({"ok": False, "error": "service_unavailable"}), 503
    if cs.get_user_credits(user_code) <= 0:
        return jsonify({"ok": False, "error": "insufficient_credits"}), 402

    try:
        file_bytes = f.read()
        hdrs = {"Authorization": f"Bearer {_KIE_KEY}"}
        r = _req.post(
            "https://kieai.redpandaai.co/api/file-stream-upload",
            headers=hdrs,
            files={"file": (f.filename, _io.BytesIO(file_bytes), f.mimetype)},
            data={"uploadPath": "uploads/"},
            timeout=120,
        )
        data = r.json() if r.status_code == 200 else {}
        url  = (data.get("data") or {}).get("downloadUrl", "")
        if url:
            return jsonify({"ok": True, "url": url})
        return jsonify({"ok": False, "error": f"kie_http_{r.status_code}"}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── Entry point local ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

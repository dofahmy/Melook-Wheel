import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote

import requests

import config
import database

DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "admin_dashboard.html")


def _json_bytes(data):
    return json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")


def _validate_init_data(init_data: str):
    """تحقق Telegram WebApp initData + رجوع بيانات المستخدم."""
    if not init_data or not config.BOT_TOKEN:
        return None
    try:
        pairs = parse_qs(init_data, keep_blank_values=True)
        received_hash = (pairs.pop("hash", [""])[0] or "").strip()
        if not received_hash:
            return None
        flat = {k: v[0] for k, v in pairs.items()}
        auth_date = int(flat.get("auth_date", "0") or 0)
        if auth_date and abs(int(time.time()) - auth_date) > 86400:
            return None
        data_check_string = "\n".join(f"{k}={flat[k]}" for k in sorted(flat))
        secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received_hash):
            return None
        user_raw = flat.get("user")
        return json.loads(user_raw) if user_raw else None
    except Exception:
        return None


def _telegram_send_message(chat_id: int, text: str) -> tuple[bool, str]:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            json={
                "chat_id": int(chat_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        data = r.json()
        if r.ok and data.get("ok"):
            return True, ""
        return False, data.get("description") or f"HTTP {r.status_code}"
    except Exception as exc:
        return False, str(exc)


class Handler(BaseHTTPRequestHandler):
    server_version = "WafrAdmin/1.0"

    def log_message(self, fmt, *args):
        return

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Telegram-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, code, data):
        body = _json_bytes(data)
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_admin(self):
        init_data = self.headers.get("X-Telegram-Init-Data", "")
        user = _validate_init_data(init_data)
        if not user:
            return None
        uid = int(user.get("id") or 0)
        if not uid or not database.is_admin(uid):
            return None
        return uid

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/admin"):
            try:
                body = open(DASHBOARD_PATH, "rb").read()
            except FileNotFoundError:
                body = b"Admin dashboard file missing"
                self.send_response(500)
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        admin_id = self._auth_admin()
        if not admin_id:
            self._send_json(403, {"ok": False, "error": "غير مسموح"})
            return

        qs = parse_qs(parsed.query)
        period = (qs.get("period", ["all"])[0] or "all")
        if parsed.path == "/api/admin/summary":
            self._send_json(200, {"ok": True, "data": database.get_admin_report_summary(period)})
            return
        if parsed.path == "/api/admin/redemptions":
            rows = [dict(x) for x in database.list_pending_redemptions_for_web()]
            self._send_json(200, {"ok": True, "data": rows})
            return
        if parsed.path == "/api/admin/customers":
            search = qs.get("search", [""])[0]
            rows = [dict(x) for x in database.list_customer_reports(period, search)]
            self._send_json(200, {"ok": True, "data": rows})
            return
        if parsed.path == "/api/admin/customer":
            try:
                uid = int(qs.get("user_id", ["0"])[0])
            except Exception:
                uid = 0
            data = database.get_customer_report(uid, period) if uid else None
            if not data:
                self._send_json(404, {"ok": False, "error": "العميل غير موجود"})
            else:
                self._send_json(200, {"ok": True, "data": data})
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        admin_id = self._auth_admin()
        if not admin_id:
            self._send_json(403, {"ok": False, "error": "غير مسموح"})
            return
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}

        if parsed.path == "/api/admin/send-gift-code":
            try:
                request_id = int(payload.get("request_id"))
            except Exception:
                request_id = 0
            code = str(payload.get("gift_code") or "").strip()
            if not request_id or not code:
                self._send_json(400, {"ok": False, "error": "اكتبي كود بطاقة الهدية"})
                return
            r = database.reserve_redemption_for_send(request_id, code)
            if not r:
                current = database.get_redemption_request(request_id)
                if current and current.get("status") == "paid":
                    error = "الطلب تم استبداله بالفعل"
                elif current and current.get("status") == "processing":
                    error = "الطلب قيد الإرسال بالفعل من أدمن آخر"
                else:
                    error = "طلب الاستبدال غير موجود أو لم يعد متاحًا"
                self._send_json(409, {"ok": False, "error": error})
                return
            amount = float(r["amount"] or 0)
            amount_text = f"{amount:g}"
            import html
            safe_code = html.escape(code)
            redeem_url = "https://link.amazon/B02oNEoYz"
            ok, err = _telegram_send_message(
                r["user_id"],
                f"🎁 تم استبدال <b>{amount_text} جنيه</b> من رصيدك بنجاح.\n\n"
                f"كود بطاقة الهدية:\n<code>{safe_code}</code>\n\n"
                "📋 اضغط على الكود لنسخه.\n\n"
                "يمكنك استرداد قيمة البطاقة باستخدام الكود أعلاه من خلال الرابط التالي:\n"
                f'<a href="{redeem_url}">استرداد قيمة بطاقة الهدية</a>\n\n'
                "شكرًا لاستخدام وفر كاش ❤️",
            )
            if not ok:
                database.release_redemption_send(request_id)
                self._send_json(502, {"ok": False, "error": f"فشل إرسال الكود للعميل: {err}"})
                return
            paid = database.mark_redemption_paid(request_id, admin_id, code)
            self._send_json(200, {"ok": True, "data": paid})
            return
        self._send_json(404, {"ok": False, "error": "Not found"})


def start_admin_server():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="admin-web")
    thread.start()
    return server

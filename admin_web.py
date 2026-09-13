import hashlib
import hmac
import html
import json
import os
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

import requests

import config
import database
import web_auth

try:
    import product_catalog
except Exception:
    product_catalog = None

DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "admin_dashboard.html")
WEB_APP_PATH = os.path.join(os.path.dirname(__file__), "web_app.html")


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


def _inject_tag(url: str, tag: str | None) -> str:
    if not tag:
        return url
    try:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["tag"] = tag
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    except Exception:
        return url


def _asin_from_url(url: str) -> str | None:
    import re
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", str(url or ""), re.I)
    return m.group(1).upper() if m else None


def _caption_without_urls(text: str) -> str:
    import re
    cleaned = re.sub(r"https?://\\S+", "", str(text or ""))
    cleaned = re.sub(r"[ \\t]+\
", "\
", cleaned)
    cleaned = re.sub(r"\
{3,}", "\
\
", cleaned)
    return cleaned.strip()


def _product_meta_for_links(links: list[str]) -> dict:
    if not product_catalog:
        return {}
    for link in links:
        asin = _asin_from_url(link)
        if not asin:
            continue
        try:
            p = product_catalog.get_product(asin)
        except Exception:
            p = None
        if p:
            return {
                "asin": p.asin,
                "title": p.title,
                "price": p.price,
                "old_price": p.old_price,
                "discount_percent": p.discount_percent,
                "image_url": p.image_url,
            }
    return {}


class Handler(BaseHTTPRequestHandler):
    server_version = "Wafr/2.0"

    def log_message(self, fmt, *args):
        return

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Telegram-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")

    def _send_json(self, code, data, extra_headers=None):
        body = _json_bytes(data)
        self.send_response(code)
        self._cors()
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self, path):
        try:
            body = open(path, "rb").read()
        except FileNotFoundError:
            body = b"File missing"
            self.send_response(500)
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _auth_admin(self):
        init_data = self.headers.get("X-Telegram-Init-Data", "")
        user = _validate_init_data(init_data)
        if not user:
            return None
        uid = int(user.get("id") or 0)
        if not uid or not database.is_admin(uid):
            return None
        return uid

    def _session_token(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
            morsel = cookie.get(web_auth.SESSION_COOKIE)
            return morsel.value if morsel else None
        except Exception:
            return None

    def _auth_web(self):
        return web_auth.get_account_from_session(self._session_token())

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/admin"):
            self._serve_html(DASHBOARD_PATH)
            return
        if parsed.path in ("/app", "/wafr"):
            self._serve_html(WEB_APP_PATH)
            return
        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "service": "wafr"})
            return

        # Public web-account API authenticated by session cookie.
        if parsed.path == "/api/app/me":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجلي دخول"})
                return
            self._send_json(200, {"ok": True, "data": {
                "account_id": account["id"],
                "phone": account["phone_e164"],
                "gift_balance": account.get("gift_balance", 0),
                "points_balance": account.get("points_balance", 0),
                "spins_balance": account.get("spins_balance", 0),
                "source_first": account.get("source_first") or "direct",
                "source_last": account.get("source_last") or "direct",
                "telegram_linked": bool(account.get("telegram_user_id")),
            }})
            return

        if parsed.path == "/api/app/deal-image":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجلي دخول"})
                return
            qs = parse_qs(parsed.query)
            try:
                deal_id = int(qs.get("deal", ["0"])[0])
            except Exception:
                deal_id = 0
            deal = database.get_deal_by_id(deal_id) if deal_id else None
            if not deal:
                self.send_response(404); self.end_headers(); return

            photo_file_id = str(deal.get("photo_file_id") or "").strip()
            if photo_file_id and config.BOT_TOKEN:
                try:
                    info = requests.get(
                        f"https://api.telegram.org/bot{config.BOT_TOKEN}/getFile",
                        params={"file_id": photo_file_id},
                        timeout=15,
                    )
                    payload = info.json() if info.content else {}
                    file_path = ((payload.get("result") or {}).get("file_path") or "") if isinstance(payload, dict) else ""
                    if info.ok and file_path:
                        image = requests.get(
                            f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/{file_path}",
                            timeout=25,
                        )
                        if image.ok and image.content:
                            body = image.content
                            self.send_response(200)
                            self._security_headers()
                            self.send_header("Content-Type", image.headers.get("Content-Type", "image/jpeg"))
                            self.send_header("Cache-Control", "private, max-age=1800")
                            self.send_header("Content-Length", str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            return
                except Exception:
                    pass

            # Fallback to the SPCC/catalog image when the Telegram photo is unavailable.
            links = database.get_deal_links(deal.get("base_link"))
            meta = _product_meta_for_links(links)
            fallback = str(meta.get("image_url") or "").strip()
            if fallback:
                self.send_response(302)
                self._security_headers()
                self.send_header("Cache-Control", "no-store")
                self.send_header("Location", fallback)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()
            return

        if parsed.path == "/api/app/offers":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجلي دخول"})
                return
            user_id = int(account["user_id"])
            rows, mode, seen_up_to = database.list_web_offers_for_user(user_id, 20)
            data = []
            for row in rows:
                deal = dict(row)
                links = database.get_deal_links(deal.get("base_link"))
                meta = _product_meta_for_links(links)
                item = {
                    "id": int(deal["id"]),
                    "caption": _caption_without_urls(deal.get("caption") or ""),
                    "posted_at": deal.get("posted_at"),
                    "links_count": len(links),
                    "has_photo": bool(deal.get("photo_file_id")),
                    "image_url": f"/api/app/deal-image?deal={int(deal['id'])}",
                    "product": meta,
                }
                data.append(item)
            database.mark_web_offers_seen(user_id, mode, seen_up_to)
            message = "عروض جديدة" if mode in ("new", "pending") else "أحدث العروض المتاحة"
            self._send_json(200, {"ok": True, "data": data, "mode": mode, "message": message})
            return

        if parsed.path == "/go":
            account = self._auth_web()
            if not account:
                self.send_response(302)
                self.send_header("Location", "/app")
                self.end_headers()
                return
            qs = parse_qs(parsed.query)
            try:
                deal_id = int(qs.get("deal", ["0"])[0])
                link_index = int(qs.get("i", ["0"])[0])
            except Exception:
                deal_id, link_index = 0, -1
            deal = database.get_deal_by_id(deal_id) if deal_id else None
            links = database.get_deal_links(deal.get("base_link")) if deal else []
            if link_index < 0 or link_index >= len(links):
                self._send_json(404, {"ok": False, "error": "العرض غير موجود"})
                return
            url = links[link_index]
            tag = database.get_user_tag_keyword(int(account["user_id"]))
            url = _inject_tag(url, tag)
            database.record_web_offer_click(
                int(account["id"]), int(account["user_id"]), deal_id, link_index, account.get("source_last") or "direct"
            )
            self.send_response(302)
            self._security_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Location", url)
            self.end_headers()
            return

        # Everything below is admin-only.
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
        parsed = urlparse(self.path)
        payload = self._read_json()

        # -------- Public account / OTP endpoints --------
        if parsed.path == "/api/auth/send-otp":
            phone = web_auth.normalize_egypt_phone(str(payload.get("phone") or ""))
            if not phone:
                self._send_json(400, {"ok": False, "error": "اكتبي رقم موبايل مصري صحيح"})
                return
            ok, err = web_auth.send_otp(phone)
            if not ok:
                self._send_json(502, {"ok": False, "error": err})
                return
            self._send_json(200, {"ok": True})
            return

        if parsed.path == "/api/auth/verify-otp":
            phone = web_auth.normalize_egypt_phone(str(payload.get("phone") or ""))
            code = str(payload.get("code") or "").strip()
            source = str(payload.get("source") or "direct")
            if not phone:
                self._send_json(400, {"ok": False, "error": "رقم الموبايل غير صحيح"})
                return
            ok, err = web_auth.verify_otp(phone, code)
            if not ok:
                self._send_json(401, {"ok": False, "error": err})
                return
            account = database.get_or_create_web_account(phone, source)
            token = web_auth.create_session(int(account["id"]))
            cookie = (
                f"{web_auth.SESSION_COOKIE}={token}; Path=/; Max-Age={30*24*3600}; "
                "HttpOnly; Secure; SameSite=Lax"
            )
            self._send_json(200, {"ok": True, "data": {"account_id": account["id"]}}, {"Set-Cookie": cookie})
            return

        if parsed.path == "/api/auth/logout":
            web_auth.logout_session(self._session_token())
            cookie = f"{web_auth.SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            self._send_json(200, {"ok": True}, {"Set-Cookie": cookie})
            return

        if parsed.path == "/api/app/link-code":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجلي دخول"})
                return
            if account.get("telegram_user_id"):
                self._send_json(409, {"ok": False, "error": "Telegram مربوط بالفعل بالحساب"})
                return
            code = web_auth.create_link_code(int(account["id"]))
            self._send_json(200, {"ok": True, "data": {"code": code, "expires_minutes": 10}})
            return

        # -------- Admin endpoints --------
        admin_id = self._auth_admin()
        if not admin_id:
            self._send_json(403, {"ok": False, "error": "غير مسموح"})
            return

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
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="wafr-web")
    thread.start()
    return server

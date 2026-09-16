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
MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "manifest.webmanifest")
SERVICE_WORKER_PATH = os.path.join(os.path.dirname(__file__), "service-worker.js")
APP_ICON_180_PATH = os.path.join(os.path.dirname(__file__), "app-icon-180.png")
APP_ICON_192_PATH = os.path.join(os.path.dirname(__file__), "app-icon-192.png")
APP_ICON_512_PATH = os.path.join(os.path.dirname(__file__), "app-icon-512.png")



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


def _telegram_send_message(chat_id: int, text: str, button_text: str | None = None,
                           button_url: str | None = None) -> tuple[bool, str]:
    try:
        payload = {
            "chat_id": int(chat_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if button_text and button_url:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": button_text, "url": button_url}]]}
        r = requests.post(
            f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        data = r.json()
        if r.ok and data.get("ok"):
            database.set_telegram_delivery_status(int(chat_id), "active")
            return True, ""
        error = data.get("description") or f"HTTP {r.status_code}"
        blocked = r.status_code == 403 and any(x in error.lower() for x in ("blocked", "deactivated", "chat not found"))
        database.set_telegram_delivery_status(int(chat_id), "blocked" if blocked else "unknown", error)
        return False, error
    except Exception as exc:
        database.set_telegram_delivery_status(int(chat_id), "unknown", str(exc))
        return False, str(exc)


def _personalize_customer_message(template: str, customer: dict) -> str:
    name = str(customer.get("first_name") or customer.get("username") or "").strip()
    return str(template or "").replace("{first_name}", html.escape(name) if name else "بيك")


def _run_customer_campaign(campaign_id: int, customers: list[dict], message: str,
                           button_text: str | None, button_url: str | None,
                           mark_welcome: bool = False):
    for customer in customers:
        user_id = int(customer["user_id"])
        if str(customer.get("telegram_status") or "unknown") == "blocked":
            database.record_campaign_delivery(campaign_id, user_id, "blocked", "مستبعد: حاظر البوت")
            continue
        text = _personalize_customer_message(message, customer)
        ok, error = _telegram_send_message(user_id, text, button_text, button_url)
        if ok:
            database.record_campaign_delivery(campaign_id, user_id, "sent")
            if mark_welcome:
                database.mark_welcome_check_sent(user_id)
        else:
            latest = database.get_user(user_id)
            status = "blocked" if latest and latest["telegram_status"] == "blocked" else "failed"
            database.record_campaign_delivery(campaign_id, user_id, status, error)
        time.sleep(0.05)



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


def _golden_question_payload(user_id: int, existing=None):
    """Create or restore one web golden question using the same rewards logic as Telegram."""
    if not product_catalog:
        raise RuntimeError("ملف منتجات العجلة غير متاح")

    row = database.get_user(user_id)
    if not row:
        raise RuntimeError("الحساب غير موجود")

    # لو الجولة خلصت، جهّز نفس لفة الجائزة الشخصية بدل سؤال جديد.
    if int(row["golden_answered_count"] or 0) >= int(row["golden_target"] or 0):
        pending = database.get_pending_lucky_spin(user_id)
        if pending:
            return {
                "stage": "prize",
                "spin_id": int(pending["id"]),
                "prize": float(pending["prize"] or 0),
            }
        spin_id, prize, _ = database.create_lucky_spin(
            user_id, float(row["golden_round_earnings"] or 0)
        )
        return {"stage": "prize", "spin_id": spin_id, "prize": float(prize)}

    qrow = existing or database.get_pending_web_golden_question(user_id)
    if qrow:
        product = product_catalog.get_product(str(qrow.get("asin") or ""))
        return {
            "stage": "question",
            "question_id": int(qrow["id"]),
            "prompt": qrow.get("prompt") or "جاوبي السؤال",
            "options": qrow.get("options") or [],
            "product_link": qrow.get("product_link") or product_catalog.build_affiliate_link(qrow["asin"]),
            "progress": {
                "answered": int(row["golden_answered_count"] or 0),
                "correct": int(row["golden_opened_count"] or 0),
                "target": int(row["golden_target"] or 0),
            },
            "product": {
                "asin": getattr(product, "asin", qrow.get("asin")) if product else qrow.get("asin"),
                "title": getattr(product, "title", "") if product else "",
                "image_url": getattr(product, "image_url", "") if product else "",
                "price": getattr(product, "price", 0) if product else 0,
            },
        }

    asked_asins = database.list_todays_quizzed_asins(user_id)
    answered_count = int(row["golden_answered_count"] or 0)
    all_seen_asins = database.list_all_quizzed_asins(user_id)
    current_round_epc = database.get_current_golden_round_epc(user_id, answered_count)
    product = product_catalog.choose_product(
        asked_asins,
        question_index=answered_count,
        user_id=user_id,
        current_round_epc=current_round_epc,
        all_seen_asins=all_seen_asins,
    )
    question = product_catalog.question_for(product)
    reward_value = product_catalog.customer_reward_for_epc(product.expected_revenue_per_click)
    product_link = product_catalog.build_affiliate_link(product.asin)
    question_id = database.create_golden_question(
        user_id=user_id,
        asin=product.asin,
        question_type=question["type"],
        correct_index=question["correct_index"],
        epc=product.expected_revenue_per_click,
        reward_value=reward_value,
        prompt=question["prompt"],
        options=question["options"],
        product_link=product_link,
    )
    database.log_quiz_asked(user_id, product.asin)
    return {
        "stage": "question",
        "question_id": int(question_id),
        "prompt": question["prompt"],
        "options": question["options"],
        "product_link": product_link,
        "progress": {
            "answered": answered_count,
            "correct": int(row["golden_opened_count"] or 0),
            "target": int(row["golden_target"] or 0),
        },
        "product": {
            "asin": product.asin,
            "title": product.title,
            "image_url": product.image_url,
            "price": product.price,
        },
    }



ADMIN_WEB_COOKIE = "wafr_admin_session"
ADMIN_WEB_MAX_AGE = 30 * 24 * 3600


def _admin_web_password():
    return (os.getenv("ADMIN_WEB_PASSWORD") or "").strip()


def _admin_cookie_secret():
    # BOT_TOKEN + password makes a stable signing secret without exposing either value.
    raw = (str(getattr(config, "BOT_TOKEN", "") or "") + "|" + _admin_web_password()).encode("utf-8")
    return hashlib.sha256(raw).digest()


def _make_admin_cookie() -> str:
    expires = int(time.time()) + ADMIN_WEB_MAX_AGE
    payload = str(expires)
    sig = hmac.new(_admin_cookie_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _valid_admin_cookie(value: str | None) -> bool:
    if not value or not _admin_web_password():
        return False
    try:
        expires_s, sig = value.split(".", 1)
        expires = int(expires_s)
        if expires < int(time.time()):
            return False
        expected = hmac.new(_admin_cookie_secret(), expires_s.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


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

    def _serve_static(self, path, content_type, cache_control="public, max-age=86400"):
        try:
            body = open(path, "rb").read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
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
        # 1) Telegram Mini App admin auth (existing path).
        init_data = self.headers.get("X-Telegram-Init-Data", "")
        user = _validate_init_data(init_data)
        if user:
            uid = int(user.get("id") or 0)
            if uid and database.is_admin(uid):
                return uid

        # 2) Normal browser admin auth through a signed HttpOnly cookie.
        raw = self.headers.get("Cookie", "")
        try:
            cookie = SimpleCookie(); cookie.load(raw)
            morsel = cookie.get(ADMIN_WEB_COOKIE)
            if morsel and _valid_admin_cookie(morsel.value):
                ids = list(getattr(config, "ADMIN_IDS", []) or [])
                return int(ids[0]) if ids else 1
        except Exception:
            pass
        return None

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

    def _client_ip(self):
        """Best-effort public client IP as forwarded by Railway's reverse proxy."""
        forwarded = (self.headers.get("X-Forwarded-For") or "").strip()
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:64]
        real_ip = (self.headers.get("X-Real-IP") or "").strip()
        if real_ip:
            return real_ip[:64]
        try:
            return str(self.client_address[0])[:64]
        except Exception:
            return ""

    def _auth_web(self):
        account = web_auth.get_account_from_session(self._session_token())
        if account and int(account.get("is_suspended") or 0):
            return None
        # Any authenticated Web App request counts as current activity, exactly like
        # a Telegram interaction. This keeps the admin Online indicator unified.
        if account:
            try:
                database.set_user_activity_now(int(account["user_id"]))
                database.set_web_account_last_ip(int(account["id"]), self._client_ip())
                account["last_ip"] = self._client_ip()
            except Exception:
                pass
        return account

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
        if parsed.path == "/manifest.webmanifest":
            self._serve_static(MANIFEST_PATH, "application/manifest+json; charset=utf-8", "no-cache")
            return
        if parsed.path == "/service-worker.js":
            self._serve_static(SERVICE_WORKER_PATH, "application/javascript; charset=utf-8", "no-cache")
            return
        if parsed.path == "/app-icon-180.png":
            self._serve_static(APP_ICON_180_PATH, "image/png", "public, max-age=604800")
            return
        if parsed.path == "/app-icon-192.png":
            self._serve_static(APP_ICON_192_PATH, "image/png", "public, max-age=604800")
            return
        if parsed.path == "/app-icon-512.png":
            self._serve_static(APP_ICON_512_PATH, "image/png", "public, max-age=604800")
            return
        if parsed.path == "/health":
            self._send_json(200, {"ok": True, "service": "wafr"})
            return

        # One-time IP capture for a Telegram online session, then redirect to Amazon.
        if parsed.path == "/tg-go":
            qs = parse_qs(parsed.query)
            try:
                user_id = int(qs.get("u", ["0"])[0])
                question_id = int(qs.get("q", ["0"])[0])
                supplied_sig = str(qs.get("s", [""])[0])
            except Exception:
                user_id, question_id, supplied_sig = 0, 0, ""
            payload = f"{user_id}:{question_id}"
            secret = str(getattr(config, "BOT_TOKEN", "") or "").encode("utf-8")
            expected_sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32] if secret else ""
            if not user_id or not question_id or not expected_sig or not hmac.compare_digest(supplied_sig, expected_sig):
                self._send_json(403, {"ok": False, "error": "invalid link"})
                return
            qrow = database.get_golden_question_for_redirect(question_id, user_id)
            if not qrow or product_catalog is None:
                self._send_json(404, {"ok": False, "error": "product not found"})
                return
            target = product_catalog.build_affiliate_link(str(qrow["asin"]))
            database.capture_telegram_user_ip(user_id, self._client_ip())
            database.set_user_activity_now(user_id)
            self.send_response(302)
            self._security_headers()
            self.send_header("Cache-Control", "no-store")
            self.send_header("Location", target)
            self.end_headers()
            return

        # Public web-account API authenticated by session cookie.
        if parsed.path == "/api/app/me":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            session_token = self._session_token()
            headers = {}
            if session_token:
                headers["Set-Cookie"] = (
                    f"{web_auth.SESSION_COOKIE}={session_token}; Path=/; "
                    f"Max-Age={web_auth.SESSION_DAYS*24*3600}; "
                    "HttpOnly; Secure; SameSite=Lax"
                )
            self._send_json(200, {"ok": True, "data": {
                "account_id": account["id"],
                "phone": account["phone_e164"],
                "gift_balance": account.get("gift_balance", 0),
                "points_balance": account.get("points_balance", 0),
                "spins_balance": account.get("spins_balance", 0),
                "source_first": account.get("source_first") or "direct",
                "source_last": account.get("source_last") or "direct",
                "telegram_linked": bool(account.get("telegram_user_id")),
            }}, headers)
            return

        if parsed.path == "/api/app/deal-image":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
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
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
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


        if parsed.path == "/api/app/golden-status":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            user_id = int(account["user_id"])
            pending = database.get_pending_lucky_spin(user_id)
            row = database.get_user(user_id)
            if not row:
                self._send_json(404, {"ok": False, "error": "الحساب غير موجود"})
                return
            if pending:
                data = {"stage": "prize", "spin_id": int(pending["id"]), "prize": float(pending["prize"] or 0)}
            elif int(row["golden_answered_count"] or 0) >= int(row["golden_target"] or 0):
                spin_id, prize, _ = database.create_lucky_spin(user_id, float(row["golden_round_earnings"] or 0))
                data = {"stage": "prize", "spin_id": spin_id, "prize": float(prize)}
            elif database.get_pending_web_golden_question(user_id):
                data = {"stage": "question"}
            else:
                data = {
                    "stage": "select",
                    "progress": {
                        "answered": int(row["golden_answered_count"] or 0),
                        "correct": int(row["golden_opened_count"] or 0),
                        "target": int(row["golden_target"] or 0),
                    },
                }
            self._send_json(200, {"ok": True, "data": data})
            return

        if parsed.path == "/api/app/golden-question":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            try:
                data = _golden_question_payload(int(account["user_id"]))
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": f"تعذر تجهيز سؤال العجلة: {exc}"})
                return
            self._send_json(200, {"ok": True, "data": data})
            return


        if parsed.path == "/api/app/redemption-status":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            data = database.get_web_redemption_status(int(account["user_id"]))
            self._send_json(200, {"ok": True, "data": data})
            return

        if parsed.path == "/api/app/account-history":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            data = database.get_web_account_history(int(account["user_id"]), 20)
            self._send_json(200, {"ok": True, "data": data})
            return

        # Everything below is admin-only.
        admin_id = self._auth_admin()
        if not admin_id:
            self._send_json(403, {"ok": False, "error": "غير مسموح"})
            return

        qs = parse_qs(parsed.query)
        period = (qs.get("period", ["all"])[0] or "all")
        date_from = (qs.get("from", [""])[0] or "").strip()
        date_to = (qs.get("to", [""])[0] or "").strip()
        if parsed.path == "/api/admin/summary":
            self._send_json(200, {"ok": True, "data": database.get_admin_report_summary(period, date_from, date_to)})
            return
        if parsed.path == "/api/admin/redemptions":
            rows = [dict(x) for x in database.list_pending_redemptions_for_web()]
            self._send_json(200, {"ok": True, "data": rows})
            return
        if parsed.path == "/api/admin/customers":
            search = qs.get("search", [""])[0]
            rows = [dict(x) for x in database.list_customer_reports(period, search, date_from=date_from, date_to=date_to)]
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
        if parsed.path == "/api/admin/central-summary":
            self._send_json(200, {"ok": True, "data": database.get_central_admin_summary(period)})
            return
        if parsed.path == "/api/admin/suspended-customers":
            rows = database.list_suspended_web_accounts(500)
            self._send_json(200, {
                "ok": True,
                "data": rows,
                "count": len(rows),
            })
            return

        if parsed.path == "/api/admin/central-customers":
            search = qs.get("search", [""])[0]
            rows = [dict(x) for x in database.list_central_customers(period, search, date_from=date_from, date_to=date_to)]
            self._send_json(200, {"ok": True, "data": rows})
            return
        if parsed.path == "/api/admin/customer-list-stats":
            self._send_json(200, {"ok": True, "data": database.get_customer_list_stats(period, date_from, date_to)})
            return
        if parsed.path == "/api/admin/funnel":
            self._send_json(200, {"ok": True, "data": database.get_admin_funnel(period)})
            return
        if parsed.path == "/api/admin/alerts":
            self._send_json(200, {"ok": True, "data": database.get_admin_alerts(30)})
            return
        if parsed.path == "/api/admin/customer-center":
            filters = {
                "search": qs.get("search", [""])[0],
                "program": qs.get("program", [""])[0],
                "telegram_status": qs.get("telegram_status", [""])[0],
                "joined_from": qs.get("joined_from", [""])[0],
                "joined_to": qs.get("joined_to", [""])[0],
                "last_active_before": qs.get("last_active_before", [""])[0],
                "min_balance": qs.get("min_balance", [""])[0],
                "max_balance": qs.get("max_balance", [""])[0],
                "welcome_unsent": qs.get("welcome_unsent", [""])[0] in ("1", "true"),
            }
            active = qs.get("is_active", [""])[0]
            if active in ("0", "1"):
                filters["is_active"] = active
            try:
                limit = int(qs.get("limit", ["500"])[0])
                offset = int(qs.get("offset", ["0"])[0])
                data = database.list_customer_center(filters, limit, offset)
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "قيمة فلتر غير صحيحة"})
                return
            self._send_json(200, {"ok": True, "data": data})
            return
        if parsed.path == "/api/admin/customer-operations":
            try:
                uid = int(qs.get("user_id", ["0"])[0])
            except Exception:
                uid = 0
            if not uid:
                self._send_json(400, {"ok": False, "error": "رقم العميل غير صحيح"})
                return
            self._send_json(200, {"ok": True, "data": database.get_customer_operations(uid)})
            return
        if parsed.path == "/api/admin/campaign":
            try:
                campaign_id = int(qs.get("id", ["0"])[0])
            except Exception:
                campaign_id = 0
            data = database.get_campaign(campaign_id) if campaign_id else None
            if not data:
                self._send_json(404, {"ok": False, "error": "الحملة غير موجودة"})
            else:
                self._send_json(200, {"ok": True, "data": data})
            return
        if parsed.path == "/api/admin/session":
            self._send_json(200, {"ok": True, "data": {"authenticated": True}})
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        payload = self._read_json()

        # -------- Browser admin login --------
        if parsed.path == "/api/admin/web-login":
            configured = _admin_web_password()
            if not configured:
                self._send_json(503, {"ok": False, "error": "ADMIN_WEB_PASSWORD مش متضاف في Railway Variables"})
                return
            password = str(payload.get("password") or "")
            if not hmac.compare_digest(password, configured):
                time.sleep(0.35)
                self._send_json(401, {"ok": False, "error": "كلمة السر غير صحيحة"})
                return
            token = _make_admin_cookie()
            cookie = f"{ADMIN_WEB_COOKIE}={token}; Path=/; Max-Age={ADMIN_WEB_MAX_AGE}; HttpOnly; Secure; SameSite=Strict"
            self._send_json(200, {"ok": True, "data": {"authenticated": True}}, {"Set-Cookie": cookie})
            return

        if parsed.path == "/api/admin/web-logout":
            cookie = f"{ADMIN_WEB_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"
            self._send_json(200, {"ok": True}, {"Set-Cookie": cookie})
            return

        # -------- Public account / OTP endpoints --------
        if parsed.path == "/api/auth/send-otp":
            phone = web_auth.normalize_egypt_phone(str(payload.get("phone") or ""))
            if not phone:
                self._send_json(400, {"ok": False, "error": "اكتب رقم موبايل مصري صحيح"})
                return
            existing = database.get_web_account_by_phone(phone)
            if existing and int(existing.get("is_suspended") or 0):
                self._send_json(423, {"ok": False, "error": 'لاحظنا دخول وخروج متكرر على حسابك. لحماية بياناتك وحسابك تم إيقاف الحساب مؤقتًا، وسيقوم أحد ممثلي خدمة العملاء بالتواصل معك خلال 24 ساعة.', "suspended": True})
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
            database.set_web_account_last_ip(int(account["id"]), self._client_ip())
            account["last_ip"] = self._client_ip()
            if int(account.get("is_suspended") or 0):
                self._send_json(423, {"ok": False, "error": 'لاحظنا دخول وخروج متكرر على حسابك. لحماية بياناتك وحسابك تم إيقاف الحساب مؤقتًا، وسيقوم أحد ممثلي خدمة العملاء بالتواصل معك خلال 24 ساعة.', "suspended": True})
                return
            database.record_web_login(int(account["id"]))
            token = web_auth.create_session(int(account["id"]))
            cookie = (
                f"{web_auth.SESSION_COOKIE}={token}; Path=/; Max-Age={web_auth.SESSION_DAYS*24*3600}; "
                "HttpOnly; Secure; SameSite=Lax"
            )
            self._send_json(200, {"ok": True, "data": {"account_id": account["id"]}}, {"Set-Cookie": cookie})
            return

        if parsed.path == "/api/auth/logout":
            token = self._session_token()
            account = web_auth.get_account_from_session(token)
            result = {"suspended": False, "cycles": 0}
            if account:
                result = database.record_web_logout_and_maybe_suspend(int(account["id"]), 3)
            web_auth.logout_session(token)
            cookie = f"{web_auth.SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
            if result.get("suspended"):
                self._send_json(
                    200,
                    {"ok": True, "suspended": True, "message": 'لاحظنا دخول وخروج متكرر على حسابك. لحماية بياناتك وحسابك تم إيقاف الحساب مؤقتًا، وسيقوم أحد ممثلي خدمة العملاء بالتواصل معك خلال 24 ساعة.', "cycles": result.get("cycles", 3)},
                    {"Set-Cookie": cookie},
                )
            else:
                self._send_json(200, {"ok": True, "suspended": False, "cycles": result.get("cycles", 0)}, {"Set-Cookie": cookie})
            return

        if parsed.path == "/api/app/link-code":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            if account.get("telegram_user_id"):
                self._send_json(409, {"ok": False, "error": "Telegram مربوط بالفعل بالحساب"})
                return
            code = web_auth.create_link_code(int(account["id"]))
            self._send_json(200, {"ok": True, "data": {"code": code, "expires_minutes": 10}})
            return


        if parsed.path == "/api/app/golden-answer":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            try:
                question_id = int(payload.get("question_id") or 0)
                chosen_index = int(payload.get("chosen_index"))
            except Exception:
                question_id, chosen_index = 0, -1
            if not question_id or chosen_index < 0:
                self._send_json(400, {"ok": False, "error": "الإجابة غير صالحة"})
                return
            user_id = int(account["user_id"])
            result = database.answer_golden_question(user_id, question_id, chosen_index)
            if not result:
                self._send_json(409, {"ok": False, "error": "الإجابة دي اتسجلت قبل كده"})
                return
            data = dict(result)
            data["ready_for_prize"] = False
            if int(result["answered_count"]) >= int(result["target"]):
                pending = database.get_pending_lucky_spin(user_id)
                if pending:
                    spin_id, prize = int(pending["id"]), float(pending["prize"] or 0)
                else:
                    spin_id, prize, _ = database.create_lucky_spin(user_id, float(result["round_earnings"] or 0))
                data.update({"ready_for_prize": True, "spin_id": spin_id, "prize": float(prize)})
            self._send_json(200, {"ok": True, "data": data})
            return

        if parsed.path == "/api/app/golden-claim":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            try:
                spin_id = int(payload.get("spin_id") or 0)
            except Exception:
                spin_id = 0
            if not spin_id:
                self._send_json(400, {"ok": False, "error": "لفة غير صالحة"})
                return
            user_id = int(account["user_id"])
            prize = database.claim_lucky_spin(spin_id, user_id)
            if prize is None:
                self._send_json(409, {"ok": False, "error": "الجائزة اتستلمت بالفعل أو اللفة غير صالحة"})
                return
            new_balance = database.add_gift_balance(user_id, float(prize))
            self._send_json(200, {"ok": True, "data": {
                "prize": float(prize),
                "gift_balance": float(new_balance or 0),
            }})
            return


        if parsed.path == "/api/app/redeem":
            account = self._auth_web()
            if not account:
                self._send_json(401, {"ok": False, "error": "محتاج تسجل دخول"})
                return
            user_id = int(account["user_id"])
            request = database.create_redemption_request(user_id)
            if not request:
                status = database.get_web_redemption_status(user_id)
                self._send_json(409, {"ok": False, "error": "رصيدك الحالي مفيهوش جنيه كامل قابل للاستبدال", "data": status})
                return
            self._send_json(200, {"ok": True, "data": request})
            return

        # -------- Admin endpoints --------
        admin_id = self._auth_admin()
        if not admin_id:
            self._send_json(403, {"ok": False, "error": "غير مسموح"})
            return

        # -------- Customer communication center --------
        if parsed.path in (
            "/api/admin/customer-message",
            "/api/admin/customer-broadcast",
            "/api/admin/customer-balance",
            "/api/admin/customer-reactivate",
            "/api/admin/welcome-check",
        ):
            filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
            explicit_ids = payload.get("user_ids") if isinstance(payload.get("user_ids"), list) else []
            if explicit_ids:
                filters = dict(filters)
                filters["user_ids"] = explicit_ids
            try:
                selected = database.list_customer_center(filters, 5000, 0)
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "الفلاتر غير صحيحة"})
                return
            customers = list(selected.get("rows") or [])
            if not customers:
                self._send_json(409, {"ok": False, "error": "مفيش عملاء مطابقين للاختيار"})
                return
            if len(customers) > 1 and str(payload.get("confirm") or "") != "CONFIRM":
                self._send_json(409, {"ok": False, "error": f"اكتب CONFIRM لتأكيد الإجراء على {len(customers)} عميل"})
                return

            if parsed.path in ("/api/admin/customer-message", "/api/admin/customer-broadcast", "/api/admin/welcome-check"):
                is_welcome = parsed.path == "/api/admin/welcome-check"
                default_welcome = (
                    "أهلًا {first_name} 👋💚\n\n"
                    "وحشتنا في <b>وفر كاش</b> 🤖\n"
                    "رجعنا لك بعروض أكتر وهدايا أكبر، وفرصة تلف <b>عجلة الهدايا</b> "
                    "وتكسب بطاقات هدايا فورية 🎁🎡\n\n"
                    "اضغط على الزر وابدأ دلوقتي 👇"
                )
                message = str(payload.get("message") or (default_welcome if is_welcome else "")).strip()
                if not message:
                    self._send_json(400, {"ok": False, "error": "اكتب نص الرسالة"})
                    return
                button_text = str(payload.get("button_text") or ("🎡 لف العجلة الآن" if is_welcome else "")).strip() or None
                button_url = str(payload.get("button_url") or getattr(config, "WHEEL_URL", "")).strip() or None
                if bool(button_text) != bool(button_url):
                    self._send_json(400, {"ok": False, "error": "زر الرسالة يحتاج اسم ورابط معًا"})
                    return
                # Known blocked customers never count as targets and never receive retries.
                customers = [x for x in customers if str(x.get("telegram_status") or "unknown") != "blocked"]
                if not customers:
                    self._send_json(409, {"ok": False, "error": "كل العملاء المختارين حاظرين البوت"})
                    return
                campaign_id = database.create_customer_campaign(
                    str(payload.get("name") or ("فحص العملاء القدامى" if is_welcome else "رسالة عملاء")),
                    message, button_text, button_url, filters,
                    [int(x["user_id"]) for x in customers], admin_id,
                )
                database.add_admin_audit(admin_id, "welcome_check" if is_welcome else "broadcast",
                                         "customers", len(customers), {"campaign_id": campaign_id, "filters": filters})
                threading.Thread(
                    target=_run_customer_campaign,
                    args=(campaign_id, customers, message, button_text, button_url, is_welcome),
                    daemon=True,
                    name=f"customer-campaign-{campaign_id}",
                ).start()
                self._send_json(202, {"ok": True, "data": {"campaign_id": campaign_id, "targets": len(customers)}})
                return

            if parsed.path == "/api/admin/customer-balance":
                try:
                    amount = float(payload.get("amount"))
                except (TypeError, ValueError):
                    amount = 0
                if amount == 0:
                    self._send_json(400, {"ok": False, "error": "اكتب قيمة رصيد صحيحة"})
                    return
                reason = str(payload.get("reason") or "تعديل إداري").strip()
                batch_key = str(payload.get("operation_key") or f"admin-{admin_id}-{int(time.time()*1000)}")[:100]
                updated, errors = [], []
                for customer in customers:
                    uid = int(customer["user_id"])
                    try:
                        balance = database.adjust_customer_balance(uid, amount, reason, admin_id, f"{batch_key}:{uid}")
                        updated.append({"user_id": uid, "balance": balance})
                    except Exception as exc:
                        errors.append({"user_id": uid, "error": str(exc)})
                database.add_admin_audit(admin_id, "balance_adjust", "customers", len(updated),
                                         {"amount": amount, "reason": reason, "errors": errors[:20]})
                self._send_json(200, {"ok": True, "data": {"updated": updated, "errors": errors}})
                return

            if parsed.path == "/api/admin/customer-reactivate":
                ids = [int(x["user_id"]) for x in customers]
                count = database.reactivate_customer_users(ids)
                database.add_admin_audit(admin_id, "reactivate", "customers", count, {"user_ids": ids[:100]})
                self._send_json(200, {"ok": True, "data": {"reactivated": count}})
                return

        if parsed.path == "/api/admin/suspend-customer":
            try:
                user_id = int(payload.get("user_id") or 0)
            except Exception:
                user_id = 0
            if not user_id:
                self._send_json(400, {"ok": False, "error": "رقم العميل غير صحيح"})
                return

            account = database.suspend_web_account_by_user_id(user_id, "manual_admin")
            if not account:
                self._send_json(
                    409,
                    {"ok": False, "error": "العميل ده ملوش حساب Web مربوط علشان نوقف تسجيل الدخول"}
                )
                return

            self._send_json(200, {
                "ok": True,
                "data": {
                    "account_id": int(account["id"]),
                    "user_id": int(account["user_id"]),
                    "phone": account.get("phone_e164"),
                    "is_suspended": True,
                },
                "message": "تم إيقاف الحساب ونقله إلى صفحة الموقوفين ✅"
            })
            return

        if parsed.path == "/api/admin/reactivate-customer":
            try:
                account_id = int(payload.get("account_id") or 0)
            except Exception:
                account_id = 0
            if not account_id:
                self._send_json(400, {"ok": False, "error": "رقم الحساب غير صحيح"})
                return

            # Read the suspended account first. Do NOT reactivate yet.
            account = database.get_web_account_by_id(account_id)
            if not account:
                self._send_json(404, {"ok": False, "error": "الحساب غير موجود"})
                return

            raw_phone = str(account.get("phone_e164") or "").strip()
            phone = web_auth.normalize_egypt_phone(raw_phone)
            if not phone:
                self._send_json(400, {"ok": False, "error": "رقم الموبايل المسجل غير صحيح"})
                return

            # Use the exact same OTP sender used by normal customer login.
            otp_ok, otp_err = web_auth.send_otp(phone)
            if not otp_ok:
                # Keep the account suspended so the admin can retry safely.
                self._send_json(502, {
                    "ok": False,
                    "error": "فشل إرسال OTP: " + (otp_err or "سبب غير معروف")
                })
                return

            # Only after Authevo accepts the OTP request do we reactivate.
            account = database.reactivate_web_account(account_id)
            if not account:
                self._send_json(500, {"ok": False, "error": "تم إرسال OTP لكن تعذر إعادة تفعيل الحساب"})
                return

            self._send_json(200, {
                "ok": True,
                "data": {
                    "account_id": account_id,
                    "phone": phone,
                    "otp_sent": True,
                    "message": "تم إعادة تفعيل الحساب وإرسال OTP للعميل ✅"
                },
                "message": "تم إعادة تفعيل الحساب وإرسال OTP للعميل ✅"
            })
            return

        if parsed.path == "/api/admin/send-gift-code":
            try:
                request_id = int(payload.get("request_id"))
            except Exception:
                request_id = 0
            code = str(payload.get("gift_code") or "").strip()
            if not request_id or not code:
                self._send_json(400, {"ok": False, "error": "اكتب كود بطاقة الهدية"})
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
            web_account = database.get_web_account_for_user(int(r["user_id"]))
            telegram_id = int((web_account or {}).get("telegram_user_id") or 0)
            delivery = "web"
            if telegram_id:
                ok, err = _telegram_send_message(
                    telegram_id,
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
                delivery = "telegram+web"
            paid = database.mark_redemption_paid(request_id, admin_id, code)
            result = dict(paid or {})
            result["delivery"] = delivery
            self._send_json(200, {"ok": True, "data": result})
            return
        self._send_json(404, {"ok": False, "error": "Not found"})


def start_admin_server():
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="wafr-web")
    thread.start()
    return server

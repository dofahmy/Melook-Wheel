import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

import requests

import config
import database


SESSION_COOKIE = "wafr_session"
SESSION_DAYS = 30
LINK_MINUTES = 10
DEV_OTP_MINUTES = 5


def _now():
    return datetime.utcnow()


def _hash(value: str) -> str:
    secret = (config.WEB_AUTH_SECRET or config.BOT_TOKEN or "wafr-dev-secret").encode("utf-8")
    return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_egypt_phone(raw: str) -> str | None:
    """Normalize Egyptian mobile numbers to +20XXXXXXXXXX."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("0020"):
        digits = digits[2:]
    if digits.startswith("20") and len(digits) == 12:
        local = "0" + digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        local = digits
    elif len(digits) == 10 and digits.startswith("1"):
        local = "0" + digits
    else:
        return None
    if not re.fullmatch(r"01[0125]\d{8}", local):
        return None
    return "+20" + local[1:]


def _authevo_headers() -> dict:
    return {
        "Authorization": f"Bearer {config.AUTHEVO_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _authevo_error(response, fallback: str) -> str:
    try:
        data = response.json() if response.content else {}
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or fallback)
        if isinstance(err, str) and err.strip():
            return err.strip()
        message = data.get("message")
        if message:
            return str(message)
    except Exception:
        pass
    return fallback


def send_otp(phone_e164: str) -> tuple[bool, str]:
    mode = (config.OTP_DELIVERY_MODE or "authevo").strip().lower()

    if mode == "authevo":
        if not config.AUTHEVO_API_KEY:
            return False, "خدمة كود التحقق لسه مش متوصلة. ضيفي AUTHEVO_API_KEY في Railway."
        try:
            r = requests.post(
                "https://api.authevo.dev/v1/otp/send",
                json={"phone": phone_e164},
                headers=_authevo_headers(),
                timeout=20,
            )
            if 200 <= r.status_code < 300:
                return True, ""
            return False, _authevo_error(r, "تعذر إرسال كود WhatsApp")
        except requests.RequestException:
            return False, "تعذر الاتصال بخدمة كود WhatsApp"

    # Development mode only: code is saved server-side and returned in Railway logs.
    if mode == "dev":
        code = f"{secrets.randbelow(1000000):06d}"
        database.save_dev_otp(phone_e164, _hash(code), (_now() + timedelta(minutes=DEV_OTP_MINUTES)).isoformat())
        print(f"[WAFR DEV OTP] {phone_e164}: {code}")
        return True, ""

    return False, "OTP_DELIVERY_MODE غير صحيح"


def verify_otp(phone_e164: str, code: str) -> tuple[bool, str]:
    code = re.sub(r"\D", "", code or "")
    if len(code) != 6:
        return False, "اكتبي كود التحقق المكوّن من 6 أرقام"

    mode = (config.OTP_DELIVERY_MODE or "authevo").strip().lower()

    if mode == "authevo":
        if not config.AUTHEVO_API_KEY:
            return False, "خدمة كود التحقق لسه مش متوصلة"
        try:
            r = requests.post(
                "https://api.authevo.dev/v1/otp/verify",
                json={"phone": phone_e164, "code": code},
                headers=_authevo_headers(),
                timeout=20,
            )
            data = r.json() if r.content else {}
            verified = bool((data.get("data") or {}).get("verified")) if isinstance(data, dict) else False
            if 200 <= r.status_code < 300 and verified:
                return True, ""
            return False, _authevo_error(r, "الكود غير صحيح أو انتهت صلاحيته")
        except requests.RequestException:
            return False, "تعذر التحقق من الكود"
        except Exception:
            return False, "تعذر التحقق من الكود"

    if mode == "dev":
        row = database.get_dev_otp(phone_e164)
        if not row:
            return False, "الكود غير موجود أو انتهت صلاحيته"
        try:
            if datetime.fromisoformat(row["expires_at"]) < _now():
                return False, "الكود انتهت صلاحيته"
        except Exception:
            return False, "الكود انتهت صلاحيته"
        if not hmac.compare_digest(row["code_hash"], _hash(code)):
            return False, "الكود غير صحيح"
        database.delete_dev_otp(phone_e164)
        return True, ""

    return False, "OTP_DELIVERY_MODE غير صحيح"


def create_session(account_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (_now() + timedelta(days=SESSION_DAYS)).isoformat()
    database.create_web_session(_hash(token), account_id, expires)
    return token


def get_account_from_session(token: str | None):
    if not token:
        return None
    return database.get_web_account_by_session(_hash(token), _now().isoformat())


def logout_session(token: str | None):
    if token:
        database.delete_web_session(_hash(token))


def create_link_code(account_id: int) -> str:
    # Human-friendly 6-character code. Store only a hash.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(secrets.choice(alphabet) for _ in range(6))
    database.create_telegram_link_code(
        account_id,
        _hash(code),
        (_now() + timedelta(minutes=LINK_MINUTES)).isoformat(),
    )
    return code


def consume_link_code(code: str, telegram_user_id: int) -> tuple[bool, str]:
    normalized = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    if len(normalized) != 6:
        return False, "كود الربط غير صحيح"
    return database.consume_telegram_link_code(_hash(normalized), telegram_user_id, _now().isoformat())

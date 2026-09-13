import base64
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


def _twilio_auth_header() -> str:
    raw = f"{config.TWILIO_ACCOUNT_SID}:{config.TWILIO_AUTH_TOKEN}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def send_otp(phone_e164: str) -> tuple[bool, str]:
    mode = (config.OTP_DELIVERY_MODE or "twilio_verify").strip().lower()
    if mode == "twilio_verify":
        if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_VERIFY_SERVICE_SID):
            return False, "خدمة كود التحقق لسه مش متوصلة. ضيفي بيانات Twilio Verify في Railway."
        url = f"https://verify.twilio.com/v2/Services/{config.TWILIO_VERIFY_SERVICE_SID}/Verifications"
        try:
            r = requests.post(
                url,
                data={"To": phone_e164, "Channel": "sms"},
                headers={"Authorization": _twilio_auth_header()},
                timeout=20,
            )
            if 200 <= r.status_code < 300:
                return True, ""
            try:
                msg = r.json().get("message") or "تعذر إرسال الكود"
            except Exception:
                msg = "تعذر إرسال الكود"
            return False, msg
        except Exception:
            return False, "تعذر الاتصال بخدمة كود التحقق"

    # Development mode only: code is saved server-side and returned in Railway logs.
    if mode == "dev":
        code = f"{secrets.randbelow(1000000):06d}"
        database.save_dev_otp(phone_e164, _hash(code), (_now() + timedelta(minutes=DEV_OTP_MINUTES)).isoformat())
        print(f"[WAFR DEV OTP] {phone_e164}: {code}")
        return True, ""

    return False, "OTP_DELIVERY_MODE غير صحيح"


def verify_otp(phone_e164: str, code: str) -> tuple[bool, str]:
    code = re.sub(r"\D", "", code or "")
    if len(code) < 4:
        return False, "كود التحقق غير صحيح"
    mode = (config.OTP_DELIVERY_MODE or "twilio_verify").strip().lower()

    if mode == "twilio_verify":
        if not (config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and config.TWILIO_VERIFY_SERVICE_SID):
            return False, "خدمة كود التحقق لسه مش متوصلة"
        url = f"https://verify.twilio.com/v2/Services/{config.TWILIO_VERIFY_SERVICE_SID}/VerificationCheck"
        try:
            r = requests.post(
                url,
                data={"To": phone_e164, "Code": code},
                headers={"Authorization": _twilio_auth_header()},
                timeout=20,
            )
            data = r.json() if r.content else {}
            if 200 <= r.status_code < 300 and data.get("status") == "approved":
                return True, ""
            return False, "الكود غير صحيح أو انتهت صلاحيته"
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

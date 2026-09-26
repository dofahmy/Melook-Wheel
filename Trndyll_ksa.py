"""
Trendyol Telegram Forwarder — Saudi Arabia

يقرأ بوستات القنوات المصدر، يفك ty.gl، يغيّر رقم الأفلييت في حقول
adjust_adgroup وutm_campaign وlink_userID، ينشئ timestamp جديدًا، يختصر
الرابط بالدومين الخاص بنا، ثم يرسل البوست إلى قنواتنا.

التثبيت:
    pip install telethon requests

متغيرات Railway:
    TRENDYOL_AFFILIATE_ID=236364332
    SHORT_BASE_URL=https://trndyll.com
    SHORT_DB_PATH=/data/trendyol_links.db
    PORT=8080

للاحتفاظ بالروابط بعد كل Deploy، أضيفي Railway Volume على /data.
"""

import asyncio
import os
import re
import secrets
import sqlite3
import string
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import emoji as emoji_lib
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetStickerSetRequest
from telethon.tl.types import (
    InputStickerSetShortName,
    MessageEntityCode,
    MessageEntityCustomEmoji,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ======================== الإعدادات ========================
_API_ID_VALUE = os.getenv("TELEGRAM_API_ID", "").strip()
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "").strip()
SESSION_FILE = os.getenv("TELEGRAM_SESSION_FILE", "forwarder_ksa_session")

if not _API_ID_VALUE or not API_HASH:
    raise RuntimeError(
        "أضيفي TELEGRAM_API_ID و TELEGRAM_API_HASH في Railway Variables"
    )
API_ID = int(_API_ID_VALUE)

# عدّلي القنوات هنا، أو ضعيها في Railway Variables مفصولة بفاصلة.
DEFAULT_SOURCE_CHANNELS = [
    "@OffersSaudiofficial",
]
DEFAULT_DESTINATION_CHANNELS = [
    "@KSAOfferzzz",
]


def _channel_list(env_name, default):
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


SOURCE_CHANNELS = _channel_list("SOURCE_CHANNELS", DEFAULT_SOURCE_CHANNELS)
DESTINATION_CHANNELS = _channel_list(
    "DESTINATION_CHANNELS", DEFAULT_DESTINATION_CHANNELS
)
TRENDYOL_AFFILIATE_ID = os.getenv(
    "TRENDYOL_AFFILIATE_ID", "236364332"
).strip()
SHORT_BASE_URL = os.getenv(
    "SHORT_BASE_URL", "https://trndyll.com"
).strip().rstrip("/")
SHORT_DB_PATH = os.getenv("SHORT_DB_PATH", "trendyol_links.db").strip()
SHORT_CODE_LENGTH = int(os.getenv("SHORT_CODE_LENGTH", "11"))
WEB_PORT = int(os.getenv("PORT", "8080"))
CUSTOM_EMOJI_PACKS = _channel_list(
    "CUSTOM_EMOJI_PACKS",
    ["CrayonsEmoji", "NewsEmoji", "HeartEm"],
)

if not TRENDYOL_AFFILIATE_ID.isdigit():
    raise ValueError("TRENDYOL_AFFILIATE_ID لازم يكون أرقام فقط")
if not SHORT_BASE_URL.startswith("https://"):
    raise ValueError("SHORT_BASE_URL لازم يبدأ بـ https://")

LINK_RE = re.compile(r'https?://[^\s\]\)\[\(< >"\'\uFFFC]+'.replace('< >', '<>'))
TRENDYOL_DOMAINS = ("ty.gl", "trendyol.sa", "trendyol.com")
STANDALONE_OFFE_RE = re.compile(r"(?<!\w)OFFE(?!\w)", re.UNICODE)
WHATSAPP_JOIN_LINE = "📞 للانضمام لقناتنا على واتساب (اضغط هنا)"
OUR_DISCOUNT_CODE = os.getenv("TRENDYOL_DISCOUNT_CODE", "OFFERZK").strip() or "OFFERZK"
TRIGGER_DISCOUNT_CODES = (
    "NOHA15",
    "3VOC15",
    "HANO15",
    "ARWA15",
    "NADA15",
    "AS916",
)
OUTPUT_DISCOUNT_CODES = tuple(
    dict.fromkeys(("3VOC15", "KSA15", "AS916", OUR_DISCOUNT_CODE))
)
KNOWN_DISCOUNT_CODES = tuple(
    dict.fromkeys(TRIGGER_DISCOUNT_CODES + OUTPUT_DISCOUNT_CODES)
)
_TRIGGER_CODES_PATTERN = "|".join(
    sorted(map(re.escape, TRIGGER_DISCOUNT_CODES), key=len, reverse=True)
)
_KNOWN_CODES_PATTERN = "|".join(
    sorted(map(re.escape, KNOWN_DISCOUNT_CODES), key=len, reverse=True)
)
_OUTPUT_CODES_PATTERN = "|".join(
    sorted(map(re.escape, OUTPUT_DISCOUNT_CODES), key=len, reverse=True)
)
TRIGGER_DISCOUNT_CODE_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:{_TRIGGER_CODES_PATTERN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
KNOWN_DISCOUNT_CODE_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:{_KNOWN_CODES_PATTERN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
MONOSPACE_DISCOUNT_CODE_RE = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:{_OUTPUT_CODES_PATTERN})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
DISCOUNT_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_DB_LOCK = threading.Lock()
_TIMESTAMP_LOCK = threading.Lock()
_LAST_TIMESTAMP = 0
_PROCESSED = set()
_CUSTOM_EMOJI_MAP = {}


# ======================== قاعدة الروابط ========================
def _db_connect():
    parent = os.path.dirname(os.path.abspath(SHORT_DB_PATH))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(SHORT_DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS short_links (
            code TEXT PRIMARY KEY,
            target_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            click_count INTEGER NOT NULL DEFAULT 0,
            last_clicked_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS forwarded_posts (
            source_chat_id TEXT NOT NULL,
            source_post_id TEXT NOT NULL,
            destination_channel TEXT NOT NULL,
            destination_message_id INTEGER NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (
                source_chat_id, source_post_id, destination_channel
            )
        )
        """
    )
    conn.commit()
    return conn


def initialize_database():
    with _DB_LOCK:
        conn = _db_connect()
        conn.close()


def save_short_link(code, target_url):
    created_at = datetime.now(timezone.utc).isoformat()
    with _DB_LOCK:
        conn = _db_connect()
        try:
            conn.execute(
                "INSERT INTO short_links(code, target_url, created_at) VALUES(?, ?, ?)",
                (code, target_url, created_at),
            )
            conn.commit()
        finally:
            conn.close()


def get_short_link(code, count_click=False):
    with _DB_LOCK:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT target_url FROM short_links WHERE code = ?", (code,)
            ).fetchone()
            if row and count_click:
                conn.execute(
                    """
                    UPDATE short_links
                    SET click_count = click_count + 1, last_clicked_at = ?
                    WHERE code = ?
                    """,
                    (datetime.now(timezone.utc).isoformat(), code),
                )
                conn.commit()
            return row[0] if row else None
        finally:
            conn.close()


def save_forwarded_post(
    source_chat_id, source_post_id, destination_channel, destination_message_id
):
    """يحفظ الربط بين بوست المصدر والرسالة المقابلة في قناتنا."""
    with _DB_LOCK:
        conn = _db_connect()
        try:
            conn.execute(
                """
                INSERT INTO forwarded_posts(
                    source_chat_id, source_post_id, destination_channel,
                    destination_message_id, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(
                    source_chat_id, source_post_id, destination_channel
                ) DO UPDATE SET
                    destination_message_id = excluded.destination_message_id,
                    updated_at = excluded.updated_at
                """,
                (
                    str(source_chat_id),
                    str(source_post_id),
                    str(destination_channel),
                    int(destination_message_id),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_forwarded_post(source_chat_id, source_post_id, destination_channel):
    """يرجع رقم الرسالة التي سبق إرسالها لنعدّلها بدل تكرارها."""
    with _DB_LOCK:
        conn = _db_connect()
        try:
            row = conn.execute(
                """
                SELECT destination_message_id
                FROM forwarded_posts
                WHERE source_chat_id = ?
                  AND source_post_id = ?
                  AND destination_channel = ?
                """,
                (
                    str(source_chat_id),
                    str(source_post_id),
                    str(destination_channel),
                ),
            ).fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()


# ======================== معالجة Trendyol ========================
def is_trendyol_url(url):
    try:
        host = (urlsplit(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in TRENDYOL_DOMAINS)
    except Exception:
        return False


def next_unique_timestamp():
    global _LAST_TIMESTAMP
    with _TIMESTAMP_LOCK:
        current = int(time.time())
        _LAST_TIMESTAMP = max(current, _LAST_TIMESTAMP + 1)
        return str(_LAST_TIMESTAMP)


def expand_trendyol_url(url):
    """يفك ty.gl ويتأكد أن الوجهة النهائية تابعة لـTrend­yol."""
    host = (urlsplit(url).hostname or "").lower()
    if host != "ty.gl" and not host.endswith(".ty.gl"):
        return url

    last_error = None
    for attempt in range(3):
        try:
            current_url = url
            for _hop in range(6):
                current_parts = urlsplit(current_url)
                current_host = (current_parts.hostname or "").lower()
                if "trendyol." in current_host:
                    return current_url

                current_query = dict(
                    parse_qsl(current_parts.query, keep_blank_values=True)
                )
                adjusted_target = current_query.get("adjust_redirect", "")
                adjusted_host = (
                    urlsplit(adjusted_target).hostname or ""
                ).lower()
                if adjusted_target and "trendyol." in adjusted_host:
                    return adjusted_target

                with requests.get(
                    current_url,
                    allow_redirects=False,
                    timeout=12,
                    stream=True,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/152 Safari/537.36"
                        ),
                        "Accept-Language": "ar-SA,ar;q=0.9,en;q=0.8",
                    },
                ) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location", "")
                        if not location:
                            raise ValueError("تحويل ty.gl لا يحتوي على وجهة")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    current_url = response.url

            final_host = (urlsplit(current_url).hostname or "").lower()
            raise ValueError(
                f"رابط ty.gl لم يصل إلى Trendyol: {final_host}"
            )
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1)
    raise RuntimeError(f"تعذر فك رابط ty.gl: {last_error}")


def normalize_trendyol_destination(url):
    """يحوّل رابط اختيار الدولة إلى رابط Trendyol السعودية الداخلي."""
    parts = urlsplit(url)
    if parts.path.rstrip("/").endswith("/select-country"):
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        callback = query.get("cb", "")
        if callback:
            callback_parts = urlsplit(callback)
            return urlunsplit(
                (
                    "https",
                    "www.trendyol.sa",
                    callback_parts.path,
                    callback_parts.query,
                    callback_parts.fragment,
                )
            )
    return url


def retag_trendyol_url(url):
    """يغيّر حقول التتبع فقط ولا يلمس المنتج أو التاجر أو الحملة."""
    final_url = normalize_trendyol_destination(expand_trendyol_url(url))
    parts = urlsplit(final_url)
    host = (parts.hostname or "").lower()
    if "trendyol." not in host:
        raise ValueError("الرابط النهائي ليس رابط Trendyol")

    replacements = {
        "adjust_adgroup": TRENDYOL_AFFILIATE_ID,
        "utm_campaign": TRENDYOL_AFFILIATE_ID,
        "link_userID": TRENDYOL_AFFILIATE_ID,
        "timestamp": next_unique_timestamp(),
    }
    output_pairs = []
    seen = set()
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in replacements:
            if key not in seen:
                output_pairs.append((key, replacements[key]))
                seen.add(key)
        else:
            output_pairs.append((key, value))

    # نضمن وجود رقمنا في الأماكن الثلاثة حتى إن كان أحدها غير موجود بالمصدر.
    for key in ("adjust_adgroup", "utm_campaign", "link_userID", "timestamp"):
        if key not in seen:
            output_pairs.append((key, replacements[key]))

    return urlunsplit(
        (
            parts.scheme or "https",
            parts.netloc,
            parts.path,
            urlencode(output_pairs, doseq=True),
            parts.fragment,
        )
    )


def create_short_link(long_url):
    """يولّد رابطًا مختلفًا كل مرة، حتى للمنتج نفسه."""
    alphabet = string.ascii_letters + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(SHORT_CODE_LENGTH))
        try:
            save_short_link(code, long_url)
            return f"{SHORT_BASE_URL}/{code}"
        except sqlite3.IntegrityError:
            continue


def convert_one_trendyol_link(original_url):
    """يفك ويعيد وسم ويختصر رابطًا واحدًا داخل thread مستقل."""
    try:
        long_url = retag_trendyol_url(original_url)
        short_url = create_short_link(long_url)
        print(f"   ✅ رابط Trendyol الجديد: {short_url}")
        return short_url, None
    except Exception as exc:
        error = str(exc)
        print(f"   ❌ فشل الرابط: {error[:180]}")
        return original_url, error


async def replace_trendyol_links(text):
    """يحوّل كل روابط البوست بالتوازي مع الحفاظ على ترتيبها في النص."""
    source_text = text or ""
    matches = [
        match
        for match in LINK_RE.finditer(source_text)
        if is_trendyol_url(match.group(0))
    ]
    if not matches:
        return source_text, 0, []

    results = await asyncio.gather(
        *(
            asyncio.to_thread(convert_one_trendyol_link, match.group(0))
            for match in matches
        )
    )

    parts = []
    last_end = 0
    converted = 0
    errors = []
    for match, (replacement, error) in zip(matches, results):
        parts.append(source_text[last_end:match.start()])
        parts.append(replacement)
        last_end = match.end()
        if error:
            errors.append(error)
        else:
            converted += 1
    parts.append(source_text[last_end:])
    return "".join(parts), converted, errors


def normalize_discount_codes(text):
    """يستبدل أي سطر يحوي كودًا مستهدفًا بقائمة أكوادنا الموحدة."""
    changed_lines = 0
    canonical = " - ".join(OUTPUT_DISCOUNT_CODES)
    canonical_set = {code.upper() for code in OUTPUT_DISCOUNT_CODES}
    output_lines = []

    for original_line in (text or "").splitlines(keepends=True):
        line_ending = ""
        line = original_line
        if line.endswith("\r\n"):
            line, line_ending = line[:-2], "\r\n"
        elif line.endswith(("\n", "\r")):
            line, line_ending = line[:-1], line[-1]

        token_set = {token.upper() for token in DISCOUNT_TOKEN_RE.findall(line)}
        if canonical_set.issubset(token_set):
            output_lines.append(line + line_ending)
            continue

        if not TRIGGER_DISCOUNT_CODE_RE.search(line):
            output_lines.append(line + line_ending)
            continue

        # إزالة علامات Markdown القديمة حتى لا تظهر كحروف حول الأكواد.
        plain_line = line.replace("`", "")
        matches = list(KNOWN_DISCOUNT_CODE_RE.finditer(plain_line))
        if not matches:
            output_lines.append(line + line_ending)
            continue

        first, last = matches[0], matches[-1]
        new_line = plain_line[:first.start()] + canonical + plain_line[last.end():]
        output_lines.append(new_line + line_ending)
        changed_lines += 1

    return "".join(output_lines), changed_lines

def clean_post_text(text):
    """يعدّل كلمة OFFE المستقلة ويحذف سطر الانضمام إلى واتساب."""
    lines = (text or "").splitlines()
    kept_lines = [
        line for line in lines if line.strip() != WHATSAPP_JOIN_LINE
    ]
    removed_whatsapp_lines = len(lines) - len(kept_lines)
    cleaned_text = "\n".join(kept_lines)
    cleaned_text, offe_replacements = STANDALONE_OFFE_RE.subn(
        "OFFERZK", cleaned_text
    )
    return cleaned_text, offe_replacements, removed_whatsapp_lines


def _emoji_key(value):
    """يوحّد شكل الإيموجي للمطابقة مع بديله داخل الباكدج."""
    return "".join(
        char
        for char in value
        if char not in ("\ufe0e", "\ufe0f")
        and not ("\U0001f3fb" <= char <= "\U0001f3ff")
    )


def _utf16_length(value):
    """Telegram يحسب مواقع التنسيق بوحدات UTF-16."""
    return len(value.encode("utf-16-le")) // 2


async def load_custom_emoji_packs():
    """يحمّل الإيموجيز المتاحة من الباكدجات المحددة على Telegram."""
    loaded = {}
    for short_name in CUSTOM_EMOJI_PACKS:
        try:
            sticker_set = await client(
                GetStickerSetRequest(
                    InputStickerSetShortName(short_name),
                    hash=0,
                )
            )
            pack_count = 0
            for sticker_pack in getattr(sticker_set, "packs", []):
                key = _emoji_key(sticker_pack.emoticon)
                if not key:
                    continue
                bucket = loaded.setdefault(key, [])
                for document_id in sticker_pack.documents:
                    if document_id not in bucket:
                        bucket.append(document_id)
                        pack_count += 1
            print(
                f"🎨 تم تحميل {pack_count} Custom Emoji من {short_name}"
            )
        except Exception as exc:
            print(
                f"⚠️ تعذر تحميل باكدج {short_name}: {str(exc)[:160]}"
            )
    _CUSTOM_EMOJI_MAP.clear()
    _CUSTOM_EMOJI_MAP.update(loaded)
    print(
        f"🎨 بدائل Custom Emoji الجاهزة: "
        f"{sum(len(items) for items in loaded.values())}"
    )


def build_message_entities(text, include_custom=True):
    """يبني تنسيق كود الخصم وبدائل الـCustom Emoji للنص."""
    source_text = text or ""
    entities = []
    protected_ranges = []

    for match in MONOSPACE_DISCOUNT_CODE_RE.finditer(source_text):
        start, end = match.span()
        protected_ranges.append((start, end))
        entities.append(
            MessageEntityCode(
                offset=_utf16_length(source_text[:start]),
                length=_utf16_length(source_text[start:end]),
            )
        )

    if include_custom and _CUSTOM_EMOJI_MAP:
        usage = {}
        for item in emoji_lib.emoji_list(source_text):
            start = item["match_start"]
            end = item["match_end"]
            if any(start < protected_end and end > protected_start
                   for protected_start, protected_end in protected_ranges):
                continue
            key = _emoji_key(item["emoji"])
            choices = _CUSTOM_EMOJI_MAP.get(key)
            if not choices:
                continue
            choice_index = usage.get(key, 0) % len(choices)
            usage[key] = usage.get(key, 0) + 1
            entities.append(
                MessageEntityCustomEmoji(
                    offset=_utf16_length(source_text[:start]),
                    length=_utf16_length(source_text[start:end]),
                    document_id=choices[choice_index],
                )
            )

    entities.sort(key=lambda entity: entity.offset)
    return entities


# ======================== سيرفر الاختصار ========================
class ShortLinkHandler(BaseHTTPRequestHandler):
    def respond(self, include_body=True):
        path = urlsplit(self.path).path.strip("/")
        if path in ("", "health"):
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)
            return

        target_url = get_short_link(path, count_click=True)
        if not target_url:
            body = "الرابط غير موجود أو انتهت صلاحيته".encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if include_body:
                self.wfile.write(body)
            return

        self.send_response(302)
        self.send_header("Location", target_url)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.respond(include_body=True)

    def do_HEAD(self):
        self.respond(include_body=False)

    def log_message(self, _format, *_args):
        return


def start_short_link_server():
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), ShortLinkHandler)
    thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="trendyol-short-link-server",
    )
    thread.start()
    print(f"🔗 خدمة الاختصار شغالة: {SHORT_BASE_URL} — port {WEB_PORT}")
    return server


# ======================== Telegram Forwarder ========================
if TELEGRAM_STRING_SESSION:
    client = TelegramClient(
        StringSession(TELEGRAM_STRING_SESSION), API_ID, API_HASH,
        connection_retries=None, retry_delay=5, auto_reconnect=True,
        request_retries=5, timeout=30,
    )
else:
    client = TelegramClient(
        SESSION_FILE, API_ID, API_HASH,
        connection_retries=None, retry_delay=5, auto_reconnect=True,
        request_retries=5, timeout=30,
    )


async def send_modified_post(channel, messages, new_text):
    """يرسل كل ميديا البوست كألبوم واحد، أو يرسل النص فقط."""
    media_items = [message.media for message in messages if message.media]
    if media_items:
        caption = new_text[:1024]
        files = media_items if len(media_items) > 1 else media_items[0]

        def album_payload(include_custom):
            entities = build_message_entities(caption, include_custom)
            if len(media_items) == 1:
                return caption, entities
            return (
                [caption] + [""] * (len(media_items) - 1),
                [entities] + [[] for _ in media_items[1:]],
            )

        try:
            captions, formatting_entities = album_payload(True)
            return await client.send_file(
                channel,
                files,
                caption=captions,
                formatting_entities=formatting_entities,
                force_document=False,
            )
        except Exception as exc:
            if _CUSTOM_EMOJI_MAP:
                print(
                    "   ⚠️ تعذر Custom Emoji؛ إعادة المحاولة "
                    f"بالإيموجي العادي: {str(exc)[:100]}"
                )
                try:
                    captions, formatting_entities = album_payload(False)
                    return await client.send_file(
                        channel,
                        files,
                        caption=captions,
                        formatting_entities=formatting_entities,
                        force_document=False,
                    )
                except Exception as fallback_exc:
                    print(
                        "   ⚠️ تعذر إرسال الميديا مباشرة: "
                        f"{str(fallback_exc)[:100]}"
                    )
            else:
                print(
                    f"   ⚠️ تعذر إرسال الميديا مباشرة: {str(exc)[:100]}"
                )

    try:
        return await client.send_message(
            channel,
            new_text,
            formatting_entities=build_message_entities(new_text, True),
            link_preview=False,
        )
    except Exception:
        if not _CUSTOM_EMOJI_MAP:
            raise
        return await client.send_message(
            channel,
            new_text,
            formatting_entities=build_message_entities(new_text, False),
            link_preview=False,
        )


def sent_message_id(sent_result):
    """يستخرج رقم أول رسالة؛ وهي صاحبة النص في الألبوم."""
    if isinstance(sent_result, (list, tuple)):
        return sent_result[0].id
    return sent_result.id


async def edit_modified_post(channel, destination_message_id, messages, new_text):
    """يعدّل النص أو وصف الميديا في نفس الرسالة المرسلة سابقًا."""
    has_media = any(message.media for message in messages)
    editable_text = new_text[:1024] if has_media else new_text
    try:
        return await client.edit_message(
            channel,
            destination_message_id,
            editable_text,
            formatting_entities=build_message_entities(editable_text, True),
            link_preview=False,
        )
    except Exception:
        if not _CUSTOM_EMOJI_MAP:
            raise
        return await client.edit_message(
            channel,
            destination_message_id,
            editable_text,
            formatting_entities=build_message_entities(editable_text, False),
            link_preview=False,
        )


async def process_post(event, messages, post_id, is_edit=False):
    """يعالج رسالة منفردة أو ألبومًا كاملًا كوحدة واحدة."""
    text = next(
        (message.message for message in messages if message.message),
        "",
    )

    if not is_edit:
        # رقم رسالة المصدر وحده هو الهوية لمنع تكرار الإرسال الأول.
        message_key = (event.chat_id, str(post_id))
        if message_key in _PROCESSED:
            return
        _PROCESSED.add(message_key)
        if len(_PROCESSED) > 5000:
            _PROCESSED.pop()

    trendyol_urls = [url for url in LINK_RE.findall(text) if is_trendyol_url(url)]
    if not trendyol_urls:
        print("⏭️ البوست مفيهوش رابط Trendyol — اتخطّى")
        return

    try:
        chat = await event.get_chat()
        source_name = (
            f"@{chat.username}" if getattr(chat, "username", None)
            else str(event.chat_id)
        )
    except Exception:
        source_name = str(event.chat_id)

    print(f"\n📩 بوست Trendyol من {source_name} — {len(trendyol_urls)} رابط")
    new_text, converted, errors = await replace_trendyol_links(text)
    new_text, offe_replacements, removed_whatsapp_lines = clean_post_text(
        new_text
    )
    new_text, discount_code_replacements = normalize_discount_codes(new_text)

    if discount_code_replacements:
        print(
            "   ✅ تم توحيد أكواد الخصم إلى: "
            f"{' - '.join(OUTPUT_DISCOUNT_CODES)} "
            f"({discount_code_replacements} سطر)"
        )
    if offe_replacements:
        print(f"   ✅ تم تغيير OFFE إلى OFFERZK ({offe_replacements} مرة)")
    if removed_whatsapp_lines:
        print(f"   ✅ تم حذف سطر واتساب ({removed_whatsapp_lines} مرة)")
    discount_code_count = len(MONOSPACE_DISCOUNT_CODE_RE.findall(new_text))
    if discount_code_count:
        print(f"   ✅ تم تنسيق كود الخصم monospace ({discount_code_count} مرة)")

    # لا ننشر لو فشل أي رابط، حتى لا يخرج Affiliate ID لشخص آخر.
    if errors or converted != len(trendyol_urls):
        print("   ⛔ لم يتم إرسال البوست لأن رابطًا واحدًا على الأقل فشل")
        return

    for channel in DESTINATION_CHANNELS:
        try:
            if is_edit:
                destination_message_id = get_forwarded_post(
                    event.chat_id, post_id, channel
                )
                if destination_message_id is None:
                    print(
                        f"   ⚠️ لم يُعدّل على {channel}: "
                        "الرسالة الأصلية غير مسجلة"
                    )
                    continue
                await edit_modified_post(
                    channel, destination_message_id, messages, new_text
                )
                print(f"   ✅ اتعدّل نفس البوست على {channel}")
            else:
                sent_result = await send_modified_post(
                    channel, messages, new_text
                )
                destination_message_id = sent_message_id(sent_result)
                save_forwarded_post(
                    event.chat_id,
                    post_id,
                    channel,
                    destination_message_id,
                )
                print(f"   ✅ اتبعت على {channel}")
        except Exception as exc:
            action = "التعديل" if is_edit else "الإرسال"
            print(f"   ❌ فشل {action} على {channel}: {str(exc)[:180]}")


async def handle_post(event):
    message = event.message
    # عناصر الألبوم تصل أيضًا كرسائل منفردة؛ نتركها لـ handle_album
    # حتى لا تُرسل صورة واحدة أو يتكرر البوست.
    if message.grouped_id:
        return
    await process_post(event, [message], message.id)


async def handle_album(event):
    messages = list(event.messages)
    if not messages:
        return
    grouped_id = getattr(event, "grouped_id", None) or messages[0].grouped_id
    await process_post(event, messages, f"album:{grouped_id}")


async def handle_edited_post(event):
    message = event.message
    post_id = (
        f"album:{message.grouped_id}"
        if message.grouped_id
        else message.id
    )
    await process_post(event, [message], post_id, is_edit=True)


async def run_forwarder():
    client.add_event_handler(handle_post, events.NewMessage(chats=SOURCE_CHANNELS))
    client.add_event_handler(handle_album, events.Album(chats=SOURCE_CHANNELS))
    client.add_event_handler(
        handle_edited_post,
        events.MessageEdited(chats=SOURCE_CHANNELS),
    )
    if not TELEGRAM_STRING_SESSION:
        raise RuntimeError(
            "TELEGRAM_STRING_SESSION غير موجود في Railway Variables. "
            "شغّلي generate_telegram_session.py على جهازك وضعي الناتج كمتغير سرّي."
        )
    await client.start()
    await load_custom_emoji_packs()
    print("=" * 58)
    print("🟠 Trendyol Forwarder شغال")
    print(f"رقم الأفلييت: {TRENDYOL_AFFILIATE_ID}")
    print(f"المصادر: {', '.join(SOURCE_CHANNELS)}")
    print(f"قنوات الإرسال: {', '.join(DESTINATION_CHANNELS)}")
    print("=" * 58)
    await client.run_until_disconnected()


if __name__ == "__main__":
    initialize_database()
    start_short_link_server()
    try:
        asyncio.run(run_forwarder())
    except KeyboardInterrupt:
        print("\n👋 تم إيقاف البرنامج")

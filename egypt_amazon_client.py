"""
عميل الاتصال بواجهة أمازون مصر (Creators API) - نفس البيانات والطريقة
المُثبتة والشغالة فعليًا في channel_forwarder_eg.py بتاعتك.

مستخدم في عجلة العروض الذهبية بس: بناخد ASIN من لينك في القناة الذهبية،
ونجيب بيه اسم المنتج، السعر الحالي، السعر قبل الخصم، ونسبة الخصم - عشان
نبني منها سؤال الاختيار من متعدد.
"""
import logging
import socket as _sock
import time
from dataclasses import dataclass

import requests

import config

logger = logging.getLogger(__name__)

MARKETPLACE = "www.amazon.eg"
API_HOST = "creatorsapi.amazon"
API_BASE = f"https://{API_HOST}/catalog/v1"
TOKEN_URL = "https://api.amazon.com/auth/o2/token"

# ---------- حل مشكلة الـ DNS بتاعة نطاق creatorsapi.amazon (نفس طريقة ملفك) ----------
_FALLBACK_IPS = ["108.159.120.21", "108.159.120.6", "108.159.120.71", "108.159.120.45"]


def _resolve_creatorsapi_ips() -> list[str]:
    try:
        import dns.resolver
        r = dns.resolver.Resolver(configure=False)
        r.nameservers = ["8.8.8.8", "1.1.1.1"]
        return [x.address for x in r.resolve(API_HOST, "A")]
    except Exception:
        return _FALLBACK_IPS


_orig_getaddrinfo = _sock.getaddrinfo


def _patched_getaddrinfo(host, *args, **kwargs):
    if host == API_HOST:
        results = []
        for ip in _resolve_creatorsapi_ips():
            try:
                results.extend(_orig_getaddrinfo(ip, *args, **kwargs))
            except Exception:
                pass
        if results:
            return results
    return _orig_getaddrinfo(host, *args, **kwargs)


_sock.getaddrinfo = _patched_getaddrinfo


@dataclass
class Product:
    asin: str
    title: str
    price: float
    old_price: float | None
    discount_percent: float
    image_url: str | None = None


_access_token: str | None = None
_token_fetched_at: float = 0.0
_TOKEN_TTL_SECONDS = 55 * 60


def _get_token(force: bool = False) -> str | None:
    global _access_token, _token_fetched_at
    if not force and _access_token and (time.time() - _token_fetched_at) < _TOKEN_TTL_SECONDS:
        return _access_token
    try:
        r = requests.post(
            TOKEN_URL,
            headers={"Content-Type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "client_id": config.EGYPT_AMAZON_CLIENT_ID,
                "client_secret": config.EGYPT_AMAZON_CLIENT_SECRET,
                "scope": "creatorsapi::default",
            },
            timeout=15,
        )
        _access_token = r.json().get("access_token")
        _token_fetched_at = time.time()
        return _access_token
    except Exception as exc:
        logger.error("فشل الحصول على توكن أمازون مصر: %s", exc)
        return None


def _is_new_listing(listing: dict) -> bool:
    c = listing.get("condition")
    if isinstance(c, dict):
        c = c.get("value") or c.get("displayValue") or ""
    c = str(c or "").strip().lower()
    return (not c) or c == "new" or "جديد" in c


def _is_resale_listing(listing: dict) -> bool:
    mn = ((listing.get("merchantInfo", {}) or {}).get("name") or "").strip().lower()
    return "resale" in mn or "ريسيل" in mn


def _is_subscribe_listing(listing: dict) -> bool:
    t = listing.get("type")
    if isinstance(t, dict):
        t = t.get("value") or ""
    return "subscribe" in str(t or "").lower()


def get_product(asin: str) -> Product | None:
    """بتجيب اسم المنتج والسعر والخصم من أمازون مصر. None لو فشلت."""
    token = _get_token()
    if not token:
        return None

    payload = {
        "itemIds": [asin],
        "itemIdType": "ASIN",
        "partnerTag": config.EGYPT_AMAZON_PARTNER_TAG,
        "partnerType": "Associates",
        "marketplace": MARKETPLACE,
        "languagesOfPreference": ["ar_AE"],
        "resources": [
            "itemInfo.title",
            "offersV2.listings.price",
            "offersV2.listings.availability",
            "offersV2.listings.merchantInfo",
            "offersV2.listings.type",
            "offersV2.listings.condition",
            "images.primary.large",
            "images.primary.highRes",
        ],
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                f"{API_BASE}/getItems",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "x-marketplace": MARKETPLACE,
                },
                json=payload,
                timeout=25,
            )
            if resp.status_code == 401:
                token = _get_token(force=True)
                if not token:
                    return None
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2)
                continue
            resp.raise_for_status()
            items = resp.json().get("itemsResult", {}).get("items", [])
            if not items:
                return None
            item = items[0]
            title = item.get("itemInfo", {}).get("title", {}).get("displayValue", "")

            listings = item.get("offersV2", {}).get("listings") or []
            priced = []
            for listing in listings:
                price = listing.get("price", {}).get("money", {}).get("amount")
                if price is None:
                    continue
                if not _is_new_listing(listing) or _is_resale_listing(listing) or _is_subscribe_listing(listing):
                    continue
                priced.append((listing, price))
            if not priced:
                return None
            priced.sort(key=lambda x: x[1])
            listing = priced[0][0]

            price_block = listing.get("price", {})
            cur = price_block.get("money", {}).get("amount")
            orig = price_block.get("savingBasis", {}).get("money", {}).get("amount")
            disc = price_block.get("savings", {}).get("percentage")
            if not disc and orig and cur and orig > cur:
                disc = round((orig - cur) / orig * 100)
            if not cur:
                return None

            primary_image = item.get("images", {}).get("primary", {}) or {}
            image_url = (primary_image.get("highRes") or primary_image.get("large") or {}).get("url")

            return Product(
                asin=asin,
                title=title or "منتج بدون اسم",
                price=float(cur),
                old_price=float(orig) if orig else None,
                discount_percent=float(disc) if disc else 0.0,
                image_url=image_url,
            )
        except Exception as exc:
            logger.error("فشل جلب بيانات المنتج %s: %s", asin, exc)
            time.sleep(1)
    return None

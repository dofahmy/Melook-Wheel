"""Standalone Telegram pilot for an Amazon Egypt shopping assistant.

Run with:
    SHOPPING_TEST_BOT_TOKEN=... AMAZON_CLIENT_ID=... AMAZON_CLIENT_SECRET=... \
    AMAZON_PARTNER_TAG=... python shopping_assistant_pilot.py

This file deliberately does not import or modify the production bot, its database,
wheel, rewards, or customer records.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_TOKEN = os.getenv("SHOPPING_TEST_BOT_TOKEN", "").strip()
AMAZON_CLIENT_ID = os.getenv("AMAZON_CLIENT_ID", os.getenv("CLIENT_ID", "")).strip()
AMAZON_CLIENT_SECRET = os.getenv(
    "AMAZON_CLIENT_SECRET", os.getenv("CLIENT_SECRET", "")
).strip()
AMAZON_PARTNER_TAG = os.getenv(
    "AMAZON_PARTNER_TAG",
    os.getenv("PARTNER_TAG", os.getenv("AMAZON_ASSOCIATE_TAG", "")),
).strip()
ASSOCIATE_TAG = os.getenv("AMAZON_ASSOCIATE_TAG", AMAZON_PARTNER_TAG).strip()
AMAZON_MARKETPLACE = os.getenv(
    "AMAZON_MARKETPLACE", os.getenv("MARKETPLACE", "www.amazon.eg")
).strip()
CATALOG_PATH = Path(os.getenv("SHOPPING_PRODUCTS_FILE", "amazon_egypt_asin_catalog.json"))
DB_PATH = Path(os.getenv("SHOPPING_PILOT_DB", "shopping_pilot.db"))
RESULT_LIMIT = 3
BUDGET_TOLERANCE = 0.05
AMAZON_MAX_CANDIDATES = int(os.getenv("AMAZON_MAX_CANDIDATES", "50"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shopping-pilot")
# httpx logs Telegram Bot API URLs at INFO level; those URLs contain the bot
# token. Keep request details out of Railway logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

_AMAZON_TOKEN = ""
_AMAZON_TOKEN_EXPIRES_AT = 0.0
_AMAZON_TOKEN_LOCK = threading.Lock()
_AMAZON_API_LOCK = threading.Lock()
_AMAZON_LAST_REQUEST_AT = 0.0
_AMAZON_RESULT_CACHE: dict[str, tuple[float, Product | None]] = {}
AMAZON_MIN_REQUEST_INTERVAL = float(os.getenv("AMAZON_MIN_REQUEST_INTERVAL", "1.1"))
AMAZON_RESULT_CACHE_SECONDS = int(os.getenv("AMAZON_RESULT_CACHE_SECONDS", "900"))


@dataclass(frozen=True)
class Product:
    asin: str
    title: str
    brand: str
    category: str
    price: float
    old_price: float | None
    rating: float | None
    reviews: int
    discount: float
    image_url: str | None
    aliases: str = ""


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _money_field(value) -> float:
    if isinstance(value, dict):
        return _number(value.get("amount"))
    return _number(value)


def load_catalog() -> tuple[set[str], dict[str, list[str]], list[Product]]:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    cached_links: dict[str, list[str]] = {}
    # The enriched catalog stores stable searchable metadata only. Prices are
    # deliberately refreshed from the product page when the customer searches.
    if isinstance(raw, dict) and isinstance(raw.get("products"), list):
        allowed: set[str] = set()
        products: list[Product] = []
        for row in raw["products"]:
            asin = str(row.get("asin") or "").strip().upper()
            if re.fullmatch(r"[A-Z0-9]{10}", asin):
                allowed.add(asin)
                cached_links[asin] = [
                    link for link in (row.get("cachedLinks") or [])
                    if isinstance(link, str) and link.startswith(("https://", "http://"))
                ]
                title = str(row.get("displayTitle") or row.get("title") or "").strip()
                if title:
                    aliases = " ".join(
                        value for value in (row.get("aliases") or [])
                        if isinstance(value, str)
                    )
                    products.append(Product(
                        asin=asin,
                        title=title,
                        brand=str(row.get("brand") or row.get("asinBrand") or "").strip(),
                        category=(str(row.get("category") or "") + " " + aliases).strip(),
                        price=0.0,
                        old_price=None,
                        rating=None,
                        reviews=0,
                        discount=0.0,
                        image_url=row.get("imageUrl") or row.get("image_url"),
                        aliases=aliases,
                    ))
        if not allowed:
            raise RuntimeError(f"No ASINs found in {CATALOG_PATH}")
        return allowed, cached_links, products
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), [])
    products: list[Product] = []
    for row in raw:
        asin = str(row.get("asin") or "").strip()
        title = str(row.get("displayTitle") or row.get("title") or "").strip()
        price = _money_field(row.get("buyingPrice") or row.get("price"))
        if not asin or not title:
            continue
        old_price = _money_field(row.get("listPrice") or row.get("oldPrice")) or None
        deal = row.get("dealMetadata") or {}
        discount = _number(
            row.get("discountPercentage")
            or row.get("discount")
            or deal.get("discountPercentage")
        )
        if not discount and old_price and old_price > price:
            discount = round((old_price - price) * 100 / old_price, 1)
        products.append(
            Product(
                asin=asin,
                title=title,
                brand=str(row.get("asinBrand") or row.get("brand") or "").strip(),
                category=str(row.get("category") or "").strip(),
                price=price,
                old_price=old_price,
                rating=_number(row.get("numberOfReviewStars") or row.get("rating")) or None,
                reviews=int(_number(row.get("reviewCount"))),
                discount=discount,
                image_url=row.get("imageUrl") or row.get("image_url"),
                aliases=" ".join(
                    value for value in (row.get("aliases") or [])
                    if isinstance(value, str)
                ),
            )
        )
    if not products:
        raise RuntimeError(f"No usable products found in {CATALOG_PATH}")
    return {p.asin for p in products}, cached_links, products


ALLOWED_ASINS, CACHED_LINKS, PRODUCTS = load_catalog()
BY_ASIN = {p.asin: p for p in PRODUCTS}


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                budget REAL,
                result_asins TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                asin TEXT,
                query TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                telegram_id INTEGER NOT NULL,
                asin TEXT NOT NULL,
                saved_price REAL NOT NULL,
                saved_at INTEGER NOT NULL,
                PRIMARY KEY (telegram_id, asin)
            );
            """
        )


ARABIC_STOPWORDS = {
    "عايز", "عايزه", "عايزة", "محتاج", "محتاجه", "محتاجة", "منتج", "سعر", "في", "من", "الى",
    "حدود", "حوالي", "جنيه", "ج", "لي", "لو", "هات", "وريني", "افضل", "احسن", "أحسن",
}

# Common Egyptian shopping expressions that may not appear literally in the
# Amazon Arabic title.  Each phrase is treated as an alternative query, not as
# extra mandatory words.
QUERY_ALIASES = {
    "اير فراير": ("قلايه هوائيه", "قلايه بدون زيت"),
    "air fryer": ("قلايه هوائيه", "قلايه بدون زيت"),
    "ميكروويف": ("ميكرووييف", "مايكروويف", "فرن ميكروويف"),
    "ميكرووييف": ("ميكروويف", "مايكروويف", "فرن ميكروويف"),
    "مايكروويف": ("ميكروويف", "ميكرووييف", "فرن ميكروويف"),
    "مكنسه روبوت": ("مكنسه كهربائيه روبوتيه", "روبوت تنظيف"),
    "وايرلس": ("لاسلكي",),
    "هيدفون": ("سماعه راس",),
    "كوتشي": ("حذاء رياضي", "سنيكر"),
    "جزمه": ("حذاء",),
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[إأآٱ]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def parse_request(text: str) -> tuple[str, float | None]:
    normalized = normalize(text)
    numbers = []
    budget_spans: list[tuple[int, int]] = []
    for match in re.finditer(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(الف|الاف|k|جنيه|ج)?", normalized):
        value = float(match.group(1).replace(",", "."))
        suffix = match.group(2) or ""
        if suffix in {"الف", "الاف", "k"}:
            value *= 1000
        if value >= 50:
            numbers.append(value)
            budget_spans.append(match.span())
    budget = max(numbers) if numbers else None
    # Remove only the number interpreted as a budget. Small numbers such as
    # "مقاس 3" or a model number remain part of the product request.
    chars = list(normalized)
    for start, end in budget_spans:
        chars[start:end] = " " * (end - start)
    query = "".join(chars)
    words = [w for w in query.split() if (len(w) > 1 or w.isdigit()) and w not in ARABIC_STOPWORDS]
    return " ".join(words), budget


def _query_variants(query: str) -> list[set[str]]:
    normalized = normalize(query)
    variants = [{w for w in normalized.split() if w}]
    for phrase, aliases in QUERY_ALIASES.items():
        normalized_phrase = normalize(phrase)
        if normalized_phrase in normalized:
            # Replace only the colloquial phrase and preserve the rest of the
            # customer's constraints (brand, gender, size, etc.).
            for alias in aliases:
                expanded = normalized.replace(normalized_phrase, normalize(alias))
                variants.append({w for w in expanded.split() if w})
    return [variant for variant in variants if variant]


def _variant_score(query_words: set[str], tokens: set[str]) -> tuple[float, float]:
    """Return (score, coverage) using whole-token matching only.

    This intentionally prevents short words such as "اير" from matching the
    middle of an unrelated word such as "ستاير".
    """
    matched = 0
    score = 0.0
    for word in query_words:
        if word in tokens:
            matched += 1
            score += 6
            continue
        # Limited stemming tolerance for Arabic suffix/plural differences.
        if len(word) >= 5 and any(len(tok) >= 5 and tok[:5] == word[:5] for tok in tokens):
            matched += 1
            score += 2
    return score, matched / max(len(query_words), 1)


def _text_score(query: str, product: Product) -> tuple[float, float]:
    tokens = set(normalize(f"{product.title} {product.brand} {product.aliases}").split())
    return max((_variant_score(words, tokens) for words in _query_variants(query)), default=(0.0, 0.0))


def _title_score(query: str, product: Product) -> tuple[float, float]:
    """Score only the canonical/live title and brand, excluding aliases."""
    tokens = set(normalize(f"{product.title} {product.brand}").split())
    return max((_variant_score(words, tokens) for words in _query_variants(query)), default=(0.0, 0.0))


def _search_local_products(query: str, budget: float | None, limit: int = RESULT_LIMIT) -> list[Product]:
    title_exact: list[tuple[float, Product]] = []
    alias_exact: list[tuple[float, Product]] = []
    partial: list[tuple[float, Product]] = []
    for p in PRODUCTS:
        relevance, coverage = _text_score(query, p)
        if relevance <= 0 or coverage < 0.5:
            continue
        # A stated budget is a real customer constraint. Never fill empty
        # results with unrelated or noticeably over-budget products.
        if budget and p.price > 0 and p.price > budget * (1 + BUDGET_TOLERANCE):
            continue
        budget_score = 0.0
        if budget and p.price > 0:
            if p.price <= budget:
                budget_score = 3 + min(p.price / budget, 1)
            else:
                budget_score = -2
        quality = min((p.rating or 0) / 5, 1) + min(p.reviews / 1000, 1)
        saving = min(p.discount / 20, 2)
        ranked_item = (relevance * 10 + budget_score + quality + saving, p)
        _, title_coverage = _title_score(query, p)
        if title_coverage == 1.0:
            title_exact.append(ranked_item)
        elif coverage == 1.0:
            alias_exact.append(ranked_item)
        else:
            partial.append(ranked_item)
    sort_key = lambda item: (item[0], item[1].rating or 0, item[1].reviews)
    title_exact.sort(key=sort_key, reverse=True)
    alias_exact.sort(key=sort_key, reverse=True)
    partial.sort(key=sort_key, reverse=True)
    # AND search first: every requested word must occur in the canonical title
    # or an alias. Canonical-title matches outrank alias-only matches. Partial
    # matching is only a last fallback when exact candidates do not fill limit.
    ranked = title_exact + alias_exact + partial
    return [p for _, p in ranked[:limit]]


def _western_digits(text: str) -> str:
    return text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬", "0123456789.,"))


def _parse_price_text(text: str | None) -> float:
    if not text:
        return 0.0
    cleaned = _western_digits(text).replace("\u200f", "").replace("\xa0", " ")
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)", cleaned)
    return _number(match.group(1).replace(",", "")) if match else 0.0


def _parse_first_number(text: str | None) -> float:
    if not text:
        return 0.0
    match = re.search(r"\d+(?:[.,]\d+)?", _western_digits(text))
    return _number(match.group(0).replace(",", ".")) if match else 0.0


UNAVAILABLE_MARKERS = tuple(normalize(value) for value in (
    "غير متوفر حالياً",
    "غير متوفر حاليا",
    "غير متاح حالياً",
    "غير متاح حاليا",
    "نفد من المخزون",
    "غير متاح من هؤلاء البائعين",
    "Currently unavailable",
    "Temporarily out of stock",
    "Out of stock",
    "Unavailable",
))

AVAILABLE_MARKERS = tuple(normalize(value) for value in (
    "متوفر",
    "متاح",
    "متبقي في المخزون",
    "In stock",
    "Available",
))


def _page_is_buyable(soup: BeautifulSoup) -> bool:
    """Return True only when Amazon currently offers this exact ASIN for sale.

    A historical catalog title or even a visible price is not sufficient: an
    unavailable variation can retain both. Requiring a live purchase control
    prevents stale size/colour variants from being sent to customers.
    """
    availability_node = (
        soup.select_one("#availability")
        or soup.select_one("#outOfStock")
        or soup.select_one("#availability_feature_div")
    )
    availability_text = normalize(
        availability_node.get_text(" ", strip=True) if availability_node else ""
    )
    if availability_text and any(marker in availability_text for marker in UNAVAILABLE_MARKERS):
        return False

    purchase_control = (
        soup.select_one("#add-to-cart-button")
        or soup.select_one("#buy-now-button")
        or soup.select_one("#addToCart input[name='submit.add-to-cart']")
        or soup.select_one("input[name='submit.add-to-cart']")
        or soup.select_one("input[name='submit.buy-now']")
        or soup.select_one("form#addToCart")
        or soup.select_one("#newAccordionRow_1")
    )
    if purchase_control is not None:
        return True

    # Amazon sometimes omits purchase controls from the lightweight/mobile
    # HTML returned to Railway, while still returning a positive stock state.
    return bool(
        availability_text
        and any(marker in availability_text for marker in AVAILABLE_MARKERS)
    )


def _amazon_search_page(query: str, page: int) -> list[Product]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
    }
    response = requests.get(
        "https://www.amazon.eg/s",
        params={"k": query, "page": page},
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    if "captcha" in response.text.lower() or "أدخل الأحرف" in response.text:
        raise RuntimeError("Amazon طلب تحققًا مؤقتًا من خدمة البحث")
    soup = BeautifulSoup(response.text, "html.parser")
    products: list[Product] = []
    for card in soup.select('[data-component-type="s-search-result"][data-asin]'):
        asin = str(card.get("data-asin") or "").strip().upper()
        if asin not in ALLOWED_ASINS:
            continue
        title_node = card.select_one("h2 span") or card.select_one(".a-text-normal")
        price_node = card.select_one(".a-price .a-offscreen")
        if not title_node or not price_node:
            continue
        price = _parse_price_text(price_node.get_text(" ", strip=True))
        if price <= 0:
            continue
        old_node = card.select_one(".a-text-price .a-offscreen")
        rating_node = card.select_one(".a-icon-alt")
        review_node = card.select_one('[aria-label$="ratings"]') or card.select_one(".s-underline-text")
        image_node = card.select_one("img.s-image")
        old_price = _parse_price_text(old_node.get_text(" ", strip=True)) if old_node else 0.0
        discount = 0.0
        if old_price > price:
            discount = round((old_price - price) * 100 / old_price, 1)
        products.append(Product(
            asin=asin,
            title=title_node.get_text(" ", strip=True),
            brand="",
            category="",
            price=price,
            old_price=old_price or None,
            rating=(
                _parse_first_number(rating_node.get_text(" ", strip=True)) or None
                if rating_node else None
            ),
            reviews=int(_parse_first_number(review_node.get_text(" ", strip=True))) if review_node else 0,
            discount=discount,
            image_url=image_node.get("src") if image_node else None,
        ))
    return products


def _amazon_product_page(seed: Product) -> Product | None:
    """Verify availability and refresh volatile fields for one exact ASIN."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
    }
    response = requests.get(
        f"https://www.amazon.eg/dp/{seed.asin}", headers=headers, timeout=15
    )
    response.raise_for_status()
    if "captcha" in response.text.lower() or "أدخل الأحرف" in response.text:
        raise RuntimeError("Amazon طلب تحققًا مؤقتًا")
    soup = BeautifulSoup(response.text, "html.parser")
    if not _page_is_buyable(soup):
        logger.info("Amazon reports ASIN %s unavailable or not buyable", seed.asin)
        return None
    price_node = (
        soup.select_one("#corePriceDisplay_desktop_feature_div .a-price .a-offscreen")
        or soup.select_one("#corePrice_feature_div .a-price .a-offscreen")
        or soup.select_one("#priceblock_ourprice")
        or soup.select_one(".a-price .a-offscreen")
    )
    price = _parse_price_text(price_node.get_text(" ", strip=True)) if price_node else 0.0
    if price <= 0:
        return None
    title_node = soup.select_one("#productTitle")
    old_node = soup.select_one(".basisPrice .a-offscreen") or soup.select_one(".a-text-price .a-offscreen")
    rating_node = soup.select_one("#acrPopover") or soup.select_one(".a-icon-alt")
    review_node = soup.select_one("#acrCustomerReviewText")
    image_node = soup.select_one("#landingImage") or soup.select_one("#imgBlkFront")
    old_price = _parse_price_text(old_node.get_text(" ", strip=True)) if old_node else 0.0
    discount = round((old_price - price) * 100 / old_price, 1) if old_price > price else 0.0
    return Product(
        asin=seed.asin,
        title=(title_node.get_text(" ", strip=True) if title_node else seed.title),
        brand=seed.brand,
        category=seed.category,
        price=price,
        old_price=old_price or None,
        rating=_parse_first_number(rating_node.get_text(" ", strip=True)) or None if rating_node else None,
        reviews=int(_parse_first_number(review_node.get_text(" ", strip=True))) if review_node else 0,
        discount=discount,
        image_url=image_node.get("src") if image_node else seed.image_url,
    )


def _amazon_access_token() -> str:
    """Return a cached OAuth token for Amazon Creators API."""
    global _AMAZON_TOKEN, _AMAZON_TOKEN_EXPIRES_AT
    if not AMAZON_CLIENT_ID or not AMAZON_CLIENT_SECRET or not AMAZON_PARTNER_TAG:
        raise RuntimeError(
            "Amazon API variables are missing: AMAZON_CLIENT_ID, "
            "AMAZON_CLIENT_SECRET, AMAZON_PARTNER_TAG"
        )
    now = time.time()
    with _AMAZON_TOKEN_LOCK:
        if _AMAZON_TOKEN and now < _AMAZON_TOKEN_EXPIRES_AT - 60:
            return _AMAZON_TOKEN
        response = requests.post(
            "https://api.amazon.com/auth/o2/token",
            headers={"Content-Type": "application/json"},
            json={
                "grant_type": "client_credentials",
                "client_id": AMAZON_CLIENT_ID,
                "client_secret": AMAZON_CLIENT_SECRET,
                "scope": "creatorsapi::default",
            },
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        token = str(body.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("Amazon API did not return an access token")
        _AMAZON_TOKEN = token
        _AMAZON_TOKEN_EXPIRES_AT = now + max(int(body.get("expires_in") or 3600), 300)
        return token


def _listing_is_available(listing: dict) -> bool:
    """Use Creators API availability, never a historical catalog flag."""
    availability = listing.get("availability") or {}
    availability_type = availability.get("type")
    if isinstance(availability_type, dict):
        availability_type = availability_type.get("value") or availability_type.get("displayValue")
    normalized_type = normalize(str(availability_type or ""))
    message = normalize(str(availability.get("message") or availability.get("displayValue") or ""))
    combined = f"{normalized_type} {message}".strip()
    if any(marker in combined for marker in UNAVAILABLE_MARKERS):
        return False
    return normalized_type in {"now", "in stock", "instock", "available"} or any(
        marker in combined for marker in AVAILABLE_MARKERS
    )


def _listing_amount(listing: dict) -> float:
    return _number(((listing.get("price") or {}).get("money") or {}).get("amount"))


def _listing_is_usable(listing: dict) -> bool:
    condition = listing.get("condition")
    if isinstance(condition, dict):
        condition = condition.get("value") or condition.get("displayValue") or ""
    condition = normalize(str(condition or ""))
    if condition and condition not in {"new", "جديد"}:
        return False
    merchant = normalize(str((listing.get("merchantInfo") or {}).get("name") or ""))
    listing_type = listing.get("type")
    if isinstance(listing_type, dict):
        listing_type = listing_type.get("value") or listing_type.get("displayValue") or ""
    listing_type = normalize(str(listing_type or ""))
    return (
        _listing_amount(listing) > 0
        and _listing_is_available(listing)
        and "resale" not in merchant
        and "subscribe" not in listing_type
    )


def _product_from_api_item(item: dict, seed: Product) -> Product | None:
    listings = [
        listing for listing in ((item.get("offersV2") or {}).get("listings") or [])
        if isinstance(listing, dict) and _listing_is_usable(listing)
    ]
    if not listings:
        return None
    listing = min(listings, key=_listing_amount)
    price_data = listing.get("price") or {}
    price = _listing_amount(listing)
    old_price = _number((((price_data.get("savingBasis") or {}).get("money") or {}).get("amount"))) or None
    discount = _number((price_data.get("savings") or {}).get("percentage"))
    if not discount and old_price and old_price > price:
        discount = round((old_price - price) * 100 / old_price, 1)
    title = str((((item.get("itemInfo") or {}).get("title") or {}).get("displayValue")) or seed.title).strip()
    return Product(
        asin=str(item.get("asin") or seed.asin).upper(),
        title=title,
        brand=seed.brand,
        category=seed.category,
        price=price,
        old_price=old_price,
        rating=None,
        reviews=0,
        discount=discount,
        image_url=seed.image_url,
    )


def _amazon_api_verify(seeds: list[Product]) -> list[Product]:
    """Verify up to ten candidate ASINs in one Creators API request."""
    global _AMAZON_LAST_REQUEST_AT
    if not seeds:
        return []
    seeds = seeds[:10]
    now = time.time()
    verified: list[Product] = []
    uncached: list[Product] = []
    for seed in seeds:
        cached = _AMAZON_RESULT_CACHE.get(seed.asin)
        if cached and cached[0] > now:
            if cached[1] is not None:
                verified.append(cached[1])
        else:
            uncached.append(seed)
    if not uncached:
        return verified

    seed_by_asin = {seed.asin: seed for seed in uncached}
    payload = {
        "itemIds": list(seed_by_asin),
        "itemIdType": "ASIN",
        "partnerTag": AMAZON_PARTNER_TAG,
        "partnerType": "Associates",
        "marketplace": AMAZON_MARKETPLACE,
        "languagesOfPreference": ["ar_AE"],
        "resources": [
            "itemInfo.title",
            "offersV2.listings.price",
            "offersV2.listings.availability",
            "offersV2.listings.merchantInfo",
            "offersV2.listings.type",
            "offersV2.listings.condition",
            "offersV2.listings.dealDetails",
        ],
    }
    with _AMAZON_API_LOCK:
        response = None
        for attempt in range(3):
            wait_for = AMAZON_MIN_REQUEST_INTERVAL - (time.time() - _AMAZON_LAST_REQUEST_AT)
            if wait_for > 0:
                time.sleep(wait_for)
            response = requests.post(
                "https://creatorsapi.amazon/catalog/v1/getItems",
                headers={
                    "Authorization": f"Bearer {_amazon_access_token()}",
                    "Content-Type": "application/json",
                    "x-marketplace": AMAZON_MARKETPLACE,
                },
                json=payload,
                timeout=30,
            )
            _AMAZON_LAST_REQUEST_AT = time.time()
            if response.status_code != 429:
                break
            retry_after = response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** (attempt + 1)
            logger.warning("Amazon API throttled; retrying in %.1f seconds", delay)
            time.sleep(delay)
    if response is None or response.status_code != 200:
        status = response.status_code if response is not None else "no-response"
        body = response.text[:400] if response is not None else ""
        raise RuntimeError(f"Amazon Creators API failed {status}: {body}")
    items = (response.json().get("itemsResult") or {}).get("items") or []
    fresh_results: dict[str, Product] = {}
    for item in items:
        asin = str(item.get("asin") or "").upper()
        seed = seed_by_asin.get(asin)
        if not seed:
            continue
        product = _product_from_api_item(item, seed)
        if product:
            fresh_results[asin] = product
            verified.append(product)
    expires_at = time.time() + AMAZON_RESULT_CACHE_SECONDS
    for asin in seed_by_asin:
        _AMAZON_RESULT_CACHE[asin] = (expires_at, fresh_results.get(asin))
    return verified


def search_products(query: str, budget: float | None) -> list[Product]:
    # The catalog supplies relevant candidate ASINs. Creators API is the sole
    # source of current price and availability; Railway never scrapes pages.
    within_budget: list[Product] = []
    available: list[Product] = []
    candidates = _search_local_products(query, None, limit=AMAZON_MAX_CANDIDATES)
    logger.info("Catalog matched %s candidates for query %r", len(candidates), query)
    for start in range(0, len(candidates), 10):
        verified = _amazon_api_verify(candidates[start:start + 10])
        # Catalog titles/aliases are discovery hints only. Some collected rows
        # can point at an ASIN whose current Amazon title belongs to another
        # product. Re-check relevance using the live API title before showing
        # it (for example, never return a Casio watch for an Adidas search).
        verified = [
            product for product in verified
            if _title_score(query, product)[1] == 1.0
        ]
        logger.info(
            "Amazon API verified %s available products in candidate batch %s",
            len(verified),
            start // 10 + 1,
        )
        available.extend(verified)
        within_budget.extend(
            product for product in verified
            if not budget or product.price <= budget * (1 + BUDGET_TOLERANCE)
        )

    if within_budget:
        if budget:
            # "في حدود 1800" means useful choices near 1800, not the three
            # cheapest brand items such as shower gel or deodorant.
            within_budget.sort(
                key=lambda p: (abs(p.price - budget), -(p.rating or 0), -p.reviews)
            )
        else:
            within_budget.sort(key=lambda p: (p.price, -(p.rating or 0), -p.reviews))
        return within_budget

    # If nothing fits the requested budget, show only the cheapest verified
    # available alternatives—never an unchecked catalog row.
    available.sort(key=lambda p: (p.price, -(p.rating or 0), -p.reviews))
    return available


def amazon_url(asin: str) -> str:
    params = {"utm_source": "wafrcash-shopping-pilot", "t": str(int(time.time()))}
    if ASSOCIATE_TAG:
        params["tag"] = ASSOCIATE_TAG
    return f"https://www.amazon.eg/dp/{asin}?{urlencode(params)}"


def format_product(product: Product, position: int) -> str:
    labels = ("💰 أقرب اختيار لطلبك", "⭐ اختيار قوي", "🏆 بديل يستحق المقارنة")
    details = [f"السعر الحالي: {product.price:,.2f} جنيه"]
    if product.old_price and product.old_price > product.price:
        details.append(f"بدل {product.old_price:,.2f} جنيه")
    if product.discount:
        details.append(f"خصم {product.discount:g}%")
    if product.rating:
        details.append(f"تقييم {product.rating:g}⭐ ({product.reviews:,})")
    return f"{labels[position]}\n\n{product.title}\n" + " • ".join(details)


def product_keyboard(product: Product) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 افتح المنتج", callback_data=f"open:{product.asin}")],
        [
            InlineKeyboardButton("❤️ احفظ وتابع السعر", callback_data=f"save:{product.asin}"),
            InlineKeyboardButton("💰 بديل أرخص", callback_data=f"cheap:{product.asin}"),
        ],
    ])


async def _send_result_page(message, context: ContextTypes.DEFAULT_TYPE, start: int = 0) -> None:
    """Send the next three already-verified results without repeating a search."""
    result_asins = list(context.user_data.get("last_results") or [])
    page_asins = result_asins[start:start + RESULT_LIMIT]
    for position, asin in enumerate(page_asins):
        product = BY_ASIN.get(asin)
        if product:
            await message.reply_text(
                format_product(product, position), reply_markup=product_keyboard(product)
            )
    next_start = start + len(page_asins)
    context.user_data["result_offset"] = next_start
    if next_start < len(result_asins):
        remaining = len(result_asins) - next_start
        await message.reply_text(
            f"مش مناسبين؟ عندي {remaining} اختيار تاني من نفس البحث.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "عرض 3 منتجات تانية ⬇️", callback_data=f"more:{next_start}"
                )
            ]]),
        )
    elif start > 0:
        await message.reply_text("دي كانت آخر المنتجات المطابقة المتاحة حاليًا ✅")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.effective_message.reply_text(
        "🛍️ أهلاً بك في تجربة مساعد وفر كاش\n\n"
        "قولي محتاج تشتري إيه وميزانيتك كام في رسالة واحدة.\n\n"
        "مثال: عايزة إير فراير في حدود 4000 جنيه\n"
        "أو: حفاضات بامبرز مقاس 3"
    )


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    query, budget = parse_request(text)
    if not query:
        await update.effective_message.reply_text("اكتبي اسم المنتج، وممكن تضيفي ميزانيتك في نفس الرسالة.")
        return
    try:
        results = await asyncio.to_thread(search_products, query, budget)
    except (requests.RequestException, RuntimeError) as exc:
        logger.warning("Live Amazon search failed: %s", exc)
        await update.effective_message.reply_text(
            "البحث في Amazon مش متاح مؤقتًا. جرّبي تاني بعد دقيقة."
        )
        return
    if not results:
        budget_text = f" في حدود {budget:,.0f} جنيه" if budget else ""
        await update.effective_message.reply_text(
            f"ملقتش «{query}» مناسب{budget_text} في المنتجات المتاحة حاليًا.\n\n"
            "جرّبي ميزانية أعلى، أو اكتبي اسم المنتج من غير ميزانية علشان أعرض أقرب المتاح."
        )
        return
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO searches(telegram_id, query, budget, result_asins, created_at) VALUES(?,?,?,?,?)",
            (update.effective_user.id, text, budget, json.dumps([p.asin for p in results]), int(time.time())),
        )
    context.user_data["last_query"] = text
    context.user_data["last_results"] = [p.asin for p in results]
    context.user_data["result_offset"] = 0
    BY_ASIN.update({p.asin: p for p in results})
    over_budget = bool(budget and results and all(p.price > budget * (1 + BUDGET_TOLERANCE) for p in results))
    if over_budget:
        await update.effective_message.reply_text(
            f"ملقتش «{query}» في حدود {budget:,.0f} جنيه، لكن دي أرخص "
            f"{len(results)} اختيارات متاحة حاليًا 👇"
        )
    else:
        budget_line = f" في حدود {budget:,.0f} جنيه" if budget else ""
        await update.effective_message.reply_text(f"لقيت لك {len(results)} اختيارات{budget_line} 👇")
    await _send_result_page(update.effective_message, context, 0)


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if q.data.startswith("more:"):
        try:
            requested_start = int(q.data.split(":", 1)[1])
        except (TypeError, ValueError):
            await q.message.reply_text("تعذر فتح باقي النتائج. اعملي بحثًا جديدًا.")
            return
        current_start = int(context.user_data.get("result_offset") or 0)
        # Old buttons cannot rewind the list and repeat products.
        start = max(requested_start, current_start)
        await q.edit_message_reply_markup(reply_markup=None)
        await _send_result_page(q.message, context, start)
        return
    try:
        action, asin = q.data.split(":", 1)
        product = BY_ASIN[asin]
    except (ValueError, KeyError):
        await q.message.reply_text("المنتج لم يعد متاحًا في نسخة البيانات الحالية.")
        return
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO events(telegram_id,event_type,asin,query,created_at) VALUES(?,?,?,?,?)",
            (q.from_user.id, action, asin, context.user_data.get("last_query"), int(time.time())),
        )
        if action == "save":
            db.execute(
                "INSERT INTO watchlist(telegram_id,asin,saved_price,saved_at) VALUES(?,?,?,?) "
                "ON CONFLICT(telegram_id,asin) DO UPDATE SET saved_price=excluded.saved_price,saved_at=excluded.saved_at",
                (q.from_user.id, asin, product.price, int(time.time())),
            )
    if action == "open":
        await q.message.reply_text(
            "🛒 افتحي المنتج وشوفي تفاصيله، وبعدها تقدري ترجعي هنا تطلبي بديل أرخص.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("فتح على Amazon", url=amazon_url(asin))]]),
        )
    elif action == "save":
        await q.message.reply_text(
            f"❤️ حفظت المنتج بسعر {product.price:,.2f} جنيه.\n"
            "استخدمي /saved لمشاهدة قائمتك. عند تحديث ملف المنتجات نقدر نكتشف انخفاض السعر."
        )
    elif action == "cheap":
        # Keep the customer's original product intent. Searching again with
        # the full returned title can drift to sibling grocery products that
        # merely share brand, weight, or words such as "مصري" (for example,
        # rice -> sugar -> flour). The original query keeps alternatives in
        # the same requested product family.
        original_text = str(context.user_data.get("last_query") or "").strip()
        original_query, _ = parse_request(original_text)
        if not original_query:
            original_query = product.category or product.title
        alternatives = [
            p for p in search_products(original_query, product.price - 0.01)
            if p.asin != asin and p.price < product.price
        ]
        if not alternatives:
            await q.message.reply_text("ملقتش بديلًا أرخص مناسبًا في القائمة الحالية.")
            return
        cheaper = min(alternatives, key=lambda p: p.price)
        await q.message.reply_text(
            "💰 ده بديل أرخص وجدته:\n\n" + format_product(cheaper, 0),
            reply_markup=product_keyboard(cheaper),
        )


async def saved(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT asin,saved_price FROM watchlist WHERE telegram_id=? ORDER BY saved_at DESC",
            (update.effective_user.id,),
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("لسه ما حفظتيش أي منتج.")
        return
    await update.effective_message.reply_text("❤️ المنتجات المحفوظة:")
    for asin, saved_price in rows:
        p = BY_ASIN.get(asin)
        if not p:
            continue
        change = p.price - saved_price
        if change < 0:
            status = f"🔥 السعر نزل {abs(change):,.2f} جنيه"
        elif change > 0:
            status = f"السعر زاد {change:,.2f} جنيه"
        else:
            status = "السعر لم يتغير"
        await update.effective_message.reply_text(
            f"{p.title}\nالسعر: {p.price:,.2f} جنيه\n{status}",
            reply_markup=product_keyboard(p),
        )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    admin_ids = {int(x) for x in os.getenv("SHOPPING_TEST_ADMIN_IDS", "").split(",") if x.strip().isdigit()}
    if update.effective_user.id not in admin_ids:
        return
    with sqlite3.connect(DB_PATH) as db:
        searches = db.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
        users = db.execute("SELECT COUNT(DISTINCT telegram_id) FROM searches").fetchone()[0]
        opens = db.execute("SELECT COUNT(*) FROM events WHERE event_type='open'").fetchone()[0]
        saves = db.execute("SELECT COUNT(*) FROM events WHERE event_type='save'").fetchone()[0]
        cheaper = db.execute("SELECT COUNT(*) FROM events WHERE event_type='cheap'").fetchone()[0]
    await update.effective_message.reply_text(
        f"📊 نتائج التجربة\n\nالمستخدمون: {users}\nعمليات البحث: {searches}\n"
        f"اختيارات فتح المنتج: {opens}\nحفظ للمتابعة: {saves}\nطلبات بديل أرخص: {cheaper}"
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set SHOPPING_TEST_BOT_TOKEN to a separate test bot token")
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saved", saved))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(callbacks, pattern=r"^(open|save|cheap|more):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    logger.info(
        "Shopping pilot loaded %s allowed ASINs (%s searchable titles)",
        len(ALLOWED_ASINS),
        len(PRODUCTS),
    )
    logger.info(
        "Amazon API configuration detected: client_id=%s client_secret=%s "
        "partner_tag=%s marketplace=%s",
        bool(AMAZON_CLIENT_ID),
        bool(AMAZON_CLIENT_SECRET),
        bool(AMAZON_PARTNER_TAG),
        AMAZON_MARKETPLACE,
    )
    # Keep messages sent during a short deployment/restart instead of silently
    # deleting them when the bot comes back online.
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()

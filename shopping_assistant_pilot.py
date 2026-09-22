"""Standalone Telegram pilot for an Amazon Egypt shopping assistant.

Run with:
    SHOPPING_TEST_BOT_TOKEN=... AMAZON_ASSOCIATE_TAG=... python shopping_assistant_pilot.py

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
ASSOCIATE_TAG = os.getenv("AMAZON_ASSOCIATE_TAG", "").strip()
CATALOG_PATH = Path(os.getenv("SHOPPING_PRODUCTS_FILE", "amazon_egypt_asin_catalog.json"))
DB_PATH = Path(os.getenv("SHOPPING_PILOT_DB", "shopping_pilot.db"))
RESULT_LIMIT = 3
BUDGET_TOLERANCE = 0.05

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shopping-pilot")
# httpx logs Telegram Bot API URLs at INFO level; those URLs contain the bot
# token. Keep request details out of Railway logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


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
        if normalize(phrase) in normalized:
            variants.extend({w for w in normalize(alias).split() if w} for alias in aliases)
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
    tokens = set(normalize(f"{product.title} {product.brand} {product.category}").split())
    return max((_variant_score(words, tokens) for words in _query_variants(query)), default=(0.0, 0.0))


def _search_local_products(query: str, budget: float | None, limit: int = RESULT_LIMIT) -> list[Product]:
    ranked: list[tuple[float, Product]] = []
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
        ranked.append((relevance * 10 + budget_score + quality + saving, p))
    ranked.sort(key=lambda item: (item[0], item[1].rating or 0, item[1].reviews), reverse=True)
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
    """Refresh volatile fields for one locally matched ASIN."""
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


def search_products(query: str, budget: float | None) -> list[Product]:
    # Match names locally first, then request current prices only for the small
    # shortlist. The stored catalog price is never used as the current price.
    if PRODUCTS:
        candidates = _search_local_products(query, None, limit=30)
        within_budget: list[Product] = []
        available: list[Product] = []
        for candidate in candidates:
            try:
                current = _amazon_product_page(candidate)
            except (requests.RequestException, RuntimeError) as exc:
                logger.info("Could not refresh %s: %s", candidate.asin, exc)
                continue
            if not current:
                continue
            available.append(current)
            if not budget or current.price <= budget * (1 + BUDGET_TOLERANCE):
                within_budget.append(current)
            # With no budget, three relevant live products are enough. With a
            # budget we keep checking if nothing fits, so we can return the
            # cheapest real alternatives instead of an empty result.
            if (not budget and len(within_budget) >= RESULT_LIMIT) or (
                budget and len(within_budget) >= RESULT_LIMIT
            ):
                break
        if within_budget:
            within_budget.sort(key=lambda p: (p.price, -(p.rating or 0), -p.reviews))
            return within_budget[:RESULT_LIMIT]
        available.sort(key=lambda p: (p.price, -(p.rating or 0), -p.reviews))
        return available[:RESULT_LIMIT]
    found: dict[str, Product] = {}
    for page in range(1, 4):
        for product in _amazon_search_page(query, page):
            if budget and product.price > budget * (1 + BUDGET_TOLERANCE):
                continue
            found.setdefault(product.asin, product)
        if len(found) >= RESULT_LIMIT:
            break
    results = list(found.values())
    results.sort(
        key=lambda p: (
            1 if not budget or p.price <= budget else 0,
            p.rating or 0,
            p.reviews,
            -p.price,
        ),
        reverse=True,
    )
    return results[:RESULT_LIMIT]


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
    for index, product in enumerate(results):
        await update.effective_message.reply_text(
            format_product(product, index), reply_markup=product_keyboard(product)
        )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
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
        alternatives = [
            p for p in search_products(f"{product.title} {product.brand}", product.price - 0.01)
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
    app.add_handler(CallbackQueryHandler(callbacks, pattern=r"^(open|save|cheap):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    logger.info("Shopping pilot loaded %s allowed ASINs", len(ALLOWED_ASINS))
    # Keep messages sent during a short deployment/restart instead of silently
    # deleting them when the bot comes back online.
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()

"""Standalone Telegram pilot for an Amazon Egypt shopping assistant.

Run with:
    SHOPPING_TEST_BOT_TOKEN=... AMAZON_ASSOCIATE_TAG=... python shopping_assistant_pilot.py

This file deliberately does not import or modify the production bot, its database,
wheel, rewards, or customer records.
"""
from __future__ import annotations

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
CATALOG_PATH = Path(os.getenv("SHOPPING_PRODUCTS_FILE", "amazon_all_accepted_products.json"))
DB_PATH = Path(os.getenv("SHOPPING_PILOT_DB", "shopping_pilot.db"))
RESULT_LIMIT = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("shopping-pilot")


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


def load_catalog() -> list[Product]:
    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), [])
    products: list[Product] = []
    for row in raw:
        asin = str(row.get("asin") or "").strip()
        title = str(row.get("displayTitle") or row.get("title") or "").strip()
        price = _money_field(row.get("buyingPrice") or row.get("price"))
        if not asin or not title or price <= 0:
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
    return products


PRODUCTS = load_catalog()
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
    "عايز", "عايزة", "محتاج", "محتاجة", "منتج", "سعر", "في", "من", "الى",
    "حدود", "حوالي", "جنيه", "ج", "لي", "لو", "هات", "وريني", "افضل", "أحسن",
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
    words = [w for w in query.split() if len(w) > 1 and w not in ARABIC_STOPWORDS]
    return " ".join(words), budget


def _text_score(query_words: set[str], product: Product) -> float:
    title = normalize(f"{product.title} {product.brand} {product.category}")
    tokens = set(title.split())
    score = 0.0
    for word in query_words:
        if word in tokens:
            score += 6
        elif word in title:
            score += 3
        elif len(word) >= 4 and any(tok.startswith(word[:4]) for tok in tokens):
            score += 1.5
    return score


def search_products(query: str, budget: float | None) -> list[Product]:
    words = set(normalize(query).split())
    ranked: list[tuple[float, Product]] = []
    for p in PRODUCTS:
        relevance = _text_score(words, p)
        if words and relevance <= 0:
            continue
        budget_score = 0.0
        if budget:
            if p.price <= budget:
                budget_score = 3 + min(p.price / budget, 1)
            else:
                budget_score = -min((p.price - budget) / budget * 8, 8)
        quality = min((p.rating or 0) / 5, 1) + min(p.reviews / 1000, 1)
        saving = min(p.discount / 20, 2)
        ranked.append((relevance * 10 + budget_score + quality + saving, p))
    ranked.sort(key=lambda item: (item[0], item[1].rating or 0, item[1].reviews), reverse=True)
    return [p for _, p in ranked[:RESULT_LIMIT]]


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
    results = search_products(query, budget)
    if not results:
        await update.effective_message.reply_text(
            "ملقتش اختيار مناسب في المنتجات المتاحة حاليًا. جرّبي اسمًا أبسط أو اسم البراند."
        )
        return
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO searches(telegram_id, query, budget, result_asins, created_at) VALUES(?,?,?,?,?)",
            (update.effective_user.id, text, budget, json.dumps([p.asin for p in results]), int(time.time())),
        )
    context.user_data["last_query"] = text
    context.user_data["last_results"] = [p.asin for p in results]
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
    logger.info("Shopping pilot loaded %s products", len(PRODUCTS))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

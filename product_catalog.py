"""قراءة منتجات SPCC المقبولة وبناء روابط وأسئلة العجلة الذهبية."""
from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import config


@dataclass(frozen=True)
class CatalogProduct:
    asin: str
    title: str
    brand: str
    category: str
    price: float
    old_price: float | None
    discount_percent: float
    rating: float | None
    review_count: int | None
    expected_revenue_per_click: float
    image_url: str | None


_products: list[CatalogProduct] | None = None
_by_asin: dict[str, CatalogProduct] = {}
_link_lock = threading.Lock()
_last_link_timestamp = 0


def _products_path() -> Path:
    configured = Path(config.EGYPT_GOLDEN_PRODUCTS_FILE)
    if configured.is_absolute():
        return configured
    return Path(__file__).resolve().parent / configured


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def load_products(force: bool = False) -> list[CatalogProduct]:
    global _products, _by_asin
    if _products is not None and not force:
        return _products

    with _products_path().open("r", encoding="utf-8") as stream:
        raw_items = json.load(stream)
    if not isinstance(raw_items, list):
        raise ValueError("ملف المنتجات لازم يحتوي على قائمة JSON")

    loaded: list[CatalogProduct] = []
    for item in raw_items:
        asin = str(item.get("asin") or "").strip().upper()
        title = str(item.get("displayTitle") or item.get("dedupeString") or "").strip()
        price_block = item.get("buyingPrice") or {}
        price = _number(price_block.get("amount"))
        epc = _number(item.get("expectedRevenuePerClick"))
        if len(asin) != 10 or not title or epc < 0:
            continue

        list_price_block = item.get("listPrice") or {}
        old_price = _number(list_price_block.get("amount")) or None
        deal = item.get("dealMetadata") or {}
        discount = _number(deal.get("discountPercentage"))
        if not discount and old_price and old_price > price:
            discount = round((old_price - price) / old_price * 100, 2)

        loaded.append(CatalogProduct(
            asin=asin,
            title=title,
            brand=str(item.get("asinBrand") or "").strip(),
            category=str(item.get("category") or "").strip(),
            price=price,
            old_price=old_price,
            discount_percent=discount,
            rating=_number(item.get("numberOfReviewStars")) or None,
            review_count=int(_number(item.get("reviewCount"))) if item.get("reviewCount") is not None else None,
            expected_revenue_per_click=epc,
            image_url=item.get("imageUrl"),
        ))

    if not loaded:
        raise ValueError("ملف المنتجات لا يحتوي على منتجات صالحة")
    _products = loaded
    _by_asin = {product.asin: product for product in loaded}
    return _products


def get_product(asin: str) -> CatalogProduct | None:
    load_products()
    return _by_asin.get(asin.upper())


def choose_product(
    excluded_asins: set[str] | None = None,
    question_index: int = 0,
    user_id: int = 0,
) -> CatalogProduct:
    products = load_products()
    excluded = excluded_asins or set()

    # كل جولة من 5: 2 Low + 2 Medium + 1 High.
    # بنعمل rotation حسب user_id عشان ترتيب الفئات مايبقاش متوقع،
    # لكن يفضل العدد نفسه بالضبط داخل كل جولة.
    mix = ("low", "medium", "low", "medium", "high")
    offset = abs(int(user_id or 0)) % len(mix)
    tier = mix[(int(question_index) + offset) % len(mix)]

    low_min = config.EGYPT_GOLDEN_LOW_MIN_EPC
    low_max = config.EGYPT_GOLDEN_LOW_MAX_EPC
    medium_max = config.EGYPT_GOLDEN_MEDIUM_MAX_EPC

    def in_tier(product: CatalogProduct) -> bool:
        epc = product.expected_revenue_per_click
        if tier == "low":
            return low_min <= epc < low_max
        if tier == "medium":
            return low_max <= epc < medium_max
        return epc >= medium_max

    eligible = [p for p in products if in_tier(p)]
    candidates = [p for p in eligible if p.asin not in excluded]

    if candidates:
        return random.choice(candidates)
    if eligible:
        # لو العميل شاف كل منتجات الفئة خلال اليوم، نسمح بإعادة الاستخدام
        # داخل نفس الفئة بدل ما نقفز لفئة أغلى.
        return random.choice(eligible)

    # Fallback لو حدود ENV اتضبطت بشكل خلّى فئة فاضية.
    remaining = [p for p in products if p.asin not in excluded] or products
    return random.choice(remaining)


def _unique_timestamp_ms() -> int:
    global _last_link_timestamp
    with _link_lock:
        now = int(time.time() * 1000)
        _last_link_timestamp = max(now, _last_link_timestamp + 1)
        return _last_link_timestamp


def build_affiliate_link(asin: str) -> str:
    timestamp = _unique_timestamp_ms()
    query = urlencode({
        "ref": "t_ac_spc_accepted_tile",
        "linkCode": "tr1",
        "tag": config.EGYPT_GOLDEN_PARTNER_TAG,
        "linkId": f"{asin}_{timestamp}",
    })
    return f"https://www.amazon.eg/dp/{asin}?{query}"


def customer_reward_for_epc(epc: float) -> float:
    return round(
        max(epc, 0)
        * config.EGYPT_APPROVED_CLICK_RATE
        * config.EGYPT_REAL_CLICK_VALUE_RATE
        * config.EGYPT_CUSTOMER_REWARD_RATE,
        6,
    )


def question_for(product: CatalogProduct) -> dict:
    """يبني سؤالًا عشوائيًا ويعيد الاختيارات مع رقم الإجابة الصحيحة."""
    products = load_products()
    types = ["title"]
    if product.price > 0:
        types.append("price")
    if product.brand:
        types.append("brand")
    if product.category:
        types.append("category")
    if product.rating:
        types.append("rating")
    if product.review_count is not None:
        types.append("reviews")
    if product.discount_percent > 0:
        types.append("discount")
    if product.old_price:
        types.append("old_price")

    qtype = random.choice(types)
    if qtype == "price":
        correct = f"{product.price:g} جنيه"
        wrong = [f"{max(round(product.price * factor), 1):g} جنيه" for factor in (0.75, 1.25, 1.5)]
        prompt = "سعر المنتج الحالي كام؟"
    elif qtype == "discount":
        correct = f"{product.discount_percent:g}%"
        wrong = [f"{max(min(round(product.discount_percent + delta), 95), 1):g}%" for delta in (-10, 10, 20)]
        prompt = "نسبة الخصم على المنتج كام؟"
    elif qtype == "old_price":
        correct = f"{product.old_price:g} جنيه"
        wrong = [f"{max(round(product.old_price * factor), 1):g} جنيه" for factor in (0.7, 0.85, 1.25)]
        prompt = "سعر المنتج قبل الخصم كان كام؟"
    elif qtype == "rating":
        correct = f"{product.rating:g} من 5"
        wrong = [f"{value:g} من 5" for value in (3.5, 4, 4.5, 5) if value != product.rating][:3]
        prompt = "تقييم المنتج كام؟"
    elif qtype == "reviews":
        correct = f"{product.review_count:,} تقييم"
        values = {max(1, round(product.review_count * factor)) for factor in (0.5, 1.5, 2)}
        wrong = [f"{value:,} تقييم" for value in values]
        prompt = "تقريبًا كام تقييم مكتوب على المنتج؟"
    else:
        field = qtype
        original_correct = getattr(product, field)
        correct = str(original_correct)[:60]
        pool = list({str(getattr(p, field))[:60] for p in random.sample(products, min(len(products), 100)) if getattr(p, field) and getattr(p, field) != original_correct})
        wrong = pool[:3]
        prompt = {"title": "إيه اسم المنتج؟", "brand": "إيه ماركة المنتج؟", "category": "المنتج تابع لأي قسم؟"}[qtype]

    options = list(dict.fromkeys([correct] + wrong))
    if len(options) < 4:
        return question_for(product)
    options = options[:4]
    random.shuffle(options)
    return {
        "type": qtype,
        "prompt": prompt,
        "options": options,
        "correct_index": options.index(correct),
    }

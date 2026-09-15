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
        raw_epc = item.get("expectedRevenuePerClick")
        if raw_epc is None:
            continue
        epc = _number(raw_epc, default=-1.0)
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
    current_round_epc: float = 0.0,
    all_seen_asins: set[str] | None = None,
    first_round_bonus: bool = False,
) -> CatalogProduct:
    """Dynamic selector: minimize repeats while guaranteeing the first 5 products
    in a round can reach EGYPT_MIN_ROUND_EPC.

    For slots 1-4 it only chooses a product if enough EPC remains in the catalog
    to finish the 5-product target. On slot 5 it chooses the smallest available
    EPC that completes the target. Previously seen products are used only when
    the unseen catalog cannot satisfy the constraint.
    """
    products = load_products()
    excluded = set(excluded_asins or set())
    seen = set(all_seen_asins or set())
    target = float(getattr(config, "EGYPT_MIN_ROUND_EPC", 21.23))
    slot = int(question_index) % 5

    # One-time welcome round: its first three questions are the three
    # highest-EPC products in the current catalog.
    if first_round_bonus and 0 <= int(question_index) < 3:
        ranked = sorted(products, key=lambda p: p.expected_revenue_per_click, reverse=True)
        top_three = ranked[:3]
        preferred = top_three[int(question_index)] if len(top_three) > int(question_index) else None
        if preferred and preferred.asin not in excluded:
            return preferred
        remaining_top = [p for p in top_three if p.asin not in excluded]
        if remaining_top:
            return remaining_top[0]


    def available(prefer_unseen: bool) -> list[CatalogProduct]:
        base = [p for p in products if p.asin not in excluded]
        if prefer_unseen:
            unseen = [p for p in base if p.asin not in seen]
            if unseen:
                return unseen
        return base

    # Wrong-answer penalty questions after the original 5 still avoid repeats,
    # but they are outside the 5-product EPC guarantee.
    if int(question_index) >= 5:
        pool = available(True) or available(False) or products
        return random.choice(pool)

    slots_after = 4 - slot
    for prefer_unseen in (True, False):
        pool = available(prefer_unseen)
        if not pool:
            continue

        if slots_after == 0:
            need = max(target - float(current_round_epc or 0), 0.0)
            enough = [p for p in pool if p.expected_revenue_per_click + 1e-12 >= need]
            if enough:
                # Smallest EPC that safely closes the round; randomize ties.
                enough.sort(key=lambda p: p.expected_revenue_per_click)
                floor = enough[0].expected_revenue_per_click
                near = [p for p in enough if p.expected_revenue_per_click <= floor + 0.05]
                return random.choice(near)
            continue

        feasible = []
        for candidate in pool:
            remaining = [
                p.expected_revenue_per_click for p in pool
                if p.asin != candidate.asin
            ]
            remaining.sort(reverse=True)
            best_future = sum(remaining[:slots_after])
            if float(current_round_epc or 0) + candidate.expected_revenue_per_click + best_future + 1e-12 >= target:
                feasible.append(candidate)
        if feasible:
            # Prefer lower EPC now, preserving high-EPC products to subsidize later rounds.
            feasible.sort(key=lambda p: p.expected_revenue_per_click)
            window = feasible[:max(1, min(20, len(feasible)))]
            return random.choice(window)

    # Safety fallback if the catalog itself cannot meet the target.
    pool = available(True) or available(False) or products
    return max(pool, key=lambda p: p.expected_revenue_per_click)

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

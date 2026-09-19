"""قراءة منتجات SPCC المقبولة وبناء روابط وأسئلة العجلة الذهبية."""
from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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
    operational_epc: float
    image_url: str | None


_products: list[CatalogProduct] | None = None
_by_asin: dict[str, CatalogProduct] = {}
_link_lock = threading.Lock()
_last_link_timestamp = 0
_history_synced = False

# Product-selection policy for the five-question golden round.
MIN_MAIN_EPC = 1.0
DEFAULT_OPERATIONAL_EPC = 0.05
PAMPERS_TIDE_OPERATIONAL_EPC = 0.10
TRUSTED_BRANDS = {"nivea", "pampers", "tide"}
BLOCKED_BRANDS = {
    "toppik", "ogx", "butterfly", "gillette", "pentel", "uniball", "tornado",
}

# Exactly the seven customer-visible facts agreed for product questions.
QUESTION_TYPE_ORDER = (
    "title", "price", "old_price", "discount", "rating", "brand", "reviews",
)


def _brand_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _is_pampers_or_tide(brand: str | None, title: str | None = None) -> bool:
    english = _brand_key(brand)
    brand_text = str(brand or "").casefold()
    if english in {"pampers", "tide"} or any(
        name in brand_text for name in ("بامبرز", "تايد")
    ):
        return True
    # بعض سجلات Amazon لا تحتوي asinBrand؛ وقتها فقط نستخدم اسم المنتج.
    if brand_text.strip():
        return False
    title_text = str(title or "").casefold()
    return any(name in title_text for name in ("pampers", "tide", "بامبرز", "تايد"))


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
    global _products, _by_asin, _history_synced
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

        brand = str(item.get("asinBrand") or "").strip()
        operational_epc = _number(
            item.get("operationalEpc"),
            PAMPERS_TIDE_OPERATIONAL_EPC if _is_pampers_or_tide(brand, title)
            else DEFAULT_OPERATIONAL_EPC,
        )
        loaded.append(CatalogProduct(
            asin=asin,
            title=title,
            brand=brand,
            category=str(item.get("category") or "").strip(),
            price=price,
            old_price=old_price,
            discount_percent=discount,
            rating=_number(item.get("numberOfReviewStars")) or None,
            review_count=int(_number(item.get("reviewCount"))) if item.get("reviewCount") is not None else None,
            expected_revenue_per_click=epc,
            operational_epc=operational_epc,
            image_url=item.get("imageUrl"),
        ))

    if not loaded:
        raise ValueError("ملف المنتجات لا يحتوي على منتجات صالحة")
    _products = loaded
    _by_asin = {product.asin: product for product in loaded}
    if not _history_synced:
        try:
            import database
            database.normalize_golden_question_epc(
                [p.asin for p in loaded if _is_pampers_or_tide(p.brand, p.title)],
                float(config.EGYPT_CUSTOMER_REWARD_RATE),
            )
            _history_synced = True
        except Exception:
            # The catalog can be imported before database.init_db(); retry later.
            pass
    return _products


def get_product(asin: str) -> CatalogProduct | None:
    load_products()
    return _by_asin.get(asin.upper())


def pool_type_for(product: CatalogProduct) -> str:
    """Classify using raw Amazon EPC; operational EPC is handled separately."""
    brand = _brand_key(product.brand)
    if brand in BLOCKED_BRANDS:
        return "blocked_brand"
    if product.expected_revenue_per_click < MIN_MAIN_EPC:
        return "excluded_low_epc"
    if brand in TRUSTED_BRANDS:
        return "trusted_brand"
    epc = product.expected_revenue_per_click
    if 2 <= epc < 5:
        return "epc_2_to_5"
    if (5 <= epc < 10) or epc >= 15:
        return "epc_5_plus"
    # EPC 1-<2 and 10-<15 remain valid reserve products. They are not used
    # while either of the two configured price pools still has capacity.
    if epc >= MIN_MAIN_EPC:
        return "reserve"
    return "excluded_low_epc"


def reward_epc_for(product: CatalogProduct) -> float:
    """Operational EPC used by rewards and reports; raw EPC remains stored."""
    return max(float(product.operational_epc), 0.0)


def available_question_types(product: CatalogProduct) -> list[str]:
    """Return the usable subset of the seven agreed question types."""
    result = ["title"]
    if product.price > 0:
        result.append("price")
    if product.old_price:
        result.append("old_price")
    if product.discount_percent > 0:
        result.append("discount")
    if product.rating:
        result.append("rating")
    if product.brand:
        result.append("brand")
    if product.review_count is not None:
        result.append("reviews")
    return [kind for kind in QUESTION_TYPE_ORDER if kind in result]


def _remaining_type_count(product: CatalogProduct, used_types: dict[str, set[str]]) -> int:
    used = set(used_types.get(product.asin, set()))
    return sum(kind not in used for kind in available_question_types(product))


def _pool_capacity(products: list[CatalogProduct], used_types: dict[str, set[str]]) -> int:
    return sum(_remaining_type_count(product, used_types) for product in products)


def choose_product(
    excluded_asins: set[str] | None = None,
    question_index: int = 0,
    user_id: int = 0,
    current_round_epc: float = 0.0,
    all_seen_asins: set[str] | None = None,
    first_round_bonus: bool = False,
    bonus_round: bool = False,
    bonus_target_epc: float | None = None,
    used_question_types: dict[str, set[str]] | None = None,
    forced_pool_type: str | None = None,
) -> CatalogProduct:
    """Choose by the configured 1 + 2 + 2 round distribution.

    Slot 1 uses Nivea/Pampers/Tide, slots 2-3 use EPC 2-<5, and slots
    4-5 use EPC 5-<10 or >=15.  A wrong-answer replacement can force the
    original pool. If a price pool has no unused product-question pairs, the
    pool with more remaining pairs supplies the missing slot. Trusted brands
    restart only after all their distinct product-question pairs are exhausted.
    """
    catalog = load_products()
    pools = {
        name: [product for product in catalog if pool_type_for(product) == name]
        for name in ("trusted_brand", "epc_2_to_5", "epc_5_plus", "reserve")
    }
    if not pools["trusted_brand"]:
        raise ValueError("لا توجد منتجات من Nivea/Pampers/Tide")
    if not pools["epc_2_to_5"] or not pools["epc_5_plus"]:
        raise ValueError("إحدى شرائح EPC الأساسية فارغة")

    used = used_question_types or {}
    slot = int(question_index) % 5
    desired = forced_pool_type or (
        "trusted_brand" if slot == 0 else "epc_2_to_5" if slot in (1, 2) else "epc_5_plus"
    )
    if desired not in pools:
        desired = "epc_2_to_5"

    def unused_candidates(pool_name: str) -> list[CatalogProduct]:
        return [p for p in pools[pool_name] if _remaining_type_count(p, used) > 0]

    candidates = unused_candidates(desired)
    if not candidates and desired == "trusted_brand":
        # The trusted pool is explicitly repeatable after its question bank ends.
        candidates = list(pools["trusted_brand"])
    elif not candidates:
        # Mid/high replace one another, choosing the side with more unused pairs.
        alternatives = ["epc_2_to_5", "epc_5_plus"]
        alternatives.sort(key=lambda name: _pool_capacity(pools[name], used), reverse=True)
        for name in alternatives:
            candidates = unused_candidates(name)
            if candidates:
                break
        if not candidates:
            # Reserve is used only after both configured price pools end.
            candidates = unused_candidates("reserve")

    if not candidates:
        raise ValueError("لا توجد أسئلة منتجات متاحة في أي شريحة")

    # Avoid the same ASIN inside one round when possible, without preventing
    # its other question types from being used in later rounds.
    excluded = set(excluded_asins or set())
    fresh = [p for p in candidates if p.asin not in excluded]
    if fresh:
        candidates = fresh
    max_remaining = max(_remaining_type_count(p, used) for p in candidates)
    best = [p for p in candidates if _remaining_type_count(p, used) == max_remaining]
    return random.choice(best)

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
        * config.EGYPT_CUSTOMER_REWARD_RATE,
        6,
    )


def question_for(product: CatalogProduct, excluded_types: set[str] | None = None) -> dict:
    """Build one of the seven facts, exhausting unused types before repeating."""
    products = [
        p for p in load_products()
        if pool_type_for(p) in {"trusted_brand", "epc_2_to_5", "epc_5_plus", "reserve"}
    ]
    all_types = available_question_types(product)
    excluded = set(excluded_types or set())
    types = [kind for kind in all_types if kind not in excluded] or all_types
    if not types:
        raise ValueError(f"المنتج {product.asin} لا يحتوي على بيانات سؤال صالحة")
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
        prompt = {"title": "إيه اسم المنتج؟", "brand": "إيه ماركة المنتج؟"}[qtype]

    options = list(dict.fromkeys([correct] + wrong))
    if len(options) < 4:
        remaining = set(excluded)
        remaining.add(qtype)
        if any(kind not in remaining for kind in all_types):
            return question_for(product, remaining)
        raise ValueError(f"تعذر تكوين 4 اختيارات مختلفة للمنتج {product.asin}")
    options = options[:4]
    random.shuffle(options)
    return {
        "type": qtype,
        "prompt": prompt,
        "options": options,
        "correct_index": options.index(correct),
    }

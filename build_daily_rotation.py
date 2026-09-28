"""Build the next WafrCash question catalog from consecutive accepted-product exports.

Usage: python build_daily_rotation.py --old yesterday.json --new today.json
The output filename defaults to amazon_all_accepted_products.json.
"""
import argparse
import json
from pathlib import Path

AVAILABLE = {"IN_STOCK", "IN_STOCK_SCARCE"}
BUDGETS = ("High", "Medium", "Low")
LOW_RETURN_BRANDS = {"ogx", "toppik", "butterfly", "pentel", "uniball", "tornado"}


def tier(item):
    if item.get("availability") not in AVAILABLE:
        return None
    budget = item.get("budgetAvailabilityScore")
    if budget not in BUDGETS:
        return None
    try:
        epc = float(item.get("expectedRevenuePerClick") or 0)
    except (TypeError, ValueError):
        return None
    if epc > 5:
        band = 0
    elif epc > 3:
        band = 1
    elif epc > 1:
        band = 2
    else:
        return None
    if budget == "Low" and band == 0:
        band = 1  # Low budget: all products above 3 EGP come first.
    return (BUDGETS.index(budget), band)


def build(old, new):
    previous = {str(x.get("asin", "")).upper(): x for x in old}
    eligible = {}
    for item in new:
        asin = str(item.get("asin", "")).strip().upper()
        level = tier(item)
        if level is not None and len(asin) == 10:
            eligible[asin] = dict(item)

    def rank(item):
        asin = item["asin"].upper()
        prior = previous.get(asin)
        previous_budget = (prior or {}).get("budgetAvailabilityScore")
        promoted = prior is not None and previous_budget in BUDGETS and (
            BUDGETS.index(previous_budget) > BUDGETS.index(item["budgetAvailabilityScore"])
        )
        return (
            *tier(item),
            1 if str(item.get("asinBrand") or "").lower().replace(" ", "") in LOW_RETURN_BRANDS else 0,
            0 if prior is None else 1 if promoted else 2,
            -float(item["expectedRevenuePerClick"]),
            asin,
        )

    ordered = sorted(eligible.values(), key=rank)
    for index, item in enumerate(ordered):
        item["dailySelection"] = {
            "rotationPriority": index,
            "rotationTier": f"{item['budgetAvailabilityScore']}:"
                            f"{'above_3' if tier(item) == (2, 1) else ('above_5', 'above_3', 'above_1')[tier(item)[1]]}",
            "newlyAdded": item["asin"].upper() not in previous,
        }
    return ordered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=Path("amazon_all_accepted_products.json"))
    args = parser.parse_args()
    old = json.loads(args.old.read_text(encoding="utf-8"))
    new = json.loads(args.new.read_text(encoding="utf-8"))
    if not isinstance(old, list) or not isinstance(new, list):
        parser.error("Both catalog files must contain JSON lists")
    products = build(old, new)
    if not products:
        parser.error("No in-stock products with EPC > 1 and known budget were found")
    args.out.write_text(json.dumps(products, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    from collections import Counter
    print(f"Saved {len(products)} products to {args.out}")
    print("Tiers:", dict(Counter(p["dailySelection"]["rotationTier"] for p in products)))
    print("First five ASINs:", [p["asin"] for p in products[:5]])


if __name__ == "__main__":
    main()

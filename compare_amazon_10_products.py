#!/usr/bin/env python3
# Wafr Cash - Amazon reconciliation for the 10 products in Amazon report
# Period: 2026-09-10 through 2026-09-17, Cairo time (inclusive)

import os
import json
import csv
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ASINS = [
    "B0002ZW5UQ",
    "B0042PY9VU",
    "B004ISW5YY",
    "B00JS8IQEM",
    "B00KK6X7CW",
    "B015R491P8",
    "B01LTIAU9W",
    "B01N9ZUAXR",
    "B073NXYJ6T",
    "B0758ZJ3CF",
]

START_DATE = "2026-09-10"
END_DATE = "2026-09-17"
CAIRO = ZoneInfo("Africa/Cairo")

def cairo_range_utc(start_date, end_date):
    start_local = datetime.fromisoformat(start_date + "T00:00:00").replace(tzinfo=CAIRO)
    # inclusive END_DATE -> next midnight
    end_day = datetime.fromisoformat(end_date + "T00:00:00").replace(tzinfo=CAIRO)
    from datetime import timedelta
    end_local = end_day + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
        end_local.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
    )

def find_db():
    candidates = [
        os.getenv("DATABASE_PATH"),
        "/data/bot_data.db",
        "bot_data.db",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    raise FileNotFoundError(
        "Database not found. Expected DATABASE_PATH or /data/bot_data.db"
    )

db_path = find_db()
start_utc, end_utc = cairo_range_utc(START_DATE, END_DATE)

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

placeholders = ",".join("?" for _ in ASINS)
params = ASINS + [start_utc, end_utc]

sql = f"""
SELECT
    UPPER(TRIM(asin)) AS asin,
    COUNT(*) AS bot_entries,
    COUNT(DISTINCT user_id) AS unique_customers,
    ROUND(AVG(epc), 6) AS avg_stored_epc,
    ROUND(SUM(epc), 6) AS sum_stored_epc,
    SUM(CASE WHEN answered=1 THEN 1 ELSE 0 END) AS answered_questions,
    SUM(CASE WHEN answered=1 AND was_correct=1 THEN 1 ELSE 0 END) AS correct_answers,
    ROUND(SUM(COALESCE(reward_value,0)), 6) AS stored_reward_values,
    MIN(created_at) AS first_seen_utc,
    MAX(created_at) AS last_seen_utc
FROM golden_questions
WHERE UPPER(TRIM(asin)) IN ({placeholders})
  AND datetime(created_at) >= datetime(?)
  AND datetime(created_at) < datetime(?)
GROUP BY UPPER(TRIM(asin))
"""

found = {r["asin"]: dict(r) for r in conn.execute(sql, params).fetchall()}

rows = []
for asin in ASINS:
    r = found.get(asin, {})
    rows.append({
        "asin": asin,
        "bot_entries": int(r.get("bot_entries") or 0),
        "unique_customers": int(r.get("unique_customers") or 0),
        "avg_stored_epc": float(r.get("avg_stored_epc") or 0),
        "sum_stored_epc": float(r.get("sum_stored_epc") or 0),
        "answered_questions": int(r.get("answered_questions") or 0),
        "correct_answers": int(r.get("correct_answers") or 0),
        "stored_reward_values": float(r.get("stored_reward_values") or 0),
        "first_seen_utc": r.get("first_seen_utc"),
        "last_seen_utc": r.get("last_seen_utc"),
    })

# Daily breakdown, useful for matching Amazon's reporting dates
daily_sql = f"""
SELECT
    UPPER(TRIM(asin)) AS asin,
    date(datetime(created_at, '+3 hours')) AS cairo_date,
    COUNT(*) AS bot_entries,
    COUNT(DISTINCT user_id) AS unique_customers,
    ROUND(AVG(epc), 6) AS avg_stored_epc,
    ROUND(SUM(epc), 6) AS sum_stored_epc
FROM golden_questions
WHERE UPPER(TRIM(asin)) IN ({placeholders})
  AND datetime(created_at) >= datetime(?)
  AND datetime(created_at) < datetime(?)
GROUP BY UPPER(TRIM(asin)), date(datetime(created_at, '+3 hours'))
ORDER BY cairo_date, asin
"""
daily = [dict(r) for r in conn.execute(daily_sql, params).fetchall()]
conn.close()

summary = {
    "period_cairo": f"{START_DATE} through {END_DATE} inclusive",
    "database": db_path,
    "total_bot_entries": sum(r["bot_entries"] for r in rows),
    "total_unique_customer_product_pairs": sum(r["unique_customers"] for r in rows),
    "total_sum_stored_epc": round(sum(r["sum_stored_epc"] for r in rows), 6),
    "products": rows,
    "daily": daily,
}

json_path = "/data/amazon_10_products_bot_result.json" if os.path.isdir("/data") else "amazon_10_products_bot_result.json"
csv_path = "/data/amazon_10_products_bot_result.csv" if os.path.isdir("/data") else "amazon_10_products_bot_result.csv"

with open(json_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

print("\n=== WAFR CASH / AMAZON 10-PRODUCT BOT RESULT ===")
print(f"Period Cairo: {START_DATE} -> {END_DATE} inclusive")
print(f"DB: {db_path}")
print("-" * 118)
print(f"{'ASIN':<12} {'ENTRIES':>8} {'USERS':>7} {'AVG_EPC':>10} {'SUM_EPC':>12} {'ANSWERED':>10} {'CORRECT':>9} {'REWARD_VAL':>12}")
for r in rows:
    print(
        f"{r['asin']:<12} {r['bot_entries']:>8} {r['unique_customers']:>7} "
        f"{r['avg_stored_epc']:>10.3f} {r['sum_stored_epc']:>12.3f} "
        f"{r['answered_questions']:>10} {r['correct_answers']:>9} "
        f"{r['stored_reward_values']:>12.3f}"
    )
print("-" * 118)
print("TOTAL BOT ENTRIES:", summary["total_bot_entries"])
print("TOTAL STORED EPC:", summary["total_sum_stored_epc"])
print("JSON RESULT:", json_path)
print("CSV RESULT :", csv_path)
print("\nCopy everything above and send it to ChatGPT, or send the generated JSON file.")

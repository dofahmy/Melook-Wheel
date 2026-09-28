"""Read-only comparison of WafrCash link openings on two Cairo calendar days.

Usage: python wafrcash_click_audit.py --db /path/to/wafr.db
Writes JSON with product, customer, hour, and currently stored IP aggregates.
No tables are changed. An IP in this report is the latest recorded IP, not
necessarily the IP used when a particular product link was opened.
"""

import argparse
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


CAIRO = ZoneInfo("Africa/Cairo")
ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?#]|$)", re.I)


def cairo_bounds(day):
    start = datetime.combine(day, datetime.min.time(), CAIRO)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), CAIRO)
    return tuple(d.astimezone(timezone.utc).replace(tzinfo=None).isoformat() for d in (start, end))


def cairo_time(value):
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc).astimezone(CAIRO) if dt.tzinfo is None else dt.astimezone(CAIRO)


def asin_from_link(link):
    if not link:
        return None
    match = ASIN_RE.search(link)
    if match:
        return match.group(1).upper()
    return (parse_qs(urlsplit(link).query).get("asin") or [None])[0]


def available(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def rows(conn, query, params=()):
    return [dict(row) for row in conn.execute(query, params)]


def report_day(conn, day):
    begin, end = cairo_bounds(day)
    users = {int(u["user_id"]): u for u in rows(conn, "SELECT user_id, username, joined_at, last_ip FROM users")}
    q = rows(conn, """SELECT id, user_id, asin, created_at, link_opened_at, answered_at,
                             answered, was_correct, is_replacement, pool_type
                      FROM golden_questions
                      WHERE datetime(link_opened_at) >= datetime(?)
                        AND datetime(link_opened_at) < datetime(?)
                      ORDER BY link_opened_at, id""", (begin, end)) if available(conn, "golden_questions") else []
    web = []
    if available(conn, "web_offer_clicks") and available(conn, "deals_cache"):
        web = rows(conn, """SELECT w.user_id, w.deal_id, w.clicked_at, w.source, w.link_index,
                                  d.base_link
                           FROM web_offer_clicks w LEFT JOIN deals_cache d ON d.id=w.deal_id
                           WHERE datetime(w.clicked_at)>=datetime(?) AND datetime(w.clicked_at)<datetime(?)
                           ORDER BY w.clicked_at, w.id""", (begin, end))
    answers = rows(conn, """SELECT user_id, was_correct FROM golden_questions
                            WHERE datetime(answered_at)>=datetime(?) AND datetime(answered_at)<datetime(?)""",
                   (begin, end)) if available(conn, "golden_questions") else []
    spins = rows(conn, """SELECT user_id FROM lucky_spins
                          WHERE datetime(created_at)>=datetime(?) AND datetime(created_at)<datetime(?)""",
                 (begin, end)) if available(conn, "lucky_spins") else []

    # Golden questions preserve the first redirect only. Web offer clicks are event rows.
    events = [{"source": "golden", "user_id": int(x["user_id"]), "asin": x["asin"],
               "time": x["link_opened_at"], "question_id": x["id"], "answered": bool(x["answered"]),
               "was_correct": x["was_correct"], "is_replacement": x["is_replacement"],
               "pool_type": x["pool_type"], "created_at": x["created_at"]} for x in q]
    events += [{"source": "web", "user_id": int(x["user_id"]), "asin": asin_from_link(x["base_link"]),
                "time": x["clicked_at"], "deal_id": x["deal_id"], "traffic_source": x["source"]} for x in web]
    per_asin = defaultdict(lambda: {"recorded_opens": 0, "customers": set(), "sources": Counter()})
    per_customer = defaultdict(lambda: {"recorded_opens": 0, "asins": Counter(), "sources": Counter(), "hours": Counter()})
    hours, current_ips = Counter(), defaultdict(lambda: {"recorded_opens": 0, "customers": set()})
    for e in events:
        uid = e["user_id"]
        asin = e["asin"] or "(unresolved link)"
        t = cairo_time(e["time"])
        hour = t.strftime("%H:00") if t else "unknown"
        hours[hour] += 1
        per_asin[asin]["recorded_opens"] += 1
        per_asin[asin]["customers"].add(uid)
        per_asin[asin]["sources"][e["source"]] += 1
        customer = per_customer[uid]
        customer["recorded_opens"] += 1
        customer["asins"][asin] += 1
        customer["sources"][e["source"]] += 1
        customer["hours"][hour] += 1
        ip = (users.get(uid) or {}).get("last_ip") or "(no IP saved)"
        current_ips[ip]["recorded_opens"] += 1
        current_ips[ip]["customers"].add(uid)

    customers = []
    for uid, c in per_customer.items():
        u = users.get(uid) or {}
        joined = cairo_time(u.get("joined_at"))
        customers.append({"user_id": uid, "username": u.get("username"),
                          "registered_cairo_day": joined.date().isoformat() if joined else None,
                          "latest_ip_at_export": u.get("last_ip"),
                          "recorded_opens": c["recorded_opens"], "distinct_asins": len(c["asins"]),
                          "asins": dict(c["asins"].most_common()), "sources": dict(c["sources"]),
                          "hours_cairo": dict(sorted(c["hours"].items()))})
    customers.sort(key=lambda c: (-c["recorded_opens"], c["user_id"]))
    answer_totals = Counter(int(a["user_id"]) for a in answers)
    correct_totals = Counter(int(a["user_id"]) for a in answers if a["was_correct"] == 1)
    spin_totals = Counter(int(s["user_id"]) for s in spins)
    for c in customers:
        c["answers_on_day"] = answer_totals[c["user_id"]]
        c["correct_answers_on_day"] = correct_totals[c["user_id"]]
        c["lucky_spins_on_day"] = spin_totals[c["user_id"]]
    registered = Counter(c["registered_cairo_day"] or "unknown" for c in customers)
    unanswered = sum(1 for x in q if not x["answered"])
    return {"date_cairo": day.isoformat(), "recorded_opens": len(events),
            "golden_first_opened_questions": len(q), "web_offer_click_events": len(web),
            "distinct_customers": len(customers), "distinct_products": len(per_asin),
            "unanswered_opened_questions": unanswered,
            "answers_on_day": len(answers), "correct_answers_on_day": sum(correct_totals.values()),
            "lucky_spins_on_day": len(spins),
            "repeat_opens_beyond_first_per_customer_and_product": sum(
                max(0, c["recorded_opens"] - len(c["asins"])) for c in per_customer.values()),
            "hourly_recorded_opens_cairo": dict(sorted(hours.items())),
            "registrations_of_opening_customers": dict(sorted(registered.items())),
            "products": {asin: {"recorded_opens": p["recorded_opens"],
                                 "distinct_customers": len(p["customers"]),
                                 "sources": dict(p["sources"])}
                         for asin, p in sorted(per_asin.items(), key=lambda x: (-x[1]["recorded_opens"], x[0]))},
            "latest_ip_groups_at_export": {ip: {"recorded_opens": p["recorded_opens"],
                                                 "distinct_customers": len(p["customers"])}
                                           for ip, p in sorted(current_ips.items(), key=lambda x: -x[1]["recorded_opens"])},
            "customers": customers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("DATABASE_PATH"), help="Path to the WafrCash SQLite database")
    parser.add_argument("--days", nargs=2, default=["2026-09-24", "2026-09-26"])
    parser.add_argument("--out", default="wafrcash_click_audit_2026-09-24_26.json")
    args = parser.parse_args()
    if not args.db:
        parser.error("Specify --db with the SQLite file path (or set DATABASE_PATH)")
    path = Path(args.db).resolve()
    if not path.is_file():
        parser.error(f"Database file not found: {path}")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        if not available(conn, "users"):
            parser.error("This SQLite file has no WafrCash users table")
        data = {"database": str(path), "generated_utc": datetime.now(timezone.utc).isoformat(),
                "note": "IP is the latest stored IP, not a historical click IP; golden first opens do not count repeated taps; Amazon earnings cannot be assigned to specific customers from this database.",
                "days": [report_day(conn, date.fromisoformat(d)) for d in args.days]}
    dest = Path(args.out).resolve()
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved: {dest}")
    for day in data["days"]:
        print(day["date_cairo"], "recorded opens:", day["recorded_opens"],
              "customers:", day["distinct_customers"], "products:", day["distinct_products"],
              "golden:", day["golden_first_opened_questions"], "web:", day["web_offer_click_events"])
        print("Top products:", list(day["products"].items())[:10])


if __name__ == "__main__":
    main()

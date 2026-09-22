# -*- coding: utf-8 -*-
"""Collect searchable Amazon Egypt products from Telegram posts.

Unlike the old ASIN-only collector, this keeps stable metadata (ASIN, product
name, source and links). It deliberately does not store price: the shopping bot
refreshes the current price from Amazon only after a local name match.

Required environment variables:
    TELEGRAM_API_ID
    TELEGRAM_API_HASH

Outputs:
    amazon_egypt_product_catalog.json
    collected_asins_eg.txt
    asin_link_cache.json
    collect_products_state.json
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from telethon import TelegramClient

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or 0)
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
SESSION = os.getenv("TELEGRAM_COLLECTOR_SESSION", "collect_products_session")

CATALOG_FILE = Path(os.getenv("AMAZON_PRODUCT_CATALOG", "amazon_egypt_product_catalog.json"))
ASIN_FILE = Path("collected_asins_eg.txt")
CACHE_FILE = Path("asin_link_cache.json")
STATE_FILE = Path("collect_products_state.json")

SHORT_DOMAINS = (
    "amzn.to", "amzn.eu", "a.co", "shorturl.at", "tinyurl.com", "bit.ly",
    "cutt.ly", "link.amazon", "amzn.asia", "a.y-ay.com", "y-ay.com",
)
LINK_RE = re.compile(r'https?://[^\s\]\)\[\(< >"\'\uFFFC]+')
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
AMAZON_DOMAINS = ("amazon.", "amzn", "link.amazon", "y-ay", "a.co")
NOISE = (
    "لينك", "الرابط", "كود الخصم", "كود", "اشتر", "اطلب", "اضغط", "انضم",
    "واتساب", "تليجرام", "telegram", "whatsapp", "السعر", "خصم", "وفر",
)

HTTP = requests.Session()
HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_connections=60, pool_maxsize=60))
HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124 Mobile Safari/537.36",
    "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7",
})
EXECUTOR = ThreadPoolExecutor(max_workers=40)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def atomic_json(path: Path, value) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def quick_asin(url: str) -> str | None:
    match = (
        re.search(r"/dp/([A-Z0-9]{10})", url, re.I)
        or re.search(r"/gp/product/([A-Z0-9]{10})", url, re.I)
        or re.search(r"\b(B0[A-Z0-9]{8})\b", url, re.I)
    )
    return match.group(1).upper() if match else None


def resolve_link(url: str) -> tuple[str | None, str]:
    final = url
    if any(domain in url.lower() for domain in SHORT_DOMAINS):
        for attempt in range(2):
            try:
                response = HTTP.get(url, allow_redirects=True, timeout=10, stream=True)
                final = response.url
                response.close()
                break
            except requests.RequestException:
                if attempt == 0:
                    time.sleep(0.4)
    return quick_asin(final), final


def amazon_links(text: str) -> list[str]:
    return [
        match.group(0).rstrip(".,،؛")
        for match in LINK_RE.finditer(text or "")
        if any(domain in match.group(0).lower() for domain in AMAZON_DOMAINS)
    ]


def product_title(text: str) -> str:
    """Pick a stable product-name line and ignore price/promo boilerplate."""
    without_links = LINK_RE.sub(" ", text or "")
    candidates: list[str] = []
    for raw in without_links.splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" -–—|:؛،👇🏻🔥✅⭐💥🎁🛒")
        if len(line) < 5 or len(line) > 350:
            continue
        normalized = line.lower()
        if any(word in normalized for word in NOISE):
            continue
        if re.fullmatch(r"[\W\d_]+", line, re.UNICODE):
            continue
        candidates.append(line)
    if not candidates:
        return ""
    # Product titles are normally the longest descriptive line in offer posts.
    return max(candidates, key=lambda value: (len(value.split()), len(value)))


def load_catalog() -> dict[str, dict]:
    raw = read_json(CATALOG_FILE, {})
    rows = raw.get("products", []) if isinstance(raw, dict) else []
    return {
        str(row.get("asin", "")).upper(): row
        for row in rows
        if isinstance(row, dict) and ASIN_RE.fullmatch(str(row.get("asin", "")).upper())
    }


def merge_product(catalog: dict[str, dict], asin: str, title: str, link: str,
                  source: str, observed_at: str) -> None:
    row = catalog.setdefault(asin, {
        "asin": asin,
        "title": "",
        "aliases": [],
        "brand": "",
        "category": "",
        "cachedLinks": [],
        "sources": [],
        "lastObservedAt": observed_at,
    })
    if title:
        previous = str(row.get("title") or "")
        aliases = [x for x in row.get("aliases", []) if isinstance(x, str)]
        if previous and previous != title and previous not in aliases:
            aliases.append(previous)
        if title != previous and title not in aliases:
            aliases.append(title)
        # Prefer a descriptive title, but avoid replacing it with promo essays.
        if not previous or (len(title.split()) > len(previous.split()) and len(title) <= 220):
            row["title"] = title
        row["aliases"] = aliases[-8:]
    if link and link not in row["cachedLinks"]:
        row["cachedLinks"].append(link)
        row["cachedLinks"] = row["cachedLinks"][-8:]
    if source and source not in row["sources"]:
        row["sources"].append(source)
    row["lastObservedAt"] = observed_at


def save_all(catalog: dict[str, dict], cache: dict[str, str | None], state: dict) -> None:
    searchable = sum(bool(row.get("title")) for row in catalog.values())
    payload = {
        "marketplace": "amazon.eg",
        "pricePolicy": "live-only",
        "productCount": len(catalog),
        "searchableProductCount": searchable,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "products": sorted(catalog.values(), key=lambda row: row["asin"]),
    }
    atomic_json(CATALOG_FILE, payload)
    atomic_json(CACHE_FILE, cache)
    atomic_json(STATE_FILE, state)
    temp = ASIN_FILE.with_suffix(".tmp")
    temp.write_text("".join(f"{asin}\n" for asin in sorted(catalog)), encoding="utf-8")
    os.replace(temp, ASIN_FILE)


async def collect(client: TelegramClient, sources: list[str]) -> None:
    catalog = load_catalog()
    cache = read_json(CACHE_FILE, {})
    state = read_json(STATE_FILE, {})
    pending: list[tuple[str, str, str, str]] = []
    added_before = len(catalog)

    for source in sources:
        key = source.lower()
        last_id = int(state.get(key, 0) or 0)
        max_id = last_id
        messages = 0
        print(f"\n📡 بقرا الجديد من {source}...")
        try:
            async for message in client.iter_messages(source, min_id=last_id):
                max_id = max(max_id, message.id)
                text = message.message or ""
                links = amazon_links(text)
                if not links:
                    continue
                messages += 1
                title = product_title(text)
                observed = (message.date or dt.datetime.now(dt.timezone.utc)).isoformat()
                for link in links:
                    asin = quick_asin(link)
                    if asin:
                        merge_product(catalog, asin, title, link, source, observed)
                    else:
                        pending.append((link, title, source, observed))
        except Exception as exc:
            print(f"⚠️ تعذر قراءة {source}: {str(exc)[:120]}")
            continue
        state[key] = max_id
        print(f"✅ {messages} بوست فيه منتجات")

    unique_pending = list(dict.fromkeys(pending))
    if unique_pending:
        print(f"🔓 بفك {len(unique_pending)} لينك مختصر...")
        loop = asyncio.get_running_loop()
        for offset in range(0, len(unique_pending), 40):
            batch = unique_pending[offset:offset + 40]
            tasks = []
            for link, *_ in batch:
                if link in cache:
                    tasks.append(asyncio.sleep(0, result=(cache[link], link)))
                else:
                    tasks.append(loop.run_in_executor(EXECUTOR, resolve_link, link))
            results = await asyncio.gather(*tasks)
            for (link, title, source, observed), (asin, final) in zip(batch, results):
                cache[link] = asin
                if asin:
                    merge_product(catalog, asin, title, link, source, observed)
            save_all(catalog, cache, state)
            print(f"   {min(offset + 40, len(unique_pending))}/{len(unique_pending)}")

    save_all(catalog, cache, state)
    searchable = sum(bool(row.get("title")) for row in catalog.values())
    print("\n" + "=" * 55)
    print(f"✅ إجمالي المنتجات: {len(catalog):,}")
    print(f"🔎 قابلة للبحث بالاسم: {searchable:,}")
    print(f"➕ الجديد في التشغيل ده: {len(catalog) - added_before:,}")
    print(f"💾 الكتالوج: {CATALOG_FILE}")


def ask_sources() -> list[str]:
    raw = input("📡 القنوات (مفصولة بمسافة): ").strip().replace(",", " ")
    sources = []
    for item in raw.split():
        name = item.split("t.me/")[-1].strip("/") if "t.me/" in item else item.lstrip("@")
        if name:
            sources.append("@" + name)
    return sources


async def main() -> None:
    if not API_ID or not API_HASH:
        raise RuntimeError("ضعي TELEGRAM_API_ID و TELEGRAM_API_HASH في متغيرات التشغيل")
    sources = ask_sources()
    if not sources:
        return
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    try:
        await collect(client, sources)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 تم الإيقاف")

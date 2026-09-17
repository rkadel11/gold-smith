#!/usr/bin/env python3
"""
gold-smith / fetch_prices.py

Runs 3x/day via GitHub Actions (10am / 2pm / 6pm Dubai time).
Fetches KT (Khaleej Times) and Kitco gold/silver rates, keeps the
running HIGH for each source for the current Dubai calendar day,
and writes it to data/current.json. On a new day, the previous
day's final numbers are archived into data/history/YYYY-MM.jsonl.

No third-party dependencies (urllib only) — keeps the Actions
workflow simple and avoids a pip-install failure point.
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime
from html import unescape as unescape_html
from zoneinfo import ZoneInfo

DUBAI_TZ = ZoneInfo("Asia/Dubai")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURRENT_PATH = os.path.join(REPO_ROOT, "data", "current.json")
HISTORY_DIR = os.path.join(REPO_ROOT, "data", "history")

KT_URL = "https://www.khaleejtimes.com/gold-forex"
OZ_TO_GRAMS = 31.1034768

# AED is pegged to USD by the Central Bank of UAE — fixed, not floating.
# No need to scrape an exchange rate for this.
AED_PER_USD = 3.6725

# Primary: Kitco's own page. Their Next.js frontend embeds the full
# server-rendered price state as JSON in a "__NEXT_DATA__" script tag
# (gold.results[0] / silver.results[0], each with bid/ask/mid/change/
# changePercentage) — a structured JSON parse, not scraping visible
# HTML for numbers. Confirmed live 2026-09-15.
KITCO_URL = "https://www.kitco.com/price/precious-metals"

# Fallback if Kitco's page structure ever changes underneath us.
# goldprice.org 403'd as bot traffic and metals.live's endpoint is
# dead — gold-api.com confirmed live 2026-09-08 (curl'd both
# endpoints, clean {"price": ...} response).
GOLD_API_XAU_URL = "https://api.gold-api.com/price/XAU"
GOLD_API_XAG_URL = "https://api.gold-api.com/price/XAG"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def _fetch_url(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "ignore")


def _kt_slot_values(rates: list, type_label: str) -> dict:
    """rates is the page's own list of {"type": ..., "morning": ...,
    "afternoon": ..., "evening": ..., "yesterday": ...} rows (silver
    rows have no "afternoon" key at all). Returns {"morning": float|
    None, "afternoon": float|None, "evening": float|None} for the row
    matching type_label, reading each slot's OWN labeled value instead
    of guessing which slot a single "latest" number belongs to."""
    row = next((r for r in rates if r.get("type") == type_label), None)
    out = {}
    for slot in SLOTS:
        raw = (row.get(slot) if row else "") or ""
        raw = raw.replace(",", "").strip()
        out[slot] = float(raw) if raw else None
    return out


def fetch_kt():
    """
    Fetch Khaleej Times 24K + 18K gold (Gms) and Silver Kilo (AED),
    per official morning/afternoon/evening slot.

    Parsed from the page's own embedded data-page JSON (an Inertia.js
    SSR payload: <div id="app" data-page="{...html-entity-encoded
    JSON...}">, same shape as Kitco's __NEXT_DATA__ below) rather than
    regex-scraping the rendered HTML table. The old regex grabbed
    "whichever numbers follow the karat label" and guessed the slot
    from our own run's wall-clock hour -- confirmed 2026-09-17 that
    this silently drifted a slot behind the page's actual labels (e.g.
    an early run capturing the still-blank "evening" cell's neighbor,
    which was really "yesterday"). Reading KT's own morning/afternoon/
    evening labels directly removes that guesswork entirely.

    Returns three dicts, each {"morning": float|None, "afternoon":
    float|None, "evening": float|None} -- KT's silver table has no
    afternoon column, so that key is always None there.
    """
    html = _fetch_url(KT_URL)

    m = re.search(r'data-page="(.*?)"', html, re.S)
    if not m:
        raise RuntimeError("KT: page structure changed (no data-page attribute found)")
    data = json.loads(unescape_html(m.group(1)))
    props = data.get("props", {})

    gold_rates = props.get("goldRates", {}).get("rates", [])
    silver_rates = props.get("silverRates", {}).get("rates", [])

    gold_24k = _kt_slot_values(gold_rates, "24K")
    gold_18k = _kt_slot_values(gold_rates, "18K")
    silver = _kt_slot_values(silver_rates, "Kilo (AED)")

    if not any(v is not None for v in gold_24k.values()):
        raise RuntimeError("KT: could not extract Gold 24K")
    if not any(v is not None for v in gold_18k.values()):
        raise RuntimeError("KT: could not extract Gold 18K")
    if not any(v is not None for v in silver.values()):
        raise RuntimeError("KT: could not extract Silver Kilo(AED)")

    return gold_24k, gold_18k, silver


def _fetch_kitco_page():
    """
    Pulls gold + silver USD/oz mid price directly from Kitco's own
    page JSON. One request gets both metals (unlike gold-api.com,
    which needs one call per metal).
    Returns (gold_usd_oz, silver_usd_oz).
    """
    html = _fetch_url(KITCO_URL)
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S,
    )
    if not m:
        raise RuntimeError("Kitco: page structure changed (no __NEXT_DATA__)")
    data = json.loads(m.group(1))
    queries = (
        data.get("props", {}).get("pageProps", {})
        .get("dehydratedState", {}).get("queries", [])
    )
    metals = next(
        (q["state"]["data"] for q in queries if "gold" in q.get("state", {}).get("data", {})),
        None,
    )
    if not metals:
        raise RuntimeError("Kitco: no gold/silver data found in page JSON")

    gold_mid = metals.get("gold", {}).get("results", [{}])[0].get("mid")
    silver_mid = metals.get("silver", {}).get("results", [{}])[0].get("mid")
    if not gold_mid or not silver_mid:
        raise RuntimeError("Kitco: gold/silver mid price missing")
    return float(gold_mid), float(silver_mid)


def _fetch_gold_api():
    """Fallback: gold-api.com, one request per metal."""
    def _fetch_price(url: str) -> float:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return float(data["price"])

    return _fetch_price(GOLD_API_XAU_URL), _fetch_price(GOLD_API_XAG_URL)


def fetch_kitco():
    """
    Gold + silver spot, AED-converted. Tries Kitco's own page first
    (see _fetch_kitco_page), falls back to gold-api.com if Kitco's
    page structure ever changes underneath us.

    Returns (gold_oz_aed: float, silver_oz_aed: float).
    """
    try:
        gold_usd_oz, silver_usd_oz = _fetch_kitco_page()
    except Exception as e:
        print(f"WARNING: Kitco page fetch failed ({e}), falling back to gold-api.com", file=sys.stderr)
        gold_usd_oz, silver_usd_oz = _fetch_gold_api()

    if not (1000 < gold_usd_oz < 8000):
        raise RuntimeError(f"Kitco: implausible gold price {gold_usd_oz}")
    if not (1 < silver_usd_oz < 200):
        raise RuntimeError(f"Kitco: implausible silver price {silver_usd_oz}")

    gold_oz_aed = gold_usd_oz * AED_PER_USD
    silver_oz_aed = silver_usd_oz * AED_PER_USD
    return gold_oz_aed, silver_oz_aed


def load_current():
    if not os.path.exists(CURRENT_PATH):
        return None
    with open(CURRENT_PATH, "r") as f:
        return json.load(f)


def archive_day(day_data: dict):
    """Append a finished day's data into its month's history file."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    day_date = datetime.strptime(day_data["date"], "%Y-%m-%d")
    month_file = os.path.join(HISTORY_DIR, f"{day_date.strftime('%Y-%m')}.jsonl")
    with open(month_file, "a") as f:
        f.write(json.dumps(day_data) + "\n")


SLOTS = ("morning", "afternoon", "evening")


def dubai_slot(now) -> str:
    """Classify a run into one of the 3 daily fetch windows by hour,
    tolerant of the run firing a few minutes late (GitHub Actions cron
    isn't exact). 10am/2pm/6pm Dubai -> morning/afternoon/evening."""
    h = now.hour
    if h < 12:
        return "morning"
    elif h < 16:
        return "afternoon"
    else:
        return "evening"


def blank_day(today_str: str) -> dict:
    return {
        "date": today_str,
        "last_updated": None,
        "gold": {
            "kheeljtimes_gms_24k": {"high": None, "last_seen": None},
            "kheeljtimes_gms_18k": {"high": None, "last_seen": None},
            "kitco_oz": {"high": None, "last_seen": None},
            "kitco_gms_24k": {"high": None, "last_seen": None},
        },
        "silver": {
            "kheeljtimes_kg": {"high": None, "last_seen": None},
            "kitco_kg": {"high": None, "last_seen": None},
        },
        # Additive alongside the "high" tracking above -- this is the
        # only place each of the 3 daily fetches survives as its own
        # correctly-timestamped reading (the "high" fields above only
        # ever show the day's running maximum, whose last_seen time can
        # drift away from the value it's attached to once a later,
        # lower reading comes in). Needed for the Numbers
        # Morning/Afternoon/Evening columns and daily Kitco open/close.
        "readings": {slot: None for slot in SLOTS},
    }


def update_high(day_data: dict, section: str, key: str, new_value: float, ts: str):
    # setdefault, not a direct index -- day_data may have been loaded
    # from an existing current.json written before this field existed
    # (confirmed 2026-09-15: adding kheeljtimes_gms_18k broke on an
    # in-progress day started under the old schema).
    entry = day_data[section].setdefault(key, {"high": None, "last_seen": None})
    if entry["high"] is None or new_value > entry["high"]:
        entry["high"] = new_value
    entry["last_seen"] = ts


def merge_reading(day_data: dict, slot: str, ts: str, gold: dict = None, silver: dict = None):
    """Sets only the given fields for this slot, leaving whatever else
    is already there (from a source this call doesn't cover, or a
    prior run) untouched -- e.g. KT can fill morning/afternoon/evening
    in one run while Kitco (a continuous spot price, no slots of its
    own) only ever touches the current wall-clock slot. Re-running the
    same window still just overwrites with the latest values, same as
    before."""
    day_data.setdefault("readings", {s: None for s in SLOTS})
    entry = day_data["readings"].get(slot) or {"time": ts, "gold": {}, "silver": {}}
    entry["time"] = ts
    entry.setdefault("gold", {})
    entry.setdefault("silver", {})
    if gold:
        entry["gold"].update(gold)
    if silver:
        entry["silver"].update(silver)
    day_data["readings"][slot] = entry


def update_kitco_open_close(day_data: dict):
    """Daily Kitco gold open/close, derived fresh from "readings" each
    run: opening = the day's earliest available slot (normally morning,
    but falls back to whichever ran first if an earlier slot was
    missed); closing = the most recent available slot -- so this keeps
    advancing through the day until evening's run makes it final."""
    readings = day_data.get("readings", {})
    available = [(s, readings[s]) for s in SLOTS if readings.get(s)]
    if not available:
        day_data["kitco_open_close"] = {"opening": None, "closing": None}
        return

    def _pick(slot, reading):
        return {
            "slot": slot,
            "time": reading["time"],
            "kitco_oz": reading["gold"]["kitco_oz"],
            "kitco_gms_24k": reading["gold"]["kitco_gms_24k"],
        }

    opening_slot, opening_reading = available[0]
    closing_slot, closing_reading = available[-1]
    day_data["kitco_open_close"] = {
        "opening": _pick(opening_slot, opening_reading),
        "closing": _pick(closing_slot, closing_reading),
    }


def main():
    now = datetime.now(DUBAI_TZ)
    today_str = now.strftime("%Y-%m-%d")
    ts = now.isoformat()

    existing = load_current()

    if existing is not None and existing.get("date") != today_str:
        # Day has rolled over — archive yesterday's final numbers, start fresh.
        archive_day(existing)
        existing = None

    day_data = existing if existing is not None else blank_day(today_str)

    errors = []

    kt_gold_24k = kt_gold_18k = kt_silver = {}
    try:
        kt_gold_24k, kt_gold_18k, kt_silver = fetch_kt()
        for slot_values, section, key in (
            (kt_gold_24k, "gold", "kheeljtimes_gms_24k"),
            (kt_gold_18k, "gold", "kheeljtimes_gms_18k"),
            (kt_silver, "silver", "kheeljtimes_kg"),
        ):
            for v in slot_values.values():
                if v is not None:
                    update_high(day_data, section, key, v, ts)
    except Exception as e:
        errors.append(f"KT fetch failed: {e}")

    kitco_gold_oz_aed = kitco_gold_gms = kitco_silver_kg = None
    try:
        kitco_gold_oz_aed, kitco_silver_oz_aed = fetch_kitco()
        kitco_gold_gms = kitco_gold_oz_aed / OZ_TO_GRAMS
        kitco_silver_kg = kitco_silver_oz_aed * (1000 / OZ_TO_GRAMS)
        update_high(day_data, "gold", "kitco_oz", kitco_gold_oz_aed, ts)
        update_high(day_data, "gold", "kitco_gms_24k", kitco_gold_gms, ts)
        update_high(day_data, "silver", "kitco_kg", kitco_silver_kg, ts)
    except Exception as e:
        errors.append(f"Kitco fetch failed: {e}")

    # KT publishes its own morning/afternoon/evening slots directly --
    # backfill whichever of those this fetch found values for,
    # regardless of what time our own run happens to be.
    for slot in SLOTS:
        gold = {}
        if kt_gold_24k.get(slot) is not None:
            gold["kheeljtimes_gms_24k"] = kt_gold_24k[slot]
        if kt_gold_18k.get(slot) is not None:
            gold["kheeljtimes_gms_18k"] = kt_gold_18k[slot]
        silver = {}
        if kt_silver.get(slot) is not None:
            silver["kheeljtimes_kg"] = kt_silver[slot]
        if gold or silver:
            merge_reading(day_data, slot, ts, gold=gold, silver=silver)

    # Kitco is a continuous spot price with no slot labels of its own,
    # so it still gets bucketed by this run's wall-clock time.
    if kitco_gold_oz_aed is not None:
        merge_reading(
            day_data, dubai_slot(now), ts,
            gold={"kitco_oz": kitco_gold_oz_aed, "kitco_gms_24k": kitco_gold_gms},
            silver={"kitco_kg": kitco_silver_kg},
        )

    update_kitco_open_close(day_data)

    day_data["last_updated"] = ts

    os.makedirs(os.path.dirname(CURRENT_PATH), exist_ok=True)
    with open(CURRENT_PATH, "w") as f:
        json.dump(day_data, f, indent=2)

    print(json.dumps(day_data, indent=2))

    if errors:
        for e in errors:
            print(f"WARNING: {e}", file=sys.stderr)
        # Don't hard-fail the whole run if one source is down —
        # partial data (e.g. KT only) still updates current.json.
        # Remove this if you'd rather the Action show as failed.


if __name__ == "__main__":
    main()

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
import time
import urllib.request
from datetime import datetime
from html import unescape as unescape_html
from zoneinfo import ZoneInfo

DUBAI_TZ = ZoneInfo("Asia/Dubai")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CURRENT_PATH = os.path.join(REPO_ROOT, "data", "current.json")
HISTORY_DIR = os.path.join(REPO_ROOT, "data", "history")

# Raw, unreconciled 24K/18K readings from all three retail sources,
# one line per run -- kept separate from current.json (which only ever
# holds ONE reconciled value per slot) so we have a record to look
# back on. Started 2026-09-18 to empirically check, over ~a week of
# 2-hourly runs, which two sources actually agree with each other most
# often, rather than assuming it from a single day's snapshot.
SOURCE_COMPARISON_PATH = os.path.join(REPO_ROOT, "data", "source_comparison.jsonl")

KT_URL = "https://www.khaleejtimes.com/gold-forex"
OZ_TO_GRAMS = 31.1034768

# Cross-check sources for KT's retail 24K/18K AED/gram rate. Confirmed
# 2026-09-18: KT briefly showed 522.75 for 24K while Gulf News and
# Dubai City of Gold both independently showed 526.25 at the same
# time -- and KT has gone silent for up to a week before. Rather than
# trust KT alone, GOLD_24K/18K use a 2-of-3 majority across these three
# (see reconcile_retail_gold()); only KT still supplies the
# morning/afternoon/evening slot structure and silver.
GULF_NEWS_URL = "https://gulfnews.com/gold-forex"
DUBAI_CITY_OF_GOLD_URL = "https://dubaicityofgold.com/"

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
    # Cache-bust every request -- confirmed 2026-09-18 that
    # khaleejtimes.com's CDN was serving a snapshot frozen over 2
    # hours stale (identical timestamp and values across repeated
    # fetches spanning real elapsed time) without this. No amount of
    # polling more often helps if every request just hits the same
    # cached copy. A unique query param plus no-cache headers reliably
    # got a fresh response in testing; applied here so every source
    # (KT, Gulf News, Dubai City of Gold, Kitco) gets it via the one
    # shared helper.
    sep = "&" if "?" in url else "?"
    busted_url = f"{url}{sep}_={int(time.time())}"
    headers = {**HEADERS, "Cache-Control": "no-cache", "Pragma": "no-cache"}
    req = urllib.request.Request(busted_url, headers=headers)
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
    float|None, "evening": float|None}. KT's silver table has no
    afternoon column at all (only morning/evening) -- so silver's
    "afternoon" is filled in by carrying the morning figure forward
    (see below), confirming silver *was* checked that run rather than
    leaving the cell blank and indistinguishable from "never checked".
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

    # KT never publishes an "afternoon" silver rate -- carry morning's
    # figure forward so the afternoon check still records something
    # (same price = confirmed unchanged, not "we didn't look"). Only
    # silver needs this: gold genuinely has all three slots published.
    if silver.get("afternoon") is None and silver.get("morning") is not None:
        silver["afternoon"] = silver["morning"]

    if not any(v is not None for v in gold_24k.values()):
        raise RuntimeError("KT: could not extract Gold 24K")
    if not any(v is not None for v in gold_18k.values()):
        raise RuntimeError("KT: could not extract Gold 18K")
    if not any(v is not None for v in silver.values()):
        raise RuntimeError("KT: could not extract Silver Kilo(AED)")

    return gold_24k, gold_18k, silver


def _extract_json_value(html: str, key: str):
    """Finds `"key":` in raw HTML and decodes the JSON value that
    follows, using json's own decoder to find the matching closing
    brace/bracket instead of a regex -- the goldApiData blob below has
    nested objects (per-country rates), so a naive non-greedy regex
    would stop at the first inner "}" instead of the real end."""
    marker = f'"{key}":'
    idx = html.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    value, _ = json.JSONDecoder().raw_decode(html, start)
    return value


def fetch_gulfnews_gold():
    """Cross-check source: Gulf News embeds a goldApiData JSON blob
    with 24K/18K AED/gram rates. It DOES fill in morning/afternoon/
    evening keys progressively through the day like KT, contrary to
    what was assumed when this was written -- confirmed 2026-09-18
    that reading only "morning" silently returned a stale value once
    "afternoon" had been published (529.75 instead of the actual
    528.75). Prefer the latest of evening/afternoon/morning that's
    actually present, same priority order used elsewhere (SLOTS).
    Returns (gold_24k, gold_18k), each float or None if unavailable."""
    try:
        html = _fetch_url(GULF_NEWS_URL)
        data = _extract_json_value(html, "goldApiData")
        if not data:
            raise RuntimeError("no goldApiData in page")

        def _latest(carat_data):
            for slot in ("evening", "afternoon", "morning"):
                v = carat_data.get(slot)
                if v not in (None, ""):
                    return float(v)
            return None

        return _latest(data.get("carat24", {})), _latest(data.get("carat18", {}))
    except Exception as e:
        print(f"WARNING: Gulf News fetch failed ({e})", file=sys.stderr)
        return None, None


def fetch_dubaicityofgold_gold():
    """Cross-check source: Dubai City of Gold server-renders today's
    rate directly in the page HTML (no JS execution needed) as
    <span class="sortd-gold-type">24K Gold</span><span
    class="sortd-gold-value">AED 526.25</span>. Returns (gold_24k,
    gold_18k), each float or None if unavailable."""
    try:
        html = _fetch_url(DUBAI_CITY_OF_GOLD_URL)

        def _extract(karat_label):
            m = re.search(
                rf'{karat_label} Gold</span>\s*<span class="sortd-gold-value">'
                rf'AED\s*([\d.]+)</span>',
                html,
            )
            return float(m.group(1)) if m else None

        return _extract("24K"), _extract("18K")
    except Exception as e:
        print(f"WARNING: Dubai City of Gold fetch failed ({e})", file=sys.stderr)
        return None, None


def reconcile_retail_gold(label: str, candidates: list):
    """candidates: [(source_name, value_or_None), ...]. Picks the
    value at least 2 of the 3 sources agree on (rounded to 2dp, AED
    cents). Falls back to the first available candidate (KT preferred,
    since callers list it first) if fewer than 2 sources agree --
    logging why, so a real 3-way split or a lone source is visible
    rather than silently trusted. Returns (value_or_None,
    warning_or_None)."""
    present = [(name, round(v, 2)) for name, v in candidates if v is not None]
    if not present:
        return None, None
    if len(present) == 1:
        return present[0][1], None

    counts = {}
    for _, v in present:
        counts[v] = counts.get(v, 0) + 1
    value, n = max(counts.items(), key=lambda kv: kv[1])
    if n >= 2:
        outliers = [f"{name}={v}" for name, v in present if v != value]
        warning = f"{label}: {value} confirmed by {n}/3, outlier(s) {outliers}" if outliers else None
        return value, warning

    return present[0][1], f"{label}: no 2/3 consensus, sources split {present}"


def log_source_comparison(ts: str, slot: str, kt_24, kt_18, gn_24, gn_18, dcg_24, dcg_18):
    """Appends this run's raw, unreconciled 24K/18K reading from each
    source to SOURCE_COMPARISON_PATH -- one line, never overwritten.
    Purely observational: doesn't feed back into current.json, so a
    logging failure here should never break the actual price update.
    """
    try:
        os.makedirs(os.path.dirname(SOURCE_COMPARISON_PATH), exist_ok=True)
        row = {
            "time": ts,
            "slot": slot,
            "kt_24k": kt_24, "kt_18k": kt_18,
            "gulfnews_24k": gn_24, "gulfnews_18k": gn_18,
            "dubaicityofgold_24k": dcg_24, "dubaicityofgold_18k": dcg_18,
        }
        with open(SOURCE_COMPARISON_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"WARNING: could not write source comparison log ({e})", file=sys.stderr)


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
        # .get(), not direct indexing -- a slot's reading may have been
        # merged from KT alone (Kitco's fetch failed, or hasn't touched
        # this slot yet), so "gold" may not have kitco_oz/kitco_gms_24k
        # at all. Same class of bug as update_high()'s .setdefault() fix.
        gold = reading.get("gold", {})
        return {
            "slot": slot,
            "time": reading["time"],
            "kitco_oz": gold.get("kitco_oz"),
            "kitco_gms_24k": gold.get("kitco_gms_24k"),
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
    except Exception as e:
        errors.append(f"KT fetch failed: {e}")

    # Reconcile KT's 24K/18K for THIS run's slot against two independent
    # cross-checks (Gulf News, Dubai City of Gold) -- 2-of-3 agreement
    # wins, overriding KT alone when it's the outlier, and still
    # producing a value from GN+DCG even if KT's fetch failed entirely
    # (KT has gone silent for up to a week before). Only the current
    # slot is touched; slots from earlier runs today are untouched.
    current_slot = dubai_slot(now)
    kt_24_raw = kt_gold_24k.get(current_slot)
    kt_18_raw = kt_gold_18k.get(current_slot)
    gn_24, gn_18 = fetch_gulfnews_gold()
    dcg_24, dcg_18 = fetch_dubaicityofgold_gold()

    log_source_comparison(ts, current_slot, kt_24_raw, kt_18_raw, gn_24, gn_18, dcg_24, dcg_18)

    consensus_24, warn_24 = reconcile_retail_gold(
        "Gold 24K", [("KT", kt_24_raw), ("GulfNews", gn_24), ("DubaiCityOfGold", dcg_24)]
    )
    consensus_18, warn_18 = reconcile_retail_gold(
        "Gold 18K", [("KT", kt_18_raw), ("GulfNews", gn_18), ("DubaiCityOfGold", dcg_18)]
    )
    for warn in (warn_24, warn_18):
        if warn:
            print(f"WARNING: {warn}", file=sys.stderr)
    if consensus_24 is not None:
        kt_gold_24k = {**kt_gold_24k, current_slot: consensus_24}
    if consensus_18 is not None:
        kt_gold_18k = {**kt_gold_18k, current_slot: consensus_18}

    for slot_values, section, key in (
        (kt_gold_24k, "gold", "kheeljtimes_gms_24k"),
        (kt_gold_18k, "gold", "kheeljtimes_gms_18k"),
        (kt_silver, "silver", "kheeljtimes_kg"),
    ):
        for v in slot_values.values():
            if v is not None:
                update_high(day_data, section, key, v, ts)

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

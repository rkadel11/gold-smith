#!/usr/bin/env python3
"""
gold-smith / update_p1_prices.py

LOCAL Mac-only script -- NOT part of the GitHub Actions workflow.
Updates the rolling price window in Raj's jewelry price list
("P1 2.0 .numbers", sheet "Price List") from GitHub's price data:

  Gold Price   table: latest 15 calendar days
  Silver Price table: latest 7 calendar days

Both tables use the exact same column layout as the "Gold Price"/
"Silver Price" tables in gold silver price history.numbers (confirmed
2026-09-15 -- same headers, same formula convention), so this script
reuses that schema and formula logic directly.

Unlike sync_history_numbers.py (which upserts by date into an
ever-growing per-year archive), this is a fixed-size rolling window:
every run takes the N most recent days with usable data and rewrites
rows 3..(3+N-1) top-to-bottom, NEWEST FIRST (Raj's preference, confirmed
2026-09-15 -- opposite of the history file's oldest-first convention),
overwriting whatever was there. No date-matching/insertion logic needed
-- old days simply fall out of the window on their own as new ones push
them out. Rows beyond the currently available day count are left as-is
(harmless blanks) until enough days accumulate to fill the window.

Two ways this runs:
  1. Manual "Update Prices" button -- a Shortcut pastes
     p1_shortcut_run_shell_script.sh's content into a Run Shell Script
     action, triggerable from any device via its shortcuts:// URL.
  2. Auto-update on open -- see scripts/p1_autoupdate_watch.py, a
     poller (via a LaunchAgent) that detects the moment "P1 2.0
     .numbers" newly appears among Numbers' open documents and runs
     this script once per open (Mac-only, best-effort -- see HANDOFF.md
     on why there's no reliable iPad equivalent).

IMPORTANT: like the history sync, this only reads whatever GitHub's
own fetch_prices.py has already written -- it does not fetch prices
itself. Safe to run any time, as often as you like.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

REPO = "rkadel11/gold-smith"
API_HISTORY_URL = f"https://api.github.com/repos/{REPO}/contents/data/history"
RAW_CURRENT_URL = f"https://raw.githubusercontent.com/{REPO}/main/data/current.json"

DOC_PATH = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~Numbers/Documents/P1 2.0 .numbers"
)
DOC_FILENAME = "P1 2.0 .numbers"
SHEET_NAME = "Price List"

LOG_PATH = os.path.expanduser("~/Library/Logs/p1_price_update.log")
DUBAI = ZoneInfo("Asia/Dubai")

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20
APPLESCRIPT_TIMEOUT = 300

SLOTS = ("morning", "afternoon", "evening")

GOLD_TABLE = "Gold Price"
GOLD_WINDOW_DAYS = 15
GOLD_PRICE_COLUMNS = {
    2: "kt_morning_gold", 3: "kt_afternoon_gold", 4: "kt_evening_gold",
    5: "kitco_morning_oz",   # 6 = formula (Gms)
    7: "kitco_afternoon_oz",  # 8 = formula (Gms)
    9: "kitco_evening_oz",    # 10 = formula (Gms)
}
GOLD_PRICE_FORMULAS = {6: 5, 8: 7, 10: 9}

SILVER_TABLE = "Silver Price"
SILVER_WINDOW_DAYS = 7
SILVER_PRICE_COLUMNS = {
    2: "kt_morning_silver", 3: "kt_afternoon_silver", 4: "kt_evening_silver",
    5: "kitco_morning_silver", 6: "kitco_afternoon_silver", 7: "kitco_evening_silver",
}


def column_letter(n: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return letters[n - 1]


def log(msg: str):
    ts = datetime.now(DUBAI).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def fetch_text(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode()


def extract_row_data(day: dict):
    """day -> {field: value}, or None if this day predates the
    readings schema (2026-09-14) and has nothing usable."""
    readings = day.get("readings") or {}
    if not any(readings.get(s) for s in SLOTS):
        return None

    def g(slot_name, *path):
        r = readings.get(slot_name)
        if not r:
            return None
        node = r
        for key in path:
            node = node.get(key) if node else None
        return node

    return {
        "kt_morning_gold": g("morning", "gold", "kheeljtimes_gms_24k"),
        "kt_afternoon_gold": g("afternoon", "gold", "kheeljtimes_gms_24k"),
        "kt_evening_gold": g("evening", "gold", "kheeljtimes_gms_24k"),
        "kitco_morning_oz": g("morning", "gold", "kitco_oz"),
        "kitco_afternoon_oz": g("afternoon", "gold", "kitco_oz"),
        "kitco_evening_oz": g("evening", "gold", "kitco_oz"),
        "kt_morning_silver": g("morning", "silver", "kheeljtimes_kg"),
        "kt_afternoon_silver": g("afternoon", "silver", "kheeljtimes_kg"),
        "kt_evening_silver": g("evening", "silver", "kheeljtimes_kg"),
        "kitco_morning_silver": g("morning", "silver", "kitco_kg"),
        "kitco_afternoon_silver": g("afternoon", "silver", "kitco_kg"),
        "kitco_evening_silver": g("evening", "silver", "kitco_kg"),
    }


def collect_all_days() -> dict:
    """-> {date_str: row_data}, skipping any day with nothing usable."""
    out = {}
    listing = fetch_json(API_HISTORY_URL)
    for item in listing:
        if item.get("name", "").endswith(".jsonl"):
            text = fetch_text(item["download_url"])
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                day = json.loads(line)
                row = extract_row_data(day)
                if row:
                    out[day["date"]] = row
    try:
        current = fetch_json(RAW_CURRENT_URL)
        row = extract_row_data(current)
        if row:
            out[current["date"]] = row
    except Exception as e:
        log(f"WARNING: could not fetch current.json: {e}")
    return out


def run_applescript(script_text: str, _retries: int = 2) -> str:
    fd, path = tempfile.mkstemp(suffix=".applescript")
    os.close(fd)
    try:
        with open(path, "w") as f:
            f.write(script_text)
        result = subprocess.run(
            ["osascript", path],
            capture_output=True, text=True, timeout=APPLESCRIPT_TIMEOUT,
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            if "-1712" in err and _retries > 0:
                import time
                time.sleep(3)
                return run_applescript(script_text, _retries - 1)
            raise RuntimeError(err)
        return result.stdout.strip()
    finally:
        os.unlink(path)


def open_doc_preamble() -> str:
    return f'''
tell application "Numbers"
    set targetFilename to "{DOC_FILENAME}"
    set theDoc to missing value
    repeat with d in documents
        try
            if (file of d as text) contains targetFilename then
                set theDoc to d
                exit repeat
            end if
        end try
    end repeat
    if theDoc is missing value then
        open POSIX file "{DOC_PATH}"
        delay 2
        repeat with d in documents
            try
                if (file of d as text) contains targetFilename then
                    set theDoc to d
                    exit repeat
                end if
            end try
        end repeat
        if theDoc is missing value then
            error "SAFETY ABORT: opened document does not match target file"
        end if
    end if
end tell
'''


def is_doc_open() -> bool:
    script = f'''
tell application "Numbers"
    repeat with d in documents
        try
            if (file of d as text) contains "{DOC_FILENAME}" then
                return "OPEN"
            end if
        end try
    end repeat
    return "CLOSED"
end tell
'''
    return run_applescript(script) == "OPEN"


def write_window(table_name: str, rows: list, column_map: dict, formula_map: dict) -> int:
    """
    rows: [(date_str, row_data), ...] sorted ascending, already trimmed
    to the window size. Rewrites rows 3..(3+len(rows)-1) unconditionally.
    """
    if not rows:
        return 0

    lines = []
    for i, (date_str, row_data) in enumerate(rows):
        r = 3 + i
        lines.append(f'                set value of cell 1 of row {r} to "{date_str}"')
        for col_idx, field in column_map.items():
            val = row_data.get(field)
            if val is not None:
                lines.append(f'                set value of cell {col_idx} of row {r} to {val}')
            else:
                # Explicitly clear rather than skip -- confirmed
                # 2026-09-18: this is a FIXED-SIZE rolling window, so
                # row 3 is reused by a different calendar day every
                # run. Skipping an unwritten cell here doesn't leave it
                # blank -- it leaves whatever the PREVIOUS occupant of
                # this row position had (e.g. yesterday's Evening Gold
                # value showing up under today's row once today became
                # row 3, because today's evening genuinely hasn't been
                # published by KT yet). current.json/history is the
                # source of truth every run, so None here should
                # always mean "show blank", never "leave whatever was
                # there from the last day that occupied this row".
                #
                # An empty string, NOT `missing value` -- confirmed
                # 2026-09-18: the "Metal Pricing" sheet's formulas
                # check `cell <> ""` to detect "not yet published" and
                # fall back to an earlier slot. A Number-column cell
                # cleared via `missing value` doesn't reliably equal
                # the TEXT "" in that comparison (reads as
                # blank-as-zero, so `<> ""` can wrongly evaluate
                # true), which was producing $0.00 instead of falling
                # back correctly to Afternoon/Morning.
                lines.append(f'                set value of cell {col_idx} of row {r} to ""')
        for formula_col, source_col in formula_map.items():
            letter = column_letter(source_col)
            lines.append(f'                set value of cell {formula_col} of row {r} to "={letter}{r}/31.1034768"')

    body = "\n".join(lines)
    script = open_doc_preamble() + f'''
tell application "Numbers"
    tell theDoc
        tell sheet "{SHEET_NAME}"
            tell table "{table_name}"
{body}
            end tell
        end tell
        save theDoc
    end tell
end tell
'''
    run_applescript(script)
    return len(rows)


def main():
    log("=== update_p1_prices.py starting ===")
    try:
        all_days = collect_all_days()
    except Exception as e:
        log(f"FATAL: could not fetch data from GitHub: {e}")
        sys.exit(1)

    if not all_days:
        log("Nothing usable to sync (no days with readings data yet).")
        log("=== update_p1_prices.py done ===")
        return

    sorted_dates = sorted(all_days)  # ascending, so slicing grabs the newest N
    gold_window = [(d, all_days[d]) for d in reversed(sorted_dates[-GOLD_WINDOW_DAYS:])]
    silver_window = [(d, all_days[d]) for d in reversed(sorted_dates[-SILVER_WINDOW_DAYS:])]

    try:
        n = write_window(GOLD_TABLE, gold_window, GOLD_PRICE_COLUMNS, GOLD_PRICE_FORMULAS)
        log(f"'{GOLD_TABLE}': wrote {n} day(s) (window={GOLD_WINDOW_DAYS}).")
    except Exception as e:
        log(f"ERROR: failed updating '{GOLD_TABLE}': {e}")

    try:
        n = write_window(SILVER_TABLE, silver_window, SILVER_PRICE_COLUMNS, {})
        log(f"'{SILVER_TABLE}': wrote {n} day(s) (window={SILVER_WINDOW_DAYS}).")
    except Exception as e:
        log(f"ERROR: failed updating '{SILVER_TABLE}': {e}")

    log("=== update_p1_prices.py done ===")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
gold-smith / sync_history_numbers.py

LOCAL Mac-only script -- NOT part of the GitHub Actions workflow.
Meant to be pasted into a single "Run Shell Script" action inside a
macOS Shortcut, triggered TWICE DAILY (~2:30pm and ~6:15pm Dubai -- a
few minutes after that day's 2pm and 6pm GitHub Actions fetches, so
current.json's morning/afternoon(/evening) readings are already in)
via two Personal Automations on the Mac Mini (24/7), added 2026-09-17
so the afternoon reading gets saved the same day rather than only
showing up in that evening's run. Safe to trigger more often than
that too -- upserting by date means a run just overwrites the day's
row with whatever's newest, so an extra run is a no-op, not a
duplicate.

Writes into "gold silver price history.numbers", one sheet per year,
ONE ROW PER CALENDAR DAY across three tables:

  Gold Price   (10 cols): Date | KT Morning/Afternoon/Evening Gold
                          | Kitco Morning/Afternoon/Evening OZ+Gms(formula)
  Silver Price (7 cols):  Date | KT Morning/Afternoon/Evening Silver
                          | Kitco Morning/Afternoon/Evening Silver
  Kitco Opening & Closing (5 cols): Date | Opening OZ+Gms(formula)
                          | Closing OZ+Gms(formula)

Source data: GitHub's data/current.json "readings" block (one entry
per fetch window: morning/afternoon/evening, added 2026-09-14) and its
derived "kitco_open_close" field. Rows are upserted by date -- each
run overwrites whatever's already there for that date with the latest
known values, so it's safe to re-run any time.

IMPORTANT LIMITATION: the "readings"/"kitco_open_close" fields did not
exist before 2026-09-14 -- any archived day in data/history/*.jsonl
from before that date has none of this data (only the old "high"/
"last_seen" fields, which don't map onto these tables) and is simply
skipped, not backfilled. There is no way to reconstruct it after the
fact. Every day from 2026-09-14 onward has full data.

Year-sheet routing: each row's year (YYYY) must have a matching sheet
name in the document. Sheets are NOT auto-created -- Numbers'
AppleScript dictionary rejects "duplicate" on rows, sheets, AND tables
alike (-1717 "can not be copied", confirmed 2026-09-14). Pre-create
sheets for future years manually in the Numbers UI (duplicate the
"Template" sheet, rename the copy) with some headroom. If a year's
sheet is missing, its rows are skipped and a warning is logged.
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
    "~/Library/Mobile Documents/com~apple~Numbers/Documents/gold silver price history.numbers"
)
DOC_FILENAME = "gold silver price history.numbers"

LOG_PATH = os.path.expanduser("~/Library/Logs/gold_silver_history_sync.log")
DUBAI = ZoneInfo("Asia/Dubai")

MIN_HEADROOM_YEARS = 1
HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20
APPLESCRIPT_TIMEOUT = 480

SLOTS = ("morning", "afternoon", "evening")

# Column layout for each table. Keys are 1-based column indices;
# values are the row_data field each column pulls from. A column
# absent from this map is either the Date column (always col 1) or a
# native formula column that must never be script-written.
GOLD_PRICE_COLUMNS = {
    2: "kt_morning_gold", 3: "kt_afternoon_gold", 4: "kt_evening_gold",
    5: "kitco_morning_oz",   # 6 = formula (Gms)
    7: "kitco_afternoon_oz",  # 8 = formula (Gms)
    9: "kitco_evening_oz",    # 10 = formula (Gms)
}
SILVER_PRICE_COLUMNS = {
    2: "kt_morning_silver", 3: "kt_afternoon_silver", 4: "kt_evening_silver",
    5: "kitco_morning_silver", 6: "kitco_afternoon_silver", 7: "kitco_evening_silver",
}
OPEN_CLOSE_COLUMNS = {
    2: "opening_oz",   # 3 = formula (Gms)
    4: "closing_oz",   # 5 = formula (Gms)
}

# Native formula columns per table: {formula_col: source_oz_col}. Set
# defensively on every row this script touches (new or existing) --
# this asserts the FORMULA TEXT, never a computed number, so it can't
# violate the "never script-write a value into this column" rule while
# still self-healing any row a table-rebuild forgot to seed (confirmed
# 2026-09-14: freshly-created blank rows don't inherit a formula that
# was only ever set on row 3, since there's no established column
# pattern yet for Numbers to extend).
GOLD_PRICE_FORMULAS = {6: 5, 8: 7, 10: 9}
OPEN_CLOSE_FORMULAS = {3: 2, 5: 4}

TABLES = [
    ("Gold Price", GOLD_PRICE_COLUMNS, GOLD_PRICE_FORMULAS),
    ("Silver Price", SILVER_PRICE_COLUMNS, {}),
    ("Kitco Opening & Closing", OPEN_CLOSE_COLUMNS, OPEN_CLOSE_FORMULAS),
]


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
    """day -> {field: value} for one calendar day, or None if this day
    predates the readings/kitco_open_close schema (2026-09-14) and so
    has nothing usable for these tables."""
    readings = day.get("readings") or {}
    oc = day.get("kitco_open_close") or {}
    if not any(readings.get(s) for s in SLOTS) and not oc.get("opening"):
        return None

    def slot(name):
        return readings.get(name)

    def g(slot_name, *path):
        r = slot(slot_name)
        if not r:
            return None
        node = r
        for key in path:
            node = node.get(key) if node else None
        return node

    opening = oc.get("opening")
    closing = oc.get("closing")

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
        "opening_oz": opening["kitco_oz"] if opening else None,
        "closing_oz": closing["kitco_oz"] if closing else None,
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
            # AppleEvent timeouts (-1712) are transient -- Numbers can
            # briefly stop responding under back-to-back automation.
            # Worth one quiet retry before treating it as a real
            # failure, since a genuine failure will just recur.
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
        -- Re-scan by path rather than assuming "document 1" -- if any
        -- other Numbers document was already open, the newly-opened
        -- one is NOT guaranteed to be index 1 (confirmed 2026-09-14:
        -- this exact assumption silently targeted the wrong document).
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


def list_sheets() -> list:
    script = open_doc_preamble() + '''
tell application "Numbers"
    tell theDoc
        return name of every sheet
    end tell
end tell
'''
    out = run_applescript(script)
    return [s.strip() for s in out.split(",")] if out else []


def read_existing_dates(sheet_name: str, table_name: str) -> list:
    """-> list of {"row": n, "blank": True} or {"row": n, "y","m","d"},
    in row order starting at row 3 (rows 1/2 are headers)."""
    script = open_doc_preamble() + f'''
tell application "Numbers"
    tell theDoc
        tell sheet "{sheet_name}"
            tell table "{table_name}"
                set rc to row count
                set outList to {{}}
                repeat with r from 3 to rc
                    set d to value of cell 1 of row r
                    if d is missing value then
                        set end of outList to "BLANK"
                    else
                        set y to year of d as string
                        set mo to (month of d) as integer as string
                        set dy to day of d as string
                        set end of outList to (y & "-" & mo & "-" & dy)
                    end if
                end repeat
            end tell
        end tell
    end tell
end tell
set AppleScript's text item delimiters to "|"
return outList as string
'''
    out = run_applescript(script)
    raw = out.split("|") if out else []
    rows = []
    for i, item in enumerate(raw):
        row_num = i + 3
        if item == "BLANK":
            rows.append({"row": row_num, "blank": True})
        else:
            y, m, d = (int(x) for x in item.split("-"))
            rows.append({"row": row_num, "y": y, "m": m, "d": d})
    return rows


def sync_table(sheet_name: str, table_name: str, day_map: dict, column_map: dict,
                formula_map: dict = None) -> int:
    """
    day_map: {date_str: row_data} for this sheet's year.
    column_map: {col_index: field_name}.
    formula_map: {formula_col: source_col} -- asserted on every row
    touched (see the note above TABLES for why this is safe to redo
    every time rather than only on newly-created rows).
    Upserts one row per date -- overwrites an existing row's mapped
    columns, or inserts a new one (reusing a blank template row, or
    positionally via add row above/below to keep chronological order).
    Returns the number of rows touched.
    """
    formula_map = formula_map or {}
    if not day_map:
        return 0

    existing = read_existing_dates(sheet_name, table_name)
    slots = []
    for r in existing:
        if r.get("blank"):
            slots.append({"row": r["row"], "blank": True})
        else:
            slots.append({"row": r["row"], "blank": False, "y": r["y"], "m": r["m"], "d": r["d"]})

    lines = []
    last_row_num = slots[-1]["row"] if slots else 2
    touched = 0

    for date_str in sorted(day_map):
        y, m, d = (int(x) for x in date_str.split("-"))
        row_data = day_map[date_str]

        match_idx = next(
            (i for i, s in enumerate(slots) if not s["blank"] and (s["y"], s["m"], s["d"]) == (y, m, d)),
            None,
        )

        if match_idx is not None:
            r = slots[match_idx]["row"]
        else:
            insert_before = next(
                (i for i, s in enumerate(slots) if not s["blank"] and (s["y"], s["m"], s["d"]) > (y, m, d)),
                None,
            )
            if insert_before is None:
                blank_idx = next((i for i, s in enumerate(slots) if s["blank"]), None)
                if blank_idx is not None:
                    r = slots[blank_idx]["row"]
                    slots[blank_idx] = {"row": r, "blank": False, "y": y, "m": m, "d": d}
                else:
                    last_row_num += 1
                    r = last_row_num
                    lines.append(f'                add row below row {r - 1}')
                    slots.append({"row": r, "blank": False, "y": y, "m": m, "d": d})
            else:
                r = slots[insert_before]["row"]
                lines.append(f'                add row above row {r}')
                for s in slots:
                    if s["row"] >= r:
                        s["row"] += 1
                if last_row_num >= r:
                    last_row_num += 1
                slots.insert(insert_before, {"row": r, "blank": False, "y": y, "m": m, "d": d})
            lines.append(f'                set value of cell 1 of row {r} to "{date_str}"')

        for col_idx, field in column_map.items():
            val = row_data.get(field)
            if val is not None:
                lines.append(f'                set value of cell {col_idx} of row {r} to {val}')
        for formula_col, source_col in formula_map.items():
            letter = column_letter(source_col)
            lines.append(f'                set value of cell {formula_col} of row {r} to "={letter}{r}/31.1034768"')
        touched += 1

    if not lines:
        return 0

    body = "\n".join(lines)
    script = open_doc_preamble() + f'''
tell application "Numbers"
    tell theDoc
        tell sheet "{sheet_name}"
            tell table "{table_name}"
{body}
            end tell
        end tell
        save theDoc
    end tell
end tell
'''
    run_applescript(script)
    return touched


def close_doc():
    script = open_doc_preamble() + '''
tell application "Numbers"
    close theDoc saving no
end tell
'''
    run_applescript(script)


def main():
    log("=== sync_history_numbers.py starting ===")
    try:
        all_days = collect_all_days()
    except Exception as e:
        log(f"FATAL: could not fetch data from GitHub: {e}")
        sys.exit(1)

    if not all_days:
        log("Nothing usable to sync (no days with readings/open-close data yet).")
        log("=== sync_history_numbers.py done ===")
        return

    try:
        existing_sheets = set(list_sheets())
    except Exception as e:
        log(f"FATAL: could not open/read the Numbers document: {e}")
        sys.exit(1)

    this_year = datetime.now(DUBAI).year
    future_sheets = [y for y in existing_sheets if y.isdigit() and int(y) > this_year]
    if not future_sheets or max(int(y) for y in future_sheets) < this_year + MIN_HEADROOM_YEARS:
        log(
            f"WARNING: fewer than {MIN_HEADROOM_YEARS} years of future sheets exist "
            f"({sorted(future_sheets)}) -- duplicate the Template sheet in the Numbers UI soon."
        )

    by_year = {}
    for date_str, row in all_days.items():
        by_year.setdefault(date_str[:4], {})[date_str] = row

    any_touched = False
    for year, day_map in sorted(by_year.items()):
        if year not in existing_sheets:
            log(f"WARNING: no sheet named '{year}' exists -- skipping {len(day_map)} day(s) for that year.")
            continue

        for table_name, column_map, formula_map in TABLES:
            try:
                touched = sync_table(year, table_name, day_map, column_map, formula_map)
            except Exception as e:
                log(f"ERROR: failed updating '{table_name}' in sheet '{year}': {e}")
                continue
            if touched:
                any_touched = True
                log(f"Sheet '{year}' / '{table_name}': upserted {touched} day(s).")

    if any_touched:
        try:
            close_doc()
            log("Document closed.")
        except Exception as e:
            log(f"WARNING: could not close document cleanly: {e}")
    else:
        log("Nothing to write.")

    log("=== sync_history_numbers.py done ===")


if __name__ == "__main__":
    main()

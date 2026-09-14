#!/usr/bin/env python3
"""
metal-price-log / sync_history_numbers.py

LOCAL Mac-only script -- NOT part of the GitHub Actions workflow.
Meant to be pasted into a single "Run Shell Script" action inside a
macOS Shortcut, triggered once/day (~7pm Dubai) via a Personal
Automation on the Mac Mini (24/7).

Pulls the full price history from GitHub (self-healing against any
gaps -- safe to miss a day, safe to re-run any time) and writes it
into "gold silver price history.numbers", one sheet per year, one row
per day. This file is a cross-reference archive only.

Column layout (fixed -- must match the real file exactly):
  1 Date
  2 Kitco Gold OZ          <- script-written
  3 Kitco Gold Gms         <- NATIVE FORMULA, never written by this script
  4 KT Gold Gms 24k        <- script-written
  5 KT Silver Kg           <- script-written
  6 Kitco Silver Kg        <- script-written

Year-sheet routing: each date's year (YYYY) must have a matching sheet
name in the document. Sheets are NOT auto-created -- Numbers'
AppleScript dictionary rejects "duplicate" on rows, sheets, AND tables
alike (-1717 "can not be copied", confirmed 2026-09-14). Pre-create
sheets for future years manually in the Numbers UI (right-click a year
tab -> Duplicate -> rename) with some headroom. If a year's sheet is
missing, that year's rows are skipped and a warning is logged --
nothing crashes, nothing is lost (GitHub stays the source of truth,
so the next run after the sheet is created will backfill everything).
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

REPO = "rkadel11/metal-price-log"
API_HISTORY_URL = f"https://api.github.com/repos/{REPO}/contents/data/history"
RAW_CURRENT_URL = f"https://raw.githubusercontent.com/{REPO}/main/data/current.json"

DOC_PATH = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~Numbers/Documents/gold silver price history.numbers"
)
DOC_FILENAME = "gold silver price history.numbers"
TABLE_NAME = "Metal Price"

LOG_PATH = os.path.expanduser("~/Library/Logs/gold_silver_history_sync.log")
DUBAI = ZoneInfo("Asia/Dubai")

# Warn (not fail) once fewer than this many future years have a sheet
# ready to receive data.
MIN_HEADROOM_YEARS = 2

HEADERS = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 20
APPLESCRIPT_TIMEOUT = 480  # keep the whole run well under the 10-min ceiling


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


def collect_all_days() -> dict:
    """Returns {date_str: day_dict} across every archived month + today."""
    days = {}
    listing = fetch_json(API_HISTORY_URL)
    for item in listing:
        if item.get("name", "").endswith(".jsonl"):
            text = fetch_text(item["download_url"])
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                day = json.loads(line)
                days[day["date"]] = day
    try:
        current = fetch_json(RAW_CURRENT_URL)
        days[current["date"]] = current
    except Exception as e:
        log(f"WARNING: could not fetch current.json: {e}")
    return days


def row_values(day: dict):
    """-> (date_str, kitco_oz, kt_gold_gms, kt_silver_kg, kitco_silver_kg).
    Any missing/null source value stays None -> left blank in Numbers."""
    return (
        day["date"],
        day["gold"]["kitco_oz"]["high"],
        day["gold"]["kheeljtimes_gms_24k"]["high"],
        day["silver"]["kheeljtimes_kg"]["high"],
        day["silver"]["kitco_kg"]["high"],
    )


def run_applescript(script_text: str) -> str:
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
            raise RuntimeError(result.stderr.strip())
        return result.stdout.strip()
    finally:
        os.unlink(path)


# The safety guard used in every generated script: refuses to touch any
# document whose file path doesn't contain our exact filename. This is
# what protects against ever writing into the wrong open document.
#
# IMPORTANT: this must stay a function, not a module-level constant.
# An earlier version baked DOC_PATH/DOC_FILENAME into a plain string at
# import time, which meant overriding those globals afterwards (e.g. to
# point at a test copy) silently did nothing -- the script kept opening
# and writing into the real file regardless. Building the string fresh
# on every call is what makes redirecting it for tests actually work.
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
        set theDoc to document 1
        if (file of theDoc as text) does not contain targetFilename then
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


def read_existing_dates(sheet_name: str) -> list:
    """-> list of 'YYYY-M-D' strings (unpadded) or 'BLANK', in row order
    starting at row 3 (row 1/2 are the header rows)."""
    script = open_doc_preamble() + f'''
tell application "Numbers"
    tell theDoc
        tell sheet "{sheet_name}"
            tell table "{TABLE_NAME}"
                set rc to row count
                set outList to {{}}
                repeat with r from 3 to rc
                    set d to value of cell 1 of row r
                    if d is missing value then
                        set end of outList to "BLANK"
                    else
                        set y to year of d as string
                        set m to (month of d) as integer as string
                        set dy to (day of d) as string
                        set end of outList to (y & "-" & m & "-" & dy)
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
    return out.split("|") if out else []


def normalize_date(loose: str) -> str:
    """'2026-9-7' -> '2026-09-07'. Passes through already-padded dates."""
    y, m, d = loose.split("-")
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def apply_sheet_updates(sheet_name: str, target_rows: list) -> int:
    """
    target_rows: list of (date_str, oz, kt_gold, kt_silver, kitco_silver)
    sorted ascending by date_str, for this sheet's year only.
    Returns the number of rows touched (updated + inserted).
    """
    existing_raw = read_existing_dates(sheet_name)
    existing_norm = [
        normalize_date(x) if x != "BLANK" else "BLANK" for x in existing_raw
    ]

    date_to_row = {
        d: i + 3 for i, d in enumerate(existing_norm) if d != "BLANK"
    }
    blank_rows = [i + 3 for i, d in enumerate(existing_norm) if d == "BLANK"]

    lines = []
    touched = 0
    blank_cursor = 0
    last_row_index = len(existing_norm) + 2  # last existing row number

    for date_str, oz, kt_gold, kt_silver, kitco_silver in target_rows:
        if date_str in date_to_row:
            r = date_to_row[date_str]
        elif blank_cursor < len(blank_rows):
            r = blank_rows[blank_cursor]
            blank_cursor += 1
            lines.append(f'                set value of cell 1 of row {r} to "{date_str}"')
        else:
            lines.append(f'                add row below row {last_row_index}')
            last_row_index += 1
            r = last_row_index
            lines.append(f'                set value of cell 1 of row {r} to "{date_str}"')

        if oz is not None:
            lines.append(f'                set value of cell 2 of row {r} to {oz}')
        if kt_gold is not None:
            lines.append(f'                set value of cell 4 of row {r} to {kt_gold}')
        if kt_silver is not None:
            lines.append(f'                set value of cell 5 of row {r} to {kt_silver}')
        if kitco_silver is not None:
            lines.append(f'                set value of cell 6 of row {r} to {kitco_silver}')
        touched += 1

    if not lines:
        return 0

    body = "\n".join(lines)
    script = open_doc_preamble() + f'''
tell application "Numbers"
    tell theDoc
        tell sheet "{sheet_name}"
            tell table "{TABLE_NAME}"
{body}
            end tell
        end tell
        save theDoc
    end tell
end tell
'''
    run_applescript(script)
    return touched


def verify_row(sheet_name: str, date_str: str, expected_oz) -> bool:
    """Re-read one cell back to confirm the write actually stuck."""
    y, m, d = date_str.split("-")
    script = open_doc_preamble() + f'''
tell application "Numbers"
    tell theDoc
        tell sheet "{sheet_name}"
            tell table "{TABLE_NAME}"
                set rc to row count
                repeat with r from 3 to rc
                    set dcell to value of cell 1 of row r
                    if dcell is not missing value then
                        if (year of dcell as integer) = {int(y)} and (month of dcell as integer) = {int(m)} and (day of dcell as integer) = {int(d)} then
                            return value of cell 2 of row r
                        end if
                    end if
                end repeat
            end tell
        end tell
    end tell
end tell
return "NOT_FOUND"
'''
    out = run_applescript(script)
    if out in ("NOT_FOUND", ""):
        return False
    if expected_oz is None:
        return True
    try:
        return abs(float(out) - float(expected_oz)) < 0.01
    except ValueError:
        return False


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
        days = collect_all_days()
    except Exception as e:
        log(f"FATAL: could not fetch data from GitHub: {e}")
        sys.exit(1)

    by_year = {}
    for date_str, day in days.items():
        year = date_str[:4]
        by_year.setdefault(year, []).append(row_values(day))
    for year in by_year:
        by_year[year].sort(key=lambda r: r[0])

    try:
        existing_sheets = set(list_sheets())
    except Exception as e:
        log(f"FATAL: could not open/read the Numbers document: {e}")
        sys.exit(1)

    this_year = int(datetime.now(DUBAI).strftime("%Y"))
    future_sheets = [y for y in existing_sheets if y.isdigit() and int(y) > this_year]
    if not future_sheets or max(int(y) for y in future_sheets) < this_year + MIN_HEADROOM_YEARS:
        log(
            f"WARNING: fewer than {MIN_HEADROOM_YEARS} years of future sheets exist "
            f"({sorted(future_sheets)}) -- duplicate a year sheet in the Numbers UI soon."
        )

    any_touched = False
    for year, rows in sorted(by_year.items()):
        if year not in existing_sheets:
            log(f"WARNING: no sheet named '{year}' exists -- skipping {len(rows)} row(s) for that year.")
            continue
        try:
            touched = apply_sheet_updates(year, rows)
        except Exception as e:
            log(f"ERROR: failed updating sheet '{year}': {e}")
            continue
        if touched:
            any_touched = True
            log(f"Sheet '{year}': wrote/updated {touched} row(s).")
            last_date, last_oz, *_ = rows[-1]
            if verify_row(year, last_date, last_oz):
                log(f"Verified: {last_date} row reads back correctly.")
            else:
                log(f"WARNING: verification failed for {last_date} in sheet '{year}'.")

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

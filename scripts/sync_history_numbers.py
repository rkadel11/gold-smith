#!/usr/bin/env python3
"""
metal-price-log / sync_history_numbers.py

LOCAL Mac-only script -- NOT part of the GitHub Actions workflow.
Meant to be pasted into a single "Run Shell Script" action inside a
macOS Shortcut, triggered 3x/day (~10:15am/2:15pm/6:15pm Dubai -- a few
minutes after each GitHub Actions fetch, so current.json has already
updated) via Personal Automations on the Mac Mini (24/7).

Appends ONE timestamped snapshot row per run into "gold silver price
history.numbers", using GitHub's data/current.json (today's
running-high values). A full day therefore shows up to 3 rows, one per
fetch -- not a single daily summary. Rows are immutable snapshots and
are never overwritten, only appended (deduped against re-running the
same fetch window twice).

Self-healing note: this trades full recoverability for granularity. If
the Mac Mini is off during a run, that specific intraday snapshot is
genuinely gone -- only the day's *final* high survives in GitHub's
data/history/*.jsonl, not each checkpoint. So as a floor, this script
also backfills: any finalized day in GitHub's history that currently
has ZERO rows in the sheet gets exactly ONE backfilled row (using that
day's own last_updated time), so no day is ever left completely blank.
Days that already have at least one real snapshot are left untouched.

Column layout (fixed -- must match the real file exactly):
  1 Date (& Time)          <- script-written, full timestamp
  2 Kitco Gold OZ          <- script-written
  3 Kitco Gold Gms         <- NATIVE FORMULA, never written by this script
  4 KT Gold Gms 24k        <- script-written
  5 KT Silver Kg           <- script-written
  6 Kitco Silver Kg        <- script-written

Year-sheet routing: each row's year (YYYY) must have a matching sheet
name in the document. Sheets are NOT auto-created -- Numbers'
AppleScript dictionary rejects "duplicate" on rows, sheets, AND tables
alike (-1717 "can not be copied", confirmed 2026-09-14). Pre-create
sheets for future years manually in the Numbers UI (right-click a year
tab -> Duplicate -> rename) with some headroom. If a year's sheet is
missing, its rows are skipped and a warning is logged -- nothing
crashes, nothing is lost (GitHub stays the source of truth).
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


def day_to_snapshot(day: dict):
    """day (a current.json or one archived-history line) -> a snapshot
    tuple: (dt_dubai, oz, kt_gold_gms, kt_silver_kg, kitco_silver_kg).
    Any missing/null source value stays None -> left blank in Numbers."""
    dt = datetime.fromisoformat(day["last_updated"]).astimezone(DUBAI)
    return (
        dt,
        day["gold"]["kitco_oz"]["high"],
        day["gold"]["kheeljtimes_gms_24k"]["high"],
        day["silver"]["kheeljtimes_kg"]["high"],
        day["silver"]["kitco_kg"]["high"],
    )


def collect_today_snapshot_and_archive():
    """-> (today_snapshot_or_None, [archived_snapshot, ...])
    today_snapshot is the live current.json reading for right now.
    archived snapshots are one per finalized day in data/history/*.jsonl,
    used only for the zero-rows-that-day backfill check."""
    archived = []
    listing = fetch_json(API_HISTORY_URL)
    for item in listing:
        if item.get("name", "").endswith(".jsonl"):
            text = fetch_text(item["download_url"])
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                day = json.loads(line)
                if day.get("last_updated"):
                    archived.append(day_to_snapshot(day))

    today = None
    try:
        current = fetch_json(RAW_CURRENT_URL)
        if current.get("last_updated"):
            today = day_to_snapshot(current)
    except Exception as e:
        log(f"WARNING: could not fetch current.json: {e}")

    return today, archived


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


def read_existing_rows(sheet_name: str) -> list:
    """-> list of dicts {row, y, m, d, h, mi} or {row, blank: True}, in
    row order starting at row 3 (rows 1/2 are the header rows)."""
    script = open_doc_preamble() + f'''
tell application "Numbers"
    tell theDoc
        tell sheet "{sheet_name}"
            tell table "{TABLE_NAME}"
                set rc to row count
                set outList to {{}}
                repeat with r from 3 to rc
                    set dcell to value of cell 1 of row r
                    if dcell is missing value then
                        set end of outList to "BLANK"
                    else
                        set y to year of dcell as string
                        set mo to (month of dcell) as integer as string
                        set dy to day of dcell as string
                        set hh to hours of dcell as string
                        set mi to minutes of dcell as string
                        set end of outList to (y & "-" & mo & "-" & dy & " " & hh & ":" & mi)
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
            date_part, time_part = item.split(" ")
            y, m, d = (int(x) for x in date_part.split("-"))
            h, mi = (int(x) for x in time_part.split(":"))
            rows.append({"row": row_num, "y": y, "m": m, "d": d, "h": h, "mi": mi})
    return rows


def apply_new_rows(sheet_name: str, snapshots: list) -> int:
    """
    snapshots: list of (dt, oz, kt_gold, kt_silver, kitco_silver) to
    insert into this sheet -- already filtered to belong to this
    sheet's year, already deduped/backfill-checked by the caller, and
    already sorted chronologically among themselves.
    Returns the number of rows actually written.

    Normally every new snapshot lands after all existing rows (reusing
    a blank template row if one's available, else appending) -- that
    covers ordinary daily operation, including the very first backfill
    from a blank sheet. The one case that needs real positional
    insertion is a backfilled day that's chronologically *older* than
    rows already in the sheet (the Mac Mini was fully off for a day
    sandwiched between days that did log) -- for that, an existing row
    is shifted down via "add row above" so order stays correct.
    """
    if not snapshots:
        return 0

    existing = read_existing_rows(sheet_name)
    # Simulate the table as an ordered list of slots so we can track
    # row-number shifts as insertions happen, without re-reading the
    # document after every single row.
    slots = []
    for r in existing:
        if r.get("blank"):
            slots.append({"row": r["row"], "blank": True})
        else:
            dt = datetime(r["y"], r["m"], r["d"], r["h"], r["mi"], tzinfo=DUBAI)
            slots.append({"row": r["row"], "blank": False, "dt": dt})

    lines = []
    last_row_num = slots[-1]["row"] if slots else 2

    for dt, oz, kt_gold, kt_silver, kitco_silver in snapshots:
        insert_before = next(
            (i for i, s in enumerate(slots) if not s["blank"] and s["dt"] > dt), None
        )

        if insert_before is None:
            # Belongs after every filled row -> reuse a blank slot if
            # one exists (there normally is, at the tail), else append.
            blank_idx = next((i for i, s in enumerate(slots) if s["blank"]), None)
            if blank_idx is not None:
                r = slots[blank_idx]["row"]
                slots[blank_idx] = {"row": r, "blank": False, "dt": dt}
            else:
                last_row_num += 1
                r = last_row_num
                lines.append(f'                add row below row {r - 1}')
                slots.append({"row": r, "blank": False, "dt": dt})
        else:
            # Out-of-order backfill: must go before an existing row.
            r = slots[insert_before]["row"]
            lines.append(f'                add row above row {r}')
            for s in slots:
                if s["row"] >= r:
                    s["row"] += 1
            if last_row_num >= r:
                last_row_num += 1
            slots.insert(insert_before, {"row": r, "blank": False, "dt": dt})

        ts = dt.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f'                set value of cell 1 of row {r} to "{ts}"')
        if oz is not None:
            lines.append(f'                set value of cell 2 of row {r} to {oz}')
        if kt_gold is not None:
            lines.append(f'                set value of cell 4 of row {r} to {kt_gold}')
        if kt_silver is not None:
            lines.append(f'                set value of cell 5 of row {r} to {kt_silver}')
        if kitco_silver is not None:
            lines.append(f'                set value of cell 6 of row {r} to {kitco_silver}')

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
    return len(snapshots)


def verify_row(sheet_name: str, dt, expected_oz) -> bool:
    """Re-read the sheet to confirm a row with this exact timestamp
    now exists and matches -- proof the write actually stuck."""
    script = open_doc_preamble() + f'''
tell application "Numbers"
    tell theDoc
        tell sheet "{sheet_name}"
            tell table "{TABLE_NAME}"
                set rc to row count
                repeat with r from 3 to rc
                    set dcell to value of cell 1 of row r
                    if dcell is not missing value then
                        if (year of dcell as integer) = {dt.year} and (month of dcell as integer) = {dt.month} and (day of dcell as integer) = {dt.day} and (hours of dcell as integer) = {dt.hour} and (minutes of dcell as integer) = {dt.minute} then
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
        today, archived = collect_today_snapshot_and_archive()
    except Exception as e:
        log(f"FATAL: could not fetch data from GitHub: {e}")
        sys.exit(1)

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
            f"({sorted(future_sheets)}) -- duplicate a year sheet in the Numbers UI soon."
        )

    # Group candidate snapshots by year, deciding per-year further down
    # which ones actually need writing (dedup for today, gap-check for
    # archived days).
    candidates_by_year = {}
    if today:
        candidates_by_year.setdefault(str(today[0].year), {"today": today, "archived": []})
    for snap in archived:
        y = str(snap[0].year)
        candidates_by_year.setdefault(y, {"today": None, "archived": []})
        candidates_by_year[y]["archived"].append(snap)

    any_touched = False
    for year, group in sorted(candidates_by_year.items()):
        if year not in existing_sheets:
            n = (1 if group["today"] else 0) + len(group["archived"])
            log(f"WARNING: no sheet named '{year}' exists -- skipping {n} snapshot(s) for that year.")
            continue

        try:
            existing_rows = read_existing_rows(year)
        except Exception as e:
            log(f"ERROR: failed reading sheet '{year}': {e}")
            continue

        existing_dates = {
            (r["y"], r["m"], r["d"]) for r in existing_rows if not r.get("blank")
        }
        existing_exact = {
            (r["y"], r["m"], r["d"], r["h"], r["mi"]) for r in existing_rows if not r.get("blank")
        }

        to_write = []

        # Today's live snapshot -- append unless this exact minute was
        # already logged (guards against the same fetch window firing twice).
        if group["today"]:
            dt = group["today"][0]
            key = (dt.year, dt.month, dt.day, dt.hour, dt.minute)
            if key in existing_exact:
                log(f"Sheet '{year}': today's snapshot ({dt.strftime('%Y-%m-%d %H:%M')}) already logged, skipping.")
            else:
                to_write.append(group["today"])

        # Backfill floor -- any archived day with literally zero rows
        # yet gets exactly one row, so no day is ever a total gap.
        for snap in sorted(group["archived"], key=lambda s: s[0]):
            dt = snap[0]
            date_key = (dt.year, dt.month, dt.day)
            if date_key not in existing_dates:
                to_write.append(snap)
                existing_dates.add(date_key)  # avoid double-backfilling the same day twice in one run

        if not to_write:
            continue

        # Insert in chronological order regardless of the order the
        # candidates were assembled above (today's snapshot vs.
        # backfilled older days) -- rows must read top-to-bottom by date.
        to_write.sort(key=lambda s: s[0])

        try:
            written = apply_new_rows(year, to_write)
        except Exception as e:
            log(f"ERROR: failed updating sheet '{year}': {e}")
            continue

        any_touched = True
        log(f"Sheet '{year}': wrote {written} row(s).")
        last_dt, last_oz, *_ = to_write[-1]
        if verify_row(year, last_dt, last_oz):
            log(f"Verified: {last_dt.strftime('%Y-%m-%d %H:%M')} row reads back correctly.")
        else:
            log(f"WARNING: verification failed for {last_dt.strftime('%Y-%m-%d %H:%M')} in sheet '{year}'.")

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

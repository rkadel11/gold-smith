# Metal Price Automation — Project Handoff

Written 2026-09-15 to hand this project off to a new Claude Code session
(moving the "main" session to the Mac Mini, since it's on 24/7). Read this
first, then check the repo's own files — this doc explains *why* things are
built the way they are, which git history won't tell you.

## Goal

Fail-proof gold/silver price tracking that doesn't depend on any single
device being on, feeding into:
1. Raj's jewelry business Numbers price list (`P1 2.0 .numbers`)
2. Athena AI's gold widget (reduces its scraping duplication)

## Repo

**Public GitHub repo**: https://github.com/rkadel11/metal-price-log
Local clone: `~/Downloads/metal-price-log`

```
scripts/
  fetch_prices.py            - runs via GitHub Actions, 3x/day
  sync_history_numbers.py    - runs LOCALLY on a Mac via a Shortcut
  shortcut_run_shell_script.sh - generated wrapper (gitignored) for the Shortcut
  athena_gold_widget.js      - Scriptable iOS widget
data/
  current.json                - today's in-progress data
  history/YYYY-MM.jsonl       - one finalized day per line
.github/workflows/fetch-prices.yml
```

## fetch_prices.py (GitHub Actions, every 2 hours starting 06:05 Dubai time)

**Updated 2026-09-18** — schedule changed from 3x/day to every 2 hours (cron
`5 */2 * * *` UTC), and the retail 24K/18K gold rate now comes from a 2-of-3
consensus across three independent sources rather than trusting KT alone.
Fetches KT (Khaleej Times), Gulf News, Dubai City of Gold, and Kitco
gold/silver, writes `data/current.json`, archives to
`data/history/YYYY-MM.jsonl` on day rollover, and logs every run's raw
per-source reading to `data/source_comparison.jsonl` (see below).

- **KT**: scrapes `khaleejtimes.com/gold-forex`, parses **24K and 18K** gold
  (Gms) + Silver Kilo (AED) from the page's own morning/afternoon/evening
  columns. Shared helper `_kt_slot_values()`.
- **Gulf News** / **Dubai City of Gold**: cross-check sources for the 24K/18K
  retail rate only (no silver, no KT's 3-slot structure) — see
  `reconcile_retail_gold()`. Added 2026-09-18 after KT briefly disagreed with
  both of them; also gives resilience against KT going silent (has happened
  for up to a week before).
- **Kitco**: primary source is **Kitco's own page**
  (`kitco.com/price/precious-metals`) — their Next.js frontend embeds the
  full server-rendered price state as JSON in a `__NEXT_DATA__` script tag
  (`gold.results[0]` / `silver.results[0]`, each with bid/ask/mid/change/
  changePercentage). This is a structured JSON parse, not scraping visible
  HTML — confirmed reliable 2026-09-15. Falls back to gold-api.com
  (`api.gold-api.com/price/XAU` and `/XAG`) if Kitco's page structure ever
  changes. AED conversion uses the fixed USD peg (3.6725), not a scraped rate.

### Known gotchas with the source websites (read before touching the scrapers)

All confirmed actually happening, not hypothetical, while building the 3-source
consensus on 2026-09-18. `fetch_prices.py` now has explicit defenses for each
(see `MAX_STALENESS`, `GOLD_GRAM_BOUNDS`, `SILVER_KG_BOUNDS` near the top of
the file) — if you're debugging a "sources disagree" report, check these
first before assuming a real price discrepancy:

1. **khaleejtimes.com serves CDN-cached snapshots, sometimes 2+ hours stale,
   even when polled repeatedly.** Confirmed: 3 fetches 10 seconds apart all
   returned the *identical* generation timestamp and values despite 2+ real
   hours passing. Polling more often does not help if every request hits the
   same cached copy. Fixed by cache-busting every scrape request in
   `_fetch_url()` (unique query param + `Cache-Control`/`Pragma: no-cache`
   headers) — applies to all four scraped sources automatically. As a second
   line of defense, `fetch_kt()` and `fetch_gulfnews_gold()` also check the
   *source's own reported timestamp* against wall clock and reject anything
   older than `MAX_STALENESS` (90 min), in case cache-busting itself stops
   working someday or a new caching layer appears. Dubai City of Gold has no
   structured timestamp, only relative text ("Updated 3 minutes ago") — that
   gets parsed too, coarser but still functional.

2. **Gulf News DOES publish separate morning/afternoon/evening rates**, not
   one static "today" figure as originally assumed when `fetch_gulfnews_gold()`
   was first written. Its `goldApiData` JSON gains an `"afternoon"` key once
   published; reading only `"morning"` silently returns a stale figure once
   that happens (caught a false "3-way source split" this way — Gulf News
   wasn't actually disagreeing, it was just being read wrong). Now walks
   evening → afternoon → morning, same priority order as `SLOTS` elsewhere.

3. **A site redesign, wrong column, or unit mix-up (e.g. an ounce price where
   a gram price is expected) can produce a number that parses fine but is
   nonsense.** `GOLD_GRAM_BOUNDS` (200–1000 AED/gram) and `SILVER_KG_BOUNDS`
   (2000–20000 AED/kg) reject anything outside those ranges rather than
   trusting it — deliberately generous so ordinary price movement never trips
   them, only genuinely broken parses.

4. **Concurrent workflow runs can silently drop fetched data.** Multiple
   `workflow_dispatch` triggers (e.g. manual taps close to the 2-hourly
   schedule) all check out the same `main` and race to `git push` — only the
   fastest wins, the rest get `[rejected] main -> main (fetch first)` and
   their fetched data is lost, not just delayed. Fixed with a `concurrency`
   group in `fetch-prices.yml` (queues overlapping runs instead of racing)
   plus a pull-and-retry loop in the push step as backup.

### current.json schema

```json
{
  "date": "2026-09-15",
  "last_updated": "...",
  "gold": {
    "kheeljtimes_gms_24k": {"high": ..., "last_seen": ...},
    "kheeljtimes_gms_18k": {"high": ..., "last_seen": ...},
    "kitco_oz": {"high": ..., "last_seen": ...},
    "kitco_gms_24k": {"high": ..., "last_seen": ...}
  },
  "silver": {
    "kheeljtimes_kg": {...}, "kitco_kg": {...}
  },
  "readings": {
    "morning": {"time": ..., "gold": {...same 4 fields...}, "silver": {...}},
    "afternoon": null,
    "evening": null
  },
  "kitco_open_close": {
    "opening": {"slot": "morning", "time": ..., "kitco_oz": ..., "kitco_gms_24k": ...},
    "closing": {...}
  }
}
```

**IMPORTANT**: `high`/`last_seen` only track the day's running maximum — the
`last_seen` timestamp can drift away from the value it's attached to once a
later, lower reading comes in (e.g. 10am is the day's high, but last_seen
keeps advancing to 6pm's time). The `readings` block is the only place each
of the 3 daily fetches survives as its own correctly-timestamped snapshot.
**`readings`/`kitco_open_close` did not exist before 2026-09-14** — any
archived day before that date has neither field; this is not recoverable,
don't try to backfill it.

`dubai_slot(now)`: hour<12 → morning, 12-15 → afternoon, 16+ → evening.

## Numbers files

Both live in `~/Library/Mobile Documents/com~apple~Numbers/Documents/`
(consolidated there from separate iCloud locations on 2026-09-14 — if you
can't find one, check `com~apple~CloudDocs/Documents/` as a fallback, that's
where they used to be).

### `P1 2.0 .numbers` — jewelry price list — **NOT YET BUILT**
Sheet "Price List" > table "Metal Price": Date | Kitco Gold OZ | Kitco Gold
Gms (native formula, NEVER script-written) | KT Gold Gms 24k | KT Silver Kg |
Kitco Silver Kg. Rolling 15 days, eviction-based. Still needs: a manual
"Update Prices" button (shortcuts:// URL, works on any device) pulling
`current.json`, plus an auto-trigger on Mac only if the file happens to be
open (no reliable equivalent on iPad — Shortcuts there can't check "is this
document open"). Max-of-day compare-and-replace, not always-insert. **Ask
Raj how the existing Kheeljtimes button is wired before building this** —
he wants to reuse that mechanism, not reinvent one.

### `gold silver price history.numbers` — cross-reference archive — **BUILT & WORKING**
One sheet per year (`Template`, `2026`, `2027` currently — **only 1 year of
headroom left, duplicate Template again for 2028+ soon**). Each year sheet
has 3 tables, one row per calendar day:

- **Gold Price** (10 cols): `Date | Kheeljtimes Morning/Afternoon/Evening
  Gold(24k) | Kitco Morning/Afternoon/Evening OZ+Gms(formula)`
- **Silver Price** (7 cols): `Date | Kheeljtimes M/A/E Silver(Kg) | Kitco
  M/A/E Silver(Kg)`
- **Kitco Opening & Closing** (5 cols): `Date | Opening OZ+Gms(formula) |
  Closing OZ+Gms(formula)` — opening = day's earliest reading, closing =
  most recent (keeps advancing through the day)

Synced by `scripts/sync_history_numbers.py`, run via a macOS Shortcut named
**"Sync Gold Silver History"** (paste `shortcut_run_shell_script.sh`'s
content into one "Run Shell Script" action, Shell=`/bin/zsh`). Currently
only set up on the MacBook Air — **needs the same Shortcut + a Personal
Automation added on the Mac Mini** (Time of Day, repeat Daily; Raj wanted
3x/day matching GitHub's own schedule, ~10:15am/2:15pm/6:15pm Dubai, run a
few minutes after each Actions fetch so `current.json` has updated).

**Real bugs found and fixed while building this** (don't reintroduce):
1. `open_doc_preamble()`'s AppleScript assumed a freshly-opened document is
   `document 1` — false whenever another Numbers doc is already open. Fixed
   by re-scanning by file path after opening, never trusting the index.
2. Numbers' AppleScript dictionary rejects `duplicate` on rows, sheets, AND
   tables alike (`-1717 can not be copied`). Sheets can't be auto-created —
   pre-create year sheets manually (right-click Template tab → Duplicate →
   rename) with headroom; the sync script just warns if a year's sheet is
   missing, never crashes.
3. Formula columns (Gold Gms, Opening/Closing Gms) only get their formula
   from Numbers' own "extend down the column" behavior if an *adjacent* row
   already has it — a formula set on only row 3 does NOT propagate to a
   freshly-created row elsewhere. The sync script now asserts the formula
   text (never a computed value) on every row it touches, every time —
   self-healing, and safe since it's never overwriting with a raw number.
4. `update_high()` in fetch_prices.py indexed `day_data[section][key]`
   directly — throws KeyError if a newly-added field (like 18K) doesn't
   exist yet in an in-progress `current.json` written before that field was
   added. Fixed with `.setdefault()`.
5. AppleEvent timeouts (`-1712`) happen under back-to-back automation —
   `run_applescript()` does one quiet retry before treating it as real.

Row insertion handles both the common case (append after existing rows,
reusing blank template rows first) and the rare case (a backfilled day
older than existing rows — Mac Mini was off for a full day) via `add row
above`/`add row below` with correct chronological positional insertion.

## Scriptable widget (`athena_gold_widget.js`)

Finalized design, iterated via visual mockups with Raj:
- No combined title bar — just time + refresh icon at top
- **GOLD PRICE** section (gold-colored header text): Kitco column (live
  spot/oz with %, spot/gm — fetched fresh every refresh, not cached) and
  Kheeljtimes column (24K, 18K, from the 3x/day GitHub data). Column
  headers ("Kitco (live)"/"Kheeljtimes") are purple; gold values are gold.
- **SILVER PRICE** section below: bare values only (no repeated Kitco/KT
  labels), purple, positioned under the same column widths as Gold Price
  above so alignment alone identifies which is which.
- Live spot source: Kitco's page (same `__NEXT_DATA__` approach as
  fetch_prices.py) → gold-api.com fallback → cached AED-converted-to-USD
  last resort. % change uses Kitco's own official `changePercentage` when
  available (vs. previous close), else a Keychain-based "since last
  refresh" comparison.
- Tracked in the repo now (`scripts/athena_gold_widget.js`) — not run by
  GitHub Actions, purely on-device.

## Remote Control status (as of this handoff)

Raj wants a session running persistently on the Mac Mini (24/7) so any
device can connect via Claude Code's Remote Control feature, similar in
feel to how claude.ai chat syncs — but note Remote Control requires an
already-running LOCAL session on whichever machine you want reachable; it
can't reach out and start one on a different computer. This session has
been running on the MacBook Air, with `claude rc` enabled/restarted several
times (it disconnects from Anthropic's backend occasionally even while the
local process stays alive — `kill <pid>` then re-run `echo y | claude rc`
fixes it every time so far). **The actual fix Raj is pursuing**: start a
fresh session directly on the Mac Mini (Terminal → `cd` into the project →
`claude` → `claude rc`), so the persistent 24/7 machine hosts the live
session instead of a laptop that can sleep/close.

## Open items, roughly in priority order

1. Get a session running on the Mac Mini with Remote Control enabled there
   (this handoff's whole purpose)
2. Add the "Sync Gold Silver History" Shortcut + 3x/day automation on the
   Mac Mini (currently only exists on MacBook Air)
3. Duplicate Template → create 2028+ sheets for headroom (only 2027 exists
   right now)
4. Build `P1 2.0 .numbers`'s "Update Prices" button + auto-trigger — ask
   Raj how the existing Kheeljtimes button is wired first
5. Athena's own gold widget migration to read from this shared JSON instead
   of its own scraping (`athena_gold.py`) — lower priority, do once
   everything above is proven stable
6. Consider adding KT 18K + Silver to the Numbers Gold Price table too
   (currently GitHub/widget have 18K, Numbers doesn't) — only if Raj asks,
   don't expand scope unprompted

## Working style notes

Raj is fast, informal, mobile-shorthand. Wants visual mockups confirmed
(via the `mcp__visualize__show_widget` tool, not code) before code changes
to anything visual. Pushes back on unverified assumptions — verify against
real files/live behavior before proceeding, always test on a scratch copy
before touching his real Numbers file. Dismisses `AskUserQuestion` widget
prompts often — prefers plain chat text for questions.

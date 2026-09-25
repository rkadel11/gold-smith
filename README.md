# metal-price-log

Automated gold/silver price tracking for the jewelry business Price List.
Runs independently of any personal device — a GitHub Actions cron job
fetches prices 3x/day and keeps the day's running high.

## Schedule
10:00 / 14:00 / 18:00 Dubai time (06:00 / 10:00 / 14:00 UTC — Dubai has no DST).

## Files
- `scripts/fetch_prices.py` — the fetch + rollover logic
- `data/current.json` — today's running high per source, read by the
  Numbers "Metal Price" table sync (last 15 days, eviction-based)
- `data/history/YYYY-MM.jsonl` — one line per finished day, appended on
  day rollover. Read by the separate "Gold Silver History.numbers" file
  (full resync, one sheet per year) on its own 7pm sync/close routine.

## data/current.json schema
```json
{
  "date": "2026-09-07",
  "last_updated": "2026-09-07T18:00:03+04:00",
  "gold": {
    "kheeljtimes_gms_24k": { "high": 533.75, "last_seen": "..." },
    "kitco_oz":             { "high": 3650.20, "last_seen": "..." },
    "kitco_gms_24k":        { "high": 117.35, "last_seen": "..." }
  },
  "silver": {
    "kheeljtimes_kg": { "high": 9075.0, "last_seen": "..." },
    "kitco_kg":       { "high": 9050.0, "last_seen": "..." }
  }
}
```
`kitco_gms_24k` is computed here for the GitHub record only — in the
Numbers "Metal Price" table, that column is a native formula
(`= Kitco OZ / 31.1034768`), never script-written.

## Consumers (all read-only, public repo — no auth needed)
- Mac shortcut → writes `kheeljtimes_gms_24k`, `kitco_oz`,
  `kheeljtimes_kg`, `kitco_kg` into the "Metal Price" table
  (last 15 days, oldest row evicted on new-day insert)
- Same Mac, separate 7pm automation → full resync of
  `data/history/*.jsonl` into "Gold Silver History.numbers"
  (one sheet per year, self-healing against any missed days)
- Athena desktop/iPhone widget (planned) → same `current.json`,
  replacing its own KT/Kitco scraping

## Why Kitco uses goldprice.org, not kitco.com directly
Athena's existing Kitco scrapers (`Kitco_Spot.scpt` and the six-method
fallback chain in `athena_gold.py`) depend on Safari rendering JS and
matching a CSS class (`h3.font-mulish`) that Kitco controls and can
change without notice — the likely cause of intermittent failures.
Neither script fetches silver at all.

Athena's own fallback list already includes a plain JSON endpoint,
`data-asg.goldprice.org/dbXRates/USD`, which returns both `xauPrice`
(gold) and `xagPrice` (silver) in one response — no browser needed.
This repo uses that as primary, with `metals.live/api/latest` as
fallback. AED conversion uses the fixed USD peg (3.6725), not a
scraped exchange rate, since the peg doesn't float.

**Not yet confirmed with a live test** — verify `fetch_kitco()`
returns real numbers (via `workflow_dispatch` or running the script
locally) before trusting it in production.

## Local HTTPS server on the Mac Mini (same wifi)
`scripts/mac_mini_https_server.py` serves `data/` over HTTPS on the LAN, so
devices on the same wifi can read `current.json` straight off the Mac Mini.

1. On the Mac Mini, from the repo folder:
   `python3 scripts/mac_mini_https_server.py`
   The first run creates a private CA + server cert in `~/.gold-smith-ssl/`
   and prints the URLs, e.g. `https://<mac-name>.local:8443/current.json`.
   Allow incoming connections if macOS asks.
2. Trust the CA once on each other device. AirDrop
   `~/.gold-smith-ssl/gold-smith-local-ca.crt` to it, then:
   - iPhone: Settings → Profile Downloaded → Install, then
     Settings → General → About → Certificate Trust Settings → turn it on.
   - Other Mac: double-click it → Keychain Access → set "Always Trust".
3. If the Mac Mini's IP changes, run it again with `--regen`. The CA stays
   the same, so you don't need to reinstall it on the other devices.

Never share `gold-smith-local-ca.key`. Anyone who has it can create
certificates your devices will trust.

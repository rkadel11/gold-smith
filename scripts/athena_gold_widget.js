// Athena Gold Widget — Scriptable iOS
// Canvas: 360×169pt (Medium widget)
// Tap to refresh · Auto-updates every 3 hrs (system budget permitting)
//
// v2 — reads gold-smith's own current.json instead of scraping
// KT/GN/Kitco/DC HTML directly. One reliable JSON fetch, no fragile
// regex parsing, no per-widget scraping duplication — the GitHub
// Actions pipeline (fetch_prices.py) already does that 3x/day and
// commits the result publicly.
//
// v3 — the top "spot" line is now live USD (fetched directly from
// gold-api.com, the same source fetch_prices.py uses), not the
// AED-converted value from the 3x/day GitHub snapshot. This refreshes
// every time the widget itself refreshes rather than only 3x/day. The
// KT-vs-Kitco AED comparison rows below still come from current.json,
// since KT is inherently an AED retail price.
//
// v4 — spot source is now Kitco itself: their page embeds a Next.js
// "__NEXT_DATA__" script tag with clean structured JSON (bid/ask/mid/
// change/changePercentage), not just HTML to regex-scrape -- as
// reliable as a real API, confirmed 2026-09-15. Falls back to
// gold-api.com, then to the cached AED value, if Kitco's page ever
// changes shape. % change now comes from Kitco's own official
// changePercentage (vs. the previous close) rather than a comparison
// against this widget's own last refresh.

const GOLD       = new Color("#f9c416")
const PURPLE     = new Color("#c4b5fd")
const PURPLE_DIM = new Color("#c4b5fd", 0.40)
const BG_DEEP    = new Color("#0d0014")
const BG_CARD    = new Color("#160025")
const RED        = new Color("#ef4444")
const GREEN      = new Color("#22c55e")

const DATA_URL = "https://raw.githubusercontent.com/rkadel11/gold-smith/main/data/current.json"
const KITCO_URL = "https://www.kitco.com/price/precious-metals"
const GOLD_API_XAU_URL = "https://api.gold-api.com/price/XAU"
const GOLD_API_XAG_URL = "https://api.gold-api.com/price/XAG"
const OZ_TO_GRAMS = 31.1034768
const AED_PER_USD = 3.6725

// ── Fetch ──────────────────────────────────────────────
async function fetchCurrent() {
  const req = new Request(DATA_URL)
  req.timeoutInterval = 15
  return await req.loadJSON()
}

// Live USD spot straight from Kitco's own page -- their Next.js
// frontend embeds the full server-rendered price state as JSON in a
// "__NEXT_DATA__" script tag, so this is a structured JSON parse, not
// scraping visible HTML for numbers. Includes Kitco's own official
// change/changePercentage (vs. the previous close), which is more
// meaningful than comparing against this widget's own last refresh.
async function fetchKitcoLive() {
  const req = new Request(KITCO_URL)
  req.headers = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
  }
  req.timeoutInterval = 12
  const html = await req.loadString()
  const m = html.match(/<script id="__NEXT_DATA__"[^>]*>(.*?)<\/script>/s)
  if (!m) throw new Error("Kitco page structure changed (no __NEXT_DATA__)")
  const data = JSON.parse(m[1])
  const queries = data?.props?.pageProps?.dehydratedState?.queries || []
  const metalsQuery = queries.find(q => q?.state?.data?.gold)
  const gold = metalsQuery?.state?.data?.gold?.results?.[0]
  const silver = metalsQuery?.state?.data?.silver?.results?.[0]
  if (!gold || !(gold.mid > 0)) throw new Error("Kitco gold data missing/invalid")
  return {
    usdOz: gold.mid,
    usdGm: gold.mid / OZ_TO_GRAMS,
    changePercentage: gold.changePercentage,
    silverUsdOz: silver && silver.mid > 0 ? silver.mid : null,
  }
}

// Fallback if Kitco's page structure ever changes underneath us.
// Fetched fresh on every widget refresh -- not cached in
// current.json, which only updates 3x/day. Two requests (gold-api.com
// needs one call per metal, unlike Kitco's single page).
async function fetchLiveUsdGold() {
  async function priceFrom(url) {
    const req = new Request(url)
    req.timeoutInterval = 10
    const json = await req.loadJSON()
    const price = parseFloat(json.price)
    return price > 0 ? price : null
  }
  const usdOz = await priceFrom(GOLD_API_XAU_URL)
  if (!usdOz) throw new Error("bad gold price from gold-api.com")
  let silverUsdOz = null
  try {
    silverUsdOz = await priceFrom(GOLD_API_XAG_URL)
  } catch (e) {
    // silver fallback failing isn't fatal -- gold is the primary spot line
  }
  return { usdOz, usdGm: usdOz / OZ_TO_GRAMS, changePercentage: null, silverUsdOz }
}

// Fallback only -- used when Kitco's own changePercentage isn't
// available (gold-api.com/cached paths). Compares the live oz price
// against the last one THIS WIDGET saw (persisted in the Keychain,
// since a widget's JS context doesn't survive between refreshes), so
// it's "change since last refresh", not "change since previous
// close" like Kitco's own figure. -> {direction, pct} or null.
//
// Read and write are kept separate (rather than one combined
// compare-and-store call) so the Keychain gets updated on EVERY
// successful fetch regardless of which source served it -- otherwise,
// if Kitco succeeds for a while and then fails once, the fallback
// comparison would be against a stale value from whenever Kitco last
// failed instead of the actual last-known price.
const LAST_OZ_KEY = "athenaGoldWidget.lastSpotOz"
function readLastOz() {
  if (!Keychain.contains(LAST_OZ_KEY)) return null
  const prev = parseFloat(Keychain.get(LAST_OZ_KEY))
  return isNaN(prev) ? null : prev
}
function writeLastOz(currentOz) {
  if (currentOz != null) Keychain.set(LAST_OZ_KEY, String(currentOz))
}
function fallbackDirection(currentOz) {
  const prev = readLastOz()
  if (currentOz == null || prev == null || prev <= 0) return null
  const pct = ((currentOz - prev) / prev) * 100
  const direction = currentOz > prev ? "up" : currentOz < prev ? "down" : "same"
  return { direction, pct }
}

// Pick whichever of evening/afternoon/morning is chronologically the
// most recent -- gives one consistent "latest" snapshot instead of
// mixing metrics that were last true at different times of day.
//
// Slot order alone decides this (evening is always later in the day
// than afternoon, which is always later than morning) rather than
// comparing each reading's "time" field -- a backfill run can touch
// several slots in one go and stamp them all with that same run's
// timestamp, which made a strict ">" comparison keep whichever slot
// was iterated first (morning) on a tie instead of the actual latest
// one. Confirmed 2026-09-17: this showed a stale morning KT price all
// day even after afternoon/evening had newer data.
function latestReading(data) {
  const slots = ["evening", "afternoon", "morning"]
  for (const s of slots) {
    const r = data.readings && data.readings[s]
    if (r) return r
  }
  return null
}

// Falls back to the day's "high" fields if readings aren't present at
// all yet (e.g. very first run of a new day, or an older data file).
function extractPrices(data) {
  const latest = latestReading(data)
  if (latest) {
    return {
      time: latest.time,
      ktGold24k: latest.gold.kheeljtimes_gms_24k,
      ktGold18k: latest.gold.kheeljtimes_gms_18k,
      kitcoGoldOz: latest.gold.kitco_oz,
      kitcoGoldGm: latest.gold.kitco_gms_24k,
      ktSilverKg: latest.silver.kheeljtimes_kg,
      kitcoSilverKg: latest.silver.kitco_kg,
      isLive: true,
    }
  }
  return {
    time: data.last_updated,
    ktGold24k: data.gold?.kheeljtimes_gms_24k?.high ?? null,
    ktGold18k: data.gold?.kheeljtimes_gms_18k?.high ?? null,
    kitcoGoldOz: data.gold?.kitco_oz?.high ?? null,
    kitcoGoldGm: data.gold?.kitco_gms_24k?.high ?? null,
    ktSilverKg: data.silver?.kheeljtimes_kg?.high ?? null,
    kitcoSilverKg: data.silver?.kitco_kg?.high ?? null,
    isLive: false,
  }
}

// ── Helpers ────────────────────────────────────────────
const f = v => v == null ? "—" : v.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ",")
const fmtTime = iso => {
  if (!iso) return "—"
  return new Date(iso).toLocaleTimeString("en-AE", { hour: "2-digit", minute: "2-digit", hour12: false })
}

// ── Build — tuned for 360×169pt ───────────────────────
async function buildWidget(p) {
  const w = new ListWidget()
  w.backgroundColor = BG_DEEP
  w.setPadding(8, 16, 8, 16)
  w.refreshAfterDate = new Date(Date.now() + 3 * 60 * 60 * 1000)
  w.url = "scriptable:///run/AthenaGold"

  // ── TOP ROW: title, then time + refresh ─────────────────────
  const hdr = w.addStack()
  hdr.layoutHorizontally()
  hdr.centerAlignContent()
  const title = hdr.addText("METAL PRICES")
  title.textColor = GOLD
  title.font = Font.boldSystemFont(11)
  hdr.addSpacer()
  const tm = hdr.addText(fmtTime(p.time))
  tm.textColor = p.isLive ? PURPLE : new Color("#a78bfa", 0.6)
  tm.font = Font.systemFont(10)
  hdr.addSpacer(4)
  const ref = hdr.addText("↻")
  ref.textColor = GOLD
  ref.font = Font.boldSystemFont(13)

  w.addSpacer(3)

  const colWidth = 161

  function sectionHeader(text) {
    const t = w.addText(text)
    t.textColor = GOLD
    t.font = Font.boldSystemFont(10)
  }

  function metricRow(container, label, valueText, valueColor, changeText, changeColor) {
    const row = container.addStack()
    row.layoutHorizontally()
    const l = row.addText(label)
    l.textColor = PURPLE_DIM
    l.font = Font.systemFont(8)
    row.addSpacer()
    if (changeText) {
      const c = row.addText(changeText)
      c.textColor = changeColor
      c.font = Font.systemFont(8)
      row.addSpacer(3)
    }
    const v = row.addText(valueText)
    v.textColor = valueColor
    v.font = Font.boldSystemFont(11)
  }

  // ── GOLD PRICE ──────────────────────────────────────────
  sectionHeader("GOLD PRICE")
  w.addSpacer(3)

  const goldCols = w.addStack()
  goldCols.layoutHorizontally()

  const ktCol = goldCols.addStack()
  ktCol.layoutVertically()
  ktCol.backgroundColor = BG_CARD
  ktCol.cornerRadius = 7
  ktCol.setPadding(5, 8, 5, 8)
  ktCol.size = new Size(colWidth, 0)
  const ktTitle = ktCol.addText("Kheeljtimes")
  ktTitle.textColor = PURPLE
  ktTitle.font = Font.boldSystemFont(9)
  ktCol.addSpacer(3)
  metricRow(ktCol, "24K", f(p.ktGold24k), GOLD, null, null)
  ktCol.addSpacer(2)
  metricRow(ktCol, "18K", f(p.ktGold18k), GOLD, null, null)

  goldCols.addSpacer(6)

  const kitcoCol = goldCols.addStack()
  kitcoCol.layoutVertically()
  kitcoCol.backgroundColor = BG_CARD
  kitcoCol.cornerRadius = 7
  kitcoCol.setPadding(5, 8, 5, 8)
  kitcoCol.size = new Size(colWidth, 0)
  const kitcoTitle = kitcoCol.addText("Kitco")
  kitcoTitle.textColor = PURPLE
  kitcoTitle.font = Font.boldSystemFont(9)
  kitcoCol.addSpacer(3)
  const changeText = (p.direction === "up" || p.direction === "down")
    ? `${p.direction === "up" ? "▲" : "▼"}${p.pct != null ? " " + Math.abs(p.pct).toFixed(2) + "%" : ""}`
    : null
  const changeColor = p.direction === "up" ? GREEN : RED
  metricRow(kitcoCol, "Spot/oz", "$" + f(p.usdOz), GOLD, changeText, changeColor)
  kitcoCol.addSpacer(2)
  metricRow(kitcoCol, "Spot/gm", "$" + f(p.usdGm), GOLD, null, null)

  w.addSpacer(6)

  // ── SILVER PRICE ────────────────────────────────────────
  // Bare values only, no repeated Kitco/KT labels -- position under
  // the Gold Price columns above already identifies each one.
  sectionHeader("SILVER PRICE")
  w.addSpacer(3)

  const silverCols = w.addStack()
  silverCols.layoutHorizontally()

  const ktSilverBox = silverCols.addStack()
  ktSilverBox.backgroundColor = BG_CARD
  ktSilverBox.cornerRadius = 7
  ktSilverBox.setPadding(5, 8, 5, 8)
  ktSilverBox.size = new Size(colWidth, 0)
  const ktSilverVal = ktSilverBox.addText(f(p.ktSilverKg))
  ktSilverVal.textColor = PURPLE
  ktSilverVal.font = Font.boldSystemFont(11)

  silverCols.addSpacer(6)

  const kitcoSilverBox = silverCols.addStack()
  kitcoSilverBox.backgroundColor = BG_CARD
  kitcoSilverBox.cornerRadius = 7
  kitcoSilverBox.setPadding(5, 8, 5, 8)
  kitcoSilverBox.size = new Size(colWidth, 0)
  const kitcoSilverVal = kitcoSilverBox.addText("$" + f(p.silverUsdOz))
  kitcoSilverVal.textColor = PURPLE
  kitcoSilverVal.font = Font.boldSystemFont(11)

  return w
}

// ── Error ──────────────────────────────────────────────
function errorWidget(msg) {
  const w = new ListWidget()
  w.backgroundColor = BG_DEEP
  w.setPadding(16, 16, 16, 16)
  const t = w.addText("⬡  GOLD & SILVER PRICE  ⬡")
  t.textColor = GOLD
  t.font = Font.boldSystemFont(13)
  w.addSpacer(8)
  const e = w.addText(msg)
  e.textColor = RED
  e.font = Font.systemFont(10)
  return w
}

// ── Main ───────────────────────────────────────────────
let widget
try {
  const data = await fetchCurrent()
  const prices = extractPrices(data)

  let live = null
  try {
    live = await fetchKitcoLive()
  } catch (e1) {
    try {
      live = await fetchLiveUsdGold()
    } catch (e2) {
      // Both live sources failed -- fall back to converting the
      // cached AED value back to USD via the fixed peg, rather than
      // showing nothing.
    }
  }

  if (live) {
    prices.usdOz = live.usdOz
    prices.usdGm = live.usdGm
  } else {
    prices.usdOz = prices.kitcoGoldOz != null ? prices.kitcoGoldOz / AED_PER_USD : null
    prices.usdGm = prices.kitcoGoldGm != null ? prices.kitcoGoldGm / AED_PER_USD : null
  }

  if (live && live.silverUsdOz != null) {
    prices.silverUsdOz = live.silverUsdOz
  } else if (prices.kitcoSilverKg != null) {
    // Cached AED/kg -> USD/oz via the fixed peg and troy-oz conversion.
    prices.silverUsdOz = (prices.kitcoSilverKg / AED_PER_USD) * (OZ_TO_GRAMS / 1000)
  } else {
    prices.silverUsdOz = null
  }

  if (live && live.changePercentage != null) {
    // Kitco's own official change vs. the previous close.
    const pct = live.changePercentage
    prices.direction = pct > 0 ? "up" : pct < 0 ? "down" : "same"
    prices.pct = pct
  } else {
    // No official change% available (gold-api/cached path) -- fall
    // back to comparing against this widget's own last refresh.
    // Read BEFORE writing, obviously.
    const fb = fallbackDirection(prices.usdOz)
    prices.direction = fb?.direction ?? null
    prices.pct = fb?.pct ?? null
  }
  writeLastOz(prices.usdOz)

  widget = (prices.ktGold24k || prices.kitcoGoldGm)
    ? await buildWidget(prices)
    : errorWidget("No price data yet\nCheck gold-smith repo")
} catch (e) {
  widget = errorWidget("Could not fetch prices\n" + e.message)
}

if (config.runsInWidget) {
  Script.setWidget(widget)
} else {
  await widget.presentMedium()
}
Script.complete()

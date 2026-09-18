// Gold Refresh Trigger — Scriptable iOS
// TEMPORARY diagnostic script, separate from the main Athena Gold
// widget (athena_gold_widget.js). Do not merge into it.
//
// Purpose: tap this (as its own small Home Screen widget, or run it
// manually from the Scriptable app) to immediately trigger the
// gold-smith GitHub Action (fetch-prices.yml) via workflow_dispatch,
// instead of waiting for its 2-hourly schedule. Two reasons to do
// this right now:
//   1. Every extra run adds a line to data/source_comparison.jsonl,
//      which is being used to figure out which of KT/Gulf News/Dubai
//      City of Gold actually agree with each other most often --
//      more data points here is explicitly wanted.
//   2. Forces a fresh current.json before you check the main widget,
//      instead of waiting on the schedule.
//
// One-time setup: needs a GitHub Personal Access Token with
// "Actions: write" access to rkadel11/gold-smith, entered once and
// stored only in this device's Keychain -- never hardcoded here,
// never sent anywhere except GitHub's own API.
//   1. github.com -> Settings -> Developer settings -> Personal
//      access tokens -> Fine-grained tokens -> Generate new token.
//   2. Repository access: "Only select repositories" -> gold-smith.
//   3. Permissions: Repository permissions -> Actions -> Read and write.
//   4. Generate, copy the token, paste it into the prompt this script
//      shows the first time it runs.

const REPO_OWNER = "rkadel11"
const REPO_NAME = "gold-smith"
const WORKFLOW_FILE = "fetch-prices.yml"
const TOKEN_KEY = "goldSmithGithubToken"

const GOLD = new Color("#f9c416")
const BG_DEEP = new Color("#0d0014")
const RED = new Color("#ef4444")
const GREEN = new Color("#22c55e")

async function getToken() {
  if (Keychain.contains(TOKEN_KEY)) {
    return Keychain.get(TOKEN_KEY)
  }
  const alert = new Alert()
  alert.title = "GitHub Token Needed"
  alert.message = "Paste a GitHub Personal Access Token (Actions: read/write on rkadel11/gold-smith). Stored only in this device's Keychain."
  alert.addTextField("ghp_... or github_pat_...")
  alert.addAction("Save")
  alert.addCancelAction("Cancel")
  const choice = await alert.presentAlert()
  if (choice === -1) throw new Error("No token provided")
  const token = alert.textFieldValue(0).trim()
  if (!token) throw new Error("Empty token")
  Keychain.set(TOKEN_KEY, token)
  return token
}

async function triggerWorkflow(token) {
  const url = `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_FILE}/dispatches`
  const req = new Request(url)
  req.method = "POST"
  req.headers = {
    "Authorization": `Bearer ${token}`,
    "Accept": "application/vnd.github+json",
    "User-Agent": "gold-smith-scriptable-trigger",
    "Content-Type": "application/json",
  }
  req.body = JSON.stringify({ ref: "main" })
  req.timeoutInterval = 15
  const bodyText = await req.loadString()
  const status = req.response.statusCode
  if (status === 401) {
    Keychain.remove(TOKEN_KEY)
    throw new Error("Token rejected as invalid (HTTP 401) -- re-run to enter a new one.")
  }
  if (status === 403) {
    // Almost always a permission problem, not a bad token -- most
    // commonly the fine-grained PAT's "Actions" repository permission
    // was left at "No access" instead of "Read and write" (easy to
    // miss among ~40 permission rows), or the wrong repo was selected
    // under "Repository access". Surface GitHub's own message (it
    // usually names the missing scope) instead of just the status
    // code, and clear the token so a corrected one can be entered.
    Keychain.remove(TOKEN_KEY)
    let detail = bodyText
    try { detail = JSON.parse(bodyText).message || bodyText } catch (e) {}
    throw new Error(`Forbidden (HTTP 403): ${detail}\nCheck the token has "Actions: Read and write" permission on gold-smith specifically, then re-run to enter a corrected one.`)
  }
  if (status !== 204) {
    throw new Error(`GitHub API returned HTTP ${status}: ${bodyText}`)
  }
}

async function main() {
  let msg, ok
  try {
    const token = await getToken()
    await triggerWorkflow(token)
    msg = "Triggered a fresh fetch on GitHub.\nGive it ~15-20s, then refresh the main widget."
    ok = true
  } catch (e) {
    msg = "Could not trigger GitHub Action:\n" + e.message
    ok = false
  }

  if (config.runsInWidget) {
    const w = new ListWidget()
    w.backgroundColor = BG_DEEP
    w.setPadding(12, 12, 12, 12)
    const title = w.addText("GOLD REFRESH TRIGGER")
    title.textColor = GOLD
    title.font = Font.boldSystemFont(11)
    w.addSpacer(6)
    const t = w.addText(msg)
    t.textColor = ok ? GREEN : RED
    t.font = Font.systemFont(11)
    Script.setWidget(w)
  } else {
    const alert = new Alert()
    alert.title = ok ? "Triggered" : "Failed"
    alert.message = msg
    alert.addAction("OK")
    await alert.presentAlert()
  }
  Script.complete()
}

await main()

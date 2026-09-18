#!/bin/zsh
# gold-smith / p1_shortcut_run_shell_script.sh
#
# Paste this exact content into the "Update P1 Prices" macOS
# Shortcut's "Run Shell Script" action (Shell = /bin/zsh). Fetches and
# runs the LATEST update_p1_prices.py from GitHub's main branch on
# every run, instead of embedding a static copy of the script -- same
# reasoning as shortcut_run_shell_script.sh (the Sync Gold Silver
# History equivalent): a pasted-in copy goes stale the moment the
# script is fixed or changed, and if this Shortcut exists on more than
# one device, every copy would need manually re-pasting. Fetching
# fresh each run means one fix on GitHub applies everywhere,
# automatically, next time this Shortcut fires.

set -e
TMP=$(mktemp /tmp/update_p1_prices.XXXXXX.py)
trap 'rm -f "$TMP"' EXIT

curl -fsSL "https://raw.githubusercontent.com/rkadel11/gold-smith/main/scripts/update_p1_prices.py" -o "$TMP"
python3 "$TMP"

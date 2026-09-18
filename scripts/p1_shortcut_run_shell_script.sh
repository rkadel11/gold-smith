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
#
# Saved to a fixed, visible folder (~/gold-smith/scripts/) rather than
# a throwaway /tmp file -- overwritten fresh every run, so it's always
# the current version, but you can open that folder anytime and see
# exactly what actually ran.

set -e
SCRIPTS_DIR="$HOME/gold-smith/scripts"
mkdir -p "$SCRIPTS_DIR"

curl -fsSL "https://raw.githubusercontent.com/rkadel11/gold-smith/main/scripts/update_p1_prices.py" -o "$SCRIPTS_DIR/update_p1_prices.py"
python3 "$SCRIPTS_DIR/update_p1_prices.py"

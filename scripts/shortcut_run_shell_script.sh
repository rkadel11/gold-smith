#!/bin/zsh
# gold-smith / shortcut_run_shell_script.sh
#
# Paste this exact content into the "Sync Gold Silver History" macOS
# Shortcut's "Run Shell Script" action (Shell = /bin/zsh). Fetches and
# runs the LATEST sync_history_numbers.py from GitHub's main branch on
# every run, instead of embedding a static copy of the script.
#
# Why: a pasted-in copy goes stale the moment sync_history_numbers.py
# is fixed or changed on GitHub -- and if this Shortcut is set up on
# more than one Mac (per HANDOFF.md, both a MacBook Air and a Mac Mini
# were planned), EVERY copy would need to be manually re-pasted, which
# is exactly the kind of thing that's easy to forget and causes silent
# drift between machines. Fetching fresh each run means every Mac
# always executes whatever's currently on main -- one fix applies
# everywhere, automatically, next time each Shortcut fires.
#
# Saved to a fixed, visible folder (~/gold-smith/scripts/) rather than
# a throwaway /tmp file -- overwritten fresh every run, so it's always
# the current version, but you can open that folder anytime and see
# exactly what actually ran.

set -e
SCRIPTS_DIR="$HOME/gold-smith/scripts"
mkdir -p "$SCRIPTS_DIR"

curl -fsSL "https://raw.githubusercontent.com/rkadel11/gold-smith/main/scripts/sync_history_numbers.py" -o "$SCRIPTS_DIR/sync_history_numbers.py"
python3 "$SCRIPTS_DIR/sync_history_numbers.py"

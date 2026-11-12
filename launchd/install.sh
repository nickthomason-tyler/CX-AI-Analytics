#!/bin/bash
# Install the launchd jobs for THIS checkout.
#
#   ./launchd/install.sh            install + load nightly, weekly and serve
#   ./launchd/install.sh --unload   unload and remove them
#
# The plists in this folder carry a __INSTALL_DIR__ placeholder; this fills it in with
# the absolute path of the repo and writes the result to ~/Library/LaunchAgents.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/Library/LaunchAgents"
UID_="$(id -u)"
mkdir -p "$DEST" "$REPO/logs" "$REPO/state"

for src in "$REPO"/launchd/*.plist; do
  name="$(basename "$src")"
  label="${name%.plist}"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
  [ "${1:-}" = "--unload" ] && { rm -f "$DEST/$name"; echo "removed $label"; continue; }
  sed "s#__INSTALL_DIR__#$REPO#g" "$src" > "$DEST/$name"
  plutil -lint "$DEST/$name" >/dev/null
  launchctl bootstrap "gui/$UID_" "$DEST/$name"
  echo "loaded $label"
done

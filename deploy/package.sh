#!/bin/bash
# Build a zip of the static dashboard for hosting on IIS. See deploy/DEPLOY.md.
#
#   ./deploy/package.sh             package what is already built
#   ./deploy/package.sh --refresh   incremental extract + rebuild first (needs creds)
#
# Only an explicit allowlist of files is copied. The warehouse database (client
# transcripts, named-user activity) must never end up in the package.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--refresh" ]; then
  python3 extract.py facts --incremental
  python3 extract.py feedback
  python3 build_dashboard.py
  python3 build_forge_poc.py
fi

FILES=(dashboard.html dashboard-forge.html forge-bundle.js forge.css forge-core.css forge-dark.css)
STAGE_FILES=(deploy/index.html deploy/web.config deploy/DEPLOY.md)

for f in "${FILES[@]}" "${STAGE_FILES[@]}"; do
  [ -f "$f" ] || { echo "missing $f -- run with --refresh or build first" >&2; exit 1; }
done

STAMP=$(date +%Y%m%d-%H%M)
OUT="dist/foundry-dashboard-$STAMP"
rm -rf "$OUT" && mkdir -p "$OUT"
cp "${FILES[@]}" "$OUT/"
cp "${STAGE_FILES[@]}" "$OUT/"

( cd dist && zip -qr "foundry-dashboard-$STAMP.zip" "foundry-dashboard-$STAMP" )
rm -rf "$OUT"
echo "built dist/foundry-dashboard-$STAMP.zip"
echo "data through: $(sqlite3 foundry_analytics.db 'SELECT MAX(run_day) FROM fact_agent_run' 2>/dev/null || echo unknown)" 

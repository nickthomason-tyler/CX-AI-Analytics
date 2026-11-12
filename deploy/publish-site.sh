#!/bin/bash
# Copy the freshly built dashboard into site/ for Netlify, then print what to commit.
#
#   ./deploy/publish-site.sh            use the dashboard.html already built
#   ./deploy/publish-site.sh --refresh  run ./refresh.sh nightly first
#
# Netlify deploys site/ straight from git (see netlify.toml), so updating the live page
# means: run this, commit site/index.html, push.
set -euo pipefail
cd "$(dirname "$0")/.."
[ "${1:-}" = "--refresh" ] && ./refresh.sh nightly
[ -f dashboard.html ] || { echo "dashboard.html missing -- run ./refresh.sh first" >&2; exit 1; }
cp dashboard.html site/index.html
echo "site/index.html updated ($(wc -c < site/index.html | tr -d ' ') bytes)"
echo "data through: $(sqlite3 foundry_analytics.db 'SELECT MAX(run_day) FROM v_agent_runs' 2>/dev/null || echo unknown)"
echo "next: git add site/index.html && git commit -m 'Update dashboard' && git push"

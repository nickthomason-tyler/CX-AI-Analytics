#!/bin/bash
# Split-cadence refresh for the Foundry analytics warehouse.
#
#   ./refresh.sh nightly    cheap top-up, safe to run unattended every night
#   ./refresh.sh weekly     full reconciliation (catches what nightly skips)
#   ./refresh.sh status     read-only: local counts vs the live API
#
# WHY TWO MODES
# The documented one-liner re-fetches ~500 requests of almost entirely unchanged
# data every run. Two costs dominate, and neither is incremental:
#
#   1. ingest_workflows fans out over EVERY visual-workflow definition -- 428
#      requests, one per definition, because there is no aggregate workflow-run
#      endpoint. It yields 22 executions across 6 definitions (1.4% have ever run).
#      Nightly skips it; weekly picks it up.
#
#   2. `extract.py feedback` defaults --since to 2025-01-01, so discovery re-walks
#      the entire history (recursive date-window split, per rating) purely to
#      re-derive conversation ids already stored. Nightly narrows it to a window.
#
# What nightly does NOT skip, because it is already cheap or already incremental:
#   - facts --incremental      : resumes from the last ingested day, so the month
#                                probe is a single request
#   - ingest_teams             : PAGE_SIZE is 1000, so 6,450 team runs is ~7 requests
#   - label_feedback.py        : only classifies unlabelled turns (no --relabel)
#
# THE TRADEOFF, STATED PLAINLY
# Nightly's narrow feedback window means a conversation OLDER than the window that
# newly collects a rating is missed until the weekly pass. Ratings arrive close to
# the conversation, so this is rare -- but it is the reason weekly exists. Widen
# FEEDBACK_WINDOW_DAYS if you would rather trade requests for immediacy.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

MODE="${1:-nightly}"
# Discovery window: how far back to look for conversations that have feedback at all.
# Cheap (16 date-window requests for 21 days), so it stays generous.
FEEDBACK_WINDOW_DAYS="${FEEDBACK_WINDOW_DAYS:-21}"
# Re-fetch window: THE dominant nightly cost. extract.py re-pulls the transcript of
# every conversation this recent, because a conversation can collect new ratings after
# its first pull. Measured on 2026-08-10: 14 days = 539 transcripts (2m12s), 3 days =
# 99. Since nightly runs every night, a conversation still gets re-checked on three
# consecutive nights, and the weekly pass sweeps the full 14 as a backstop.
NIGHTLY_REFRESH_DAYS="${NIGHTLY_REFRESH_DAYS:-3}"
# How long a directly-invoked refresh waits for another run to finish. Default 0:
# a run you started by hand should say who is holding the warehouse and stop, not
# sit there looking wedged. Set LOCK_WAIT_MINS=90 to queue behind it instead.
LOCK_WAIT_MINS="${LOCK_WAIT_MINS:-0}"
LOCK="${LOCK:-.refresh.lock}"
LOG_DIR="logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/refresh-$(date +%Y%m%d-%H%M%S)-$MODE.log"

# Tee everything: this is meant to run unattended, so the log is the record.
exec > >(tee -a "$LOG") 2>&1

say() { printf '\n=== %s  [%s] ===\n' "$1" "$(date +%H:%M:%S)"; }
SECONDS=0

# Run a step; abort the whole refresh if it fails, so a half-updated warehouse is
# never rendered into a dashboard that looks authoritative.
step() {
  local label="$1"; shift
  say "$label"
  printf '  $ %s\n' "$*"
  if ! "$@"; then
    printf '\nFAILED at "%s" after %dm%02ds. Warehouse left as-is; no dashboard rendered.\n' \
      "$label" $((SECONDS / 60)) $((SECONDS % 60))
    exit 1
  fi
}

# ------------------------------------------------------------------- the lock
# Two refreshes must never touch the SQLite warehouse at once. This lived only in
# refresh-scheduled.sh, which left the hole that actually bit on 2026-09-02: a
# hand-run ./refresh.sh does not go through the wrapper, so it happily ran
# alongside a scheduled attempt with nothing to stop it.
#
# mkdir is the atomic primitive available here: macOS ships no flock(1), and this
# is bash 3.2. The pid file lets a crashed run's lock be reclaimed rather than
# blocking every subsequent night.
#
# REFRESH_LOCK_HELD is how refresh-scheduled.sh says "I already hold this" -- the
# wrapper locks, then calls this script, so without that hand-off the child would
# block on its own parent's lock until LOCK_WAIT_MINS ran out.
acquire_lock() {
  if [ -n "${REFRESH_LOCK_HELD:-}" ]; then
    printf 'Lock already held by pid %s (scheduled wrapper) — not re-acquiring.\n' \
      "$REFRESH_LOCK_HELD"
    return 0
  fi
  local waited=0 holder
  while ! mkdir "$LOCK" 2>/dev/null; do
    holder="$(cat "$LOCK/pid" 2>/dev/null)"
    if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
      printf 'Stale lock from dead pid %s — reclaiming.\n' "$holder"
      rm -rf "$LOCK"
      continue
    fi
    if [ "$waited" -ge "$LOCK_WAIT_MINS" ]; then
      printf 'ABORT: refresh pid %s is already updating the warehouse.\n' "${holder:-?}"
      printf 'Wait for it, or re-run with LOCK_WAIT_MINS=90 to queue behind it.\n'
      exit 1
    fi
    [ "$waited" -eq 0 ] && printf 'Waiting for refresh pid %s to finish (up to %sm) ...\n' \
      "${holder:-?}" "$LOCK_WAIT_MINS"
    sleep 60
    waited=$((waited + 1))
  done
  printf '%s\n' "$$" > "$LOCK/pid"
  trap 'rm -rf "$LOCK"' EXIT
  trap 'printf "\ninterrupted — releasing lock\n"; rm -rf "$LOCK"; exit 130' INT TERM
}

# `status` is read-only, so it neither takes the lock nor waits behind one.
case "$MODE" in
  nightly|weekly|full) acquire_lock ;;
esac

case "$MODE" in
  status)
    step "status" python3 extract.py status
    exit 0
    ;;

  nightly)
    SINCE=$(date -v-"${FEEDBACK_WINDOW_DAYS}"d +%F)
    printf 'Nightly refresh — skipping the 428-request workflow fan-out;\n'
    printf 'feedback discovery narrowed to %s .. today (%s-day window).\n' \
      "$SINCE" "$FEEDBACK_WINDOW_DAYS"
    step "agent runs (incremental, no workflow fan-out)" \
      python3 extract.py facts --incremental --skip-workflows
    step "feedback (${FEEDBACK_WINDOW_DAYS}-day discovery, ${NIGHTLY_REFRESH_DAYS}-day re-fetch)" \
      python3 extract.py feedback --since "$SINCE" \
        --refresh-days "$NIGHTLY_REFRESH_DAYS"
    ;;

  weekly|full)
    printf 'Weekly reconciliation — full workflow fan-out and full-history\n'
    printf 'feedback discovery. Expect this one to take a while.\n'
    step "agent runs (incremental, with teams + workflow fan-out)" \
      python3 extract.py facts --incremental
    step "feedback (full history)" python3 extract.py feedback
    ;;

  *)
    printf 'usage: %s [nightly|weekly|status]\n' "$0" >&2
    exit 2
    ;;
esac

step "classify new complaints" python3 label_feedback.py
step "render dashboard.html" python3 build_dashboard.py
step "render dashboard-forge.html" python3 build_forge_poc.py

# The warehouse lives outside OneDrive because ~/Library/CloudStorage is TCC-protected
# and a launchd job cannot read it (and a 289 MB SQLite has no business being synced).
# The two rendered pages DO belong in OneDrive, for sharing and backup, so publish just
# those. Non-fatal: a failed copy must not fail an otherwise good refresh.
PUBLISH_DIR="${PUBLISH_DIR:-$HOME/Library/CloudStorage/OneDrive-TylerTechnologies,Inc/Documents PC/AI_Busyness/foundry-analytics-output}"
say "publish rendered pages to OneDrive"
if mkdir -p "$PUBLISH_DIR" 2>/dev/null; then
  for f in dashboard.html dashboard-forge.html; do
    if cp "$f" "$PUBLISH_DIR/$f" 2>/dev/null; then
      printf '  copied %s\n' "$f"
    else
      printf '  WARNING: could not copy %s to OneDrive (refresh itself succeeded)\n' "$f"
    fi
  done
  # The Forge page loads these as separate files, so a published copy without them
  # renders unstyled with no components.
  for f in forge-bundle.js forge.css forge-core.css forge-dark.css; do
    cp "$f" "$PUBLISH_DIR/$f" 2>/dev/null || printf '  WARNING: could not copy %s\n' "$f"
  done
  printf '  published to %s\n' "$PUBLISH_DIR"
else
  printf '  WARNING: %s unavailable — skipped publishing (refresh itself succeeded)\n' \
    "$PUBLISH_DIR"
fi

say "done"
printf 'Mode: %s · elapsed %dm%02ds · log: %s\n' \
  "$MODE" $((SECONDS / 60)) $((SECONDS % 60)) "$LOG"
# Summary is informational only: never let it decide the script's exit status, or a
# cron job reports failure on a refresh that actually succeeded.
python3 - <<'PY' || true
import sqlite3
c = sqlite3.connect('foundry_analytics.db')
q = lambda s: c.execute(s).fetchone()[0]
DOWN = "SELECT COUNT(*) FROM v_feedback_turns WHERE rating='THUMBS_DOWN'"
COMMENTED = DOWN + " AND comment IS NOT NULL AND comment<>''"
UNLABELLED = COMMENTED + " AND (label IS NULL OR label='')"
print(f"  agent runs      {q('SELECT COUNT(*) FROM fact_agent_run'):,}"
      f"   through {q('SELECT MAX(run_day) FROM v_agent_runs')}")
print(f"  feedback turns  {q('SELECT COUNT(*) FROM fact_feedback_turn'):,}"
      f"   through {q('SELECT MAX(conv_day) FROM v_feedback_turns')}")
print(f"  thumbs-down     {q(DOWN):,} ({q(COMMENTED):,} commented)")
unl = q(UNLABELLED)
print(f"  unclassified    {unl:,}" + ("  <- rerun label_feedback.py" if unl else ""))
PY

exit 0

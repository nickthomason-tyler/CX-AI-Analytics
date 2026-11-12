#!/bin/bash
# Retry wrapper around refresh.sh, for launchd-scheduled runs only.
#
#   ./refresh-scheduled.sh nightly [--force]
#   ./refresh-scheduled.sh weekly  [--force]
#
# WHY THIS EXISTS
# refresh.sh aborts the whole run if any step fails, which is correct -- a
# half-updated warehouse must never be rendered into an authoritative-looking
# dashboard. But it means a *transient* failure costs the whole day. On
# 2026-08-19 the 12:15 nightly died 48 minutes in with
#   URLError on /api/agent-logs: [Errno 8] nodename nor servname provided
# i.e. DNS did not resolve: the Mac was awake enough for launchd to fire the job
# but had no usable network (asleep Wi-Fi / off VPN). Nothing was wrong with the
# warehouse, the credentials, or the code. The data simply went two days stale
# because a scheduled job got one attempt and no more.
#
# So this wrapper adds, in order:
#   1. a lock, so two refreshes never touch the SQLite warehouse at once
#   2. a preflight wait for the API host to actually be reachable -- this is the
#      real fix; retrying into a dead network just burns the retry budget
#   3. bounded retries with backoff on a genuine failure
#   4. a per-attempt watchdog, so a wedged attempt cannot eat the whole budget
#   5. a once-per-day success stamp, which makes a second calendar slot free:
#      if 12:15 succeeded, the 19:15 slot exits in a second having done nothing
#
# It deliberately does NOT use launchd's KeepAlive/SuccessfulExit=false. Combined
# with StartCalendarInterval that relaunches the job in a tight loop with no
# backoff and no network awareness, which against a rate-limited API is worse
# than the failure it is trying to fix.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

MODE="${1:-nightly}"
FORCE="${2:-}"

case "$MODE" in
  nightly)      ATTEMPT_CAP_MINS="${ATTEMPT_CAP_MINS:-150}" ;;
  weekly|full)  ATTEMPT_CAP_MINS="${ATTEMPT_CAP_MINS:-420}" ;;
  status)       exec ./refresh.sh status ;;   # read-only: no lock, no retry
  *)            printf 'usage: %s [nightly|weekly] [--force]\n' "$0" >&2; exit 2 ;;
esac

MAX_ATTEMPTS="${MAX_ATTEMPTS:-4}"
BACKOFF_MINS="${BACKOFF_MINS:-10 30 60}"   # waits before attempts 2, 3, 4
NET_WAIT_MINS="${NET_WAIT_MINS:-45}"       # how long to wait for a usable network
LOCK_WAIT_MINS="${LOCK_WAIT_MINS:-90}"     # how long to wait for another run to finish

# Overridable so the wrapper can be exercised against a fixture config.
CFG="${FOUNDRY_CFG:-$HOME/.config/foundry/foundry.local.md}"
LOCK=".refresh.lock"
STATE="state"
mkdir -p logs "$STATE"

WLOG="logs/scheduled-$(date +%Y%m%d-%H%M%S)-$MODE.log"
exec > >(tee -a "$WLOG") 2>&1

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

# ---------------------------------------------------------------- already done?
STAMP="$STATE/last-success-$MODE"
TODAY="$(date +%F)"
if [ "$FORCE" != "--force" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$TODAY" ]; then
  log "a $MODE refresh already succeeded today ($TODAY) — nothing to do."
  exit 0
fi

# -------------------------------------------------------------------- the lock
# mkdir is the atomic primitive available here: macOS ships no flock(1), and this
# is bash 3.2. The pid file lets a crashed run's lock be reclaimed rather than
# blocking every subsequent night.
waited=0
while ! mkdir "$LOCK" 2>/dev/null; do
  holder="$(cat "$LOCK/pid" 2>/dev/null)"
  if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
    log "stale lock from dead pid $holder — reclaiming"
    rm -rf "$LOCK"
    continue
  fi
  if [ "$waited" -ge "$LOCK_WAIT_MINS" ]; then
    log "ABORT: another refresh (pid ${holder:-?}) still holds the lock after ${LOCK_WAIT_MINS}m."
    exit 1
  fi
  [ "$waited" -eq 0 ] && log "waiting for refresh pid ${holder:-?} to finish before starting $MODE"
  sleep 60
  waited=$((waited + 1))
done
printf '%s\n' "$$" > "$LOCK/pid"
# refresh.sh takes this same lock when run directly. Telling it we already hold
# the lock is what keeps the child from blocking on its own parent.
export REFRESH_LOCK_HELD=$$
release_lock() { rm -rf "$LOCK"; }
trap release_lock EXIT
# A bare cleanup handler on INT/TERM would free the lock and then let the retry
# loop carry on. Signals must actually stop the run.
trap 'log "interrupted — releasing lock and stopping"; release_lock; exit 130' INT TERM

# ------------------------------------------------------------ network preflight
# Host is read from the same config foundry_client.py uses, so this probe follows
# the real base URL instead of a second copy that can drift. Only the hostname is
# touched; the API key is never read here.
HOST="$(grep -i '^- *Base URL' "$CFG" 2>/dev/null | head -1 \
        | sed 's/^[^:]*: *//' | sed -E 's#^https?://##; s#[/:].*##')"

net_ok() {
  [ -z "$HOST" ] && return 0                     # cannot probe -> do not block
  curl -sS -o /dev/null --max-time 15 "https://$HOST/" 2>/dev/null
}

wait_for_net() {
  [ -z "$HOST" ] && { log "no Base URL in $CFG — skipping the reachability probe"; return 0; }
  local n=0
  while ! net_ok; do
    if [ "$n" -ge "$NET_WAIT_MINS" ]; then
      log "GIVING UP: $HOST unreachable for ${NET_WAIT_MINS}m. No network, so no refresh."
      return 1
    fi
    [ "$n" -eq 0 ] && log "$HOST unreachable — waiting for a usable network (up to ${NET_WAIT_MINS}m)"
    sleep 60
    n=$((n + 1))
  done
  [ "$n" -gt 0 ] && log "$HOST reachable after ${n}m"
  return 0
}

# --------------------------------------------------------- one guarded attempt
# set -m puts refresh.sh in its own process group, so the watchdog can kill the
# whole tree. Killing the bash pid alone would leave python3 running and holding
# the warehouse.
attempt() {
  set -m
  ./refresh.sh "$MODE" &
  local child=$!
  set +m

  ( sleep $((ATTEMPT_CAP_MINS * 60))
    if kill -0 "$child" 2>/dev/null; then
      printf '[%s] WATCHDOG: attempt exceeded %dm — terminating process group\n' \
        "$(date +%H:%M:%S)" "$ATTEMPT_CAP_MINS"
      kill -TERM -"$child" 2>/dev/null
      sleep 20
      kill -KILL -"$child" 2>/dev/null
    fi ) &
  local dog=$!

  wait "$child"; local rc=$?
  kill "$dog" 2>/dev/null
  wait "$dog" 2>/dev/null
  return $rc
}

# ------------------------------------------------------------------- the loop
log "scheduled $MODE refresh — up to $MAX_ATTEMPTS attempts, watchdog ${ATTEMPT_CAP_MINS}m/attempt"
START=$SECONDS
set -- $BACKOFF_MINS
n=1
while :; do
  if wait_for_net; then
    log "attempt $n/$MAX_ATTEMPTS"
    # Capture the status directly: after an `if` with a false condition and no
    # else branch, $? is the if-statement's own 0, not the command's failure.
    attempt; rc=$?
    if [ "$rc" -eq 0 ]; then
      printf '%s\n' "$TODAY" > "$STAMP"
      log "SUCCESS on attempt $n after $(( (SECONDS - START) / 60 ))m total. Stamped $STAMP."
      exit 0
    fi
    # refresh.sh exits 2 only for a bad mode argument. Retrying cannot fix that.
    if [ "$rc" -eq 2 ]; then
      log "ABORT: refresh.sh rejected mode '$MODE' (exit 2). Not retrying."
      exit 2
    fi
    log "attempt $n failed (exit $rc)"
  fi

  if [ "$n" -ge "$MAX_ATTEMPTS" ]; then
    log "FAILED: $MAX_ATTEMPTS attempts over $(( (SECONDS - START) / 60 ))m. Warehouse untouched; no dashboard published."
    log "See the per-attempt logs: logs/refresh-$(date +%Y%m%d)-*-$MODE.log"
    exit 1
  fi

  back="${1:-60}"; [ $# -gt 0 ] && shift
  log "backing off ${back}m before attempt $((n + 1))"
  sleep $((back * 60))
  n=$((n + 1))
done

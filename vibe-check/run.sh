#!/bin/sh
# Hourly vibe-check: generate today's editorial copy, prune old messages,
# then commit + push repo-root vibe.json so the site redeploys.
#
# Designed to run from launchd after the hourly data collection. It waits
# (bounded) for the collector lock so it never races a partially-written
# weather/events/news document.
#
# Usage:
#   ./run.sh                 # normal hourly run
#   VIBE_NO_PUSH=1 ./run.sh  # generate + write, skip commit/push
#   VIBE_DRY_RUN=1 ./run.sh  # call the model, print, write nothing
#
# Env knobs (defaults in brackets):
#   VIBE_NODE           [/opt/homebrew/bin/node]  node binary
#   VIBE_NO_PUSH        [unset]                   set to 1 to skip commit/push
#   VIBE_DRY_RUN        [unset]                   set to 1 to skip write/push
#   VIBE_WAIT_LIMIT     [1800]                    seconds to wait for the collector lock
#   OPENROUTER_VIBE_MODEL, VIBE_RETENTION_HOURS   passed through to generate.mjs

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
NODE="${VIBE_NODE:-/opt/homebrew/bin/node}"
NODE="${NODE:-/opt/homebrew/bin/node}"
LOCK="/tmp/dailybrief-vibe.lock"
COLLECT_LOCK="/tmp/dailybrief-collect.lock"
LOG="$DIR/vibe.log"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

# Keep the Mac awake for the API call + push, same trick as run_collect.sh.
if [ "${VIBE_CAFFEINATED:-}" != "1" ]; then
    VIBE_CAFFEINATED=1
    export VIBE_CAFFEINATED
    exec /usr/bin/caffeinate -i "$0" "$@"
fi

# Serialize runs; shlock detects stale locks by pid.
if ! /usr/bin/shlock -f "$LOCK" -p $$ >/dev/null 2>&1; then
    echo "$STAMP [skip] another vibe-check is still running" >> "$LOG"
    exit 0
fi
trap 'rm -f "$LOCK"' EXIT INT TERM

# Run after the hourly collection. When launched standalone, wait for the
# collector's lock to clear so the JSON documents are complete; when chained
# from the collector itself (VIBE_SKIP_COLLECT_WAIT=1) the documents are
# already published, so skip the wait.
waited=0
if [ "${VIBE_SKIP_COLLECT_WAIT:-}" != "1" ]; then
    while [ -f "$COLLECT_LOCK" ] && [ "$waited" -lt "${VIBE_WAIT_LIMIT:-1800}" ]; do
        sleep 30
        waited=$((waited + 30))
    done
    # Still collecting? Skip this tick rather than read a half-written document.
    # The next hourly tick will find a gap.
    if [ -f "$COLLECT_LOCK" ]; then
        echo "$STAMP [skip] collection still running after ${waited}s; will retry next tick" >> "$LOG"
        exit 0
    fi
fi

exec >> "$LOG" 2>&1
echo "$STAMP [start] vibe-check (waited ${waited}s for collect)"

if [ ! -x "$NODE" ]; then
    NODE="$(command -v node || true)"
fi
if [ -z "$NODE" ] || [ ! -x "$NODE" ]; then
    echo "$STAMP [done] exit=1 (node not found)"
    exit 1
fi

cd "$DIR" || exit 1

set -- 
if [ "${VIBE_DRY_RUN:-}" = "1" ]; then set -- --dry-run --print; fi
"$NODE" generate.mjs "$@" || { echo "$STAMP [done] exit=1 (generate failed)"; exit 1; }

if [ "${VIBE_DRY_RUN:-}" = "1" ] || [ "${VIBE_NO_PUSH:-}" = "1" ]; then
    echo "$STAMP [done] exit=0 (push skipped)"
    exit 0
fi

# --- commit + push only vibe.json ------------------------------------------ #
cd "$ROOT" || exit 1
# Only ever stage/snapshot vibe.json so unrelated work in the tree is never
# swept into this commit.
git add -- vibe.json
if git diff --cached --quiet -- vibe.json; then
    echo "$STAMP [done] exit=0 (unchanged)"
    exit 0
fi
DATE="$(date '+%Y-%m-%d')"
git -c user.name="daily-brief bot" -c user.email="phurley@gmail.com" \
    commit -m "Update daily editorial vibe for $DATE" -- vibe.json \
    || { echo "$STAMP [done] exit=1 (commit failed)"; exit 1; }
if ! git pull --rebase --autostash origin main; then
    echo "$STAMP [done] exit=1 (pull --rebase failed; commit left local)"
    exit 1
fi
if git push; then
    echo "$STAMP [done] exit=0 (pushed)"
else
    echo "$STAMP [done] exit=1 (push failed; commit left local)"
    exit 1
fi
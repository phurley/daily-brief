#!/bin/sh
# Nightly family-calendar refresh: fetch the Google/iCloud ICS feeds into
# calendar.json, then commit and push it when it changed.
#
# Runs from launchd once a night (com.dailybrief.calendar). Feed definitions
# live in the git-ignored calendars.json (or CALENDARS_JSON in the env).
#
# Usage:
#   ./scripts/run_calendar.sh                    # collect + commit + push
#   CALENDAR_NO_PUSH=1 ./scripts/run_calendar.sh # collect only, no commit/push
#
# Env knobs:
#   CALENDAR_PYTHON  [/opt/homebrew/bin/python3]  interpreter to run the collector
#   CALENDAR_NO_PUSH [unset]                      set to 1 to skip commit/push

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
PY="${CALENDAR_PYTHON:-/opt/homebrew/bin/python3}"
LOCK="/tmp/dailybrief-calendar.lock"
LOG="$ROOT/data-collect/cron.log"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

# Re-exec once under caffeinate so a run keeps the Mac awake through the push.
if [ "${DAILYBRIEF_CAFFEINATED:-}" != "1" ]; then
    DAILYBRIEF_CAFFEINATED=1
    export DAILYBRIEF_CAFFEINATED
    exec /usr/bin/caffeinate -i "$0" "$@"
fi

# Serialize runs; shlock detects stale locks by pid.
if ! /usr/bin/shlock -f "$LOCK" -p $$ >/dev/null 2>&1; then
    echo "$STAMP [skip] another calendar refresh is still running" >> "$LOG"
    exit 0
fi
trap 'rm -f "$LOCK"' EXIT INT TERM

exec >> "$LOG" 2>&1
echo "$STAMP [start] calendar refresh"

cd "$ROOT" || exit 1

if ! "$PY" scripts/update_calendar.py; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] collection failed"
    exit 1
fi

if [ "${CALENDAR_NO_PUSH:-}" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] push skipped (CALENDAR_NO_PUSH=1)"
    exit 0
fi

# Only ever stage/snapshot calendar.json so a concurrent edit elsewhere in the
# work tree is never swept into this commit.
git add -- calendar.json
if git diff --cached --quiet -- calendar.json; then
    echo "calendar.json unchanged; nothing to push"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] unchanged"
    exit 0
fi

git -c user.name="daily-brief bot" -c user.email="phurley@gmail.com" \
    commit -m "Refresh calendar snapshot" -- calendar.json || { echo "commit failed"; exit 1; }

if ! git pull --rebase --autostash origin main; then
    echo "pull --rebase failed; leaving commit local"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] push failed"
    exit 1
fi

if git push; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] pushed"
else
    echo "push failed; leaving commit local"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] push failed"
    exit 1
fi
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
    "$PY" scripts/publish_brief.py --no-push
else
    "$PY" scripts/publish_brief.py calendar.json
fi

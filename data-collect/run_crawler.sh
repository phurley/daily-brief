#!/bin/sh
# Cron wrapper for the Daily Brief collector.
#
# - serializes runs (no overlap if a crawl runs long)
# - keeps the Mac awake during the run (caffeinate)
# - appends timestamped output to cron.log
#
# Usage: run_crawler.sh [crawl_sources.py args...]
# Cron:  0 * * * * /Users/phurley/daily-brief/data-collect/run_crawler.sh --concurrency 8

DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK="/tmp/dailybrief-crawl.lock"
LOG="$DIR/cron.log"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

if ! /usr/bin/shlock -f "$LOCK" -p $$ >/dev/null 2>&1; then
    echo "$STAMP [skip] another crawl is still running" >> "$LOG"
    exit 0
fi
trap 'rm -f "$LOCK"' EXIT INT TERM

echo "$STAMP [start] crawl_sources $*" >> "$LOG"
/usr/bin/caffeinate -i "$DIR/.venv/bin/python" "$DIR/crawl_sources.py" "$@" >> "$LOG" 2>&1
rc=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') [done] exit=$rc" >> "$LOG"
exit $rc
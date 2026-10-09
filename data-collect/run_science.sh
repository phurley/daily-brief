#!/bin/sh
# Hourly independent science refresh. Source intervals live in science-sources.json.
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
LOCK=/tmp/dailybrief-science.lock
if [ "${SCIENCE_CAFFEINATED:-}" != 1 ]; then
    export SCIENCE_CAFFEINATED=1
    exec /usr/bin/caffeinate -i "$0" "$@"
fi
if ! /usr/bin/shlock -f "$LOCK" -p $$ >/dev/null 2>&1; then exit 0; fi
trap 'rm -f "$LOCK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
exec >> "$DIR/science.log" 2>&1
echo "$(date '+%Y-%m-%d %H:%M:%S') [start] science"
cd "$DIR" || exit 1
before="$(cksum "$ROOT/geeknews.json")"
"$DIR/.venv/bin/python" -u -m science.pipeline "$@"
rc=$?
# A failed refresh still publishes its health report and preserves the edition.
if [ "${SCIENCE_NO_PUSH:-}" != 1 ]; then
    "$ROOT/scripts/push_generated.sh" "Refresh science digest and source health" geeknews.json science-health.json || exit 1
fi
if [ "$rc" = 0 ] && [ "$before" != "$(cksum "$ROOT/geeknews.json")" ] && [ "${SCIENCE_VIBE_ENABLED:-1}" = 1 ]; then
    VIBE_SKIP_SCIENCE_WAIT=1 VIBE_MIN_INTERVAL=0 VIBE_NO_PUSH="${SCIENCE_NO_PUSH:-0}" "$ROOT/vibe-check/run.sh" || echo "science editorial refresh failed"
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') [done] science exit=$rc"
exit "$rc"

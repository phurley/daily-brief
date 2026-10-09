#!/bin/sh
# Independent hourly source collection, immutable history and publication.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE="${STARSHIP_NODE:-/opt/homebrew/bin/node}"
LOCK=/tmp/dailybrief-starship.lock
if ! /usr/bin/shlock -f "$LOCK" -p $$; then exit 0; fi
trap 'rm -f "$LOCK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cd "$ROOT"
if [ "${STARSHIP_DRY_RUN:-0}" = 1 ]; then
    "$NODE" starship/collect.mjs --dry-run
    exit 0
fi
/usr/bin/caffeinate -i "$NODE" starship/collect.mjs
if [ "${STARSHIP_NO_PUSH:-0}" = 1 ]; then exit 0; fi
# Source history and compact web/widget edition share one scoped publication.
exec /usr/bin/python3 scripts/publish_brief.py starship.json starship/history

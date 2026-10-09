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
# Serialize with other recurring git writers. Generation can run independently.
. "$ROOT/scripts/git-publish-lock.sh"
acquire_publish_lock
# Never include unrelated staged files.
git add -- starship.json starship/history
git -c user.name="daily-brief bot" -c user.email="phurley@gmail.com" commit -m "Refresh Starship evidence and forecast history" -- starship.json starship/history
# Retry an earlier unpushed commit as well as today's publication.
git pull --rebase --autostash origin main
git push origin main

#!/bin/sh
# Serialize all scheduled writers' git operations; commit only named outputs.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK=/tmp/dailybrief-publish.lock
message="$1"
shift
waited=0
until /usr/bin/shlock -f "$LOCK" -p $$ >/dev/null 2>&1; do
    if [ "$waited" -ge 120 ]; then echo "git writer busy; retry next run"; exit 1; fi
    sleep 5
    waited=$((waited + 5))
done
trap 'rm -f "$LOCK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cd "$ROOT"
git add -- "$@"
if ! git diff --cached --quiet -- "$@"; then
    git -c user.name="daily-brief bot" -c user.email="phurley@gmail.com" commit --only -m "$message" -- "$@"
fi
# Always retry a previous failed push, even when this run produced no changes.
git fetch origin main
if ! git merge-base --is-ancestor origin/main HEAD; then
    git rebase --autostash origin/main
fi
git push origin HEAD:main

#!/bin/sh
# Roll the Detroit edition date even when no collector has new content.
# Scheduled runs start from a clean origin/main in the deploy checkout (scripts/deploy_guard.py).
DEPLOY_GUARD="$(cd "$(dirname "$0")/.." && pwd)/scripts/deploy_guard.py"
if [ -z "${DAILYBRIEF_DEPLOY_GUARD:-}" ] && [ -f "$DEPLOY_GUARD" ]; then exec /usr/bin/python3 "$DEPLOY_GUARD" "$0" "$@"; fi
set -eu
cd "$(dirname "$0")/.."
data-collect/.venv/bin/python data-collect/review_events.py
exec /usr/bin/python3 scripts/publish_brief.py event-editorial.json

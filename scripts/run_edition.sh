#!/bin/sh
# Roll the Detroit edition date even when no collector has new content.
set -eu
cd "$(dirname "$0")/.."
data-collect/.venv/bin/python data-collect/review_events.py
exec /usr/bin/python3 scripts/publish_brief.py event-editorial.json

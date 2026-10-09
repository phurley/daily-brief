#!/bin/sh
# Roll the Detroit edition date even when no collector has new content.
set -eu
cd "$(dirname "$0")/.."
exec /usr/bin/python3 scripts/publish_brief.py

#!/bin/sh
# Shared science publisher; keep the existing caller interface (message, paths).
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
shift
exec /usr/bin/python3 "$ROOT/scripts/publish_brief.py" "$@"

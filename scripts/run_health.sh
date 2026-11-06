#!/bin/sh
# Daily health report at 08:00, after the night's jobs (com.dailybrief.health).
# On Sundays the model quality canary runs first, so the report includes it.
#
# Writes data-collect/processed/health/<date>.{md,json} and notifies through
# HEALTH_NTFY_TOPIC / HEALTH_WEBHOOK_URL (data-collect/.env) when anything is
# off. HEALTH_RUN_CANARY=1 runs the canary on any day.
DEPLOY_GUARD="$(cd "$(dirname "$0")/.." && pwd)/scripts/deploy_guard.py"
if [ -z "${DAILYBRIEF_DEPLOY_GUARD:-}" ] && [ -f "$DEPLOY_GUARD" ]; then exec /usr/bin/python3 "$DEPLOY_GUARD" "$0" "$@"; fi
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/data-collect" || exit 1
echo "$(date '+%Y-%m-%d %H:%M:%S') [start] health"
if [ "$(date +%u)" = 7 ] || [ "${HEALTH_RUN_CANARY:-}" = 1 ]; then
    .venv/bin/python quality_canary.py || echo "quality canary: regression flagged or run failed"
fi
.venv/bin/python health_report.py "$@"
rc=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') [done] health exit=$rc"
exit "$rc"

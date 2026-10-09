#!/bin/sh
# Full Daily Brief collection, publish, and editorial refresh.
#
#   1. crawl_sources.py  -> data-collect/crawl/   (only sources that are due)
#   2. extraction funnel -> repo-root events.json / news.json
#   3. select recommendations, then commit + push all three data documents
#   4. vibe-check/run.sh -> refresh repo-root vibe.json (best-effort)
#
# Serialized with a lock, keeps the Mac awake, appends to data-collect/cron.log.
# Designed to run hourly from cron/launchd: the crawler itself decides which
# sources are due from their `frequency`, so an invocation with nothing due
# exits immediately after step 1.
#
# Usage:
#   ./run_collect.sh                 # normal hourly run
#   ./run_collect.sh --force         # re-crawl every source (args go to crawl_sources.py)
#   COLLECT_NO_PUSH=1 ./run_collect.sh --limit 2   # verify without touching GitHub
#
# Env knobs (defaults in brackets):
#   COLLECT_OUT_DIR      [/repo root]   where events.json/news.json are written
#   COLLECT_NO_PUSH      [unset]        set to 1 to skip commit/push
#   COLLECT_MAX_ITEMS    [2000]         funnel --max-items (Jev calls per run;
#                                          processed candidates are fingerprinted
#                                          and never re-paid, so this is a cap, not
#                                          a steady-state cost)
#   COLLECT_EXTRACT_LIMIT[800]          funnel --extract-limit (generation calls)
#   COLLECT_CONCURRENCY  [8]            crawler --concurrency
#   EVENT_IDENTITY_MODE [shadow]       legacy, shadow, or canonical publication
#   EVENT_IDENTITY_FUZZY[0]            1 enables constrained detail-URL typo matching

DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
PY="$DIR/.venv/bin/python"
LOCK="/tmp/dailybrief-collect.lock"
LOG="$DIR/cron.log"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

# Final step: refresh the editorial vibe now that the documents are published.
# Best-effort: a vibe failure must never fail the data collection.
run_vibe() {
    # Release the shared Git lock before the chained writer acquires it, but
    # retain the collection lock until this process exits.
    if [ -n "${PUBLISH_LOCK:-}" ]; then
        rm -f "$PUBLISH_LOCK"
        PUBLISH_LOCK=""
        trap 'rm -f "$LOCK"' EXIT
    fi
    [ "${VIBE_ENABLED:-1}" = "1" ] || return 0
    echo "--- step 4/4: editorial vibe ---"
    VIBE_SKIP_COLLECT_WAIT=1 "$ROOT/vibe-check/run.sh" || echo "vibe-check failed; data push is unaffected"
}

# Re-exec once under caffeinate so the whole run (crawl + funnel + push) keeps
# the Mac awake, not just the crawl. Same PID, so the lock below still works.
if [ "${DAILYBRIEF_CAFFEINATED:-}" != "1" ]; then
    DAILYBRIEF_CAFFEINATED=1
    export DAILYBRIEF_CAFFEINATED
    exec /usr/bin/caffeinate -i "$0" "$@"
fi

# Serialize runs; shlock detects stale locks by pid.
if ! /usr/bin/shlock -f "$LOCK" -p $$ >/dev/null 2>&1; then
    echo "$STAMP [skip] another collection is still running" >> "$LOG"
    exit 0
fi
trap 'rm -f "$LOCK"' EXIT INT TERM

# Everything below is timestamped into cron.log.
exec >> "$LOG" 2>&1
echo "$STAMP [start] collect $*"

cd "$DIR" || exit 1

# Pin the rollout policy for scheduled and manual runs alike. Validate before
# spending crawler/model work; the publisher reads these exported variables.
EVENT_IDENTITY_MODE="${EVENT_IDENTITY_MODE:-shadow}"
EVENT_IDENTITY_FUZZY="${EVENT_IDENTITY_FUZZY:-0}"
case "$EVENT_IDENTITY_MODE" in legacy|shadow|canonical) ;; *) echo "invalid EVENT_IDENTITY_MODE"; exit 1 ;; esac
case "$EVENT_IDENTITY_FUZZY" in 0|1) ;; *) echo "invalid EVENT_IDENTITY_FUZZY"; exit 1 ;; esac
export EVENT_IDENTITY_MODE EVENT_IDENTITY_FUZZY
echo "event identity: mode=$EVENT_IDENTITY_MODE fuzzy=$EVENT_IDENTITY_FUZZY"

# --- step 1: crawl only the sources that are due --------------------------- #
echo "--- step 1/4: crawl (due sources only) ---"
"$PY" crawl_sources.py --concurrency "${COLLECT_CONCURRENCY:-8}" "$@"
crawl_rc=$?
if [ "$crawl_rc" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] crawl failed exit=$crawl_rc"
    exit "$crawl_rc"
fi

# --- step 2: mechanical funnel + extraction + publish ---------------------- #
echo "--- step 2/4: extraction funnel ---"
"$PY" -m extract || { echo "extract failed"; exit 1; }
"$PY" -m extract.pipeline \
    --max-items "${COLLECT_MAX_ITEMS:-2000}" \
    --extract-limit "${COLLECT_EXTRACT_LIMIT:-800}" \
    || { echo "pipeline failed"; exit 1; }

RECORDS="$DIR/processed/extracted_records.jsonl"
if [ ! -s "$RECORDS" ]; then
    echo "no extracted records this run; keeping existing events.json/news.json"
    run_vibe
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] exit=0 (no records)"
    exit 0
fi

OUT_DIR="${COLLECT_OUT_DIR:-$ROOT}"
"$PY" -m extract.publish --out "$OUT_DIR" || { echo "publish failed"; exit 1; }

# Public shortlist shares the browser/editorial selector; never read private profiles.
NODE="$(command -v node || true)"
[ -n "$NODE" ] || NODE=/opt/homebrew/bin/node
"$NODE" "$ROOT/scripts/select-best-bets.mjs" --events "$OUT_DIR/events.json" --out "$OUT_DIR/recommendations.json" || { echo "shortlist failed"; exit 1; }

# --- step 3: commit + push the published JSON ------------------------------ #
if [ "${COLLECT_NO_PUSH:-}" = "1" ]; then
    echo "--- step 3/3: push skipped (COLLECT_NO_PUSH=1) ---"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] exit=0 (no push)"
    exit 0
fi

echo "--- step 3/4: commit + push events.json/news.json/recommendations.json ---"
cd "$ROOT" || exit 1
. "$ROOT/scripts/git-publish-lock.sh"
acquire_publish_lock
git add events.json news.json recommendations.json
if git diff --cached --quiet -- events.json news.json recommendations.json; then
    echo "events.json/news.json/recommendations.json unchanged; nothing to push"
    run_vibe
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] exit=0 (unchanged)"
    exit 0
fi
git -c user.name="daily-brief bot" -c user.email="phurley@gmail.com" \
    commit -m "Update events, news, and recommendations" -- events.json news.json recommendations.json || { echo "commit failed"; exit 1; }
if ! git pull --rebase --autostash origin main; then
    echo "pull --rebase failed; leaving commit local"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] exit=1 (push failed)"
    exit 1
fi
if git push; then
    run_vibe
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] exit=0 (pushed)"
else
    echo "push failed; leaving commit local"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [done] exit=1 (push failed)"
    exit 1
fi
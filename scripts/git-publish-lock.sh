# Source only. All local scheduled git writers share this short-lived lock.
acquire_publish_lock() {
    PUBLISH_LOCK=/tmp/dailybrief-publish.lock
    publish_wait=0
    until /usr/bin/shlock -f "$PUBLISH_LOCK" -p $$ >/dev/null 2>&1; do
        publish_wait=$((publish_wait + 1))
        if [ "$publish_wait" -ge 120 ]; then
            echo "Timed out waiting for daily-brief git publication" >&2
            exit 1
        fi
        sleep 1
    done
    # Callers already hold their own job lock. Preserve its cleanup, too.
    trap 'rm -f "$PUBLISH_LOCK" "$LOCK"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

#!/bin/sh
# Install the kitchen.local calendar, science and Starship system timers.
# Run as phurley after obtaining administrator credentials with sudo -v.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="/Users/phurley/Library/LaunchAgents/DisabledDailyBrief/$(date '+%Y%m%d-%H%M%S')"
JOBS="calendar science starship"

sudo -n true
# Validate every input before changing installed timers.
for job in $JOBS; do
    source="$ROOT/scripts/launchd/com.dailybrief.$job.daemon.plist"
    /usr/bin/plutil -lint "$source"
done
for script in /Users/phurley/daily-brief/scripts/run_calendar.sh \
              /Users/phurley/daily-brief/data-collect/run_science.sh \
              /Users/phurley/daily-brief/starship/run.sh; do
    [ -x "$script" ] || { echo "Missing executable: $script" >&2; exit 1; }
done
for job in $JOBS; do
    label="com.dailybrief.$job"
    source="$ROOT/scripts/launchd/$label.daemon.plist"
    target="/Library/LaunchDaemons/$label.plist"
    if sudo -n launchctl print "system/$label" >/dev/null 2>&1 && ! cmp -s "$source" "$target"; then
        echo "$label is loaded with a different configuration; boot it out while idle before reinstalling." >&2
        exit 1
    fi
done

for job in $JOBS; do
    label="com.dailybrief.$job"
    source="$ROOT/scripts/launchd/$label.daemon.plist"
    target="/Library/LaunchDaemons/$label.plist"
    # Remove registrations in either kitchen account; archive the old definition
    # outside LaunchAgents' top level so a later desktop login cannot duplicate it.
    for domain in gui/501 user/501 gui/502 user/502; do
        if sudo -n launchctl print "$domain/$label" >/dev/null 2>&1; then
            sudo -n launchctl bootout "$domain/$label"
        fi
    done
    agent="/Users/phurley/Library/LaunchAgents/$label.plist"
    if [ -f "$agent" ]; then
        mkdir -p "$ARCHIVE"
        mv "$agent" "$ARCHIVE/"
    fi
    sudo -n install -o root -g wheel -m 644 "$source" "$target"
    if ! sudo -n launchctl print "system/$label" >/dev/null 2>&1; then
        sudo -n launchctl bootstrap system "$target"
    fi
    echo "Installed system/$label (runs as phurley)."
done

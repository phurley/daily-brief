#!/bin/sh
# Swap the published data (events, news, manifest, editions ...) for a pinned
# snapshot so CI browser checks test code, not whatever the jobs published
# today: a day with no featured picks used to fail every PR. Prints the
# BRIEF_CLOCK to use (the snapshot's publish time plus 30 minutes).
#
# Refresh the snapshot when a change needs newer data, e.g. a new field:
#   git tag -f ci-browser-data origin/main && git push -f origin ci-browser-data
set -eu
REF=${1:-ci-browser-data}
cd "$(dirname "$0")/.."
git rev-parse --quiet --verify "refs/tags/$REF" >/dev/null ||
  git fetch --quiet --depth 1 origin "+refs/tags/$REF:refs/tags/$REF"
FILES="brief-manifest.json widget-events.json recommendations.json weather.json calendar.json photos.json
  events.json event-editorial.json news.json news-editorial.json geeknews.json science-editorial.json
  science-health.json starship.json vibe.json"
rm -rf editions starship/history
git checkout "$REF" -- $FILES editions starship/history
node -e 'const m=require("./brief-manifest.json"); console.log(new Date(Date.parse(m.generatedAt)+30*60000).toISOString())'

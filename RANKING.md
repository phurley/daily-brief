# Household recommendations

The brief shows up to seven best bets in each Today, Ongoing and Future lane
across the selected day and the next 30 days. “Show all events” and the full chronological calendar preserve the complete
collection, including cancellation notices and hidden occurrences. The small info
icon opens a details dialog with taste, practicality, novelty and data-quality
points, unknown facts and feedback controls.
Points are an inspectable ordering rule, not a probability of enjoyment.

`ranking.mjs` is the common, deterministic selector for the browser,
`vibe-check/generate.mjs`, and `scripts/select-best-bets.mjs`. A fixed edition,
reference timestamp, preferences, and weights produce the same occurrence IDs.
The browser and compact publisher use `selectEventLanes` to rank each lane
independently; widgets and editorial still use the combined public shortlist.
Live views remove events as known end times pass. Seven per lane is a maximum: we do not
fill below the score floor or violate diversity limits to reach a quota. One slot
is reserved for an eligible option active on the selected day. Limits per venue,
category and series are editable in `brief-preferences.json`.

Canceled/completed/postponed occurrences and known-ended events are ineligible.
A title beginning “CANCELED” is treated as a cancellation notice; unknown status
is allowed and labeled. Explicit distance limits exclude unknown distances,
with this behavior stated in the UI. No mileage is fabricated: publishing now
preserves known distance, otherwise city-based locality tiers provide a coarse
hint. These tiers are relative to the brief's established Canton coverage.

“More like this” and “Less like this” adjust the displayed category by ten
points. “Favorite” applies to a series (title + venue until a canonical series ID
is supplied); “Hide this occurrence” only hides that occurrence. Undo, reset,
and a summary of stored feedback are in Recommendation preferences. Browser
storage is isolated by profile ID. Clearing browser storage clears feedback;
other browsers, editorial generation and widgets use public defaults. No clicks
or lack of clicks infer preferences. Novelty is currently neutral; explicit hides
are eligibility rules. No private feedback is sent to an API or committed.

Public defaults expose topic/venue affinities, news-topic affinities, explicit
occurrence overrides, constraints and ranking limits. The UI provides common
controls; edit configuration for advanced defaults. Do not put private history
or household details in public JSON. Synchronized profiles require a separate
authenticated persistence design.

Scoring rules version 2 suppresses generic retail penalties in proportion to
the produce-market signal, prevents craft-shopping and generic retail overlap,
and removes the blanket recurrence penalty. Signal questions retain their
meaning; stored probabilities can be recomputed without model calls. The
publisher recomputes event scores on every publish. To update an existing edition:

```sh
data-collect/.venv/bin/python scripts/export_scoring_weights.py
data-collect/.venv/bin/python scripts/rescore-events.py             # preview
data-collect/.venv/bin/python scripts/rescore-events.py --write
node scripts/select-best-bets.mjs
```

`recommendations.json` contains the selected public events and explanations for
today and tomorrow. The collection script generates and includes it in its
normal data commit. Widgets read this feed and display the selected events
active today. The iOS app already uses the same web view. Editorial generation
receives only the public shortlist, with occurrence IDs; personalized cards use
local explanations so stale generated event recommendations cannot contradict
browser feedback. Editorial generation itself remains an explicit model call.

The debug page can load a saved `events.json` locally and compare the default
shortlist with tuned weights at that edition's reference time. Saving weights
applies only to this browser after reloading the brief; clearing them restores
exported defaults. `scoring-weights-legacy.json` preserves the previous scoring
configuration for comparison/rollback. Saved baseline market/cancellation
examples are in `scripts/fixtures/ranking-baseline.json`.

News uses `news-ranking.mjs`, never event scoring signals. It combines published
freshness, locality, news-topic affinities and optional source-type metadata.
Explicit, recent local safety/service alerts lead regardless of taste. New
extractions include named entities, an urgent-alert flag, and source type;
existing stories retain unknown metadata and need no bulk re-extraction.
Source type describes attribution, not fact-checking. Science uses its separate
`science.mjs` selector and verification pipeline; see `data-collect/SCIENCE.md`.

Checks (offline; no paid extraction or editorial calls):

```sh
node --test scripts/*.test.mjs
data-collect/.venv/bin/python scripts/test_ranking_schedule.py
(cd data-collect && .venv/bin/python -m unittest extract.test_scoring extract.test_publish extract.test_jev extract.test_urls)
node vibe-check/generate.mjs --context-only
```

Remaining evaluation/dependencies: household judgments on 30–50 event pairs
must come from people; none are fabricated. Canonical cross-source occurrence
reconciliation and reliable cancellation updates are Plan 1 work. The selector
uses supplied occurrence/series IDs and stable fallbacks in the meantime. Learned
preferences, synchronized profiles, exposure history, and science changes are
outside this initial explicit-feedback rollout.


## Recurring operation on kitchen.local

This host uses launchd; `crontab -l` has no user crontab. Ranking needs no
separate job. The loaded `com.dailybrief.collect` LaunchAgent runs
`data-collect/run_collect.sh` every 3,600 seconds with Homebrew on PATH. After
publishing events/news, it runs `scripts/select-best-bets.mjs` and includes
`recommendations.json` in the same scoped data commit. A shortlist failure stops
publication so widgets cannot silently keep an old selection. `COLLECT_NO_PUSH=1`
still writes the shortlist to `COLLECT_OUT_DIR` but skips Git and editorial calls.

The collector then chains `vibe-check/run.sh`, which uses the same event selector.
The loaded `com.dailybrief.vibe` job also runs at :40 each hour and skips recently
refreshed output. The collector must release the shared Git publication lock
before invoking this child, while retaining its own collection lock until exit.
This prevents a parent/child publication deadlock. The standalone editorial job
waits for active collection; the chained invocation skips that wait.

Installed plists are `~/Library/LaunchAgents/com.dailybrief.collect.plist` and
`~/Library/LaunchAgents/com.dailybrief.vibe.plist`; repository templates are in
`data-collect/launchd/` and `vibe-check/launchd/`. Both invoke scripts in this
checkout, so ranking script changes take effect on the next run without changing
or reloading their schedule. Existing calendar, science and Starship jobs retain
their own schedules; ranking neither replaces them nor adds duplicate jobs.

Diagnostics:

```sh
launchctl print gui/$(id -u)/com.dailybrief.collect
launchctl print gui/$(id -u)/com.dailybrief.vibe
node scripts/select-best-bets.mjs --out /tmp/recommendations-check.json
node vibe-check/generate.mjs --context-only > /tmp/editorial-context-check.json
```

The last two commands are offline and do not invoke a model or publish anything.
Use `--events /path/to/saved/events.json --at <ISO timestamp>` with the selector
for a reproducible replay. Inspect `data-collect/cron.log` for collection/selection
failures and `vibe-check/vibe.log` for editorial failures. The publisher's existing
job lock and shared Git lock remain required. Do not run a second overlapping
collector or add a new cron entry for the shortlist.


## Compact edition integration

Plan 5 publication rebuilds `recommendations.json` and `widget-events.json` using
the same public selector, together with the versioned web manifest. Widgets use
the dated compact feed; browser feedback stays private. All publishers now pass
through `scripts/publish_brief.py`, using the existing shared macOS publication
mutex. See [mobile edition operations](docs/mobile-edition.md).


Saved event preferences and custom scoring weights trigger a full candidate fetch
when the brief loads or its edition changes. The compact public pool remains the
fast initial view; it is not the final personalized pool. When the full archive
cannot be loaded, the page explains that preferences are applied only to the
compact selection and retries on refresh. Browser feedback remains local.

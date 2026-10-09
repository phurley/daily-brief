# Event occurrence identity

Implemented from the October 9, 2026 brief. The collector defaults to **shadow** publication: it computes occurrence identity and writes a private audit report while retaining legacy published output. Canonical publication and constrained typo matching are independent feature flags. No network or model calls are needed to compare or republish retained source records.

## Operation

Run from `data-collect` with the existing virtual environment:

```sh
# Default: legacy documents plus a private identity report and shadow registry.
.venv/bin/python -m extract.publish --identity-mode shadow

# Preview canonical output without changing the live edition or production registry.
.venv/bin/python -m extract.publish --identity-mode canonical \
  --identity-registry processed/preview/registry.json \
  --identity-report processed/preview/report.json --out processed/preview

# Optional typo matching, limited to the same detail URL, time and venue.
.venv/bin/python -m extract.publish --identity-mode canonical --identity-fuzzy \
  --identity-registry processed/preview-fuzzy/registry.json \
  --identity-report processed/preview-fuzzy/report.json --out processed/preview-fuzzy

# Collector rollout switches; legacy remains available for rollback.
EVENT_IDENTITY_MODE=canonical ./run_collect.sh
EVENT_IDENTITY_MODE=legacy ./run_collect.sh
```

`EVENT_IDENTITY_FUZZY=1` is equivalent to `--identity-fuzzy`. Set environment variables in the collector's existing launch configuration for a persistent rollout. Preview both modes before switching. Shadow and canonical registries have different defaults so experimental shadow decisions cannot fix production records into provisional groups. When comparing matching policies, use separate preview registries. Changing fuzzy policy does not silently combine already assigned occurrences; ambiguous bridges remain review candidates.

`--dry-run` never updates the registry or published documents. It writes a diagnostic report only when an explicit `--identity-report` path is supplied. Ordinary reports and registries live under ignored `processed/`, not the public site.

## Identity and evidence

Event storage keys are immutable source snapshot hashes rather than title/day slugs. The pipeline retains legacy IDs as aliases, archives event snapshots in `processed/event_source_archive.jsonl` before pruning, archives original candidate text/feed metadata in `processed/source_candidate_archive.jsonl`, and leaves the global extractor version unchanged. ICS UID, recurrence ID/rule, status, and modification time survive crawling, preprocessing, and extraction. JSON-LD native identifiers and explicit status also survive. A native series ID is retained internally.

Occurrences have persistent IDs and reversible source-to-occurrence mappings. The registry retains matching anchors and rule reasons; the archive and the initial baseline preserve raw records for rebuilding after a bad merge. Back up the registry before changing matching rules. To undo a rollout, restore its matching registry and republish the retained records using the previous rule version or legacy mode; never attempt to reconstruct sources from the merged public cards.

Exact normalized title or a complete equivalent music lineup requires the same instant and resolved venue/city. Explicit same-source native occurrence identity also supports title corrections. Native identity plus an explicit old start permits a reschedule. Different native occurrences, detail identifiers, venues, known performers or recurrence instances block automatic merging. Missing times and ambiguous changes go to review; midnight in legacy data is treated conservatively as date precision. URLs preserve meaningful query parameters and discard known tracking parameters. The city normalizer recognizes the optional Michigan suffix; room/branch names remain distinct.

Fuzzy matching is disabled by default. When enabled, it requires a detail URL, identical known start/venue, no negative evidence and title similarity of at least 0.92. General listing URLs never suffice. No model-assisted matching is used.

Fields follow source authority and recency. Catalog sources of type `Venue` are organizer sources; other sources remain unknown unless `identity_authority` is configured as `organizer` or `ticket`. Explicit status evidence is ranked separately from richness. Implicit scheduled data cannot undo a cancellation; a newer authoritative explicit reinstatement can. Conflicts and per-field suppliers are retained. A reschedule never inherits the original performance's end time. Public occurrence metadata includes source links and merge evidence, also shown in the scoring debug page.

The browser and editorial generator exclude canceled/postponed occurrences from recommendations while the calendar keeps labeled notices. Date-only records display “Time to be confirmed.” Widget rollout remains coupled to enabling canonical publication; concurrent recommendation changes are preserved separately.

## Extraction cost and recovery

Preprocessing only applies title and SimHash suppression to definite news. Event/mixed-listing suppression requires identical content plus occurrence context. Complete ICS/JSON-LD records with a known city supply the structured base directly and skip model extraction. Incomplete feeds continue through enrichment; source event fields override generated guesses. Feed fingerprints now include mutable content/status, so later cancellations are processed. Existing feeds receive one bounded refresh; chunks keep their existing cache keys. `--max-items` and `--extract-limit` remain in force. Pipeline statistics expose mechanical feed count and generation calls.

Already overwritten source records cannot be recovered from the old cumulative store. The private baseline retains the available history. A targeted refresh can reconstruct a source without invalidating every cache:

```sh
.venv/bin/python -m extract.pipeline --reextract-source ann-arbor-district-library-aadl \
  --max-items 100 --extract-limit 50
```

This intentionally performs bounded model work. Repeat it only when needed; no full-corpus rerun is part of migration. Existing ICS recurrence expansion/timezone interpretation is outside this change; recurrence metadata is preserved without inventing unadvertised sessions.

## Validation and local evidence

```sh
.venv/bin/python -m unittest discover -s extract -t .
.venv/bin/python -m extract.identity_benchmark \
  --pairs processed/identity-baseline-2026-10-09/labeled-pairs-v1.json --fuzzy
```

The October 9 baseline is under `processed/identity-baseline-2026-10-09/`: edition, retained source records, repository revision, labeled pairs and comparison reports. The selected benchmark has 10 pairs (7 observed comparisons and 3 synthetic negative controls), including the screening typo, cancellation and three Red Leather lineup comparisons. With fuzzy matching enabled it yields 5/5 correct automatic matches and no false positive merge. The 95% Wilson precision interval is approximately 56.6–100%; this does **not** establish the proposed 99% production precision target. Expand and independently label this benchmark before broadening rules.

Reports record source counts, occurrence counts, merge mappings, review candidates and conflicting field groups. Inspect duplicate candidate rates and recurrence retention separately from confirmed duplicate counts: preserving legitimate performances can increase the published count. `extract.test_identity` covers replay, title correction, time/venue/recurrence negatives, explicit reschedules, cancellation precedence, source retention, schema validity and shadow equivalence. Browser tests cover status exclusion and unknown-time display.

## Recurring job verification (October 9, 2026)

On kitchen.local the collector is `gui/501/com.dailybrief.collect`, scheduled
at a 3,600-second interval by launchd. The repository template and installed
`~/Library/LaunchAgents/com.dailybrief.collect.plist` explicitly set shadow mode
and disable fuzzy matching. `run_collect.sh` validates and logs both settings
before collection; the identity report is refreshed by its publication step.
There is no separate cron entry or identity daemon to install.

After editing the template, update and reload the installed definition while
the collector is idle:

```sh
cp launchd/com.dailybrief.collect.plist ~/Library/LaunchAgents/
plutil -lint ~/Library/LaunchAgents/com.dailybrief.collect.plist
launchctl bootout gui/$(id -u)/com.dailybrief.collect
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dailybrief.collect.plist
launchctl print gui/$(id -u)/com.dailybrief.collect
```

Check the printed interval, program path and environment. `RunAtLoad` is false,
so reloading does not start a paid crawl/model run. The existing calendar job
(03:30 Detroit time) and editorial job (hourly at :40) remain loaded and need no
identity-specific schedule change. Before reload, all three reported exit code
0 on their previous run. Identity publication was separately validated against
retained records, including reversed-order replay with an unchanged registry.

# Evidence-backed Starship status

`starship.json` is the canonical, independently refreshed record. The page and
`vibe-check` share the freshness and date rules in `../starship.mjs`. The old
`geeknews.starshipEstimatedLaunch` field is retained only for compatibility;
neither consumer reads it. Its September 14 estimate was imported once as
**unverified historical editorial evidence**, not a SpaceX announcement.

## Collection and rollout

On kitchen.local, `com.dailybrief.starship` runs `starship/run.sh` at **:25 every
hour**, using the existing `data-collect/.venv` and `/opt/homebrew/bin/node`.
This runs independently of the general crawler and before the standalone
editorial timer at :40. The collector's own editorial chain may run at another
time; it reads the latest atomic Starship record with the same freshness gate.

The initial mode is **shadow** (`config.json`). The public card shows verified
operator status, outside reports, source health and uncertainty. The rule-based
forecast is saved in immutable snapshots but withheld from public and editorial
predictions until `mode` is deliberately changed to `live` after reviewing
history. An unavailable target is a supported result, not a fabricated forecast.

```sh
cd ~/daily-brief
# Dependencies already available in the crawler environment on kitchen.local:
data-collect/.venv/bin/python -m pip install -r starship/requirements.txt
# Only needed when setting up a new host/browser installation:
data-collect/.venv/bin/python -m playwright install chromium
STARSHIP_NO_PUSH=1 starship/run.sh        # collect + validate + write, no git
STARSHIP_DRY_RUN=1 starship/run.sh        # collect + validate, no writes or git
/opt/homebrew/bin/node --test scripts/starship.test.mjs
(cd starship && ../data-collect/.venv/bin/python -m unittest test_sources.py test_validation.py)
data-collect/.venv/bin/python starship/test_browser.py
data-collect/.venv/bin/python starship/validate.py < starship.json
/opt/homebrew/bin/node starship/evaluate.mjs
```

`STARSHIP_NODE` and `STARSHIP_PYTHON` override runtimes. Collection has bounded
HTTP/browser/process timeouts. Source failures are recorded per source, so one
outage does not hide the others. Schema or adapter-process failures exit nonzero
and preserve the previously published document. Operational logs are at
`starship/collector.log`; source HTTP success is not mission verification.

Install/reload after changing the plist (script changes need no reload):

```sh
cp scripts/launchd/com.dailybrief.starship.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/com.dailybrief.starship 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dailybrief.starship.plist
launchctl print gui/$(id -u)/com.dailybrief.starship
# Run the installed job immediately:
launchctl kickstart gui/$(id -u)/com.dailybrief.starship
```

## Evidence and adapters

- SpaceX: render the JavaScript launches page with the already installed
  Playwright Chromium; follow up to three Starship/flight-test detail links.
  Accept an explicit target only with a flight association and a separate
  publication timestamp. Initial automatic parsing supports day-level dated
  targets/NET; it deliberately retains that precision even if prose mentions
  a time. Missing dates or changed markup produce `no-dated-evidence` health.
- FAA: retain dated regulatory paragraphs as background. Neither a generic
  project page nor a completed environmental review proves mission authorization.
- Cameron County: parse dates from notice rows, not footers. An operational
  notice's date is kept separately from its unknown publication time. Old
  notices are reported as stale. Closures never establish a launch target.
- Every claim includes provenance, excerpt, observation/publication dates,
  mission association, parser version, verification and shared `originId`.
  Repeated reporting of one announcement does not become independent support.

The first run may have **no current verified target**. That means the adapters
cannot establish one from their sources; it does not claim the operator has
made no announcement elsewhere. The general next-five launch feed is never
used to hide missing Starship coverage.

Reviewed evidence belongs in `reviewed-evidence.json`, following the evidence
contract in `../schemas/starship.schema.json`. This supports explicit scrubs,
delays, readiness, mission-specific regulatory authorization, exact/window
announcements, retractions and outcomes that the initial parsers cannot safely
extract. Inspect the original source; copy its original wording, actual
publication/observation times and mission identifier. Do not advance
`observedAt` without rechecking the source. Vehicle IDs remain empty until known.

Use `verification: "unverified"` for unconfirmed claims. For a changed target,
use a new ID and explicit `supersedes: ["old-id"]` only when the source really
supersedes that claim. A retraction retains the original ID with
`verification: "retracted"`. Reviewed entries override automatic records with
the same ID, including during source outages. Use the same `originId` for
reports repeating one original announcement. Distinct contradictory targets
remain visible until explicitly reconciled; the rules do not average them.
`config.activeMission` can explicitly select a known mission; normally the most
recent operator mission claim selects it. Completed missions cannot establish
the next mission's target.

Dates retain source timezone, original wording, precision and a separate NET
flag. Day/month/quarter bounds are strings, never invented midnight launch
instants. Exact/window instants require timezone offsets; the UI converts them
to America/Detroit. Claim confirmation describes evidence, not liftoff odds.

## Freshness, history and recovery

`lastAttemptAt` records every run; `lastSuccessAt` requires a successfully parsed
source. `lastVerifiedAt` requires a verified, mission-associated claim, and
`expiresAt` is six hours later. Source outages do not renew that deadline.
Reviewed claims also expire unless reverified. A passed target requests an
update; it never creates a successful launch or cancellation. Unknown readiness
or authorization remains unknown. The initial plausible state requires a
current operator target within seven days plus independently sourced readiness
and explicit mission-specific authorization. No numeric probabilities are used.

Each publication writes `history/<UTC timestamp>.json` exclusively before an
atomic rename of the root document, then commits both. Snapshots include the
available evidence, original claims, parser/model versions, outcomes and a link
to the previous snapshot. They are never edited or pruned by the collector.
Correct mistakes with reviewed retractions/new claims and a new snapshot.
Do not overwrite history to conceal a parser error. To roll back the rules,
revert the code commit and run a fresh collection; the chain preserves both
versions. Keep public history free of secrets and private sources.

`evaluate.mjs` joins eventual explicit liftoff outcomes to earlier snapshots
only. It reports sample size, lead time, target changes and coverage alongside
the latest-official-target baseline. NET lower bounds are not bounded coverage
trials. Repeated snapshots are not independent missions. No probabilistic model
or calibration claim is appropriate until enough comparable mission history
exists; a future model needs time-ordered holdouts and proper scoring.

The worldwide RocketLaunch.Live strip uses a 30-minute normal cache and a
six-hour absolute fallback cap, labels cached fallback data, distinguishes
`win_open` from `t0`, and says "targeted today" for tentative schedules.

## Recurring-task inventory

| Job | Schedule on kitchen.local / GitHub | Publication |
| --- | --- | --- |
| `com.dailybrief.starship` | Local hourly at :25 | `starship.json`, immutable history |
| `com.dailybrief.collect` | Local 3600-second interval | events/news, then editorial |
| `com.dailybrief.vibe` | Local hourly at :40 | freshness-gated editorial |
| `com.dailybrief.calendar` | Local 03:30 | family calendar |
| GitHub `update-weather.yml` | `17 */4 * * *` UTC | weather |
| GitHub `update-calendar.yml` | `23 9 * * *` UTC | calendar, if secret configured |

There is no user crontab on kitchen.local. Do not add a second cron or GitHub
Starship collector: the local launchd job owns it. All local scheduled Git
writers now use `scripts/git-publish-lock.sh` (`/tmp/dailybrief-publish.lock`)
and path-scoped commits. The collector releases that lock before chaining
vibe-check. Per-job `shlock` PID locks still serialize collection. GitHub jobs
operate in separate checkouts and are handled by the existing pull/rebase step.
A failed push is retained locally and retried by the next Starship publication.
Check `launchctl print gui/$(id -u)/com.dailybrief.starship`, the log and the
record's `lastAttemptAt`/source health after installing or changing a schedule.


### Compact edition integration

The existing scheduled job now routes publication through
`scripts/publish_brief.py` (science via `scripts/push_generated.sh`). It commits
source output and the matching compact web/widget edition together under the
shared publication lock. Schedules and collection behavior are unchanged. See
[mobile edition operations](../docs/mobile-edition.md) for cache and rollover
behavior.

## r/SpaceX community outlook

The existing hourly :25 Starship job also reads the public r/SpaceX new/hot
Atom feeds and up to two recent Starship development-thread comment feeds.
No additional scheduler or Reddit credentials are required. Requests are bounded;
blocked, malformed or partially failing feeds are exposed in source health.

Posts/comments remain `sourceType: community`, `claimType: discussion`, and
`verification: unverified`, including posts carrying an "Official" flair.
The source permalink, original publication date, observation date, excerpt and
up to five linked source URLs are retained. Date suggestions remain in their
original wording; Reddit text does not manufacture precise timestamps or odds.
Speculation is listed ahead of general discussion. Posts older than 30 days or
not observed within 24 hours are hidden from the outlook; retractions, known
completed missions and known different missions are excluded. Reposts sharing
an underlying URL are displayed once and never counted as corroboration.

The public card shows up to three leads under **Community outlook · unverified**,
separately from operator targets and the shadow/live rule-based forecast. Forum
text is excluded from the editorial model's fact input. Community evidence
cannot select a mission, reset `lastVerifiedAt`, supersede operator evidence,
or establish a target, authorization, readiness, or terminal outcome. Follow
an item's linked primary source and verify it independently before creating a
separate reviewed primary-source claim. A source link alone is not verification.

Tests: `(cd starship && ../data-collect/.venv/bin/python -m unittest test_reddit.py)`
and `node --test scripts/starship.test.mjs`. Existing parser, schema and browser
checks still apply. The collector process timeout is six minutes to accommodate
the bounded additional feed requests; its per-request timeout stays 25 seconds.

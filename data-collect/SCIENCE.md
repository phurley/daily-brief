# Science digest operations

Science has its own domain, catalog, state and daily 6 AM launchd job (Detroit time). It bypasses
the local-event gate. U-M research is collected independently from its existing
local-news route; accepted findings keep their Ann Arbor connection. Launch-date
forecasting remains outside this pipeline; the legacy launch object is preserved.

## Run and inspect

From `~/daily-brief/data-collect` on `kitchen.local`:

```sh
SCIENCE_NO_PUSH=1 SCIENCE_VIBE_ENABLED=0 ./run_science.sh --force
.venv/bin/python -m unittest science.test_pipeline -v
/opt/homebrew/bin/node --test ../scripts/science.test.mjs ../scripts/story-order.test.mjs
/opt/homebrew/bin/node ../vibe-check/generate.mjs --context-only
cat ../science-health.json
tail -80 science.log
```

`run_science.sh` holds `/tmp/dailybrief-science.lock`, keeps the machine awake,
then runs fetch, article verification/extraction, selection and publication. It
commits/pushes **both** `geeknews.json` and `science-health.json`, even when a run
fails and only health changes. `SCIENCE_NO_PUSH=1` suppresses both science and
chained editorial pushes. A changed edition refreshes editorial copy, bypassing
its usual 50-minute minimum; `SCIENCE_VIBE_ENABLED=0` disables that chain.

Direct Python invocation writes output without touching Git. `--out DIR` and
`--state-dir DIR` allow a fully isolated fixture or trial run. `--force` ignores
source fetch intervals but retains article fingerprint reuse. Delete only a
specific fingerprint in private state to deliberately pay for re-extraction.

The model uses the existing `data-collect/.env` key and
`OPENROUTER_SCIENCE_MODEL` (default `google/gemini-2.5-flash`). `SCIENCE_NODE` overrides
`/opt/homebrew/bin/node` for shared selection. Dependencies are declared in
`requirements.txt` and were already present on kitchen at rollout.

## Sources, intervals and use

`science-sources.json` is the explicit science catalog. Official catalogs and
concrete endpoints were checked October 9, 2026 from kitchen. The parser accepts
RSS and Atom, rejects HTML/error responses, preserves feed publication dates,
and separately records supplied article modification timestamps.

| Source | Fetch interval | Overdue after successful fetch | Feed |
| --- | --- | --- | --- |
| NASA | daily | 36 hours | https://www.nasa.gov/feed/ |
| NASA JPL | daily | 36 hours | https://www.nasa.gov/centers-and-facilities/jpl/feed/ |
| ESA Space Science | daily | 36 hours | https://www.esa.int/rssfeed/Our_Activities/Space_Science |
| NSF | daily | 36 hours | https://www.nsf.gov/rss/rss_www_news.xml |
| U-M | daily | 36 hours | https://news.umich.edu/feed/ |

Catalogs: [NASA](https://www.nasa.gov/rss-feeds/),
[JPL](https://www.jpl.nasa.gov/rss/),
[ESA](https://www.esa.int/Services/RSS_Feeds), [NSF](https://www.nsf.gov/rss).
JPL's advertised `https://www.jpl.nasa.gov/feeds/news/` returned a non-feed
response in the rollout check; the documented NASA JPL category feed is used.
Do not silently switch to a guessed endpoint when a source breaks.

Publication is limited to attributed paraphrases and links; article text and
supporting quotes remain in ignored local diagnostics. No source imagery or
full article is republished. Review [NASA media guidance](https://www.nasa.gov/nasa-brand-center/images-and-media/),
[ESA copyright](https://www.esa.int/About_Us/ESA_Publications/Copyright),
[NSF reuse](https://www.nsf.gov/policies/reuse.jsp), and individual article rights
before expanding republication. U-M's existing public RSS is reused for linking
and attributed summaries; it is not treated as a blanket content license.

## Verification and failure contract

Each candidate needs a timezone-aware feed publication timestamp corroborated by
article metadata or the article's visible date. Missing/ambiguous/future dates
are counted as rejected, never replaced with fetch time. Article URLs must be
in the source's configured domain list. A finding needs an exact supporting
quote with consistent planned/completed tense; material caveats and research or
mission evidence labels also need article quotes. Absent caveat evidence uses a
neutral limitations-not-specified label. paper links must actually occur in that article.
A paper link alone does not establish peer review: the extraction prompt requires
explicit journal support, and unknown evidence remains unknown. There is no DOI
guesser. Evidence classification and paraphrases are model assisted, so use the
sample-review procedure below when changing sources/models.

Defaults: 45-day ingestion horizon, four article fetch/extraction attempts per
source per due run, 24-hour unchanged-article recheck, 180 public records. The
feed and article fingerprints avoid repeated model charges. Original story IDs,
publication dates and first-seen timestamps survive updates; `checkedAt` changes
only when the article is checked again. `verifiedAt` dates the last extracted
content; `sourceUpdatedAt` is only supplied metadata, never inferred from fetch.

A fetch with no new stories is healthy. A failed source, article verification,
extraction, selection, or full-document validation holds the previous edition
byte-for-byte. Valid work is cached so the next attempt can retry cheaply.
The health report is replaced independently and lists specific errors. Publication
uses a validated temporary file, fsync and atomic rename. Separate files are
not a cross-file transaction: a crash between edition and health writes may
briefly leave conservative stale health until the next run.

Private stage diagnostics live in ignored `processed/science/`:

- `feeds/<source>.json`: latest successful fetch body and time.
- `extractions/<hash>.json`: article text, links and model evidence for review.
- `state.json`: records, fingerprints and source attempts (safe to rebuild).

Public `science-health.json` records per-source `lastAttemptAt`, `lastSuccessAt`,
`lastNewStoryAt`, candidate/accepted/rejected/deferred counts and errors. It
separates `documentGeneratedAt` from `newestVerifiedStoryAt`. Counts reflect each
source's last due run, not the entire catalog. It also reports topic/source
counts, newest story age, and maximum publication lag. The browser computes age
at render time, including a 36-hour scheduler heartbeat timeout. Quiet feeds
are not stale solely because they have no new article.

## Selection, caveats and continuing stories

`science.mjs` provides the single selector for the CLI publisher, browser and
editorial context. Up to six verified, non-background stories from the last
14 days lead; everything else is explicitly archived. Scoring uses publication
age, configured source quality, optional household topic interests, and penalties
for repeated sources and topics. NASA and JPL share a concentration group.
Selection IDs and per-story explanations are saved in the public edition.

The daily publisher saves six selected IDs. Browser JavaScript cyclically reorders
those six in four-hour Detroit slots (00:00, 04:00, 08:00, 12:00, 16:00, 20:00).
Viewers with the same edition and clock slot see the same order. Open visible
pages check the slot each minute and returning tabs update immediately; past/future
date views keep their base order. No fetch, model call, publication-date change,
commit, or recurring rotation job is involved. The displayed intro uses that
same view. A manual refresh with materially new findings may reset the day's
selection. Dates use America/Detroit. Optional preferences
are read from positive `topicAffinities` and `newsTopicAffinities` in the shared
`brief-preferences.json` household profile;
the selected topic list is published so browser and editorial scoring agree.
No household interests are assumed when the file is empty.

Cards show publication dates, evidence labels, significance, caveats and primary
links. Unverified legacy records keep their IDs and caveats in the archive and
cannot become leads just because an edition was regenerated. Deterministic,
dated science section copy preserves the lead's complete caveat; the wider
editorial model receives the exact selected IDs/dates/caveats/freshness and is
instructed not to equate collection with discovery. The frontend derives the
science section intro itself to avoid mismatches during asynchronous refreshes.

Only a verified `mission-milestone` with an exact curated entity can join a
`storyClusterId`. Earlier verified milestones appear under “Previously” and
are linked by `supersedes`; discoveries involving the same instrument remain
separate. Tests use synthetic milestones. The historical Roman claims have not
been promoted to verified facts. Expand the curated map only after reviewing
article evidence; fuzzy discovery grouping remains a future extension.

## Recurring jobs on kitchen

There is no user crontab. These launchd jobs are the active equivalent:

| Job | Cadence | Tracked configuration |
| --- | --- | --- |
| `com.dailybrief.science` | daily at 06:00 local (Detroit) | `scripts/launchd/com.dailybrief.science.plist` |
| `com.dailybrief.collect` | every 3600 seconds | `data-collect/launchd/com.dailybrief.collect.plist` |
| `com.dailybrief.starship` | daily at 07:25 America/Detroit | `scripts/launchd/com.dailybrief.starship.plist` |
| `com.dailybrief.vibe` | hourly at :40 | `vibe-check/launchd/com.dailybrief.vibe.plist` |
| `com.dailybrief.calendar` | daily at 03:30 local | `scripts/launchd/com.dailybrief.calendar.plist` |
| `com.dailybrief.edition` | daily at 00:05 local | `scripts/launchd/com.dailybrief.edition.plist` |

Install/reload the science timer (not a second cron entry):

```sh
cp ../scripts/launchd/com.dailybrief.science.plist ~/Library/LaunchAgents/
launchctl bootout gui/$(id -u)/com.dailybrief.science 2>/dev/null || true
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dailybrief.science.plist
launchctl kickstart gui/$(id -u)/com.dailybrief.science
launchctl print gui/$(id -u)/com.dailybrief.science
```

The existing collector, calendar, launch and vibe wrappers share
`/tmp/dailybrief-publish.lock` with science (through their Git lock helper).
Science uses `scripts/push_generated.sh` with that same lock. Its helper
commits only named outputs, serializes Git operations and retries a previously
failed push even when the current output is unchanged. The vibe timer waits for
the science lock unless invoked by the science chain itself. Collection remains
independent: science is not appended after a fallible local-events stage.
All science sources are checked once per Detroit calendar date, allowing late
runs and DST transitions without skipping the next morning. `--force` permits
an explicit retry. All source health thresholds are 36 hours, allowing the normal
24-hour interval plus delay. Failures still become visible immediately.

The existing GitHub Actions calendar schedule (`23 9 * * *`, UTC) remains a
calendar-only fallback and does not need a science key or duplicate science job.
Its concurrent remote commits are handled by each local writer's rebase/push.

## Sample review and rollout checks

Review selected stories against `processed/science/extractions/`: compare the
finding, significance and full caveat to the retained article; verify dates and
paper links; check evidence labels rather than assuming all agencies publish
peer-reviewed research. Record a sample result here after source/model changes.
Monitor source concentration, topic distribution and publication lag together:
a high-volume source must not hide failures elsewhere. Test continuing-story
linking separately from exact URL deduplication.

### October 9, 2026 rollout review

All five feeds fetched successfully; 25 verified records produced a six-story
lead digest spanning NASA, ESA, NSF and U-M. The initial selection contains two
NASA stories, two U-M stories and one each from ESA and NSF. The remaining 37
records are in the archive, including 18 unverified legacy records.

Reviewed all six leads against their retained primary articles. Corrected two
unsupported specific phrases in the initial model output: heat-shield capsules
operate during re-entry (not a claimed deorbit burn), and Solar Orbiter measured
charged oxygen/carbon particles (not an unsupported isotope claim). Re-extraction
with the science-specific model preserved the source wording and uncertainty.
An SSRN working paper is now explicitly labeled preprint, with a visible peer
review caveat. Validation includes regression guards for these failure modes.
NASAs flood-impact figures, the U-M constituent-service result and NSFs planned
metascience collaboration match the retained source text. This is a six-lead
review, not a claim of independent replication of the underlying research.

The existing stale edition was shown conservatively in the browser before the
first successful refresh. After refresh, publication dates, full caveats and
preprint labels were verified in the rendered page, with significance expandable.
Live editorial context selected exactly the same six IDs and full caveats.
Twelve Python fixture/integration checks and nine focused JavaScript checks pass.
All five installed LaunchAgent property lists match their tracked definitions;
there is no user crontab. The science wrapper also completed with exit 0 using
push/editorial disabled before activation of scheduled publishing.

The installed science LaunchAgent was then kicked once with publishing enabled:
launchd reports one run and exit 0, and its health refresh committed and pushed
successfully. All 66 JavaScript tests in the isolated integration snapshot passed.


### Compact edition integration

The existing scheduled job now routes publication through
`scripts/publish_brief.py` (science via `scripts/push_generated.sh`). It commits
source output and the matching compact web/widget edition together under the
shared publication lock. The science cadence is daily; other collection schedules are unchanged. See
[mobile edition operations](../docs/mobile-edition.md) for cache and rollover
behavior.


### Daily collection and client rotation rollout

On October 9, the science LaunchAgent was reloaded with a 06:00 calendar trigger;
its installed plist matches the tracked file and the old hourly interval is gone.
A no-push wrapper run completed successfully and updated the source thresholds
without refetching already-checked sources or changing their verification dates.
Thirteen Python science tests and all 89 JavaScript tests passed. A Chromium
browser check crossed the 16:00 Detroit slot while offline: the same six cards
reordered, the intro matched the new lead, and all six caveats remained visible,
with no JavaScript errors. The shell and module cache versions were bumped.

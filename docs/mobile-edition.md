# Mobile edition, publication and offline operation

Implemented from Plan 5 on October 9, 2026 and integrated with the concurrent
identity, household ranking, verified science and Starship work. Canonical IDs,
`ranking.mjs`, `news-ranking.mjs`, `science.mjs` and `starship.json` are the shared
contracts; this increment does not introduce a competing recommendation system.

## Data contract

`node scripts/build_edition.mjs [output-directory-with-source-json]` builds:

- `brief-manifest.json`: contract version 1, content-derived edition ID, Detroit
  edition date, publication time, section schema versions, source timestamps,
  content-addressed section URLs and SHA-256 hashes, and lazy archive hashes.
- `editions/<section>-<sha256>.json`: weather, ranked picks in today, ongoing and future lanes for adjacent dates plus a small candidate pool, ten
  local stories, the shared verified science digest, dedicated Starship status,
  science health, public ranking defaults/weights, and dated editorial copy. Section
  documents keep their existing schemas. A source's original timestamp is never
  advanced just because publication ran.
- `widget-events.json`: the dated public best bets (seven by default) from the same selector
  using the same ranking rules as the web brief, with edition ID/date and source timestamp.

`ranking.mjs` selects events for the publisher and web; `news-ranking.mjs` and
`science.mjs` select the other digests. The compact candidate pool preserves
occurrence/series IDs, status, scores and source evidence. Opening recommendation
preferences loads the full candidate archive on demand, so private feedback can
rank against the complete set. Browser feedback never enters publication.
Widgets consume the shared public ordering, label future dates explicitly,
remove known ended events and discard yesterday's edition at midnight.
`recommendations.json` is also rebuilt by publication, keeping the legacy Plan 2
feed and widget selection coherent across midnight.

Publication writes section files atomically, then the widget and finally the
manifest. Unchanged input/selection retains the publication timestamp. At most
two generations of section files remain in Git. The publisher rejects compact
section data above 250,000 gzip bytes. Full events/news/science stay in their
existing root files and load only on calendar, See all, preference changes, or scrolling to the end of a story shelf.
Their hashes must match the manifest; an edition change asks the user to refresh
and retry instead of silently combining source generations.

Private browser preference histories and family-feed credentials are never read
by this build. Only the explicit public defaults in `brief-preferences.json` are
included with public scoring weights.
`calendar.json` stays at its existing visibility and loads separately when Family
calendar expands; it is not copied into the compact edition or offline caches.
Photos load when Science approaches the viewport. Neither family calendar nor
external images are promised offline.

## Browser behavior and cache lifecycle

The four section shortcuts are immediately available. Mobile masthead notes and
weather/almanac details expand explicitly. Events appear in Today, Ongoing and
Future lanes, each with its own ranked selection and per-lane preference limit.
Multi-day events ending on the selected date appear in Today; those continuing
past it appear in Ongoing. The small info icon at each card's bottom right opens
the details dialog, including ranking reasons and feedback. Titles open websites.
See all opens a vertical list.

Local and science shelves append up to 20 more cards when the reader scrolls to
the end, swipes beyond it, presses Right/End, or uses the shelf arrow. Archive
requests are deduplicated and retain card order and scroll position. Errors
appear beneath the shelf; another scroll or arrow action retries. Older science
cards are labelled Archive / background and retain evidence qualifiers.

Starship is a short best-guess date on the worldwide launch line. It preserves
the source's date precision and shows date pending when no current usable target
exists. The independent Starship collector and evidence history remain intact. Calendar Close remains in the
header; native modal dialogs include Escape, focus trapping and focus return.
Reduced-motion preferences apply to scrolling as well as animation.

The manifest is revalidated without timestamp cache busting. Content-addressed
sections load independently with eight-second timeouts and SHA-256 checks.
Weather/events can render while science is stalled. The manifest times out after
five seconds. Archives have a twelve-second bound. Static schemas are memoized
only for the full-document compatibility route; compact sections use bundled
runtime contracts. Source dates appear per section, separate from publication
time. A successful check is never labelled Live.

`daily-brief-edition:v1` in localStorage holds the last **complete** validated
edition. On reopening it renders immediately with Saved labels; a failed or
partial refresh does not replace that saved copy. Quota or storage denial leaves
the live view usable and reports that it cannot save. Unsupported saved versions
are ignored. If no compatible saved edition or manifest is available, the old
full-document loader remains as a progressive compatibility path.

`sw.js` pre-caches the shell, required modules and moon texture. It caches at most
32 immutable section responses; manifest checks remain network-only. It never
caches family calendar, preferences, or third-party API responses. The deploy workflow generates the shell
cache name from all shell assets and the worker template; bump the data/cache contract version for incompatible
payloads. The new worker waits for old pages to close, avoiding a code swap beneath
an open edition. Activation removes only obsolete Daily Brief caches. Browser
cache eviction may remove offline capability; opening online rebuilds it. A saved
edition keeps its original date even across midnight and failed refreshes.

Refresh runs every 15 minutes while visible, on reconnection, and on foreground
when overdue or the Detroit date changes. Hidden tabs do not start refresh work.
Jokes and live rocket launches are bounded, noncritical requests; the saved
Starship section supplies the current target without enlarging the launch line.

## Recurring publishers (kitchen.local and GitHub)

There is no user crontab on kitchen.local; these are launchd agents:

| Agent | Schedule in kitchen.local time | Entry point |
| --- | --- | --- |
| `com.dailybrief.collect` | Every 3600 seconds; missed work coalesces on wake | `data-collect/run_collect.sh` |
| `com.dailybrief.vibe` | Hourly at :40, also chained from collection | `vibe-check/run.sh` |
| `com.dailybrief.calendar` | Daily 03:30 | `scripts/run_calendar.sh` |
| `com.dailybrief.science` | Daily at 06:00 Detroit time; browser order rotates every four hours | `data-collect/run_science.sh` |
| `com.dailybrief.starship` | Daily at 07:25 America/Detroit | `starship/run.sh` |
| `com.dailybrief.edition` | Daily 00:05; date rollover without AI/network collection | `scripts/run_edition.sh` |

All publishing entry points call `scripts/publish_brief.py` under the shared
publication lock. The writer captures only its named source output, creates a
disposable checkout of `origin/main`, and rebuilds/commits the matching edition
there. After a rejected push it discards that checkout and starts from the new
remote revision, up to three attempts. Generated commits are never rebased.
Other writers' unsubmitted local files and staged changes are excluded.

After success, the collector checkout fast-forwards if Git can do so safely.
If unrelated local edits prevent that, publication still succeeds and the local
checkout/index stay intact; future runs continue from `origin/main`. If all pushes
fail, captured source files remain local and the last candidate commit is retained
under `refs/dailybrief/failed/<timestamp>-<pid>` for inspection with `git show`.
`--no-push` builds only: no Git mutation, network collection, or push. Collector
custom output directories are not published from the repository.

GitHub Actions weather (`17 */4 * * *`, UTC) and calendar (`23 9 * * *`, UTC)
use the same publisher. `update-edition.yml` runs at 04:07 and 05:07 UTC to cover
Detroit midnight under EDT and EST, including when the Mac sleeps. GitHub
publishers share concurrency group `publish-brief`. Source collection schedules
remain unchanged. A missing calendar secret still skips calendar collection.

Install/update launch agents from their checked-in plists. Existing agents use
scripts by absolute path, so script updates take effect at the next run. For the
new agent:

```sh
cp scripts/launchd/com.dailybrief.edition.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.dailybrief.edition.plist
launchctl print gui/$(id -u)/com.dailybrief.edition
```

Do not use `kickstart` just to validate installation: that performs a publication.
The publisher itself can be checked without pushing:

```sh
python3 scripts/publish_brief.py --no-push
node --test scripts/*.test.mjs
```

## Validation and measured results

October 9 fixture: 2,221 events and 2,440 local stories. Initial DOM fell from
20,254 to under 700 elements, including the integrated ranking/science/Starship controls. Calendar cards previously measured 1,039px inside
a 366px dialog; they now fit the available column and wrap long unbroken text.
Browser regression checks cover 320, 390 and 430px, 844×390 landscape, 200% text
at 320px, modal keyboard navigation/focus return, direct Science access, lazy
archives, offline reload, storage denial, cache eviction, unsupported saved
contracts, and a stalled science section. Automated native tests on the iPhone
17 Pro simulator cover ordinary/new-window URL dispatch, unsafe-scheme rejection,
retry error state and Detroit midnight cache expiry.

In a controlled Chromium run with gzip, 150ms latency and 200 KiB/s download,
initial JSON transfer fell from 1,395,113 to 30,703 bytes (including response
headers); encoded JSON bodies fell from 1,390,913 to 28,003 bytes. First useful
weather improved from 9.88s to 1.42s. An unchanged refresh requested only the
manifest, returning a 304 with 300 measured transfer bytes. The integrated
sections total about 27 KB gzip including ranking defaults, science health and
Starship data. These are local controlled measurements, not claims about actual
GitHub Pages/CDN latency or cellular Safari performance. Images and the shell
are excluded from JSON totals.

```sh
npm ci
npx playwright install chromium webkit
python3 -m http.server 4173
# In another terminal:
npm run test:mobile
BRIEF_BROWSER=webkit npm run test:mobile
```

Browser tests use the published fixture's timestamp so historical fixture
editions remain testable. `check-brief.yml` runs Node and Chromium/WebKit checks
for code changes. Native checks: see README-ios.md.

Manual VoiceOver and physical-device Safari/system-browser handoff remain device
acceptance checks; automated DOM focus and simulator dispatch tests do not certify
those experiences. The initial science/Starship source was dated September 14;
this change exposes source age and now consumes the separately refreshed
science/Starship feeds, without fabricating a current launch estimate.


## Integration recovery checks

A complete displayed edition is retained while a replacement downloads. Any
section failure keeps the previous complete edition, labels it as saved, and
allows refresh to retry; a first visit still renders available sections promptly.
Saved event preferences or custom weights automatically load the full candidate
archive and rerank on initial load, reload, and edition changes. If unavailable,
the page explicitly identifies the compact selection as the fallback.

`npm run test:integration` covers automatic service-worker upgrades with offline
reopening, failed edition replacement/retry, and a favorite outside the public
shortlist surviving reload. `python3 scripts/test_publish_brief.py` includes a
competing publisher and exhausted retries in isolated repositories. The deploy
workflow also runs after successful weather/calendar/edition workflows because
commits made with `GITHUB_TOKEN` do not trigger a new push workflow.
October 9 layout follow-up: all six installed launchd agents were compared with
the repository templates and confirmed loaded. This presentation change adds no
recurring job; the existing edition publishers include candidates for all three
lanes on their next run. Science remains daily 06:00 and Starship daily 07:25.

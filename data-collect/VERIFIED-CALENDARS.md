# Verified calendar collection

The catalog now includes all eight requested calendars: North Star Lounge, The
Ark, Blue LLama, Ann Arbor Comedy Showcase, Park Players, Northville Drama Club,
Community Ensemble Theatre, and Village Theater at Cherry Hill (OvationTix
client 36533). Direct adapters also cover Happening at Michigan, Planet Ant,
Go Comedy, SEMJA, Waterloo, Royal Oak Nature Society, Kempf House, Five15,
Fisher Theatre, and DSO.

## Collection and coverage

Configured `verified_calendar` sources use bounded public HTTP calendars rather
than link-depth discovery. Each adapter supplies individual performances and a
coverage receipt with the queried window, pages, request count, unresolved
listings, errors, and completion state. The normal window is 90 calendar days,
inclusive of today. A fetch can succeed while its coverage remains partial.
Failed attempts preserve the last good crawl and write a new
`crawl/<slug>/calendar_attempt.json` receipt.

U-M and The Events Calendar APIs follow pagination. Squarespace exposes all
upcoming events. Crowdwork and OvationTix supply dated performances. SEMJA's
keyed monthly popups cover the window in at most four requests. DSO and Five15
use the same public feeds as their own calendars. Fisher's explicit advertised
weekly schedules are expanded within their stated run; unsupported schedules
retain verified previews and flag the missing coverage.

Season pages retain date-only performances when times are absent. Current
content takes precedence over an outdated page title. Conflicting season years,
unknown venues, ambiguous showtimes, and ticket calendar blocks are reported;
the adapter does not invent the missing facts.

## Retention and ranking

Complete configured calendar events bypass the model relevance gate and the
model generation cap. Their geography comes from the advertised location,
preventing an out-of-state artist biography from dropping a local show. Real
out-of-area locations still follow the normal geographic policy. Incomplete
candidates retain the existing retry and validation path.

`--max-items` bounds the remaining model intake. Its selection reserves 20% for
days 8–30 and 10% for days 31–90, returning unused capacity to other candidates.
`--extract-limit` bounds candidates requiring generation. `--score-limit`
(default 800) independently bounds optional taste scoring; an exhausted scoring
budget cannot remove calendar facts. `--mechanical-only` performs verified
calendar extraction without model calls. Unchanged successful checks update
`lastCheckedAt` without repeating extraction or changing snapshot identity.

Structured prices, USD minimum/maximum, availability, registration, age limits,
and check timestamps survive publication. Explicit cancellation, postponement,
or reinstatement notices take precedence over stale generic scheduled markup.
An explicitly dated replacement retains the old evidence and supplies the new
slot when it falls within the collection window. Missing pages never imply
cancellation.

Recommendation preferences now offer an admission budget per person and an
optional requirement for a known price. The budget compares the lowest listed
USD ticket; free admission is zero. Discounts, fees, course prices, and booking
terms remain in the source's price text. Unknown prices remain eligible by
default. Sold-out events remain in the calendar but leave recommendations.
These preferences are saved in the browser.

## Running and auditing

The existing `run_collect.sh` schedule incorporates the adapters, schedule
repair, a bounded organizer detail refresh, publication, the coverage audit,
and the shared shortlist. `COLLECT_SCORE_LIMIT` defaults to 800 and
`COLLECT_DETAIL_LIMIT` to 40. Detail refresh intervals are three hours within
one day, twelve hours within three days or for recommended items, daily within
one week, and weekly for the rest of the 90-day window. Failed detail requests
back off three hours and preserve facts. Related-event sections are excluded.

To refresh a particular date locally, run from `data-collect` with its existing
virtualenv. This updates local calendar data; it does not push Git changes:

```sh
.venv/bin/python crawl_sources.py --calendar-date 2026-11-01 --calendar-horizon-days 1
.venv/bin/python -m extract
.venv/bin/python -m extract.pipeline --mechanical-only
.venv/bin/python -m extract.repair_schedules --apply
.venv/bin/python refresh_event_changes.py --apply --limit 40
.venv/bin/python -m extract.publish --events-only
node ../scripts/select-best-bets.mjs
.venv/bin/python calendar_health.py --date 2026-11-01
```

The cumulative record store retains other dates and sources. `--events-only`
leaves the news feed in place, including for a calendar-only validation run.
Use `--calendar-horizon-days 90` to refresh the planning window. For isolated
verification, direct crawl `--output-dir`/`--db`, funnel `--crawl-dir`/`--out`,
pipeline `--processed`/`--out`/`--index`, and publish `--records`/`--out` to a
scratch directory. No model credentials are needed with `--mechanical-only`.

`processed/calendar_health.json` contains private per-source counts through the
funnel, duplicate detection, extraction/retries, quarantine, and publication
by date. Current candidates and retained historical snapshots are distinguished.
Review partial receipts and drop reasons when a date looks sparse; a lack of
events is not proof of complete coverage.

Offline checks:

```sh
.venv/bin/python -m unittest discover -s extract -t .
.venv/bin/python -m unittest test_verified_calendars
cd ..
npm test
```

## Current source limitations and next additions

Ann Arbor Comedy Showcase's homepage has dated cards, but Etix currently
blocks automated ticket requests. Its separate workshop page provides Sunday
sessions, breaks, prices, and graduation dates. Homepage-only performances
retain unknown times. Northville's fall play omits AM/PM, so its days are kept
without fabricated hours. CET states a 2026–2027 season but dates its spring
musical March 2026; those conflicting dates remain unresolved. U-M locations
that cannot be verified locally and Five15 partner events with unknown venues
are held for review. Fisher schedules with unresolved performance times remain
partial.

Useful next calendars to evaluate are [Tipping Point Theatre in Northville](https://www.tippingpointtheatre.com/),
[The Encore in Dexter](https://theencoretheatre.org/), and
[UMS season and events](https://ums.org/season/). Their current official season
pages were checked during implementation; adapters for these three are not yet
configured.

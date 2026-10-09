import test from "node:test";
import assert from "node:assert/strict";
import { reconcile, starshipView, editorialStarship, targetPassed, targetLabel, usableLaunchCache, launchDateKey, launchDateTime } from "../starship.mjs";
const now = new Date("2026-10-09T18:00:00Z");
const target = (extra = {}) => ({ precision: "day", lower: "2026-10-10", upper: null, label: "October 10, 2026", net: false, timeZone: "America/Chicago", ...extra });
const claim = (extra = {}) => ({ id: "target-1", sourceId: "spacex", sourceUrl: "https://www.spacex.com/launches/test", sourceType: "operator", publishedAt: "2026-10-09T16:00:00Z", observedAt: "2026-10-09T17:00:00Z", missionId: "starship-flight-14", claimType: "target", target: target(), excerpt: "Flight 14 is targeted for October 10, 2026", verification: "verified", claimConfidence: "confirmed", originId: "announcement-1", supersedes: [], parserVersion: "test", ...extra });
const run = (evidence, extra = {}) => reconcile({ evidence, now, ...extra });

test("a passed target is never evidence of completion or cancellation", () => {
  const record = run([claim({ target: target({ lower: "2026-10-08" }) })]);
  assert.equal(record.status, "target-passed");
  assert.deepEqual(record.outcomes, []);
  assert.equal(starshipView(record, now).officialTarget, null);
});
test("NET date retains precision and only requests an update after passing", () => {
  const t = target({ net: true, lower: "2026-10-08", label: "No earlier than October 8" });
  assert.equal(targetLabel(t), "No earlier than October 8");
  assert.equal(run([claim({ target: t })]).status, "target-passed");
});
test("month and quarter estimates survive until the period ends in source timezone", () => {
  assert.equal(targetPassed(target({ precision: "month", lower: "2026-10" }), now), false);
  assert.equal(targetPassed(target({ precision: "month", lower: "2026-09" }), now), true);
  assert.equal(targetPassed(target({ precision: "quarter", lower: "2026-Q4" }), now), false);
  assert.equal(targetPassed(target({ precision: "quarter", lower: "2026-Q3" }), now), true);
});
test("scrubs and explicit delays invalidate the old target", () => {
  for (const claimType of ["scrub", "delay"]) {
    const record = run([claim(), claim({ id: claimType, claimType, publishedAt: "2026-10-09T17:00:00Z" })]);
    assert.equal(record.status, "delayed");
    assert.equal(starshipView(record, now).officialTarget, null);
  }
});
test("source outages do not renew verification or editorial freshness", () => {
  const previous = run([claim()]);
  const later = new Date("2026-10-10T02:00:00Z");
  const record = run(previous.evidence, { now: later, previous, sourceHealth: [{ state: "error" }] });
  assert.equal(record.lastVerifiedAt, previous.lastVerifiedAt);
  assert.equal(record.expiresAt, previous.expiresAt);
  assert.equal(starshipView(previous, later).fresh, false);
  assert.equal(editorialStarship(record, later).officialTarget, null);
});
test("an unrelated new milestone does not reverify an old operator target", () => {
  const record = run([claim({ observedAt: "2026-10-08T17:00:00Z" }), claim({ id: "ready", claimType: "readiness" })]);
  assert.equal(record.status, "unannounced");
  assert.equal(starshipView(record, now).officialTarget, null);
});
test("contradictions are retained; repeated reporting is not independent corroboration", () => {
  const outside = claim({ id: "report", sourceType: "reporter", originId: "different-report", target: target({ lower: "2026-10-12" }) });
  const record = run([claim(), outside]);
  assert.equal(record.status, "uncertain");
  assert.equal(record.conflicts.length, 1);
  assert.equal(record.forecast.window, null);
  assert.equal(starshipView(record, now).officialTarget, null);
  assert.equal(run([claim(), { ...outside, originId: "announcement-1" }]).conflicts.length, 1);
  assert.equal(run([claim(), { ...outside, originId: "announcement-1", target: target() }]).conflicts.length, 0);
  assert.equal(run([claim(), claim({ id: "same", originId: "other", target: target({ label: "Oct 10" }) })]).conflicts.length, 0);
});
test("explicit supersession and retraction; future evidence cannot rewrite history", () => {
  const newer = claim({ id: "newer", publishedAt: "2026-10-09T17:00:00Z", target: target({ lower: "2026-10-12" }), supersedes: ["target-1"] });
  const record = run([claim(), newer]);
  assert.equal(record.conflicts.length, 0);
  assert.equal(record.evidence[0].verification, "superseded");
  assert.equal(run([claim({ verification: "retracted" })]).officialTarget, null);
  assert.equal(run([claim(), { ...newer, publishedAt: "2026-10-11T17:00:00Z" }]).officialTarget.evidenceId, "target-1");
});
test("outcomes are separate from the next flight and require operator evidence", () => {
  const outcome = claim({ id: "outcome", claimType: "outcome", outcome: "launched", actualLiftoffAt: "2026-10-09T16:30:00Z" });
  const next = claim({ id: "next", missionId: "starship-flight-15", publishedAt: "2026-10-09T17:00:00Z" });
  const record = run([claim(), outcome, next]);
  assert.equal(record.mission.id, "starship-flight-15");
  assert.equal(record.outcomes[0].missionId, "starship-flight-14");
  assert.equal(run([outcome]).officialTarget, null);
  assert.deepEqual(run([{ ...outcome, sourceType: "reporter" }]).outcomes, []);
});
test("shadow mode withholds the model forecast even with a plausible attempt", () => {
  const evidence = [claim(), claim({ id: "ready", originId: "ready", claimType: "readiness" }), claim({ id: "license", originId: "license", sourceType: "regulator", claimType: "regulatory", authorization: true })];
  const record = run(evidence);
  assert.equal(record.forecast.state, "plausible");
  assert.equal(starshipView(record, now).forecast, null);
  assert.match(starshipView(record, now).summary, /^Operator targeting/);
  assert.doesNotMatch(editorialStarship(record, now).summary, /plausible/);
  assert.equal(starshipView(run(evidence, { mode: "live" }), now).forecast.state, "plausible");
});
test("unknown authorization and generic closure notices never produce a plausible forecast", () => {
  const record = run([claim(), claim({ id: "ready", originId: "r", claimType: "readiness" }), claim({ id: "road", originId: "n", claimType: "notice", missionId: null })]);
  assert.equal(record.forecast.state, "targeted");
  assert.equal(record.forecast.window, null);
});
test("Detroit midnight and DST transitions preserve instant/day precision", () => {
  assert.equal(launchDateKey({ t0: "2026-10-10T03:59:00Z" }), "2026-10-09");
  assert.equal(launchDateKey({ t0: "2026-10-10T04:00:00Z" }), "2026-10-10");
  assert.match(launchDateTime({ t0: "2026-11-01T05:30:00Z" }), /1:30 AM EDT/);
  assert.match(launchDateTime({ t0: "2026-11-01T06:30:00Z" }), /1:30 AM EST/);
  assert.match(launchDateTime({ win_open: "2026-03-08T07:30:00Z" }), /Window opens at.*3:30 AM EDT/);
  assert.equal(launchDateKey({ est_date: { year: 2026, month: 10 } }), "");
  assert.equal(launchDateTime({ est_date: { year: 2026, month: 10, day: 10 }, date_str: "October 10" }), "Targeted October 10");
  assert.equal(targetPassed(target({ lower: "2026-10-09", timeZone: "America/Detroit" }), new Date("2026-10-10T03:59:00Z")), false);
  assert.equal(targetPassed(target({ lower: "2026-10-09", timeZone: "America/Detroit" }), new Date("2026-10-10T04:00:00Z")), true);
});
test("cache fallback has an absolute six-hour cap, including malformed/future cache", () => {
  const cached = { fetchedAt: +now - 2 * 3600000, launches: [] };
  assert.equal(usableLaunchCache(cached, false, +now), null);
  assert.equal(usableLaunchCache(cached, true, +now), cached);
  assert.equal(usableLaunchCache({ ...cached, fetchedAt: +now - 6 * 3600000 - 1 }, true, +now), null);
  assert.equal(usableLaunchCache({ ...cached, fetchedAt: +now + 1 }, true, +now), null);
  assert.equal(usableLaunchCache({ fetchedAt: "oops", launches: [] }, true, +now), null);
});
test("browser expiry cannot turn a newer readiness observation into a fresh target", () => {
  const record = run([claim({ observedAt: "2026-10-09T14:00:00Z" }), claim({ id: "ready", claimType: "readiness" })]);
  const view = starshipView(record, new Date("2026-10-09T21:00:00Z"));
  assert.equal(view.status, "unannounced");
  assert.equal(view.officialTarget, null);
  assert.doesNotMatch(view.summary, /Operator targeting/);
});

test("publication ordering compares instants across source timezones", () => {
  const older = claim({ id: "old", publishedAt: "2026-10-09T12:00:00-04:00" });
  const newer = claim({ id: "new", publishedAt: "2026-10-09T11:30:00-05:00", supersedes: ["old"] });
  assert.equal(run([older, newer]).officialTarget.evidenceId, "new");
});

test("community leads cannot select a mission, reverify, authorize, or complete it", () => {
  const e = claim({ sourceType: "community", claimType: "discussion", verification: "unverified", communityKind: "speculation", linkedSourceUrls: ["https://www.spacex.com/launches/"], excerpt: "Maybe in November" });
  const record = run([e]);
  assert.equal(record.mission.id, null);
  assert.equal(record.lastVerifiedAt, null);
  assert.equal(record.officialTarget, null);
  assert.equal(record.forecast.window, null);
  assert.deepEqual(record.outcomes, []);
  assert.equal(starshipView(record, now).communityOutlook.length, 1);
  assert.equal(editorialStarship(record, now).communityOutlook, undefined);
  // Defensive even if a malformed input claims community evidence is verified.
  const forged = run([claim(), { ...e, id: "forged", verification: "verified", claimType: "outcome", supersedes: ["target-1"], outcome: "launched" }]);
  assert.equal(forged.officialTarget.evidenceId, "target-1");
  assert.deepEqual(forged.outcomes, []);
});
test("community outlook expires, deduplicates sources and excludes other/completed missions", () => {
  const e = claim({ sourceType: "community", claimType: "discussion", verification: "unverified", communityKind: "speculation" });
  const record = run([e, { ...e, id: "repost" }, { ...e, id: "old", originId: "old", publishedAt: "2026-08-01T12:00:00Z" }, { ...e, id: "outage", originId: "outage", observedAt: "2026-10-07T12:00:00Z" }]);
  assert.equal(starshipView(record, now).communityOutlook.length, 1);
  assert.equal(starshipView(record, new Date("2026-10-11T18:00:00Z")).communityOutlook.length, 0);
  assert.equal(starshipView(run([e], { activeMission: "starship-flight-15" }), now).communityOutlook.length, 0);
  const outcome = claim({ id: "finished", claimType: "outcome", outcome: "completed" });
  assert.equal(starshipView(run([e, outcome]), now).communityOutlook.length, 0);
});

test("generic launch-thread boilerplate does not appear in the Starship community outlook", () => {
  const e = claim({ sourceId: "reddit-spacex", sourceType: "community", claimType: "discussion", verification: "unverified", sourceUrl: "https://www.reddit.com/r/spacex/comments/abc/dragon/", excerpt: "Dragon launch thread — Statistics include Starship launches" });
  assert.equal(starshipView(run([e]), now).communityOutlook.length, 0);
  assert.equal(starshipView(run([{ ...e, sourceUrl: "https://www.reddit.com/r/spacex/comments/abc/development/def/", excerpt: "Comment — Maybe next month" }]), now).communityOutlook.length, 1);
});

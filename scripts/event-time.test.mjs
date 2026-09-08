import test from "node:test";
import assert from "node:assert/strict";
import { eventDateLabel } from "../event-time.mjs";

test("labels use Detroit's actual day and ignore stale supplied text", () => {
  const event = { start: "2026-09-09T00:00:00Z", dateLabel: "Tomorrow" };
  assert.equal(eventDateLabel(event, new Date("2026-09-09T01:00:00Z")), "Tonight, 8:00 PM");
  assert.equal(eventDateLabel(event, new Date("2026-09-09T05:00:00Z")), "Yesterday, 8:00 PM");
});

test("same-day ranges and evening threshold", () => {
  const now = new Date("2026-09-08T12:00:00-04:00");
  assert.equal(eventDateLabel({ start: "2026-09-08T17:00:00-04:00", end: "2026-09-08T19:00:00-04:00" }, now), "Today, 5:00 PM–7:00 PM");
  assert.equal(eventDateLabel({ start: "2026-09-08T18:00:00-04:00" }, now), "Tonight, 6:00 PM");
});

test("overnight ranges identify both days", () => {
  assert.equal(eventDateLabel({ start: "2026-09-08T23:00:00-04:00", end: "2026-09-09T01:00:00-04:00" }, new Date("2026-09-08T12:00:00-04:00")), "Tonight, 11:00 PM – Tomorrow, 1:00 AM");
});

test("tomorrow survives daylight saving change and later dates include year when needed", () => {
  assert.equal(eventDateLabel({ start: "2026-11-01T18:00:00-05:00" }, new Date("2026-10-31T12:00:00-04:00")), "Tomorrow, 6:00 PM");
  assert.match(eventDateLabel({ start: "2027-01-05T18:00:00-05:00" }, new Date("2026-09-08T12:00:00Z")), /2027/);
});

test("missing or invalid ends do not fabricate a range", () => {
  const now = new Date("2026-09-08T12:00:00Z");
  assert.equal(eventDateLabel({ start: "2026-09-08T18:00:00-04:00", end: "invalid" }, now), "Tonight, 6:00 PM");
  assert.equal(eventDateLabel({ start: "invalid" }, now), "Date to be confirmed");
});

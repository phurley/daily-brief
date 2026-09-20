import test from "node:test";
import assert from "node:assert/strict";
import {
  getAstronomicalSeason,
  getCrossQuarterDays,
  getEquinoxesAndSolstices,
  getPerihelionAphelion,
  isLeapYear,
  daysInYear,
  dayOfYear,
  nextLeapDay,
} from "../almanac-calc.mjs";

const minutesBetween = (a, b) => Math.abs(Date.parse(a) - Date.parse(b)) / 60_000;

test("equinoxes and solstices match published 2026 times", () => {
  const events = getEquinoxesAndSolstices(2026, "America/Detroit");
  const expected = {
    "march-equinox": "2026-03-20T14:46:00Z",
    "june-solstice": "2026-06-21T08:25:00Z",
    "september-equinox": "2026-09-23T00:05:00Z",
    "december-solstice": "2026-12-21T20:50:00Z",
  };
  for (const event of events) {
    assert.ok(
      minutesBetween(event.utc, expected[event.key]) <= 2,
      `${event.key}: ${event.utc} vs ${expected[event.key]}`,
    );
    assert.ok(event.local, `${event.key} missing local time`);
  }
});

test("cross-quarter days land near the start of their months", () => {
  const days = getCrossQuarterDays(2026);
  assert.deepEqual(
    days.map((day) => day.key),
    ["imbolc", "beltane", "lughnasadh", "samhain"],
  );
  assert.equal(days[0].utc.slice(0, 7), "2026-02");
  assert.equal(days[1].utc.slice(0, 7), "2026-05");
  assert.equal(days[2].utc.slice(0, 7), "2026-08");
  assert.equal(days[3].utc.slice(0, 7), "2026-11");
});

test("perihelion is in January and aphelion in July", () => {
  const { perihelion, aphelion } = getPerihelionAphelion(2026);
  assert.equal(perihelion.utc.slice(0, 7), "2026-01");
  assert.equal(aphelion.utc.slice(0, 7), "2026-07");
});

test("astronomical season follows the sun's longitude", () => {
  assert.equal(getAstronomicalSeason(new Date("2026-02-01T12:00:00Z")), "winter");
  assert.equal(getAstronomicalSeason(new Date("2026-04-01T12:00:00Z")), "spring");
  assert.equal(getAstronomicalSeason(new Date("2026-08-01T12:00:00Z")), "summer");
  assert.equal(getAstronomicalSeason(new Date("2026-11-01T12:00:00Z")), "autumn");
});

test("leap-year rules include the century exceptions", () => {
  assert.equal(isLeapYear(2024), true);
  assert.equal(isLeapYear(2026), false);
  assert.equal(isLeapYear(1900), false);
  assert.equal(isLeapYear(2000), true);
  assert.equal(isLeapYear(2100), false);
  assert.equal(daysInYear(2024), 366);
  assert.equal(daysInYear(2026), 365);
});

test("day of year and the next leap day", () => {
  assert.equal(dayOfYear(new Date("2024-02-29T12:00:00Z")), 60);
  assert.equal(dayOfYear(new Date("2024-12-31T12:00:00Z")), 366);
  assert.deepEqual(nextLeapDay(new Date("2026-09-01T00:00:00Z")), {
    year: 2028,
    date: new Date("2028-02-29T00:00:00Z"),
  });
  assert.deepEqual(nextLeapDay(new Date("2028-01-01T00:00:00Z")).year, 2028);
  // A date on Feb 29 itself should roll to the following leap year.
  assert.equal(nextLeapDay(new Date("2028-02-29T06:00:00Z")).year, 2032);
});
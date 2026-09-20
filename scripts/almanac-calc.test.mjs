import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  computeAlmanacDay,
  formatInZone,
  startOfLocalDay,
  getMoonIllumination,
  moonPhaseName,
  nextFullMoon,
} from "../almanac-calc.mjs";

// Captured MET Norway reference values, kept only for these regression checks.
const almanac = JSON.parse(
  readFileSync(new URL("./fixtures/almanac-reference.json", import.meta.url), "utf8"),
);
const location = almanac.location;

const minutesBetween = (a, b) => Math.abs(Date.parse(a) - Date.parse(b)) / 60_000;

test("solar events match the reference almanac within a minute", () => {
  let worst = 0;
  for (const reference of almanac.days) {
    const day = computeAlmanacDay(reference.date, location);
    for (const key of ["sunrise", "sunset", "solarNoon"]) {
      worst = Math.max(worst, minutesBetween(day[key], reference[key]));
    }
    assert.ok(
      Math.abs(day.daylightMinutes - reference.daylightMinutes) <= 2,
      reference.date,
    );
  }
  assert.ok(worst <= 1.5, `worst solar drift ${worst.toFixed(2)} minutes`);
});

test("moon illumination and phase match the reference almanac", () => {
  // Elongation-sector boundaries land at these illumination values; the
  // reference and this model can disagree by one sector right on a boundary.
  const sectorBoundaries = [3.8, 30.9, 69.1, 96.2];
  const nearBoundary = (percent) =>
    sectorBoundaries.some((boundary) => Math.abs(percent - boundary) <= 2);

  let worst = 0;
  for (const reference of almanac.days) {
    const { moon } = computeAlmanacDay(reference.date, location);
    worst = Math.max(
      worst,
      Math.abs(moon.illuminationPercent - reference.moon.illuminationPercent),
    );
    // The reference names phases from the same 45-degree elongation sectors.
    if (!nearBoundary(reference.moon.illuminationPercent)) {
      assert.equal(moon.phase, reference.moon.phase, reference.date);
    }
  }
  assert.ok(worst <= 1, `worst illumination drift ${worst.toFixed(2)} points`);
});

test("moonrise and moonset instants match the reference within two minutes", () => {
  const referenceInstants = (kind) =>
    almanac.days
      .map((day) => day.moon[kind])
      .filter(Boolean)
      .map((value) => Date.parse(value));

  for (const kind of ["moonrise", "moonset"]) {
    const instants = referenceInstants(kind);
    for (const reference of almanac.days) {
      const value = computeAlmanacDay(reference.date, location).moon[kind];
      if (!value) continue;
      const nearest = Math.min(
        ...instants.map((instant) => Math.abs(instant - Date.parse(value))),
      );
      assert.ok(nearest <= 2 * 60_000, `${kind} ${reference.date}`);
    }
  }
});

test("phase names follow the eight elongation sectors", () => {
  assert.equal(moonPhaseName(0), "new");
  assert.equal(moonPhaseName(0.1), "waxing-crescent");
  assert.equal(moonPhaseName(0.25), "first-quarter");
  assert.equal(moonPhaseName(0.35), "waxing-gibbous");
  assert.equal(moonPhaseName(0.5), "full");
  assert.equal(moonPhaseName(0.6), "waning-gibbous");
  assert.equal(moonPhaseName(0.75), "last-quarter");
  assert.equal(moonPhaseName(0.9), "waning-crescent");
});

test("the next full moon lands on a full moon", () => {
  const next = nextFullMoon(new Date("2026-08-01T06:00:00Z"));
  assert.equal(moonPhaseName(getMoonIllumination(next).phase), "full");
  // The August 2026 full moon occurs on the 28th.
  assert.equal(next.toISOString().slice(0, 10), "2026-08-28");
});

test("instants carry the zone offset across a DST boundary", () => {
  const summer = new Date(Date.UTC(2026, 7, 1, 12));
  const winter = new Date(Date.UTC(2026, 11, 1, 12));
  assert.match(formatInZone(summer, "America/Detroit"), /-04:00$/);
  assert.match(formatInZone(winter, "America/Detroit"), /-05:00$/);
  assert.equal(
    startOfLocalDay("2026-11-01", "America/Detroit"),
    Date.parse("2026-11-01T04:00:00Z"),
  );
});

test("returns schema-shaped data", () => {
  const day = computeAlmanacDay("2026-09-15", location);
  assert.equal(day.date, "2026-09-15");
  assert.match(day.sunrise, /^2026-09-15T\d{2}:\d{2}:00-04:00$/);
  assert.equal(typeof day.moon.daysUntilFull, "number");
  assert.match(day.moon.summary, /^[A-Z][a-z]+(?: [A-Z][a-z]+){0,1}, \d+\.\d% illuminated\.$/);
  assert.equal(day.moon.imageAlt, day.moon.summary);
});
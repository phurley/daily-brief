import test from "node:test";
import assert from "node:assert/strict";
import {
  dateKeyInZone,
  equationOfTime,
  getBlueMoons,
  getDaylightInfo,
  getEquilux,
  getEquationOfTimeExtremes,
  getNextMoonPhases,
  getSunPhases,
  getSunTimes,
  getSupermoonFullMoons,
  isSupermoon,
  moonDistanceKm,
  sunAzimuth,
} from "../almanac-calc.mjs";
import {
  ECLIPSES,
  getActiveMeteorShowers,
  getEclipses,
  getUpcomingEclipses,
  getUpcomingMeteorShowers,
  SKY_EVENT_COVERAGE,
} from "../sky-events.mjs";
import { assessAurora, geomagneticLatitude, kpThresholds } from "../aurora.mjs";
import { getOnThisDate } from "../on-this-date.mjs";

const location = {
  name: "Canton",
  region: "Michigan",
  latitude: 42.3086,
  longitude: -83.4824,
  timeZone: "America/Detroit",
};

test("twilight and golden/blue hour windows are ordered and bounded", () => {
  const phases = getSunPhases("2026-08-01", location.latitude, location.longitude, location.timeZone);
  const sun = getSunTimes("2026-08-01", location.latitude, location.longitude, location.timeZone);
  const order = [
    phases.astronomicalDawn,
    phases.nauticalDawn,
    phases.civilDawn,
    sun.sunrise,
    sun.sunset,
    phases.civilDusk,
    phases.nauticalDusk,
    phases.astronomicalDusk,
  ];
  for (let i = 1; i < order.length; i += 1) {
    assert.ok(order[i - 1] < order[i], `element ${i} out of order`);
  }
  assert.equal(phases.blueHourMorning.start.getTime(), phases.civilDawn.getTime());
  assert.equal(phases.goldenHourMorning.start.getTime(), phases.blueHourMorning.end.getTime());
  assert.equal(phases.goldenHourEvening.end.getTime(), phases.blueHourEvening.start.getTime());
});

test("sunrise is northeast and sunset northwest in early August", () => {
  const { sunrise, sunset } = getSunTimes("2026-08-01", location.latitude, location.longitude, location.timeZone);
  const rise = sunAzimuth(sunrise, location.latitude, location.longitude);
  const set = sunAzimuth(sunset, location.latitude, location.longitude);
  assert.ok(rise > 45 && rise < 90, `sunrise azimuth ${rise}`);
  assert.ok(set > 270 && set < 315, `sunset azimuth ${set}`);
});

test("equation of time hits its known annual extremes", () => {
  const extremes = getEquationOfTimeExtremes(2026, location.timeZone);
  assert.ok(Math.abs(extremes.maximum.minutes - 16.4) < 0.5, `${extremes.maximum.minutes}`);
  assert.ok(Math.abs(extremes.minimum.minutes - -14.2) < 0.5, `${extremes.minimum.minutes}`);
  assert.equal(extremes.maximum.utc.slice(5, 7), "11");
  assert.equal(extremes.minimum.utc.slice(5, 7), "02");
  assert.ok(equationOfTime(new Date("2026-11-03T12:00:00Z")) > 16);
});

test("equilux days have twelve hours of daylight", () => {
  const equilux = getEquilux(2026, location.latitude, location.timeZone);
  for (const key of ["spring", "autumn"]) {
    const day = dateKeyInZone(equilux[key].instant, location.timeZone);
    const times = getSunTimes(day, location.latitude, location.longitude, location.timeZone);
    assert.ok(Math.abs(times.daylightMinutes - 720) <= 2, `${key}: ${times.daylightMinutes}`);
  }
  assert.ok(equilux.spring.instant < new Date("2026-03-20T14:46:00Z"));
  assert.ok(equilux.autumn.instant > new Date("2026-09-23T00:05:00Z"));
});

test("daylight deltas shrink in August and exceed the winter solstice", () => {
  const info = getDaylightInfo("2026-08-01", location.latitude, location.longitude, location.timeZone);
  assert.ok(info.changeFromYesterdayMinutes < 0);
  assert.ok(info.sinceWinterSolstice.daylightDeltaMinutes > 0);
  assert.ok(info.solarNoonElevationDegrees > 60 && info.solarNoonElevationDegrees < 75);
});

test("next moon phases are valid and ordered by type", () => {
  const from = new Date("2026-08-01T06:00:00Z");
  const phases = getNextMoonPhases(from);
  assert.ok(phases.new > from);
  assert.ok(phases.firstQuarter > from);
  assert.ok(phases.full > from);
  assert.ok(phases.lastQuarter > from);
  assert.equal(phases.full.toISOString().slice(0, 10), "2026-08-28");
});

test("blue moons and supermoons are detected", () => {
  const blues = getBlueMoons(2026);
  assert.equal(blues.length, 1);
  assert.equal(blues[0].month, 5);
  const supermoons = getSupermoonFullMoons(2026);
  assert.ok(supermoons.length >= 1);
  assert.ok(isSupermoon(supermoons[0].at));
  assert.ok(moonDistanceKm(supermoons[0].at) < 360_000);
});

test("sky-event tables cover the promised range", () => {
  assert.equal(SKY_EVENT_COVERAGE.eclipses.first, "2021-05-26");
  assert.equal(SKY_EVENT_COVERAGE.eclipses.last, "2030-12-09");
  assert.equal(ECLIPSES.length, 44);
  const solar2026 = getEclipses(2026, "solar");
  assert.equal(solar2026.length, 2);
  assert.equal(solar2026[0].type, "Annular");
  assert.equal(solar2026[1].type, "Total");
});

test("meteor showers are active in season and upcoming peaks are ordered", () => {
  const active = getActiveMeteorShowers(new Date("2026-08-01T16:00:00Z")).map((s) => s.key);
  assert.ok(active.includes("perseids"));
  assert.ok(active.includes("southern-delta-aquariids"));
  // Windows that wrap New Year still work in late December.
  assert.deepEqual(
    getActiveMeteorShowers(new Date("2026-12-30T16:00:00Z")).map((s) => s.key),
    ["quadrantids"],
  );
  const upcoming = getUpcomingMeteorShowers(new Date("2026-08-01T16:00:00Z"), 3);
  assert.deepEqual(upcoming.map((s) => s.key), ["perseids", "draconids", "orionids"]);
});

test("aurora assessment follows geomagnetic latitude", () => {
  const mlat = geomagneticLatitude(location.latitude, location.longitude);
  assert.ok(Math.abs(mlat - 51.5) < 0.2, `${mlat}`);
  const thresholds = kpThresholds(location.latitude, location.longitude);
  assert.ok(thresholds.overhead > thresholds.horizon);
  assert.equal(assessAurora(2, location.latitude, location.longitude).level, "none");
  assert.equal(assessAurora(6, location.latitude, location.longitude).level, "horizon");
  assert.equal(assessAurora(8, location.latitude, location.longitude).level, "overhead");
});

test("getOnThisDate returns a complete, consistent day", () => {
  const day = getOnThisDate("2026-08-01", location);
  assert.equal(day.date, "2026-08-01");
  assert.equal(day.season.current, "summer");
  assert.equal(day.season.next.name, "September Equinox");
  assert.equal(day.sun.daylightMinutes, day.daylight.minutes);
  assert.equal(day.moon.phase, "waning-gibbous");
  assert.ok(day.moon.nextPhases.full.startsWith("2026-08-28"));
  assert.equal(day.skyEvents.activeMeteorShowers.length, 2);
  assert.ok(day.skyEvents.upcomingEclipses[0].at.startsWith("2026-08-12"));
  assert.equal(day.aurora, null);
});

test("getOnThisDate works in early January before this year's first marker", () => {
  const day = getOnThisDate("2027-01-05", location);
  assert.equal(day.season.current, "winter");
  assert.equal(day.season.began.name, "December Solstice");
  assert.ok(day.season.began.at.startsWith("2026-12-21"));
  assert.equal(day.season.next.name, "March Equinox");
});
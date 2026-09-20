// on-this-date.mjs
//
// `getOnThisDate()` is the one call the brief needs: it gathers the computed
// almanac day plus twilight, azimuths, daylight deltas, equilux, equation of
// time, moon events, seasonal markers, meteor showers, and eclipses into a
// single JSON-friendly object.
//
// Everything here is offline and synchronous except the optional `aurora`
// field, which you supply by awaiting fetchAuroraOutlook() from aurora.mjs.

import {
  computeAlmanacDay,
  formatInZone,
  getAstronomicalSeason,
  getBlueMoons,
  getCrossQuarterDays,
  getDaylightInfo,
  getEquationOfTimeExtremes,
  getEquilux,
  getEquinoxesAndSolstices,
  getNextMoonPhases,
  getSunPhases,
  getSunTimes,
  getSupermoonFullMoons,
  isSupermoon,
  moonDistanceKm,
  startOfLocalDay,
  sunAzimuth,
  equationOfTime,
} from "./almanac-calc.mjs";
import {
  getActiveMeteorShowers,
  getUpcomingEclipses,
  getUpcomingMeteorShowers,
} from "./sky-events.mjs";

const DAY_MS = 86_400_000;
const round1 = (value) => Math.round(value * 10) / 10;
const daysUntil = (target, reference) =>
  Math.round((target.getTime() - reference.getTime()) / DAY_MS);

/**
 * Everything the brief can say about one local calendar day.
 *
 * @param {string} dateString  Local calendar date, `YYYY-MM-DD`.
 * @param {{name?: string, region?: string, latitude: number, longitude: number, timeZone: string}} location
 * @param {{moonReferenceHourUtc?: number, aurora?: object}} [options]
 *   Pass `aurora` (from fetchAuroraOutlook) to attach the northern-lights
 *   outlook; it is omitted from the result when not supplied.
 */
export function getOnThisDate(dateString, location, options = {}) {
  const { latitude, longitude, timeZone } = location;
  const { aurora = null, ...almanacOptions } = options;
  const year = Number(dateString.slice(0, 4));
  const dayStart = startOfLocalDay(dateString, timeZone);
  const reference = new Date(dayStart + 12 * 3_600_000);

  const iso = (date) =>
    date instanceof Date && !Number.isNaN(date.getTime())
      ? formatInZone(date, timeZone, { seconds: false })
      : undefined;

  const almanacDay = computeAlmanacDay(dateString, location, almanacOptions);
  const sunTimes = getSunTimes(dateString, latitude, longitude, timeZone);
  const phases = getSunPhases(dateString, latitude, longitude, timeZone);
  const daylight = getDaylightInfo(dateString, latitude, longitude, timeZone);

  // --- Seasons -------------------------------------------------------------
  const thisYearMarkers = getEquinoxesAndSolstices(year, timeZone);
  const previousYearMarkers = getEquinoxesAndSolstices(year - 1, timeZone);
  const marker = (event) =>
    event ? { name: event.name, at: iso(event.instant), inDays: daysUntil(event.instant, reference) } : undefined;
  const pastMarkers = [...previousYearMarkers, ...thisYearMarkers].filter(
    (event) => event.instant <= reference,
  );
  const beganMarker = pastMarkers[pastMarkers.length - 1];
  const nextMarker =
    thisYearMarkers.find((event) => event.instant > reference) ??
    getEquinoxesAndSolstices(year + 1, timeZone)[0];

  // --- Equilux -------------------------------------------------------------
  const thisYearEquilux = getEquilux(year, latitude, timeZone);
  const equiluxList = [
    { name: "Spring equilux", ...thisYearEquilux.spring },
    { name: "Autumn equilux", ...thisYearEquilux.autumn },
  ].filter((entry) => entry.instant);
  let nextEquilux = equiluxList.find((entry) => entry.instant > reference);
  if (!nextEquilux) {
    const following = getEquilux(year + 1, latitude, timeZone);
    if (following.spring) {
      nextEquilux = { name: "Spring equilux", ...following.spring };
    }
  }

  // --- Moon ----------------------------------------------------------------
  const nextPhases = getNextMoonPhases(reference);
  const blueMoons = [...getBlueMoons(year), ...getBlueMoons(year + 1)];
  const nextBlueMoon = blueMoons.find(
    (blue) => blue.second.getTime() >= reference.getTime(),
  );
  const supermoons = [
    ...getSupermoonFullMoons(year),
    ...getSupermoonFullMoons(year + 1),
  ];
  const nextSupermoon = supermoons.find(
    (supermoon) => supermoon.at.getTime() >= reference.getTime(),
  );
  const fullDistanceKm = moonDistanceKm(nextPhases.full);

  // --- Equation of time ----------------------------------------------------
  const eotExtremes = getEquationOfTimeExtremes(year, timeZone);

  // --- Fixed sky events ----------------------------------------------------
  const describeShower = (shower) => ({
    key: shower.key,
    name: shower.name,
    radiant: shower.radiant,
    zhr: shower.zhr,
    velocityKmS: shower.velocityKmS,
    parent: shower.parent,
    peak: iso(shower.peak),
    activeStart: iso(shower.activeStart),
    activeEnd: iso(shower.activeEnd),
    peakInDays: daysUntil(shower.peak, reference),
  });
  const describeEclipse = (eclipse) => ({
    kind: eclipse.kind,
    type: eclipse.type,
    at: iso(eclipse.at),
    inDays: daysUntil(eclipse.at, reference),
    magnitude: eclipse.magnitude,
    duration: eclipse.duration,
    visibility: eclipse.visibility,
  });

  return {
    date: dateString,
    location: { ...location },
    season: {
      current: getAstronomicalSeason(reference),
      began: beganMarker
        ? { name: beganMarker.name, at: iso(beganMarker.instant) }
        : undefined,
      next: marker(nextMarker),
    },
    sun: {
      sunrise: iso(sunTimes.sunrise),
      sunset: iso(sunTimes.sunset),
      solarNoon: iso(sunTimes.solarNoon),
      daylightMinutes: sunTimes.daylightMinutes,
      solarNoonElevationDegrees: round1(daylight.solarNoonElevationDegrees),
      sunriseAzimuthDegrees: sunTimes.sunrise
        ? round1(sunAzimuth(sunTimes.sunrise, latitude, longitude))
        : undefined,
      sunsetAzimuthDegrees: sunTimes.sunset
        ? round1(sunAzimuth(sunTimes.sunset, latitude, longitude))
        : undefined,
      civilDawn: iso(phases.civilDawn),
      civilDusk: iso(phases.civilDusk),
      nauticalDawn: iso(phases.nauticalDawn),
      nauticalDusk: iso(phases.nauticalDusk),
      astronomicalDawn: iso(phases.astronomicalDawn),
      astronomicalDusk: iso(phases.astronomicalDusk),
      goldenHourMorning: {
        start: iso(phases.goldenHourMorning.start),
        end: iso(phases.goldenHourMorning.end),
      },
      goldenHourEvening: {
        start: iso(phases.goldenHourEvening.start),
        end: iso(phases.goldenHourEvening.end),
      },
      blueHourMorning: {
        start: iso(phases.blueHourMorning.start),
        end: iso(phases.blueHourMorning.end),
      },
      blueHourEvening: {
        start: iso(phases.blueHourEvening.start),
        end: iso(phases.blueHourEvening.end),
      },
      equationOfTimeMinutes: round1(equationOfTime(reference)),
    },
    daylight: {
      minutes: daylight.minutes,
      changeFromYesterdayMinutes: daylight.changeFromYesterdayMinutes,
      sinceSolstice: daylight.sinceSolstice
        ? {
            name: daylight.sinceSolstice.name,
            at: iso(daylight.sinceSolstice.at),
            deltaMinutes: daylight.sinceSolstice.daylightDeltaMinutes,
          }
        : undefined,
      sinceWinterSolstice: daylight.sinceWinterSolstice
        ? {
            name: daylight.sinceWinterSolstice.name,
            at: iso(daylight.sinceWinterSolstice.at),
            deltaMinutes: daylight.sinceWinterSolstice.daylightDeltaMinutes,
          }
        : undefined,
    },
    equilux: {
      next: nextEquilux
        ? {
            name: nextEquilux.name,
            at: iso(nextEquilux.instant),
            inDays: daysUntil(nextEquilux.instant, reference),
          }
        : undefined,
    },
    moon: {
      ...almanacDay.moon,
      distanceKm: Math.round(moonDistanceKm(reference)),
      nextPhases: {
        new: iso(nextPhases.new),
        firstQuarter: iso(nextPhases.firstQuarter),
        full: iso(nextPhases.full),
        lastQuarter: iso(nextPhases.lastQuarter),
      },
      blueMoon: nextBlueMoon
        ? {
            at: iso(nextBlueMoon.second),
            inDays: daysUntil(nextBlueMoon.second, reference),
          }
        : undefined,
      supermoon: {
        nextFullIsSupermoon: isSupermoon(nextPhases.full),
        nextFullDistanceKm: Math.round(fullDistanceKm),
        next: nextSupermoon
          ? {
              at: iso(nextSupermoon.at),
              inDays: daysUntil(nextSupermoon.at, reference),
              distanceKm: Math.round(nextSupermoon.distanceKm),
            }
          : undefined,
      },
    },
    equationOfTimeExtremes: {
      maximum: {
        at: iso(eotExtremes.maximum.instant),
        minutes: round1(eotExtremes.maximum.minutes),
      },
      minimum: {
        at: iso(eotExtremes.minimum.instant),
        minutes: round1(eotExtremes.minimum.minutes),
      },
    },
    seasonalMarkers: thisYearMarkers.map((event) => ({
      key: event.key,
      name: event.name,
      season: event.season,
      at: iso(event.instant),
      inDays: daysUntil(event.instant, reference),
    })),
    crossQuarterDays: getCrossQuarterDays(year, timeZone).map((event) => ({
      key: event.key,
      name: event.name,
      at: iso(event.instant),
      inDays: daysUntil(event.instant, reference),
    })),
    skyEvents: {
      activeMeteorShowers: getActiveMeteorShowers(reference).map(describeShower),
      upcomingMeteorShowers: getUpcomingMeteorShowers(reference, 3).map(describeShower),
      upcomingEclipses: getUpcomingEclipses(reference, 3).map(describeEclipse),
    },
    aurora: aurora
      ? {
          source: aurora.source,
          geomagneticLatitude: aurora.geomagneticLatitude,
          currentKp: aurora.currentKp,
          maxKpNext24h: aurora.maxKpNext24h,
          maxKpNext48h: aurora.maxKpNext48h,
          bestWindow: aurora.bestWindow
            ? { at: iso(aurora.bestWindow.time), kp: aurora.bestWindow.kp }
            : undefined,
          solarWind: aurora.solarWind
            ? {
                speedKmS: aurora.solarWind.speedKmS,
                bt: aurora.solarWind.bt,
                bzGsm: aurora.solarWind.bz_gsm,
              }
            : undefined,
          visibility: aurora.visibility,
        }
      : null,
  };
}

export default getOnThisDate;
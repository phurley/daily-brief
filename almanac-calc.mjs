// almanac-calc.mjs
//
// Computes the astronomical facts used by the Daily Brief almanac instead of
// fetching them. Given a date and a location it returns sunrise, sunset, solar
// noon, daylight minutes, moon phase, illumination, days until full, and
// moonrise/moonset — the shape the app's sky card consumes.
//
// Solar position uses the NOAA/Meeus low-precision series; lunar position uses
// the periodic series from Meeus, "Astronomical Algorithms" (ch. 47). Phase is
// taken directly from the ecliptic elongation of the moon from the sun. Rise
// and set times are found by sampling each body's altitude across the local
// calendar day and bisecting the horizon crossings. Accuracy is within about a
// minute for mid-latitudes.
//
// The module has no dependencies and no network access. It is timezone aware:
// pass an IANA zone (e.g. "America/Detroit") and all returned instants are ISO
// 8601 strings carrying that zone's offset.

const DEG = Math.PI / 180;
const RAD = 180 / Math.PI;
const DAY_MS = 86_400_000;
const MINUTE_MS = 60_000;
const J2000 = 2_451_545;
const UNIX_EPOCH_JD = 2_440_587.5;
const AU_KM = 149_597_871;

// Sun altitude at which the upper limb touches the horizon (refraction +
// solar semidiameter), in degrees.
const SUN_HORIZON_ALTITUDE = -0.833;
// Moon altitude at which the upper limb touches the horizon (refraction +
// lunar semidiameter + parallax), matching the common SunCalc convention.
const MOON_HORIZON_ALTITUDE = 0.133;

const PHASES = [
  ["new", "New"],
  ["waxing-crescent", "Waxing Crescent"],
  ["first-quarter", "First Quarter"],
  ["waxing-gibbous", "Waxing Gibbous"],
  ["full", "Full"],
  ["waning-gibbous", "Waning Gibbous"],
  ["last-quarter", "Last Quarter"],
  ["waning-crescent", "Waning Crescent"],
];
const PHASE_LABELS = Object.fromEntries(PHASES);

const mod = (value, span) => ((value % span) + span) % span;

function toJulian(date) {
  return date.getTime() / DAY_MS + UNIX_EPOCH_JD;
}

// --- Ecliptic positions ----------------------------------------------------

/** Apparent geocentric ecliptic longitude of the sun (degrees) and distance (AU). */
function sunEcliptic(jd) {
  const T = (jd - J2000) / 36525;
  const L0 = mod(280.46646 + 36000.76983 * T + 0.0003032 * T * T, 360);
  const M = 357.52911 + 35999.05029 * T - 0.0001537 * T * T;
  const e = 0.016708634 - 0.000042037 * T - 0.0000001267 * T * T;
  const C =
    (1.914602 - 0.004817 * T - 0.000014 * T * T) * Math.sin(M * DEG) +
    (0.019993 - 0.000101 * T) * Math.sin(2 * M * DEG) +
    0.000289 * Math.sin(3 * M * DEG);
  const trueAnomaly = M + C;
  const trueLongitude = L0 + C;
  const omega = 125.04 - 1934.136 * T;
  const apparentLongitude =
    trueLongitude - 0.00569 - 0.00478 * Math.sin(omega * DEG);
  const radius =
    (1.000001018 * (1 - e * e)) / (1 + e * Math.cos(trueAnomaly * DEG));
  return { longitude: apparentLongitude, radius };
}

// Periodic terms from Meeus ch. 47, each [D, M, M', F, coefficient].
// Coefficients are in units of 1e-6 degree (longitude/latitude) or 1e-3 km
// (distance). The leading terms dominate; the tails are included for fidelity.
const MOON_LONGITUDE_TERMS = [
  [0, 0, 1, 0, 6288774], [2, 0, -1, 0, 1274027], [2, 0, 0, 0, 658314],
  [0, 0, 2, 0, 213618], [0, 1, 0, 0, -185116], [0, 0, 0, 2, -114332],
  [2, 0, -2, 0, 58793], [2, -1, -1, 0, 57066], [2, 0, 1, 0, 53322],
  [2, -1, 0, 0, 45758], [0, 1, -1, 0, -40923], [1, 0, 0, 0, -34720],
  [0, 1, 1, 0, -30383], [2, 0, 0, -2, 15327], [0, 0, 1, 2, -12528],
  [0, 0, 1, -2, 10980], [4, 0, -1, 0, 10675], [0, 0, 3, 0, 10034],
  [4, 0, -2, 0, 8548], [2, 1, -1, 0, -7888], [2, 1, 0, 0, -6766],
  [1, 0, -1, 0, -5163], [1, 1, 0, 0, 4987], [2, -1, 1, 0, 4036],
  [2, 0, 2, 0, 3994], [4, 0, 0, 0, 3861], [2, 0, -3, 0, 3665],
  [0, 1, -2, 0, -2689], [2, 0, -1, 2, -2602], [2, -1, -2, 0, 2390],
  [1, 0, 1, 0, -2348], [2, -2, 0, 0, 2236], [0, 1, 2, 0, -2120],
  [0, 2, 0, 0, -2069], [2, -2, -1, 0, 2048], [2, 0, 1, -2, -1773],
  [2, 0, 0, 2, -1595], [4, -1, -1, 0, 1215], [0, 0, 2, 2, -1110],
  [3, 0, -1, 0, -892], [2, 1, 1, 0, -810], [4, -1, -2, 0, 759],
  [0, 2, -1, 0, -713], [2, 2, -1, 0, -700], [2, 1, -2, 0, 691],
  [2, -1, 0, -2, 596], [4, 0, 1, 0, 549], [0, 0, 4, 0, 537],
  [4, -1, 0, 0, 520], [1, 0, -2, 0, -487], [2, 1, 0, -2, -399],
  [0, 0, 2, -2, -381], [1, 1, 1, 0, 351], [3, 0, -2, 0, -340],
  [4, 0, -3, 0, 330], [2, -1, 2, 0, 327], [0, 2, 1, 0, -323],
  [1, 1, -1, 0, 299], [2, 0, 3, 0, 294],
];

const MOON_LATITUDE_TERMS = [
  [0, 0, 0, 1, 5128122], [0, 0, 1, 1, 280602], [0, 0, 1, -1, 277693],
  [2, 0, 0, -1, 173237], [2, 0, -1, 1, 55413], [2, 0, -1, -1, 46271],
  [2, 0, 0, 1, 32573], [0, 0, 2, 1, 17198], [2, 0, 1, -1, 9266],
  [0, 0, 2, -1, 8822], [2, -1, 0, -1, 8216], [2, 0, -2, -1, 4324],
  [2, 0, 1, 1, 4200], [2, 1, 0, -1, -3359], [2, -1, -1, 1, 2463],
  [2, -1, 0, 1, 2211], [2, -1, -1, -1, 2065], [0, 1, -1, -1, -1870],
  [4, 0, -1, -1, 1828], [0, 1, 0, 1, -1794], [0, 0, 0, 3, -1749],
  [0, 1, -1, 1, -1565], [1, 0, 0, 1, -1491], [0, 1, 1, 1, -1475],
  [0, 1, 1, -1, -1410], [0, 1, 0, -1, -1344], [1, 0, 0, -1, -1335],
  [0, 0, 3, 1, 1107], [4, 0, 0, -1, 1021], [4, 0, -1, 1, 833],
  [0, 0, 1, -3, 777], [4, 0, -2, 1, 671], [2, 0, 0, -3, 607],
  [2, 0, 2, -1, 596], [2, -1, 1, -1, 491], [2, 0, -2, 1, -451],
  [0, 0, 3, -1, 439], [2, 0, 2, 1, 422], [2, 0, -3, -1, 421],
  [2, 1, -1, 1, -366], [2, 1, 0, 1, -351], [4, 0, 0, 1, 331],
  [2, -1, 1, 1, 315], [2, -2, 0, -1, 302], [0, 0, 1, 3, -283],
  [2, 1, 1, -1, -229], [1, 1, 0, -1, 223], [1, 1, 0, 1, 223],
  [0, 1, -2, -1, -220], [2, 1, -1, -1, -220], [1, 0, 1, 1, -185],
  [2, -1, -2, -1, 181], [0, 1, 2, 1, -177], [4, 0, -2, -1, 176],
  [4, -1, -1, -1, 166], [1, 0, 1, -1, -164], [4, 0, 1, -1, 132],
  [1, 0, -1, -1, -119], [4, -1, 0, -1, 115], [2, -2, 0, 1, 107],
];

const MOON_DISTANCE_TERMS = [
  [0, 0, 1, 0, -20905355], [2, 0, -1, 0, -3699111], [2, 0, 0, 0, -2955968],
  [0, 0, 2, 0, -569925], [0, 1, 0, 0, 48888], [0, 0, 0, 2, -3149],
  [2, 0, -2, 0, 246158], [2, -1, -1, 0, -152138], [2, 0, 1, 0, -170733],
  [2, -1, 0, 0, -204586], [0, 1, -1, 0, -129620], [1, 0, 0, 0, 108743],
  [0, 1, 1, 0, 104755], [2, 0, 0, -2, 10321], [0, 0, 1, 2, 79661],
  [0, 0, 1, -2, 0], [4, 0, -1, 0, -34782], [0, 0, 3, 0, -23210],
  [4, 0, -2, 0, -21636], [2, 1, -1, 0, 24208], [2, 1, 0, 0, 30824],
  [1, 0, -1, 0, -8379], [2, -1, 1, 0, -16675], [2, 0, 2, 0, -12831],
  [4, 0, 0, 0, -10445], [2, 0, -3, 0, -11650], [0, 1, -2, 0, 14403],
  [2, 0, -1, 2, -7003],
];

/** Geocentric ecliptic longitude/latitude (degrees) and distance (km). */
function moonEcliptic(jd) {
  const T = (jd - J2000) / 36525;
  const Lp =
    218.3164477 + 481267.88123421 * T - 0.0015786 * T * T +
    (T * T * T) / 538841 - (T * T * T * T) / 65194000;
  const D =
    297.8501921 + 445267.1114034 * T - 0.0018819 * T * T +
    (T * T * T) / 545868 - (T * T * T * T) / 113065000;
  const M =
    357.5291092 + 35999.0502909 * T - 0.0001536 * T * T +
    (T * T * T) / 24490000;
  const Mp =
    134.9633964 + 477198.8675055 * T + 0.0087414 * T * T +
    (T * T * T) / 69699 - (T * T * T * T) / 14712000;
  const F =
    93.272095 + 483202.0175233 * T - 0.0036539 * T * T -
    (T * T * T) / 3526000 + (T * T * T * T) / 863310000;
  const A1 = 119.75 + 131.849 * T;
  const A2 = 53.09 + 479264.29 * T;
  const A3 = 313.45 + 481266.484 * T;
  const E = 1 - 0.002516 * T - 0.0000074 * T * T;

  const D_ = D * DEG;
  const M_ = M * DEG;
  const Mp_ = Mp * DEG;
  const F_ = F * DEG;

  let sumL = 0;
  let sumR = 0;
  for (const [d, m, mp, f, coefficient] of MOON_LONGITUDE_TERMS) {
    const eccentricity = Math.abs(m) === 1 ? E : Math.abs(m) === 2 ? E * E : 1;
    sumL += coefficient * eccentricity * Math.sin(d * D_ + m * M_ + mp * Mp_ + f * F_);
  }
  for (const [d, m, mp, f, coefficient] of MOON_DISTANCE_TERMS) {
    const eccentricity = Math.abs(m) === 1 ? E : Math.abs(m) === 2 ? E * E : 1;
    sumR += coefficient * eccentricity * Math.cos(d * D_ + m * M_ + mp * Mp_ + f * F_);
  }

  let sumB = 0;
  for (const [d, m, mp, f, coefficient] of MOON_LATITUDE_TERMS) {
    const eccentricity = Math.abs(m) === 1 ? E : Math.abs(m) === 2 ? E * E : 1;
    sumB += coefficient * eccentricity * Math.sin(d * D_ + m * M_ + mp * Mp_ + f * F_);
  }

  // Additive corrections to the longitude and latitude sums.
  sumL +=
    3958 * Math.sin(A1 * DEG) +
    1962 * Math.sin((Lp - F) * DEG) +
    318 * Math.sin(A2 * DEG);
  sumB +=
    -2235 * Math.sin(Lp * DEG) +
    382 * Math.sin(A3 * DEG) +
    175 * Math.sin((A1 - F) * DEG) +
    175 * Math.sin((A1 + F) * DEG) +
    127 * Math.sin((Lp - Mp) * DEG) -
    115 * Math.sin((Lp + Mp) * DEG);

  return {
    longitude: mod(Lp + sumL / 1e6, 360),
    latitude: sumB / 1e6,
    distance: 385000.56 + sumR / 1e3,
  };
}

// --- Coordinate helpers ----------------------------------------------------

function obliquity(jd) {
  const T = (jd - J2000) / 36525;
  return (
    23 +
    (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60
  );
}

function rightAscension(longitude, latitude, epsilon) {
  return Math.atan2(
    Math.sin(longitude) * Math.cos(epsilon) - Math.tan(latitude) * Math.sin(epsilon),
    Math.cos(longitude),
  );
}

function declination(longitude, latitude, epsilon) {
  return Math.asin(
    Math.sin(latitude) * Math.cos(epsilon) +
      Math.cos(latitude) * Math.sin(epsilon) * Math.sin(longitude),
  );
}

function siderealTime(jd, longitudeRadians) {
  const T = (jd - J2000) / 36525;
  const theta =
    280.46061837 +
    360.98564736629 * (jd - J2000) +
    0.000387933 * T * T -
    (T * T * T) / 38710000;
  return mod(theta, 360) * DEG - longitudeRadians;
}

function altitude(hourAngle, latitude, declinationAngle) {
  return Math.asin(
    Math.sin(latitude) * Math.sin(declinationAngle) +
      Math.cos(latitude) * Math.cos(declinationAngle) * Math.cos(hourAngle),
  );
}

// --- Local-time helpers ----------------------------------------------------

const offsetFormatters = new Map();

function offsetFormatter(timeZone) {
  if (!offsetFormatters.has(timeZone)) {
    offsetFormatters.set(
      timeZone,
      new Intl.DateTimeFormat("en-US", {
        timeZone,
        hourCycle: "h23",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      }),
    );
  }
  return offsetFormatters.get(timeZone);
}

// Milliseconds to add to UTC to get local time in `timeZone` at `date`.
function timeZoneOffsetMs(date, timeZone) {
  const parts = {};
  for (const { type, value } of offsetFormatter(timeZone).formatToParts(date)) {
    parts[type] = value;
  }
  const asUtc = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    Number(parts.hour) % 24,
    Number(parts.minute),
    Number(parts.second),
  );
  return asUtc - date.getTime();
}

function pad(value, width = 2) {
  return String(value).padStart(width, "0");
}

/** Format an instant as an ISO 8601 string with the zone's offset. */
export function formatInZone(date, timeZone, { seconds = true } = {}) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return undefined;
  const rounded = new Date(
    seconds ? date.getTime() : Math.round(date.getTime() / MINUTE_MS) * MINUTE_MS,
  );
  const offset = timeZoneOffsetMs(rounded, timeZone);
  const local = new Date(rounded.getTime() + offset);
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  const stamp =
    `${pad(local.getUTCFullYear(), 4)}-${pad(local.getUTCMonth() + 1)}-` +
    `${pad(local.getUTCDate())}T${pad(local.getUTCHours())}:` +
    `${pad(local.getUTCMinutes())}` +
    (seconds ? `:${pad(local.getUTCSeconds())}` : ":00");
  return `${stamp}${sign}${pad(Math.floor(abs / 3_600_000))}:${pad(
    Math.floor((abs % 3_600_000) / MINUTE_MS),
  )}`;
}

/** The `YYYY-MM-DD` calendar date of an instant in the given zone. */
export function dateKeyInZone(date, timeZone) {
  const offset = timeZoneOffsetMs(date, timeZone);
  return new Date(date.getTime() + offset).toISOString().slice(0, 10);
}

/**
 * The UTC instant at which the local calendar day `dateString` begins.
 * Handles DST transitions by evaluating the offset at the boundary.
 */
export function startOfLocalDay(dateString, timeZone) {
  const [year, month, day] = dateString.split("-").map(Number);
  const midnightUtc = Date.UTC(year, month - 1, day, 0, 0, 0);
  let start = midnightUtc - timeZoneOffsetMs(new Date(midnightUtc), timeZone);
  start = midnightUtc - timeZoneOffsetMs(new Date(start), timeZone);
  return start;
}

function nextDate(dateString) {
  const [year, month, day] = dateString.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + 1)).toISOString().slice(0, 10);
}

// --- Horizon search --------------------------------------------------------

function bisectCrossing(a, b, fn) {
  let low = a;
  let high = b;
  let lowValue = fn(low);
  for (let i = 0; i < 45; i += 1) {
    const mid = (low + high) / 2;
    const midValue = fn(mid);
    if ((lowValue < 0 && midValue < 0) || (lowValue > 0 && midValue > 0)) {
      low = mid;
      lowValue = midValue;
    } else {
      high = mid;
    }
  }
  return (low + high) / 2;
}

/**
 * Find the first upward and downward zero crossings of `fn` in a time range.
 * @returns {{rise: Date|null, set: Date|null}}
 */
function findHorizonCrossings(fn, startMs, endMs, stepMs = 5 * MINUTE_MS) {
  let previousT = startMs;
  let previous = fn(previousT);
  let rise = null;
  let set = null;

  for (let t = startMs + stepMs; t <= endMs; t += stepMs) {
    const value = fn(t);
    if ((previous <= 0 && value > 0) || (previous >= 0 && value < 0)) {
      const crossing = new Date(bisectCrossing(previousT, t, fn));
      if (previous < 0 && value > 0 && rise === null) rise = crossing;
      if (previous > 0 && value < 0 && set === null) set = crossing;
    }
    previousT = t;
    previous = value;
  }

  return { rise, set };
}

// --- Altitude as a function of time ----------------------------------------

function sunAltitudeFn(latitude, longitude) {
  const lat = latitude * DEG;
  const lon = longitude * DEG;
  return (ms) => {
    const jd = toJulian(new Date(ms));
    const sun = sunEcliptic(jd);
    const epsilon = obliquity(jd) * DEG;
    const ra = rightAscension(sun.longitude * DEG, 0, epsilon);
    const dec = declination(sun.longitude * DEG, 0, epsilon);
    const hourAngle = siderealTime(jd, -lon) - ra;
    return altitude(hourAngle, lat, dec);
  };
}

function moonAltitudeFn(latitude, longitude) {
  const lat = latitude * DEG;
  const lon = longitude * DEG;
  return (ms) => {
    const jd = toJulian(new Date(ms));
    const moon = moonEcliptic(jd);
    const epsilon = obliquity(jd) * DEG;
    const ra = rightAscension(moon.longitude * DEG, moon.latitude * DEG, epsilon);
    const dec = declination(moon.longitude * DEG, moon.latitude * DEG, epsilon);
    const hourAngle = siderealTime(jd, -lon) - ra;
    return altitude(hourAngle, lat, dec);
  };
}

// --- Public solar and lunar calculations -----------------------------------

/** Sun altitude in radians above the true horizon at a given instant. */
export function sunAltitude(date, latitude, longitude) {
  return sunAltitudeFn(latitude, longitude)(date.getTime());
}

/** Moon altitude in radians above the true horizon at a given instant. */
export function moonAltitude(date, latitude, longitude) {
  return moonAltitudeFn(latitude, longitude)(date.getTime());
}

/**
 * Solar events for a local calendar day.
 * @returns {{sunrise?: Date, sunset?: Date, solarNoon: Date, daylightMinutes: number}}
 */
export function getSunTimes(dateString, latitude, longitude, timeZone) {
  const start = startOfLocalDay(dateString, timeZone);
  const end = startOfLocalDay(nextDate(dateString), timeZone);
  const altitudeFn = sunAltitudeFn(latitude, longitude);
  const belowHorizon = (ms) => altitudeFn(ms) - SUN_HORIZON_ALTITUDE * DEG;

  const { rise, set } = findHorizonCrossings(belowHorizon, start, end);

  // Solar noon is the maximum altitude, found by ternary search on the
  // unimodal altitude curve across the day.
  let low = start;
  let high = end;
  for (let i = 0; i < 60; i += 1) {
    const a = low + (high - low) / 3;
    const b = high - (high - low) / 3;
    if (altitudeFn(a) < altitudeFn(b)) low = a;
    else high = b;
  }
  const solarNoon = new Date((low + high) / 2);

  const daylightMinutes =
    rise !== null && set !== null
      ? Math.round((set - rise) / MINUTE_MS)
      : altitudeFn((start + end) / 2) > SUN_HORIZON_ALTITUDE * DEG
        ? 1440
        : 0;

  return {
    sunrise: rise ?? undefined,
    sunset: set ?? undefined,
    solarNoon,
    daylightMinutes,
  };
}

// --- Seasonal markers ------------------------------------------------------

/** Apparent geocentric ecliptic longitude of the sun, in degrees [0, 360). */
export function sunLongitude(date) {
  return sunEcliptic(toJulian(date)).longitude;
}

/** Earth–sun distance in astronomical units at an instant. */
export function sunDistanceAu(date) {
  return sunEcliptic(toJulian(date)).radius;
}

// Ecliptic longitudes defining the quarter days and the cross-quarter days.
const QUARTER_DAYS = [
  { key: "march-equinox", name: "March Equinox", longitude: 0, season: "spring" },
  { key: "june-solstice", name: "June Solstice", longitude: 90, season: "summer" },
  { key: "september-equinox", name: "September Equinox", longitude: 180, season: "autumn" },
  { key: "december-solstice", name: "December Solstice", longitude: 270, season: "winter" },
];

// Meeus ch. 27: mean JDE coefficients for the four cardinal solar terms,
// as [constant, Y, Y^2, Y^3, Y^4] with Y = (year - 2000) / 1000.
const SOLAR_TERM_MEAN_JDE = [
  [2451623.80984, 365242.37404, 0.05169, -0.00411, -0.00057],
  [2451716.56767, 365241.62603, 0.00325, 0.00888, -0.0003],
  [2451810.21715, 365242.01767, -0.11575, 0.00337, 0.00078],
  [2451900.05952, 365242.74049, -0.06223, -0.00823, 0.00032],
];

// Periodic corrections to the mean terms, each [amplitude, phase, frequency].
const SOLAR_TERM_PERIODIC_TERMS = [
  [485, 324.96, 1934.136], [203, 337.23, 32964.467], [199, 342.08, 20.186],
  [182, 27.85, 445267.112], [156, 73.14, 45036.886], [136, 171.52, 22518.443],
  [77, 222.54, 65928.934], [74, 296.72, 3034.906], [70, 243.58, 9037.513],
  [58, 119.81, 33718.147], [52, 297.17, 150.678], [50, 21.02, 2281.226],
  [45, 247.54, 29929.562], [44, 325.15, 31555.956], [29, 60.93, 4443.417],
  [18, 155.12, 67555.328], [17, 288.79, 4562.452], [16, 198.04, 62894.029],
  [14, 199.76, 31436.921], [12, 95.39, 14577.848], [12, 287.11, 31931.756],
  [12, 320.81, 34777.259], [9, 227.73, 1222.114], [8, 15.45, 16859.074],
];

// Difference between Terrestrial Time and UTC, in seconds. Espenak–Meeus
// polynomial for 2005-2050; only seconds, so it never moves a displayed time
// by more than a minute either way.
function deltaTSeconds(year) {
  const t = year - 2000;
  return 62.92 + 0.32217 * t + 0.005589 * t * t;
}

function solarTermInstant(year, index) {
  const [c0, c1, c2, c3, c4] = SOLAR_TERM_MEAN_JDE[index];
  const Y = (year - 2000) / 1000;
  const jde0 = c0 + c1 * Y + c2 * Y ** 2 + c3 * Y ** 3 + c4 * Y ** 4;
  const T = (jde0 - J2000) / 36525;
  const W = (35999.373 * T - 2.47) * DEG;
  const deltaLambda = 1 + 0.0334 * Math.cos(W) + 0.0007 * Math.cos(2 * W);
  let sum = 0;
  for (const [amplitude, phase, frequency] of SOLAR_TERM_PERIODIC_TERMS) {
    sum += amplitude * Math.cos((phase + frequency * T) * DEG);
  }
  const jde = jde0 + (0.00001 * sum) / deltaLambda;
  const jdUtc = jde - deltaTSeconds(year) / 86_400;
  return new Date((jdUtc - UNIX_EPOCH_JD) * DAY_MS);
}

const CROSS_QUARTER_DAYS = [
  { key: "imbolc", name: "Imbolc", longitude: 315 },
  { key: "beltane", name: "Beltane", longitude: 45 },
  { key: "lughnasadh", name: "Lughnasadh", longitude: 135 },
  { key: "samhain", name: "Samhain", longitude: 225 },
];

function longitudeDelta(date, target) {
  return mod(sunLongitude(date) - target + 180, 360) - 180;
}

// The next instant after `startMs` at which the sun reaches `target` degrees
// of ecliptic longitude. The sun gains about a degree a day, so a daily scan
// with a bisection refine is exact to the second.
function nextSunLongitude(target, startMs, maxDays = 400) {
  let previousT = startMs;
  let previous = longitudeDelta(new Date(previousT), target);
  if (previous === 0) return new Date(previousT);
  for (let day = 1; day <= maxDays; day += 1) {
    const t = startMs + day * DAY_MS;
    const value = longitudeDelta(new Date(t), target);
    if (previous < 0 && value >= 0) {
      let low = previousT;
      let high = t;
      for (let i = 0; i < 50; i += 1) {
        const mid = (low + high) / 2;
        if (longitudeDelta(new Date(mid), target) < 0) low = mid;
        else high = mid;
      }
      return new Date((low + high) / 2);
    }
    previousT = t;
    previous = value;
  }
  return undefined;
}

function formatEventOption(date, timeZone) {
  if (!(date instanceof Date)) return {};
  return timeZone
    ? { instant: date, utc: date.toISOString(), local: formatInZone(date, timeZone) }
    : { instant: date, utc: date.toISOString() };
}

/**
 * The four cardinal solar events of a year.
 * @param {number} year
 * @param {string} [timeZone]  Adds a `local` ISO string when supplied.
 */
export function getEquinoxesAndSolstices(year, timeZone) {
  return QUARTER_DAYS.map((event, index) => ({
    ...event,
    ...formatEventOption(solarTermInstant(year, index), timeZone),
  }));
}

/**
 * The four traditional cross-quarter days (the midpoints between an equinox
 * and the following solstice), computed from the sun's actual longitude.
 */
export function getCrossQuarterDays(year, timeZone) {
  let cursor = Date.UTC(year, 0, 1);
  return CROSS_QUARTER_DAYS.map((event) => {
    const instant = nextSunLongitude(event.longitude, cursor);
    cursor = instant.getTime() + 1;
    return { ...event, ...formatEventOption(instant, timeZone) };
  });
}

/**
 * Earth's closest and farthest point from the sun for a year.
 * @returns {{perihelion: object, aphelion: object}}
 */
export function getPerihelionAphelion(year, timeZone) {
  const findExtremum = (startMs, endMs, mode) => {
    const step = DAY_MS / 4;
    let best = startMs;
    let bestValue = sunDistanceAu(new Date(best));
    for (let t = startMs; t <= endMs; t += step) {
      const value = sunDistanceAu(new Date(t));
      if (mode === "min" ? value < bestValue : value > bestValue) {
        best = t;
        bestValue = value;
      }
    }
    let low = Math.max(startMs, best - step);
    let high = Math.min(endMs, best + step);
    for (let i = 0; i < 60; i += 1) {
      const a = low + (high - low) / 3;
      const b = high - (high - low) / 3;
      const fa = sunDistanceAu(new Date(a));
      const fb = sunDistanceAu(new Date(b));
      const aBetter = mode === "min" ? fa < fb : fa > fb;
      if (aBetter) high = b;
      else low = a;
    }
    return new Date((low + high) / 2);
  };

  const perihelion = findExtremum(Date.UTC(year, 0, 1), Date.UTC(year, 1, 15), "min");
  const aphelion = findExtremum(Date.UTC(year, 5, 1), Date.UTC(year, 7, 15), "max");
  return {
    perihelion: { key: "perihelion", name: "Perihelion", ...formatEventOption(perihelion, timeZone) },
    aphelion: { key: "aphelion", name: "Aphelion", ...formatEventOption(aphelion, timeZone) },
  };
}

/** Northern-hemisphere astronomical season for an instant. */
export function getAstronomicalSeason(date) {
  const longitude = sunLongitude(date);
  if (longitude < 90) return "spring";
  if (longitude < 180) return "summer";
  if (longitude < 270) return "autumn";
  return "winter";
}

/** Gregorian leap-year rule. */
export function isLeapYear(year) {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

/** Number of calendar days in a Gregorian year. */
export function daysInYear(year) {
  return isLeapYear(year) ? 366 : 365;
}

/** Day of the year, 1-based (Feb 29 makes 60 in a leap year). */
export function dayOfYear(date) {
  const year = date.getUTCFullYear();
  const start = Date.UTC(year, 0, 1);
  return Math.floor((date.getTime() - start) / DAY_MS) + 1;
}

/**
 * The next February 29 strictly after `date`.
 * @returns {{year: number, date: Date}}
 */
export function nextLeapDay(date) {
  let year = date.getUTCFullYear();
  while (!isLeapYear(year) || Date.UTC(year, 1, 29) <= date.getTime()) year += 1;
  return { year, date: new Date(Date.UTC(year, 1, 29)) };
}

// --- Solar detail ----------------------------------------------------------

/** Sun's apparent declination in radians at an instant. */
function sunDeclination(date) {
  const jd = toJulian(date);
  return declination(
    sunEcliptic(jd).longitude * DEG,
    0,
    obliquity(jd) * DEG,
  );
}

/**
 * Compass bearing of the sun in degrees clockwise from north (0 = N, 90 = E).
 * Useful around sunrise and sunset for watching the sun's seasonal swing.
 */
export function sunAzimuth(date, latitude, longitude) {
  const jd = toJulian(date);
  const sun = sunEcliptic(jd);
  const epsilon = obliquity(jd) * DEG;
  const ra = rightAscension(sun.longitude * DEG, 0, epsilon);
  const dec = declination(sun.longitude * DEG, 0, epsilon);
  const hourAngle = siderealTime(jd, -longitude * DEG) - ra;
  const phi = latitude * DEG;
  const fromSouth = Math.atan2(
    Math.sin(hourAngle),
    Math.cos(hourAngle) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi),
  );
  return mod(fromSouth * RAD + 180, 360);
}

/**
 * Equation of time in minutes: apparent solar time minus mean solar time.
 * Positive means a sundial runs ahead of the clock.
 */
export function equationOfTime(date) {
  const jd = toJulian(date);
  const T = (jd - J2000) / 36525;
  const L0 = mod(280.46646 + 36000.76983 * T + 0.0003032 * T * T, 360);
  const M = 357.52911 + 35999.05029 * T - 0.0001537 * T * T;
  const e = 0.016708634 - 0.000042037 * T - 0.0000001267 * T * T;
  const epsilon = obliquity(jd) * DEG;
  const y = Math.tan(epsilon / 2) ** 2;
  const minutes =
    4 *
    RAD *
    (y * Math.sin(2 * L0 * DEG) -
      2 * e * Math.sin(M * DEG) +
      4 * e * y * Math.sin(M * DEG) * Math.cos(2 * L0 * DEG) -
      0.5 * y * y * Math.sin(4 * L0 * DEG) -
      1.25 * e * e * Math.sin(2 * M * DEG));
  return minutes;
}

/** The date's extreme values of the equation of time (~mid-Feb and early Nov). */
export function getEquationOfTimeExtremes(year, timeZone) {
  const find = (mode) => {
    let bestT = Date.UTC(year, 0, 1);
    let best = equationOfTime(new Date(bestT));
    for (let day = 0; day < 366; day += 1) {
      const t = Date.UTC(year, 0, 1 + day);
      if (new Date(t).getUTCFullYear() !== year) break;
      const value = equationOfTime(new Date(t));
      if (mode === "min" ? value < best : value > best) {
        best = value;
        bestT = t;
      }
    }
    let low = Math.max(Date.UTC(year, 0, 1), bestT - DAY_MS);
    let high = Math.min(Date.UTC(year + 1, 0, 1), bestT + DAY_MS);
    for (let i = 0; i < 60; i += 1) {
      const a = low + (high - low) / 3;
      const b = high - (high - low) / 3;
      const fa = equationOfTime(new Date(a));
      const fb = equationOfTime(new Date(b));
      const aBetter = mode === "min" ? fa < fb : fa > fb;
      if (aBetter) high = b;
      else low = a;
    }
    const instant = new Date((low + high) / 2);
    return { minutes: equationOfTime(instant), ...formatEventOption(instant, timeZone) };
  };
  return { maximum: find("max"), minimum: find("min") };
}

/**
 * Dawn/dusk for each twilight band, plus the golden and blue hour windows.
 * Thresholds follow the usual definitions: civil -6°, nautical -12°,
 * astronomical -18°, golden hour -4° to +6°, blue hour -6° to -4°.
 * @returns Object of Date values (undefined in polar day/night).
 */
export function getSunPhases(dateString, latitude, longitude, timeZone) {
  const start = startOfLocalDay(dateString, timeZone);
  const end = startOfLocalDay(nextDate(dateString), timeZone);
  const altitudeFn = sunAltitudeFn(latitude, longitude);
  const crossingsAt = (degrees) =>
    findHorizonCrossings(
      (ms) => altitudeFn(ms) - degrees * DEG,
      start,
      end,
    );

  const astronomical = crossingsAt(-18);
  const nautical = crossingsAt(-12);
  const civil = crossingsAt(-6);
  const blue = crossingsAt(-4);
  const golden = crossingsAt(6);

  return {
    astronomicalDawn: astronomical.rise,
    astronomicalDusk: astronomical.set,
    nauticalDawn: nautical.rise,
    nauticalDusk: nautical.set,
    civilDawn: civil.rise,
    civilDusk: civil.set,
    blueHourMorning: { start: civil.rise, end: blue.rise },
    goldenHourMorning: { start: blue.rise, end: golden.rise },
    goldenHourEvening: { start: golden.set, end: blue.set },
    blueHourEvening: { start: blue.set, end: civil.set },
  };
}

/**
 * The two equiluxes of a year: the days when sunrise-to-sunset is exactly
 * 12 hours. They sit a few days on the winter side of each equinox because of
 * atmospheric refraction and the sun's apparent diameter.
 */
export function getEquilux(year, latitude, timeZone) {
  const sinDeclination =
    Math.sin(SUN_HORIZON_ALTITUDE * DEG) / Math.sin(latitude * DEG);
  if (Math.abs(sinDeclination) > 1) return { spring: undefined, autumn: undefined };

  const sinObliquity = Math.sin(
    obliquity(toJulian(new Date(Date.UTC(year, 2, 20)))) * DEG,
  );
  const longitude = Math.asin(sinDeclination / sinObliquity) * RAD;
  const spring = nextSunLongitude(mod(longitude, 360), Date.UTC(year, 0, 1));
  const autumn = nextSunLongitude(mod(180 - longitude, 360), Date.UTC(year, 6, 1));
  return {
    spring: { ...formatEventOption(spring, timeZone) },
    autumn: { ...formatEventOption(autumn, timeZone) },
  };
}

/**
 * Day-length context for a date: yesterday's change, distance from the most
 * recent solstice, and the noontime sun angle.
 */
export function getDaylightInfo(dateString, latitude, longitude, timeZone) {
  const today = getSunTimes(dateString, latitude, longitude, timeZone);
  const previousKey = new Date(
    Date.parse(`${dateString}T12:00:00Z`) - DAY_MS,
  ).toISOString().slice(0, 10);
  const yesterday = getSunTimes(previousKey, latitude, longitude, timeZone);

  const year = Number(dateString.slice(0, 4));
  const solstices = [
    ...getEquinoxesAndSolstices(year - 1, timeZone),
    ...getEquinoxesAndSolstices(year, timeZone),
  ]
    .filter((event) => event.season === "summer" || event.season === "winter")
    .sort((a, b) => a.instant - b.instant);

  const atNoon = Date.parse(`${dateString}T12:00:00Z`);
  const past = solstices.filter((event) => event.instant.getTime() <= atNoon);
  const describe = (event) => {
    if (!event) return undefined;
    const key = dateKeyInZone(event.instant, timeZone);
    const times = getSunTimes(key, latitude, longitude, timeZone);
    return {
      name: event.name,
      at: event.instant,
      daylightDeltaMinutes:
        today.daylightMinutes - times.daylightMinutes,
    };
  };
  const mostRecent = past[past.length - 1];
  const mostRecentWinter = [...past]
    .reverse()
    .find((event) => event.season === "winter");

  return {
    minutes: today.daylightMinutes,
    changeFromYesterdayMinutes:
      today.daylightMinutes - yesterday.daylightMinutes,
    solarNoonElevationDegrees: sunAltitude(today.solarNoon, latitude, longitude) * RAD,
    sinceSolstice: describe(mostRecent),
    sinceWinterSolstice: describe(mostRecentWinter),
  };
}

/** Reference value for the sun's angular diameter (used for azimuth sanity). */
export const SOLAR_HORIZON_ALTITUDE_DEGREES = SUN_HORIZON_ALTITUDE;

// --- Moon detail -----------------------------------------------------------

/** Earth–moon distance in kilometres at an instant. */
export function moonDistanceKm(date) {
  return moonEcliptic(toJulian(date)).distance;
}

function signedPhaseTargetDelta(ms, target) {
  const { phase } = getMoonIllumination(new Date(ms));
  return mod(phase - target + 0.5, 1) - 0.5;
}

/**
 * The next time the moon reaches a phase fraction (0 new, 0.25 first quarter,
 * 0.5 full, 0.75 last quarter) at or after `date`.
 */
export function nextMoonPhase(date, target) {
  let previousT = date.getTime();
  let previous = signedPhaseTargetDelta(previousT, target);
  if (previous === 0) return new Date(previousT);

  for (let day = 1; day <= 40; day += 1) {
    const t = previousT + DAY_MS;
    const value = signedPhaseTargetDelta(t, target);
    if (previous < 0 && value >= 0) {
      let low = previousT;
      let high = t;
      for (let i = 0; i < 50; i += 1) {
        const mid = (low + high) / 2;
        if (signedPhaseTargetDelta(mid, target) < 0) low = mid;
        else high = mid;
      }
      return new Date((low + high) / 2);
    }
    previousT = t;
    previous = value;
  }
  return new Date(date.getTime());
}

/** The next occurrence of each principal moon phase. */
export function getNextMoonPhases(date) {
  return {
    new: nextMoonPhase(date, 0),
    firstQuarter: nextMoonPhase(date, 0.25),
    full: nextMoonPhase(date, 0.5),
    lastQuarter: nextMoonPhase(date, 0.75),
  };
}

/** Months containing two full moons — a monthly blue moon. */
export function getBlueMoons(year) {
  const blues = [];
  for (let month = 0; month < 12; month += 1) {
    const monthStart = Date.UTC(year, month, 1);
    const monthEnd = Date.UTC(year, month + 1, 1);
    const first = nextMoonPhase(new Date(monthStart), 0.5);
    if (first.getTime() >= monthEnd) continue;
    const second = nextMoonPhase(new Date(first.getTime() + DAY_MS), 0.5);
    if (second.getTime() < monthEnd) {
      blues.push({ year, month: month + 1, first, second });
    }
  }
  return blues;
}

/** Close full moons that make the disc look largest (perigee-syzygy). */
export function getSupermoonFullMoons(year, thresholdKm = 360_000) {
  const moons = [];
  let cursor = new Date(Date.UTC(year, 0, 1));
  while (cursor.getUTCFullYear() <= year) {
    const full = nextMoonPhase(cursor, 0.5);
    if (full.getUTCFullYear() > year) break;
    const distanceKm = moonDistanceKm(full);
    if (distanceKm < thresholdKm) moons.push({ at: full, distanceKm });
    cursor = new Date(full.getTime() + DAY_MS);
  }
  return moons;
}

/** Whether a given full-moon instant counts as a supermoon. */
export function isSupermoon(date, thresholdKm = 360_000) {
  return moonDistanceKm(date) < thresholdKm;
}

/**
 * Moon phase state at an instant.
 * @returns {{phase: number, illuminationPercent: number, waxing: boolean}}
 *   `phase` is 0 at new, 0.25 first quarter, 0.5 full, 0.75 last quarter.
 */
export function getMoonIllumination(date) {
  const jd = toJulian(date);
  const sun = sunEcliptic(jd);
  const moon = moonEcliptic(jd);

  const elongation = mod(moon.longitude - sun.longitude, 360);
  const separation = Math.acos(
    Math.cos(moon.latitude * DEG) * Math.cos(elongation * DEG),
  );
  const sunDistance = sun.radius * AU_KM;
  const phaseAngle = Math.atan2(
    sunDistance * Math.sin(separation),
    moon.distance - sunDistance * Math.cos(separation),
  );

  const fraction = (1 + Math.cos(phaseAngle)) / 2;
  return {
    phase: elongation / 360,
    illuminationPercent: fraction * 100,
    waxing: elongation < 180,
  };
}

/** The canonical phase slug for a phase fraction in [0, 1). */
export function moonPhaseName(phase) {
  const index = Math.floor(mod(phase, 1) * 8 + 0.5) % 8;
  return PHASES[index][0];
}

/** The next full moon at or after `date`. */
export function nextFullMoon(date) {
  let previousT = date.getTime();
  let previousF = signedPhaseDelta(previousT);
  if (previousF === 0) return new Date(previousT);

  // `signedPhaseDelta` rises smoothly through zero at the full moon and wraps
  // at the new moon, so a daily scan finds the next upward crossing reliably.
  for (let day = 1; day <= 40; day += 1) {
    const t = previousT + DAY_MS;
    const f = signedPhaseDelta(t);
    if (previousF < 0 && f >= 0) {
      let low = previousT;
      let high = t;
      for (let i = 0; i < 50; i += 1) {
        const mid = (low + high) / 2;
        if (signedPhaseDelta(mid) < 0) low = mid;
        else high = mid;
      }
      return new Date((low + high) / 2);
    }
    previousT = t;
    previousF = f;
  }
  return new Date(date.getTime());
}

function signedPhaseDelta(ms) {
  // Distance in phase-space from full, kept in [-0.5, 0.5] so it is smooth
  // in the neighbourhood of the full moon.
  const { phase } = getMoonIllumination(new Date(ms));
  return mod(phase - 0.5 + 0.5, 1) - 0.5;
}

function formatEvent(date, timeZone) {
  return date instanceof Date && !Number.isNaN(date.getTime())
    ? formatInZone(date, timeZone, { seconds: false })
    : undefined;
}

// --- Main entry point ------------------------------------------------------

/**
 * Compute a full almanac day for a location.
 *
 * @param {string} dateString  Local calendar date, `YYYY-MM-DD`.
 * @param {{latitude: number, longitude: number, timeZone: string}} location
 * @param {{moonReferenceHourUtc?: number}} [options]  Instant at which the
 *   phase and illumination are sampled, as an hour of UTC on `dateString`.
 *   Defaults to 06:00 UTC, matching the MET Norway sunrise reference.
 * @returns A day object with the solar and lunar facts the sky card renders.
 */
export function computeAlmanacDay(dateString, location, options = {}) {
  const { latitude, longitude, timeZone } = location;
  const { moonReferenceHourUtc = 6 } = options;

  const sun = getSunTimes(dateString, latitude, longitude, timeZone);
  const dayStart = startOfLocalDay(dateString, timeZone);
  const dayEnd = startOfLocalDay(nextDate(dateString), timeZone);
  const reference = new Date(
    Date.parse(`${dateString}T00:00:00Z`) + moonReferenceHourUtc * 3_600_000,
  );

  const illumination = getMoonIllumination(reference);
  const phase = moonPhaseName(illumination.phase);
  const label = PHASE_LABELS[phase];
  const illuminationPercent =
    Math.round(illumination.illuminationPercent * 10) / 10;

  const fullMoon = nextFullMoon(reference);
  const daysUntilFull = Math.max(
    0,
    Math.round((fullMoon.getTime() - reference.getTime()) / DAY_MS),
  );

  const moonCrossings = findHorizonCrossings(
    (ms) => moonAltitudeFn(latitude, longitude)(ms) - MOON_HORIZON_ALTITUDE * DEG,
    dayStart,
    dayEnd,
  );

  const summary = `${label}, ${illuminationPercent.toFixed(1)}% illuminated.`;

  const moon = {
    phase,
    illuminationPercent,
    daysUntilFull,
    summary,
    imageAlt: summary,
  };
  const moonrise = formatEvent(moonCrossings.rise, timeZone);
  const moonset = formatEvent(moonCrossings.set, timeZone);
  if (moonrise) moon.moonrise = moonrise;
  if (moonset) moon.moonset = moonset;

  const day = {
    date: dateString,
    moon,
  };
  const sunrise = formatEvent(sun.sunrise, timeZone);
  const sunset = formatEvent(sun.sunset, timeZone);
  if (sunrise) day.sunrise = sunrise;
  if (sunset) day.sunset = sunset;
  day.solarNoon = formatEvent(sun.solarNoon, timeZone);
  day.daylightMinutes = sun.daylightMinutes;

  return day;
}

export default computeAlmanacDay;
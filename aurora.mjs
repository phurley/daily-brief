// aurora.mjs
//
// Northern-lights outlook for a location. Two layers:
//
//   1. Pure math — convert a geographic location to geomagnetic latitude and
//      translate a Kp index into "unlikely / horizon glow / overhead".
//   2. An optional async fetch of NOAA SWPC's free, keyless, CORS-enabled
//      products (3-day Kp forecast plus solar-wind speed and Bz).
//
// Nothing here is required by the rest of the almanac; it is a separate,
// network-optional concern.

const DEG = Math.PI / 180;
const RAD = 180 / Math.PI;
const SWPC = "https://services.swpc.noaa.gov";

// The north geomagnetic (dipole) pole, IGRF epoch 2020.
const GEOMAGNETIC_POLE = { latitude: 80.65, longitude: -72.68 };

const round1 = (value) => Math.round(value * 10) / 10;

/** Dipole geomagnetic latitude in degrees (northern hemisphere). */
export function geomagneticLatitude(latitude, longitude) {
  const phi = latitude * DEG;
  const lambda = longitude * DEG;
  const polePhi = GEOMAGNETIC_POLE.latitude * DEG;
  const poleLambda = GEOMAGNETIC_POLE.longitude * DEG;
  const cosine =
    Math.sin(phi) * Math.sin(polePhi) +
    Math.cos(phi) * Math.cos(polePhi) * Math.cos(lambda - poleLambda);
  return 90 - Math.acos(Math.max(-1, Math.min(1, cosine))) * RAD;
}

/**
 * Rough Kp needed for aurora at a location. The auroral oval's equatorward
 * edge moves about two degrees of geomagnetic latitude per unit of Kp.
 */
export function kpThresholds(latitude, longitude) {
  const mlat = geomagneticLatitude(latitude, longitude);
  const overhead = Math.max(0, (66.5 - mlat) / 2);
  return {
    geomagneticLatitude: round1(mlat),
    overhead: round1(overhead),
    horizon: round1(Math.max(0, overhead - 2)),
  };
}

/** Turn a Kp value into a plain-language outlook for a location. */
export function assessAurora(kp, latitude, longitude) {
  const thresholds = kpThresholds(latitude, longitude);
  let level;
  let label;
  if (kp >= thresholds.overhead) {
    level = "overhead";
    label = "Aurora may be visible overhead";
  } else if (kp >= thresholds.horizon) {
    level = "horizon";
    label = "A faint glow low on the northern horizon is possible";
  } else {
    level = "none";
    label = "Aurora is unlikely at this latitude";
  }
  return {
    kp: round1(kp),
    level,
    label,
    geomagneticLatitude: thresholds.geomagneticLatitude,
    requiredKp: { overhead: thresholds.overhead, horizon: thresholds.horizon },
  };
}

async function getJson(url, fetchImpl) {
  const response = await fetchImpl(url, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`SWPC ${response.status} for ${url}`);
  return response.json();
}

function parseUtcTime(timeTag) {
  // SWPC time tags are UTC but usually carry no zone suffix.
  return new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(timeTag) ? timeTag : `${timeTag}Z`);
}

/**
 * Fetch the current Kp, the 3-day forecast, and solar-wind conditions, then
 * assess visibility for `location`.
 *
 * @param {{latitude: number, longitude: number}} location
 * @param {{fetchImpl?: Function, now?: Date}} [options]
 */
export async function fetchAuroraOutlook(location, options = {}) {
  const { fetchImpl = globalThis.fetch, now = new Date() } = options;
  if (typeof fetchImpl !== "function") {
    throw new Error("fetchAuroraOutlook needs a fetch implementation");
  }

  const [kpRows, speedRows, fieldRows] = await Promise.all([
    getJson(`${SWPC}/products/noaa-planetary-k-index-forecast.json`, fetchImpl),
    getJson(`${SWPC}/products/summary/solar-wind-speed.json`, fetchImpl),
    getJson(`${SWPC}/products/summary/solar-wind-mag-field.json`, fetchImpl),
  ]);

  const forecast = kpRows.map((row) => ({
    time: parseUtcTime(row.time_tag),
    kp: Number(row.kp),
    observed: row.observed === "observed",
    noaaScale: row.noaa_scale || null,
  }));

  const observed = forecast.filter((row) => row.observed);
  const current = observed[observed.length - 1] || forecast[0];
  const upcoming = forecast.filter(
    (row) => row.time.getTime() >= now.getTime() - 90 * 60 * 1000,
  );
  const next24 = upcoming.filter(
    (row) => row.time.getTime() <= now.getTime() + 24 * 3_600_000,
  );
  const next48 = upcoming.filter(
    (row) => row.time.getTime() <= now.getTime() + 48 * 3_600_000,
  );

  const maxBy = (rows) =>
    rows.reduce((best, row) => (best === null || row.kp > best.kp ? row : best), null);
  const bestWindow = maxBy(next24.length ? next24 : upcoming);
  const peak24 = bestWindow ? bestWindow.kp : current.kp;
  const peak48Row = maxBy(next48);
  const peak48 = peak48Row ? peak48Row.kp : peak24;
  const peakKp = Math.max(current.kp, peak24, peak48);

  const speed = speedRows[0] || {};
  const field = fieldRows[0] || {};

  return {
    source: {
      name: "NOAA Space Weather Prediction Center",
      url: "https://www.swpc.noaa.gov/",
    },
    fetchedAt: now,
    geomagneticLatitude: kpThresholds(location.latitude, location.longitude)
      .geomagneticLatitude,
    currentKp: current.kp,
    currentObservedAt: current.time,
    maxKpNext24h: peak24,
    maxKpNext48h: peak48,
    bestWindow: bestWindow
      ? { time: bestWindow.time, kp: bestWindow.kp }
      : undefined,
    forecast: forecast.slice(-24),
    solarWind: {
      speedKmS: speed.proton_speed,
      bt: field.bt,
      bzGsm: field.bz_gsm,
      observedAt: speed.time_tag ? parseUtcTime(speed.time_tag) : undefined,
    },
    visibility: assessAurora(peakKp, location.latitude, location.longitude),
  };
}

/** One-sentence summary suitable for a brief. */
export function auroraSummary(outlook) {
  if (!outlook) return "";
  const { visibility, currentKp, maxKpNext24h } = outlook;
  const peak =
    maxKpNext24h > currentKp
      ? `peaking near Kp ${round1(maxKpNext24h)} in the next 24 hours`
      : `holding near Kp ${round1(currentKp)}`;
  return `Geomagnetic activity is ${peak}. ${visibility.label}.`;
}
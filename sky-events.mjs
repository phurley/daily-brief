// sky-events.mjs
//
// Fixed-date sky events that cannot be derived from orbital mechanics in a
// few lines: annual meteor showers and the NASA/GSFC (Espenak) eclipse
// catalog. Everything here is a static table plus small lookups — no network.
//
// Eclipse source: NASA GSFC "Decade Tables" (2021–2030), times are Terrestrial
// Dynamical Time at greatest eclipse converted to UTC with the same ΔT
// polynomial used by almanac-calc.mjs.
//
// Meteor-shower source: IMO working list of major showers; peak dates are
// stable to about a day, active windows are approximate.

const DAY_MS = 86_400_000;
const MINUTE_MS = 60_000;

function deltaTSeconds(year) {
  const t = year - 2000;
  return 62.92 + 0.32217 * t + 0.005589 * t * t;
}

// --- Meteor showers --------------------------------------------------------

// [startMonth, startDay], [peakMonth, peakDay], [endMonth, endDay]
export const METEOR_SHOWERS = [
  { key: "quadrantids", name: "Quadrantids", radiant: "Boötes", zhr: 110, velocityKmS: 41, parent: "Asteroid 2003 EH1", active: [[12, 28], [1, 12]], peak: [1, 3] },
  { key: "lyrids", name: "Lyrids", radiant: "Lyra", zhr: 18, velocityKmS: 49, parent: "Comet C/1861 G1 Thatcher", active: [[4, 16], [4, 25]], peak: [4, 22] },
  { key: "eta-aquariids", name: "Eta Aquariids", radiant: "Aquarius", zhr: 50, velocityKmS: 66, parent: "Comet 1P/Halley", active: [[4, 19], [5, 28]], peak: [5, 5] },
  { key: "southern-delta-aquariids", name: "Southern Delta Aquariids", radiant: "Aquarius", zhr: 25, velocityKmS: 41, parent: "Comet 96P/Machholz", active: [[7, 12], [8, 23]], peak: [7, 29] },
  { key: "perseids", name: "Perseids", radiant: "Perseus", zhr: 100, velocityKmS: 59, parent: "Comet 109P/Swift-Tuttle", active: [[7, 17], [8, 24]], peak: [8, 12] },
  { key: "draconids", name: "Draconids", radiant: "Draco", zhr: 10, velocityKmS: 20, parent: "Comet 21P/Giacobini-Zinner", active: [[10, 6], [10, 10]], peak: [10, 8] },
  { key: "orionids", name: "Orionids", radiant: "Orion", zhr: 20, velocityKmS: 66, parent: "Comet 1P/Halley", active: [[10, 2], [11, 7]], peak: [10, 21] },
  { key: "southern-taurids", name: "Southern Taurids", radiant: "Taurus", zhr: 5, velocityKmS: 27, parent: "Comet 2P/Encke", active: [[9, 10], [11, 20]], peak: [11, 4] },
  { key: "northern-taurids", name: "Northern Taurids", radiant: "Taurus", zhr: 5, velocityKmS: 29, parent: "Comet 2P/Encke", active: [[10, 13], [12, 2]], peak: [11, 11] },
  { key: "leonids", name: "Leonids", radiant: "Leo", zhr: 15, velocityKmS: 71, parent: "Comet 55P/Tempel-Tuttle", active: [[11, 6], [11, 30]], peak: [11, 17] },
  { key: "geminids", name: "Geminids", radiant: "Gemini", zhr: 120, velocityKmS: 35, parent: "Asteroid 3200 Phaethon", active: [[12, 4], [12, 17]], peak: [12, 13] },
  { key: "ursids", name: "Ursids", radiant: "Ursa Minor", zhr: 10, velocityKmS: 33, parent: "Comet 8P/Tuttle", active: [[12, 17], [12, 26]], peak: [12, 22] },
];

function utcDate(year, month, day) {
  return new Date(Date.UTC(year, month - 1, day, 12, 0, 0));
}

// An active window that wraps the new year (e.g. Quadrantids) needs the
// previous year's start when the date is in January, and the next year's end
// when the date is in late December.
function activeWindowFor(shower, year) {
  const [[startMonth, startDay], [endMonth, endDay]] = shower.active;
  const wraps = endMonth < startMonth;
  const start = utcDate(wraps ? year - 1 : year, startMonth, startDay);
  const end = utcDate(wraps ? year : year, endMonth, endDay);
  return { start, end };
}

/** All showers with concrete dates for a year, sorted by peak. */
export function getMeteorShowers(year) {
  return METEOR_SHOWERS.map((shower) => {
    const { start, end } = activeWindowFor(shower, year);
    return {
      key: shower.key,
      name: shower.name,
      radiant: shower.radiant,
      zhr: shower.zhr,
      velocityKmS: shower.velocityKmS,
      parent: shower.parent,
      activeStart: start,
      activeEnd: end,
      peak: utcDate(year, shower.peak[0], shower.peak[1]),
    };
  }).sort((a, b) => a.peak - b.peak);
}

/** Showers whose active window contains `date`. */
export function getActiveMeteorShowers(date) {
  const year = date.getUTCFullYear();
  // Check the neighbouring year too so windows that straddle New Year work.
  const candidates = [
    ...getMeteorShowers(year - 1),
    ...getMeteorShowers(year),
    ...getMeteorShowers(year + 1),
  ];
  const matches = candidates.filter(
    (shower) => date >= shower.activeStart && date <= addDays(shower.activeEnd, 1),
  );
  const seen = new Set();
  return matches.filter((shower) => {
    if (seen.has(shower.key)) return false;
    seen.add(shower.key);
    return true;
  });
}

/** Upcoming shower peaks on or after `date`, soonest first. */
export function getUpcomingMeteorShowers(date, limit = 3) {
  const year = date.getUTCFullYear();
  const peaks = [
    ...getMeteorShowers(year),
    ...getMeteorShowers(year + 1),
  ].sort((a, b) => a.peak - b.peak);
  return peaks.filter((shower) => shower.peak >= date).slice(0, limit);
}

function addDays(date, days) {
  return new Date(date.getTime() + days * DAY_MS);
}

// --- Eclipses (NASA/GSFC Espenak decade tables, 2021–2030) -----------------

// kind, date, TD of greatest eclipse, type, saros, magnitude, duration, visibility
const SOLAR_ECLIPSE_ROWS = [
  ["2021-06-10", "10:43:06", "Annular", 147, 0.943, "03m51s", "n N. America, Europe, Asia [Annular: n Canada, Greenland, Russia]"],
  ["2021-12-04", "07:34:38", "Total", 152, 1.037, "01m54s", "Antarctica, S. Africa, s Atlantic [Total: Antarctica]"],
  ["2022-04-30", "20:42:36", "Partial", 119, 0.64, "-", "se Pacific, s S. America"],
  ["2022-10-25", "11:01:19", "Partial", 124, 0.862, "-", "Europe, ne Africa, Mid East, w Asia"],
  ["2023-04-20", "04:17:55", "Hybrid", 129, 1.013, "01m16s", "se Asia, E. Indies, Australia, Philippines, N.Z. [Hybrid: Indonesia, Australia, Papua New Guinea]"],
  ["2023-10-14", "18:00:40", "Annular", 134, 0.952, "05m17s", "N. America, C. America, S. America [Annular: w US, C. America, Colombia, Brazil]"],
  ["2024-04-08", "18:18:29", "Total", 139, 1.057, "04m28s", "N. America, C. America [Total: Mexico, c US, e Canada]"],
  ["2024-10-02", "18:46:13", "Annular", 144, 0.933, "07m25s", "Pacific, s S. America [Annular: s Chile, s Argentina]"],
  ["2025-03-29", "10:48:36", "Partial", 149, 0.938, "-", "nw Africa, Europe, n Russia"],
  ["2025-09-21", "19:43:04", "Partial", 154, 0.855, "-", "s Pacific, N.Z., Antarctica"],
  ["2026-02-17", "12:13:05", "Annular", 121, 0.963, "02m20s", "s Argentina & Chile, s Africa, Antarctica [Annular: Antarctica]"],
  ["2026-08-12", "17:47:05", "Total", 126, 1.039, "02m18s", "n N. America, w Africa, Europe [Total: Arctic, Greenland, Iceland, Spain]"],
  ["2027-02-06", "16:00:47", "Annular", 131, 0.928, "07m51s", "S. America, Antarctica, w & s Africa [Annular: Chile, Argentina, Atlantic]"],
  ["2027-08-02", "10:07:49", "Total", 136, 1.079, "06m23s", "Africa, Europe, Mid East, w & s Asia [Total: Morocco, Spain, Algeria, Libya, Egypt, Saudi Arabia, Yemen, Somalia]"],
  ["2028-01-26", "15:08:58", "Annular", 141, 0.921, "10m27s", "e N. America, C. & S. America, w Europe, nw Africa [Annular: Ecuador, Peru, Brazil, Suriname, Spain, Portugal]"],
  ["2028-07-22", "02:56:39", "Total", 146, 1.056, "05m10s", "SE Asia, E. Indies, Australia, N.Z. [Total: Australia, N. Z.]"],
  ["2029-01-14", "17:13:47", "Partial", 151, 0.871, "-", "N. America, C. America"],
  ["2029-06-12", "04:06:13", "Partial", 118, 0.458, "-", "Arctic, Scandinavia, Alaska, n Asia, n Canada"],
  ["2029-07-11", "15:37:18", "Partial", 156, 0.23, "-", "s Chile, s Argentina"],
  ["2029-12-05", "15:03:57", "Partial", 123, 0.891, "-", "s Argentina, s Chile, Antarctica"],
  ["2030-06-01", "06:29:13", "Annular", 128, 0.944, "05m21s", "Europe, n Africa, Mid East, Asia, Arctic, Alaska [Annular: Algeria, Tunisia, Greece, Turkey, Russia, n China, Japan]"],
  ["2030-11-25", "06:51:37", "Total", 133, 1.047, "03m44s", "s Africa, s Indian Oc., E. Indies, Australia, Antarctica [Total: Botswana, S. Africa, Australia]"],
];

const LUNAR_ECLIPSE_ROWS = [
  ["2021-05-26", "11:19:53", "Total", 121, 1.009, "00h15m", "e Asia, Australia, Pacific, Americas"],
  ["2021-11-19", "09:04:06", "Partial", 126, 0.974, "03h28m", "Americas, n Europe, e Asia, Australia, Pacific"],
  ["2022-05-16", "04:12:42", "Total", 131, 1.414, "01h25m", "Americas, Europe, Africa"],
  ["2022-11-08", "11:00:22", "Total", 136, 1.359, "01h25m", "Asia, Australia, Pacific, Americas"],
  ["2023-05-05", "17:24:05", "Penumbral", 141, -0.046, "-", "Africa, Asia, Australia"],
  ["2023-10-28", "20:15:18", "Partial", 146, 0.122, "01h17m", "e Americas, Europe, Africa, Asia, Australia"],
  ["2024-03-25", "07:13:59", "Penumbral", 113, -0.132, "-", "Americas"],
  ["2024-09-18", "02:45:25", "Partial", 118, 0.085, "01h03m", "Americas, Europe, Africa"],
  ["2025-03-14", "06:59:56", "Total", 123, 1.178, "01h05m", "Pacific, Americas, w Europe, w Africa"],
  ["2025-09-07", "18:12:58", "Total", 128, 1.362, "01h22m", "Europe, Africa, Asia, Australia"],
  ["2026-03-03", "11:34:52", "Total", 133, 1.151, "00h58m", "e Asia, Australia, Pacific, Americas"],
  ["2026-08-28", "04:14:04", "Partial", 138, 0.93, "03h18m", "e Pacific, Americas, Europe, Africa"],
  ["2027-02-20", "23:14:06", "Penumbral", 143, -0.057, "-", "Americas, Europe, Africa, Asia"],
  ["2027-07-18", "16:04:09", "Penumbral", 110, -1.068, "-", "e Africa, Asia, Australia, Pacific"],
  ["2027-08-17", "07:14:59", "Penumbral", 148, -0.525, "-", "Pacific, Americas"],
  ["2028-01-12", "04:14:13", "Partial", 115, 0.066, "00h56m", "Americas, Europe, Africa"],
  ["2028-07-06", "18:20:57", "Partial", 120, 0.389, "02h21m", "Europe, Africa, Asia, Australia"],
  ["2028-12-31", "16:53:15", "Total", 125, 1.246, "01h11m", "Europe, Africa, Asia, Australia, Pacific"],
  ["2029-06-26", "03:23:22", "Total", 130, 1.844, "01h42m", "Americas, Europe, Africa, Mid East"],
  ["2029-12-20", "22:43:12", "Total", 135, 1.117, "00h54m", "Americas, Europe, Africa, Asia"],
  ["2030-06-15", "18:34:34", "Partial", 140, 0.502, "02h24m", "Europe, Africa, Asia, Australia"],
  ["2030-12-09", "22:28:51", "Penumbral", 145, -0.163, "-", "Americas, Europe, Africa, Asia"],
];

function buildEclipses(rows, kind) {
  return rows.map(([date, td, type, saros, magnitude, duration, visibility]) => {
    const year = Number(date.slice(0, 4));
    const at = new Date(
      Date.parse(`${date}T${td}Z`) - deltaTSeconds(year) * 1000,
    );
    return { kind, date, at, type, saros, magnitude, duration, visibility };
  });
}

export const SOLAR_ECLIPSES = buildEclipses(SOLAR_ECLIPSE_ROWS, "solar");
export const LUNAR_ECLIPSES = buildEclipses(LUNAR_ECLIPSE_ROWS, "lunar");
export const ECLIPSES = [...SOLAR_ECLIPSES, ...LUNAR_ECLIPSES].sort(
  (a, b) => a.at - b.at,
);

/** Eclipses in a calendar year, optionally filtered to "solar" or "lunar". */
export function getEclipses(year, kind) {
  return ECLIPSES.filter(
    (eclipse) =>
      eclipse.at.getUTCFullYear() === year && (!kind || eclipse.kind === kind),
  );
}

/** The next eclipses on or after `date`, soonest first. */
export function getUpcomingEclipses(date, limit = 3, kind) {
  return ECLIPSES.filter(
    (eclipse) => eclipse.at >= date && (!kind || eclipse.kind === kind),
  ).slice(0, limit);
}

/** Convenience: the spans covered by the bundled astronomy tables. */
export const SKY_EVENT_COVERAGE = {
  eclipses: {
    first: ECLIPSES[0].date,
    last: ECLIPSES[ECLIPSES.length - 1].date,
  },
};
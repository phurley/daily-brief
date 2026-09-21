// Event-fit scoring math shared by the debug page. Mirrors
// data-collect/extract/scoring.py so the browser reproduces stored scores
// exactly and can re-rank live as weights change.

export const DEFAULT_BASE = 0.5;
export const DEFAULT_SCALE = 100;

// Python's round() is half-to-even; matching it keeps recomputed scores equal
// to the stored ones at exact .5 boundaries.
function roundHalfEven(value) {
  const floor = Math.floor(value);
  if (value - floor === 0.5) return floor % 2 === 0 ? floor : floor + 1;
  return Math.round(value);
}

export function signalConfig(config) {
  return config && typeof config === "object" && config.signals ? config.signals : {};
}

export function computeRaw(signals, config) {
  const base = Number.isFinite(config?.base) ? config.base : DEFAULT_BASE;
  let raw = base;
  for (const [name, spec] of Object.entries(signalConfig(config))) {
    const probability = Number(signals?.[name] ?? 0);
    const weight = Number(spec?.weight ?? 0);
    if (!Number.isFinite(probability) || !Number.isFinite(weight)) continue;
    raw += (spec?.polarity === "negative" ? -1 : 1) * weight * probability;
  }
  return raw;
}

export function computeScore(signals, config) {
  const scale = Number.isFinite(config?.scale) ? config.scale : DEFAULT_SCALE;
  const raw = Math.min(1, Math.max(0, computeRaw(signals, config)));
  return roundHalfEven(scale * raw);
}

// Signed contribution of each signal to the pre-clamp raw value, largest first.
export function contributions(signals, config) {
  return Object.entries(signalConfig(config))
    .map(([name, spec]) => {
      const probability = Number(signals?.[name] ?? 0);
      const weight = Number(spec?.weight ?? 0);
      const polarity = spec?.polarity === "negative" ? "negative" : "positive";
      const delta = (polarity === "negative" ? -1 : 1) * weight * probability;
      return { name, probability, weight, polarity, delta };
    })
    .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta) || a.name.localeCompare(b.name));
}

export function hasSignals(event) {
  const signals = event?.scoring?.signals;
  return Boolean(signals && typeof signals === "object" && Object.keys(signals).length);
}

export function discoverSignals(events) {
  const names = new Set();
  for (const event of events || []) {
    for (const name of Object.keys(event?.scoring?.signals || {})) names.add(name);
  }
  return [...names].sort();
}

// Merge the exported config with signals found in the data so a brand-new
// signal shows up (weight 0, flagged) instead of being silently dropped.
export function mergeSignals(config, discovered) {
  const signals = { ...signalConfig(config) };
  const added = [];
  for (const name of discovered) {
    if (!(name in signals)) {
      signals[name] = { weight: 0, polarity: "positive", isNew: true };
      added.push(name);
    }
  }
  return { signals, added };
}
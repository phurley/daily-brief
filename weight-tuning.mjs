import { computeRaw, computeScore, effectiveProbability, hasSignals } from './scoring.mjs?v=20261009-2';

export const WEIGHTS_KEY = 'daily-brief:weights:v2';
export function restoreWeights(defaults, storage) {
  try {
    const saved = JSON.parse(storage?.getItem(WEIGHTS_KEY) || 'null');
    if (!saved || saved.rulesVersion !== defaults?.rulesVersion || saved.scale !== defaults.scale ||
        !Number.isFinite(saved.base) || saved.base < 0 || saved.base > 1 || !saved.signals) return defaults;
    const signals = { ...defaults.signals };
    for (const [name, spec] of Object.entries(saved.signals)) {
      if (!Number.isFinite(spec?.weight) || Math.abs(spec.weight) > 1 || !['positive', 'negative'].includes(spec.polarity)) return defaults;
      if (Object.hasOwn(signals, name)) signals[name] = { ...spec };
    }
    return { ...defaults, base: saved.base, signals };
  } catch { return defaults; }
}
export function saveWeights(config, storage) {
  try { storage.setItem(WEIGHTS_KEY, JSON.stringify(config)); return true; } catch { return false; }
}
export function tasteScore(event, config) {
  return config?.signals && hasSignals(event) ? computeScore(event.scoring.signals, config) : event.score;
}
export function compareTaste(a, b, config) {
  const raw = event => config?.signals && hasSignals(event) ? computeRaw(event.scoring.signals, config) : (event.score ?? 0) / (config?.scale || 100);
  return (tasteScore(b, config) ?? 0) - (tasteScore(a, config) ?? 0) || raw(b) - raw(a) || String(a.start).localeCompare(String(b.start)) || String(a.id).localeCompare(String(b.id));
}

// Convex squared-hinge fit of adjacent ordering constraints, regularized toward
// the current weights. Signed coefficients may cross zero; the base stays fixed.
// Raw-score ordering is also the calendar tie-breaker for rounded/clamped scores.
export function fitOrder(events, config, { movedId } = {}) {
  if (events.length < 2 || events.some(event => !hasSignals(event))) return { config, matched: 0, total: 0, unsupported: true };
  const names = Object.keys(config.signals);
  const initial = names.map(name => config.signals[name].weight * (config.signals[name].polarity === 'negative' ? -1 : 1));
  const vectors = events.map(event => names.map(name => effectiveProbability(name, event.scoring.signals, config)));
  const constraints = vectors.slice(1).map((v, i) => vectors[i].map((x, j) => x - v[j]));
  // Give the explicit move more influence than preserving unrelated neighbors.
  const moved = events.findIndex(event => event.id === movedId);
  if (moved >= 0) for (let i = 0; i < events.length; i++) {
    if (i === moved) continue;
    const before = i < moved ? i : moved, after = i < moved ? moved : i;
    const strength = compareTaste(events[before], events[after], config) > 0 ? 20 : 4;
    const factor = Math.sqrt(strength);
    constraints.push(vectors[before].map((x, j) => (x - vectors[after][j]) * factor));
  }
  const dot = (a, b) => a.reduce((sum, x, i) => sum + x * b[i], 0);
  const regularization = 0.0001;
  const step = 1 / (regularization + constraints.reduce((sum, d) => sum + dot(d, d), 0) || 1);
  let weights = [...initial];
  const margin = 0.012;
  for (let iteration = 0; iteration < 1800; iteration++) {
    const gradient = weights.map((x, i) => regularization * (x - initial[i]));
    for (const difference of constraints) {
      const error = Math.min(0, dot(difference, weights) - margin);
      if (error) for (let i = 0; i < names.length; i++) gradient[i] += error * difference[i];
    }
    const next = weights.map((x, i) => Math.max(-1, Math.min(1, x - step * gradient[i])));
    const change = next.reduce((sum, x, i) => sum + Math.abs(x - weights[i]), 0);
    weights = next;
    if (change < 1e-8) break;
  }
  const fitted = { ...config, signals: Object.fromEntries(names.map((name, i) => [name, { ...config.signals[name], weight: Math.abs(weights[i]), polarity: weights[i] < 0 ? 'negative' : 'positive' }])) };
  let matched = 0, total = 0, ties = 0;
  for (let i = 0; i < events.length; i++) for (let j = i + 1; j < events.length; j++) {
    total++;
    const difference = computeRaw(events[i].scoring.signals, fitted) - computeRaw(events[j].scoring.signals, fitted);
    if (difference > 1e-7) matched++;
    if (Math.abs(difference) <= 1e-7) ties++;
  }
  return { config: fitted, matched, total, ties };
}

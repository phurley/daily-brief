// This file and brief-preferences.json contain only public defaults. Feedback is
// stored per browser/profile and never sent to the publisher or a model.
export const DEFAULT_PREFERENCES = {
  schemaVersion: '1.0.0', profileId: 'household', updatedAt: null,
  topicAffinities: {}, venueAffinities: {}, newsTopicAffinities: {},
  favorites: [], hiddenOccurrences: [], overrides: {},
  constraints: { maxDistanceMiles: null, selectedDayOnly: false },
  ranking: { version: 1, limit: 7, maxPerVenue: 2, maxPerCategory: 3, maxPerSeries: 1, minScore: 35, horizonDays: 30 },
};
const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const bounded = (value, fallback, min, max) => Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
const affinities = value => Object.fromEntries(Object.entries(object(value)).filter(([, v]) => Number.isFinite(v)).map(([k, v]) => [k.toLowerCase(), bounded(v, 0, -40, 40)]));
export function normalizePreferences(value = {}, profileId = value?.profileId || 'household') {
  const v = value?.schemaVersion && value.schemaVersion !== '1.0.0' ? {} : object(value);
  const c = object(v.constraints), r = object(v.ranking);
  return {
    ...structuredClone(DEFAULT_PREFERENCES), profileId: String(profileId),
    updatedAt: typeof v.updatedAt === 'string' ? v.updatedAt : null,
    topicAffinities: affinities(v.topicAffinities), venueAffinities: affinities(v.venueAffinities), newsTopicAffinities: affinities(v.newsTopicAffinities),
    favorites: [...new Set((Array.isArray(v.favorites) ? v.favorites : []).filter(x => typeof x === 'string'))],
    hiddenOccurrences: [...new Set((Array.isArray(v.hiddenOccurrences) ? v.hiddenOccurrences : []).filter(x => typeof x === 'string'))],
    overrides: affinities(v.overrides),
    constraints: { maxDistanceMiles: Number.isFinite(c.maxDistanceMiles) && c.maxDistanceMiles >= 0 ? c.maxDistanceMiles : null, selectedDayOnly: c.selectedDayOnly === true },
    ranking: { version: 1, limit: Math.round(bounded(r.limit, 7, 1, 20)), maxPerVenue: Math.round(bounded(r.maxPerVenue, 2, 1, 20)), maxPerCategory: Math.round(bounded(r.maxPerCategory, 3, 1, 20)), maxPerSeries: Math.round(bounded(r.maxPerSeries, 1, 1, 20)), minScore: bounded(r.minScore, 35, 0, 100), horizonDays: Math.round(bounded(r.horizonDays, 30, 0, 90)) },
  };
}
export const occurrenceKey = e => String(e.occurrenceId || `${e.id || e.url || e.title}|${e.start || ''}`);
export const seriesKey = e => String(e.seriesId || `${e.title || ''}|${e.venue || ''}`).toLowerCase();
export function feedback(preferences, event, action) {
  const p = normalizePreferences(preferences);
  const key = occurrenceKey(event), series = seriesKey(event);
  if (action === 'hide') p.hiddenOccurrences = [...new Set([...p.hiddenOccurrences, key])];
  else if (action === 'favorite') p.favorites = p.favorites.includes(series) ? p.favorites.filter(x => x !== series) : [...p.favorites, series];
  else if (action === 'more' || action === 'less') {
    const topic = String(event.category || 'uncategorized').toLowerCase();
    p.topicAffinities[topic] = bounded((p.topicAffinities[topic] || 0) + (action === 'more' ? 10 : -10), 0, -40, 40);
  } else throw new Error(`Unknown feedback: ${action}`);
  p.updatedAt = new Date().toISOString();
  return p;
}
export function preferenceStore(storage, defaults = {}) {
  const base = normalizePreferences(defaults), key = `daily-brief:preferences:v1:${base.profileId}`;
  let current = base, history = [];
  try {
    const saved = JSON.parse(storage?.getItem(key) || 'null');
    if (saved?.current?.profileId === base.profileId && saved.current.schemaVersion === '1.0.0') {
      current = normalizePreferences(saved.current, base.profileId);
      history = (Array.isArray(saved.history) ? saved.history : []).filter(p => p.profileId === base.profileId).slice(-30).map(p => normalizePreferences(p, base.profileId));
    }
  } catch { /* Corrupt/unavailable storage falls back to public defaults. */ }
  const persist = () => {
    try { if (!storage) return false; storage.setItem(key, JSON.stringify({ current, history })); return true; }
    catch { return false; }
  };
  return {
    get: () => structuredClone(current), canUndo: () => history.length > 0,
    set(next) { history.push(current); history = history.slice(-30); current = normalizePreferences(next, base.profileId); current.updatedAt = new Date().toISOString(); return persist(); },
    undo() { if (history.length) current = history.pop(); return persist(); },
    reset() { return this.set(base); },
  };
}

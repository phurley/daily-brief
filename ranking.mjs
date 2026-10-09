import { computeScore, contributions, hasSignals } from './scoring.mjs?v=20261009-2';
import { normalizePreferences, occurrenceKey, occurrenceKeys, seriesKey, isFavorite } from './preferences.mjs?v=20261009-identity1';
export { occurrenceKey, seriesKey } from './preferences.mjs?v=20261009-identity1';
const DAY = 86400000;
const key = value => String(value || '').toLowerCase();
export function localDay(value) {
  const date = new Date(value);
  if (!Number.isFinite(+date)) return '';
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Detroit', year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}
export function eventStatus(event) {
  const status = key(event.status).split('/').pop();
  if (['canceled', 'cancelled', 'eventcancelled'].includes(status) || /^\s*(?:\[|\()?cancel(?:l)?ed\b/i.test(event.title || '')) return 'cancelled';
  if (status === 'scheduled' && event.occurrenceId && !event.statusEvidence) return 'unknown';
  if (['completed', 'postponed', 'confirmed', 'scheduled', 'rescheduled'].includes(status)) return status;
  return 'unknown';
}
export function eligibility(event, { day, now, preferences }) {
  const p = normalizePreferences(preferences);
  const status = eventStatus(event), start = Date.parse(event.start), end = Date.parse(event.end);
  if (['cancelled', 'completed', 'postponed'].includes(status)) return status;
  if (!Number.isFinite(start)) return 'Date unknown';
  if (Number.isFinite(end) && end < start) return 'Invalid date range';
  const startDay = localDay(start), endDay = localDay(Number.isFinite(end) ? end : start);
  if (endDay < day || (Number.isFinite(end) && end <= now)) return 'Completed';
  if (!Number.isFinite(end) && startDay < localDay(now)) return 'Past occurrence; end unknown';
  const days = (Date.parse(startDay) - Date.parse(day)) / DAY;
  if (days > p.ranking.horizonDays || (p.constraints.selectedDayOnly && startDay > day)) return 'Outside selected dates';
  if (occurrenceKeys(event).some(k => p.hiddenOccurrences.includes(k))) return 'Hidden occurrence';
  const distance = Number.isFinite(event.distanceMiles) && event.distanceMiles >= 0 ? event.distanceMiles : null;
  if (p.constraints.maxDistanceMiles !== null && (distance === null || distance > p.constraints.maxDistanceMiles)) return distance === null ? 'Travel range unknown' : 'Outside travel range';
  return '';
}
export function rankEvent(event, { day, now, preferences, weights }) {
  const p = normalizePreferences(preferences), reasons = [], unknowns = [];
  const scored = hasSignals(event) && weights?.signals;
  const baseTaste = scored ? computeScore(event.scoring.signals, weights) : (Number.isFinite(event.score) ? event.score : 50);
  const affinity = (p.topicAffinities[key(event.category)] || 0) + (p.venueAffinities[key(event.venue)] || 0);
  const favorite = isFavorite(p, event) ? 20 : 0;
  const override = occurrenceKeys(event).map(k => p.overrides[key(k)]).find(value => value !== undefined) || 0;
  const taste = baseTaste + affinity + favorite + override;
  if (scored) reasons.push(...contributions(event.scoring.signals, weights).filter(r => r.delta > 0.025).slice(0, 2).map(r => `${r.name.replaceAll('_', ' ')} +${Math.round(r.delta * 100)}`));
  if (affinity) reasons.push(`Your topic/venue preferences ${affinity > 0 ? '+' : ''}${affinity}`);
  if (favorite) reasons.push('Favorite series +20');
  if (override) reasons.push(`Explicit override ${override > 0 ? '+' : ''}${override}`);
  const days = Math.max(0, (Date.parse(localDay(event.start)) - Date.parse(day)) / DAY);
  const ongoing = localDay(event.start) < day;
  let practicality = ongoing ? 4 : days === 0 ? 12 : days <= 2 ? 8 : days <= 7 ? 4 : 0;
  if (practicality) reasons.push(`${ongoing ? 'Still running' : days === 0 ? 'Available on selected day' : 'Coming soon'} +${practicality}`);
  if (Number.isFinite(event.distanceMiles) && event.distanceMiles >= 0) {
    const travel = event.distanceMiles <= 10 ? 6 : event.distanceMiles <= 25 ? 3 : event.distanceMiles > 60 ? -6 : 0;
    practicality += travel;
    if (travel) reasons.push(`Travel distance ${travel > 0 ? '+' : ''}${travel}`);
  } else { unknowns.push('Distance unknown'); if (event.localityTier === 'nearby') { practicality += 3; reasons.push('Nearby locality +3'); } }
  // Only machine-readable deadlines qualify. Never guess a date from prose.
  const deadline = /^\d{4}-\d{2}-\d{2}T/.test(event.deadline || '') ? Date.parse(event.deadline) : NaN;
  if (deadline >= now && deadline - now <= 2 * DAY) { practicality += 10; reasons.push('Booking deadline within two days +10'); }
  let quality = 0;
  if (['scheduled', 'confirmed'].includes(eventStatus(event))) quality += 2; else unknowns.push('Status unconfirmed');
  const updated = Date.parse(event.sourceUpdatedAt);
  if (updated <= now && now - updated <= 3 * DAY) quality += 2;
  if (!event.price) unknowns.push('Price unknown');
  if (!event.venue) unknowns.push('Venue unknown');
  if (quality) reasons.push(`Confirmed/recent source information +${quality}`);
  // No click-based inference or automatic exposure penalty. Explicit hides are eligibility rules.
  const components = { taste, practicality, novelty: 0, quality };
  return { event, occurrenceId: occurrenceKey(event), score: taste + practicality + quality, components, reasons, unknowns, ineligible: eligibility(event, { day, now, preferences: p }) };
}
export function selectBestBets(events, { day, now = Date.now(), preferences = {}, weights } = {}) {
  day ||= localDay(now);
  const p = normalizePreferences(preferences), limits = p.ranking;
  const compare = (a, b) => b.score - a.score || Date.parse(a.event.start) - Date.parse(b.event.start) || a.occurrenceId.localeCompare(b.occurrenceId) || String(a.event.id).localeCompare(String(b.event.id));
  const ranked = events.map(event => rankEvent(event, { day, now, preferences: p, weights })).filter(r => !r.ineligible && r.score >= limits.minScore).sort(compare);
  let diversityLimited = false;
  const selected = [], seen = new Set(), venues = new Map(), categories = new Map(), series = new Map();
  const take = (row, nearTerm = false) => {
    const e = row.event, v = key(e.venue) || occurrenceKey(e), c = key(e.category) || occurrenceKey(e), s = seriesKey(e);
    if (seen.has(row.occurrenceId)) return false;
    if ((venues.get(v) || 0) >= limits.maxPerVenue || (categories.get(c) || 0) >= limits.maxPerCategory || (series.get(s) || 0) >= limits.maxPerSeries) { diversityLimited = true; return false; }
    if (diversityLimited) row = { ...row, reasons: [...row.reasons, 'Adds variety after venue, category or series limits'] };
    if (nearTerm && ranked[0] !== row) row = { ...row, reasons: [...row.reasons, 'Reserved a place for the selected day'] };
    selected.push(row); seen.add(row.occurrenceId);
    for (const [map, k] of [[venues, v], [categories, c], [series, s]]) map.set(k, (map.get(k) || 0) + 1);
    return true;
  };
  const today = ranked.find(r => localDay(r.event.start) === day) || ranked.find(r => localDay(r.event.start) < day);
  if (today) take(today, true);
  for (const row of ranked) { if (selected.length >= limits.limit) break; take(row); }
  return selected;
}

// Keep date lanes independent so a high-scoring future event cannot crowd out today.
export function selectEventLanes(events, options = {}) {
  const day = options.day || localDay(options.now ?? Date.now());
  const lanes = { today: [], ongoing: [], future: [] };
  for (const event of events) {
    const start = localDay(event.start), end = localDay(event.end || event.start);
    const lane = start > day ? 'future' : end > day ? 'ongoing' : 'today';
    lanes[lane].push(event);
  }
  return Object.fromEntries(Object.entries(lanes).map(([lane, candidates]) =>
    [lane, selectBestBets(candidates, { ...options, day })]));
}

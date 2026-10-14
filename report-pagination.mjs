import { localDay, eventStatus, admissionPrice } from './ranking.mjs';
import { normalizePreferences, occurrenceKeys } from './preferences.mjs';
import { reconcileRecommendationEvents } from './event-experiences.mjs';
import { tasteScore } from './weight-tuning.mjs';

export const REPORT_PAGE_SIZE = 20;
export const eventPageKey = event => event.occurrenceId ||
  (event.experienceId ? `${event.experienceId}|${event.start}` : event.id);

// Preserve the existing page, and deduplicate both across and within new pages.
export function appendReportPage(previous, candidates, limit, key = item => item.id) {
  const result = [], seen = new Set();
  for (const item of [...previous, ...candidates]) {
    const id = key(item);
    if (!id || seen.has(id)) continue;
    seen.add(id); result.push(item);
    if (result.length >= limit) break;
  }
  return limit > 0 ? result : [];
}

export function eventHasTimeRemaining(event, day, now) {
  return day < localDay(now) || Date.parse(event.end || event.start) > now;
}

// A newly collected document can still contain stories for yesterday's report.
export function storiesOnDate(document = {}, day) {
  return (document.stories || []).filter(story => {
    const published=localDay(story.publishedAt);
    return published ? published<=day : Boolean(document.editionDate && document.editionDate<=day);
  });
}

// Browsing removes recommendation quotas, score floors and the planning horizon.
// Explicit reader constraints and unavailable occurrences still apply.
export function eventBrowseLanes(events, { day, now = Date.now(), preferences = {}, weights } = {}) {
  day ||= localDay(now);
  const p = normalizePreferences(preferences), hidden = new Set(p.hiddenOccurrences);
  const lanes = { today: [], ongoing: [], future: [] };
  const rows = [];
  for (const event of reconcileRecommendationEvents(events)) {
    const start = localDay(event.start), end = localDay(event.end || event.start);
    if (!start || !end || end < start || end < day || !eventHasTimeRemaining(event, day, now)) continue;
    if (['cancelled', 'completed', 'postponed'].includes(eventStatus(event))) continue;
    if (/SoldOut|OutOfStock/i.test(event.availability || '') && !/InStock|LimitedAvailability/i.test(event.availability || '')) continue;
    if (occurrenceKeys(event).some(id => hidden.has(id))) continue;
    if (p.constraints.selectedDayOnly && start > day) continue;
    if (p.constraints.maxDistanceMiles !== null &&
        (!Number.isFinite(event.distanceMiles) || event.distanceMiles > p.constraints.maxDistanceMiles)) continue;
    const price = admissionPrice(event);
    if (price === null && p.constraints.excludeUnknownPrice) continue;
    if (price !== null && p.constraints.maxPrice !== null && price > p.constraints.maxPrice) continue;
    rows.push({ event, start, end, score: tasteScore(event, weights) ?? 0 });
  }
  rows.sort((a, b) => b.score - a.score || Date.parse(a.event.start) - Date.parse(b.event.start) || a.event.id.localeCompare(b.event.id));
  for (const { event, start, end } of rows) {
    const lane = start > day ? 'future' : end > day ? 'ongoing' : 'today';
    lanes[lane].push(event);
  }
  return lanes;
}

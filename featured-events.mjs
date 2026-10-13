// The page, publisher, editorial context and widgets share this verified pool.
import { rankEvent, localDay, eventStatus } from './ranking.mjs';
import { reconcileRecommendationEvents } from './event-experiences.mjs';

export function reviewInput(event) {
  return Object.fromEntries(['id', 'url', 'start', 'end', 'venue', 'city', 'price', 'status']
    .map(key => [key, event[key] ?? null]));
}

export function currentFeaturedEvents(raw = [], reviewed = []) {
  const current = new Map(raw.map(event => [event.id, event]));
  return reviewed.filter(event => {
    const source = current.get(event.id);
    return source && JSON.stringify(reviewInput(source)) === JSON.stringify(event.editorial?.inputFacts);
  });
}

export function verifiedEvents(events = [], now = Date.now()) {
  return events.filter(event => {
    const review = event.editorial;
    const words = String(event.summary || '').trim().split(/\s+/).length;
    return review?.state === 'verified' && Date.parse(review.reviewedAt) <= now
      && Date.parse(review.expiresAt) > now && review.sourceUrl === event.url
      && review.evidence?.date && review.evidence?.time && review.evidence?.venue && review.evidence?.admission
      && event.timePrecision === 'time' && event.venue && event.city && event.price
      && (event.end ? Date.parse(event.end) > now : Date.parse(event.start) > now)
      && words >= 30 && words <= 50 && !['cancelled', 'postponed', 'completed'].includes(eventStatus(event));
  });
}

export function selectFeaturedLanes(events, options = {}) {
  const now = options.now ?? Date.now(), day = options.day || localDay(now);
  const lanes = { today: [], ongoing: [], future: [] };
  const used = new Set(), venues = new Map();
  const pool = reconcileRecommendationEvents(verifiedEvents(events, now));
  // Reviews carry their own concise copy; reconciliation must never replace it
  // with an unreviewed archive description.
  const ranked = pool.map(event => rankEvent(event, { ...options, day, now }))
    .filter(row => !row.ineligible)
    .sort((a, b) => b.score - a.score || Date.parse(a.event.start) - Date.parse(b.event.start) || a.event.id.localeCompare(b.event.id));
  for (const row of ranked) {
    const e = row.event, start = localDay(e.start), end = localDay(e.end || e.start);
    const lane = start > day ? 'future' : end > day ? 'ongoing' : 'today';
    const experience = e.experienceId || e.seriesId || `${e.title.toLowerCase()}|${e.venue.toLowerCase()}|${e.city.toLowerCase()}`;
    const venue = `${lane}|${e.venue.toLowerCase()}|${e.city.toLowerCase()}`;
    if (lanes[lane].length >= 3 || used.has(experience) || (venues.get(venue) || 0) >= 2) continue;
    used.add(experience); venues.set(venue, (venues.get(venue) || 0) + 1);
    lanes[lane].push(row);
  }
  return lanes;
}

export function featuredBestBets(events, options = {}) {
  const lanes = selectFeaturedLanes(events, options);
  return (lanes.today.length ? lanes.today : lanes.ongoing.length ? lanes.ongoing : lanes.future).slice(0, 3);
}

export function featuredMasthead(events, options = {}) {
  const lead = featuredBestBets(events, options)[0]?.event;
  const day = options.day || localDay(options.now ?? Date.now());
  return lead ? { headline: localDay(lead.start) > day ? `Plan ahead: ${lead.title}` : lead.title, summary: lead.summary, leadId: lead.id }
    : { headline: 'A good day, well considered.', summary: 'A little weather, the family calendar, and worthwhile discoveries.', leadId: null };
}

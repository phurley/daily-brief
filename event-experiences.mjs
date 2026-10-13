// Recommendation-only reconciliation. Source occurrences and the calendar stay intact.
import { occurrenceKeys, seriesKeys } from './preferences.mjs?v=20261009-identity1';

const normalize = value => String(value || '').normalize('NFKC').toLowerCase()
  .replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
const instant = value => Date.parse(value) || 0;
const dateFormatter = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/Detroit', year: 'numeric', month: '2-digit', day: '2-digit',
});
const day = value => value ? dateFormatter.format(new Date(value)) : '';
const city = event => normalize(String(event.city || '').replace(/,?\s+(?:MI|Michigan)$/i, ''));
const venue = event => normalize(String(event.venue || '').replace(/,\s*\d+\b.*$/, '')).replace(/^the /, '');
const eventKey = event => String(event.occurrenceId || event.id || `${event.url}|${event.start}`);
export const recommendationVenueKey = event => venue(event) ? JSON.stringify([city(event), venue(event)]) : '';
const precise = event => (!event.timePrecision || event.timePrecision === 'time') && instant(event.start);
const blocked = event => /^(?:cancelled|canceled|postponed|completed)$/i.test(event.status || '')
  || /^\s*[\[(]?cancel(?:l)?ed\b/i.test(event.title || '');

function title(event) {
  let text = normalize(String(event.title || '').replace(/^\s*[\[(]?(?:cancelled|canceled|postponed|rescheduled)(?:\s+by\s+(?:the\s+)?(?:artist|organizer))?[\])]?(?:\s*[:–—-]\s*|\s+)/i, ''));
  // Some crawled headings append the performance's YYMMDD; retain other numbers/years.
  const date = instant(event.start) ? day(event.start).replaceAll('-', '') : '';
  if (date) text = text.replace(new RegExp(` (?:${date}|${date.slice(2)})$`), '');
  return text;
}

function detailUrl(event) {
  try {
    const url = new URL(event.canonicalUrl || event.url);
    if (!/^https?:$/.test(url.protocol) || !/\/(?:events?|e|exhibitions?|tickets|tm-event)\/[^/]+/i.test(url.pathname)) return '';
    url.hostname = url.hostname.replace(/^www\./, '');
    url.hash = '';
    url.pathname = url.pathname.replace(/\/$/, '');
    for (const key of [...url.searchParams.keys()]) if (/^(?:utm_|fbclid$|gclid$|mc_cid$|mc_eid$)/i.test(key)) url.searchParams.delete(key);
    url.searchParams.sort();
    return url.href;
  } catch { return ''; }
}

function authority(event) {
  const supplier = event.identity?.fields?.start;
  const sources = supplier ? (event.sources || []).filter(source => source.sourceRecordId === supplier) : (event.sources || []);
  return Math.max(0, ...[event, ...sources].map(source => {
    const rank = { organizer: 3, ticket: 2 }[source.sourceAuthority] || 0;
    // A venue's own feed takes precedence over a calendar syndicating its listing.
    const own = normalize(source.sourceId).replace(/^the /, '') === venue(event);
    return rank === 3 && own ? 4 : rank;
  }));
}
function checked(event) {
  return Math.max(instant(event.lastCheckedAt), instant(event.sourceUpdatedAt),
    ...(event.sources || []).map(source => instant(source.observedAt)));
}
function facts(event) {
  return ['end', 'price', 'priceMin', 'registration', 'statusEvidence'].filter(key => event[key] != null).length;
}
function compareSources(a, b) {
  return authority(b) - authority(a) || Number(Boolean(precise(b))) - Number(Boolean(precise(a)))
    || checked(b) - checked(a) || facts(b) - facts(a) || eventKey(a).localeCompare(eventKey(b));
}
function representative(records) {
  const ordered = [...records].sort(compareSources);
  const notice = ordered.find(blocked);
  if (!notice) return ordered[0];
  const reinstated = ordered.find(event => !blocked(event) && authority(event) >= authority(notice)
    && checked(event) >= checked(notice) && /reinstat|resum|not cancel/i.test(event.statusEvidence || ''));
  return reinstated || notice;
}

// A subtitle may elaborate a shortened listing, but must be corroborated by a
// shared performance day/time. Ambiguous prefixes never bridge different shows.
function supportsAbbreviation(short, long) {
  if (short.title.split(' ').length < 2 || short.title.length < 6 || !long.title.startsWith(short.title + ' ')) return false;
  const shortIds = new Set(short.records.map(record => record.event.experienceId).filter(Boolean));
  const longIds = new Set(long.records.map(record => record.event.experienceId).filter(Boolean));
  if (shortIds.size && longIds.size && ![...shortIds].some(id => longIds.has(id))) return false;
  if (!long.records.some(record => authority(record.event) >= 2)) return false;
  return short.records.some(a => long.records.some(b => a.date && a.date === b.date
    && (!precise(a.event) || !precise(b.event) || Math.abs(instant(a.event.start) - instant(b.event.start)) <= 90 * 60000)));
}

export function reconcileRecommendationEvents(events) {
  const nodes = [], byTitle = new Map(), byOccurrence = new Map(), byPublished = new Map(), byUrl = new Map();
  const parents = [];
  const find = index => {
    let root = index;
    while (parents[root] !== root) root = parents[root];
    while (parents[index] !== index) { const next = parents[index]; parents[index] = root; index = next; }
    return root;
  };
  const join = (a, b) => { const x = find(a), y = find(b); if (x !== y) parents[Math.max(x, y)] = Math.min(x, y); };
  const anchor = (map, key, index) => { if (!key) return; if (map.has(key)) join(index, map.get(key)); else map.set(key, index); };
  for (const event of events) {
    const timestamp = instant(event.start);
    const name = title(event), location = venue(event), place = city(event);
    const locationKey = place && location ? JSON.stringify([place, location]) : `unknown:${eventKey(event)}`;
    const titleKey = JSON.stringify([locationKey, name]);
    let index = byTitle.get(titleKey);
    if (index === undefined) {
      index = nodes.length; byTitle.set(titleKey, index); parents.push(index);
      nodes.push({ title: name, venue: locationKey, records: [] });
    }
    nodes[index].records.push({ event, date: timestamp ? day(event.start) : '' });
    anchor(byOccurrence, eventKey(event), index);
    anchor(byPublished, event.experienceId, index);
    // A specific organizer page can reconcile a room/address variation. A
    // generic calendar URL cannot join same-named events at different venues.
    const url = detailUrl(event);
    if (url && place && name) anchor(byUrl, JSON.stringify([place, name, url]), index);
  }
  const locations = new Map();
  for (let index = 0; index < nodes.length; index++) {
    const node = nodes[index];
    if (!locations.has(node.venue)) locations.set(node.venue, []);
    locations.get(node.venue).push(index);
  }
  for (const indices of locations.values()) {
    indices.sort((a, b) => nodes[b].title.length - nodes[a].title.length || nodes[a].title.localeCompare(nodes[b].title));
    for (let position = 0; position < indices.length; position++) {
      const index = indices[position], matches = new Set();
      for (const other of indices.slice(0, position)) if (supportsAbbreviation(nodes[index], nodes[other])) matches.add(find(other));
      if (matches.size === 1) join(index, [...matches][0]);
    }
  }
  const groups = new Map();
  for (let index = 0; index < nodes.length; index++) {
    const root = find(index);
    if (!groups.has(root)) groups.set(root, []);
    groups.get(root).push(...nodes[index].records);
  }
  const result = [];
  for (const records of groups.values()) {
    const events = records.map(record => record.event);
    const published = events.map(event => event.experienceId).filter(Boolean).sort();
    const best = [...events].sort(compareSources)[0];
    const name = events.map(title).filter(Boolean).sort((a, b) => a.length - b.length || a.localeCompare(b))[0];
    const identity = city(best) && venue(best) ? [city(best), venue(best), name || eventKey(best)] : [eventKey(best), name];
    const experienceId = published[0] || `experience:${JSON.stringify(identity)}`;
    const legacySeriesKeys = [...new Set(events.flatMap(seriesKeys))].sort();
    const dates = new Map();
    for (const record of records) {
      const date = record.date || `unknown:${eventKey(record.event)}`;
      if (!dates.has(date)) dates.set(date, []);
      dates.get(date).push(record.event);
    }
    for (const dated of dates.values()) {
      // Retain distinct organizer-advertised performances on the same day.
      // A weaker conflicting time is reconciled only to a unique nearby slot.
      const known = dated.filter(precise), topAuthority = Math.max(0, ...known.map(authority));
      const anchors = [...new Set(known.filter(event => authority(event) === topAuthority).map(event => instant(event.start)))];
      const slots = new Map();
      for (const event of dated) {
        const start = instant(event.start);
        let slot = String(start || eventKey(event));
        if (topAuthority > authority(event) || !precise(event)) {
          const matches = anchors.filter(time => !precise(event) || Math.abs(time - start) <= 90 * 60000);
          if (matches.length === 1) slot = String(matches[0]);
        }
        if (!slots.has(slot)) slots.set(slot, []);
        slots.get(slot).push(event);
      }
      for (const members of slots.values()) {
        const event = representative(members);
        // Authority resolves practical facts. Retain the fullest description
        // and its associated taste signals instead of ranking ticket boilerplate.
        const description = [...members].sort((a, b) => String(b.summary || '').length - String(a.summary || '').length || compareSources(a, b))[0];
        const editorial = Object.fromEntries(['summary', 'category', 'score', 'scoring'].filter(key => description[key] !== undefined).map(key => [key, description[key]]));
        result.push({ ...event, ...editorial, experienceId, legacySeriesKeys,
          legacyOccurrenceKeys: [...new Set(members.flatMap(occurrenceKeys))].filter(key => key !== eventKey(event)).sort() });
      }
    }
  }
  return result.sort((a, b) => eventKey(a).localeCompare(eventKey(b)));
}

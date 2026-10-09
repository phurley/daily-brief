import test from 'node:test';
import assert from 'node:assert/strict';
import { selectBestBets, rankEvent, eventStatus, localDay } from '../ranking.mjs';
import { normalizePreferences, feedback, preferenceStore, occurrenceKey, seriesKey } from '../preferences.mjs';
const now = Date.parse('2026-10-09T12:00:00-04:00');
const opts = { day: '2026-10-09', now };
const event = (id, extra = {}) => ({ id, title: `Event ${id}`, start: '2026-10-09T18:00:00-04:00', end: '2026-10-09T20:00:00-04:00', venue: `Venue ${id}`, category: `Category ${id}`, score: 70, ...extra });

test('cancellations, completed occurrences, invalid dates, and hidden occurrences cannot be recommended', () => {
  const hidden = event('hidden');
  const preferences = feedback({}, hidden, 'hide');
  const events = [event('ok'), hidden, event('cancelled', { status: 'cancelled', score: 100 }), event('title', { title: 'CANCELED BY ARTIST: Concert', score: 100 }), event('ended', { start: '2026-10-09T08:00:00-04:00', end: '2026-10-09T10:00:00-04:00' }), event('invalid', { start: 'nonsense' }), event('past', { start: '2026-10-08T18:00:00-04:00', end: undefined })];
  assert.deepEqual(selectBestBets(events, { ...opts, preferences }).map(r => r.event.id), ['ok']);
  assert.equal(eventStatus(event('art', { title: 'Cancellation Culture: A Play' })), 'unknown');
  assert.equal(eventStatus(event('art', { title: 'Not canceled: The Show' })), 'unknown');
});

test('missing values are neutral and explicit distance constraints exclude unverified travel', () => {
  const e = event('unknown', { score: undefined, venue: undefined });
  const row = rankEvent(e, { ...opts, preferences: {} });
  assert.equal(row.components.taste, 50);
  assert.ok(row.unknowns.includes('Distance unknown'));
  assert.equal(row.ineligible, '');
  const preferences = normalizePreferences({ constraints: { maxDistanceMiles: 20 } });
  assert.deepEqual(selectBestBets([e, event('near', { distanceMiles: 0 }), event('far', { distanceMiles: 50 })], { ...opts, preferences }).map(r => r.event.id), ['near']);
});

test('selection is deterministic and caps total, venue, category and series', () => {
  const events = Array.from({ length: 30 }, (_, i) => event(String(i).padStart(2, '0'), { venue: `venue-${i % 3}`, category: `cat-${i % 2}`, seriesId: `series-${i % 8}` }));
  const selected = selectBestBets(events, opts);
  assert.deepEqual(selected, selectBestBets([...events].reverse(), opts));
  assert.ok(selected.length <= 7 && selected.length >= 5);
  for (const field of ['venue', 'category', 'seriesId']) {
    const cap = { venue: 2, category: 3, seriesId: 1 }[field];
    for (const r of selected) assert.ok(selected.filter(x => x.event[field] === r.event[field]).length <= cap);
  }
});

test('near-term opportunity is reserved and deadline urgency is explained', () => {
  const tomorrow = event('future', { score: 100, start: '2026-10-20T18:00:00-04:00', end: undefined });
  const today = event('today', { score: 40 });
  const selected = selectBestBets([tomorrow, today], { ...opts, preferences: { ranking: { limit: 1 } } });
  assert.equal(selected[0].event.id, 'today');
  assert.ok(selected[0].reasons.some(x => x.includes('Reserved')));
  assert.ok(rankEvent({ ...tomorrow, deadline: '2026-10-10T18:00:00-04:00' }, opts).reasons.some(x => x.includes('Booking')));
});

test('explicit date constraints, horizon, and timezone boundary are respected', () => {
  assert.equal(localDay('2026-10-10T01:00:00Z'), '2026-10-09');
  const rows = [event('today'), event('later', { start: '2026-10-10T18:00:00-04:00', end: undefined }), event('outside', { start: '2027-01-01T00:00:00Z', end: undefined })];
  assert.deepEqual(selectBestBets(rows, { ...opts, preferences: { constraints: { selectedDayOnly: true } } }).map(r => r.event.id), ['today']);
});

test('favorites affect a series, hides affect one occurrence, and more/less affect category', () => {
  const a = event('one'), next = { ...a, start: '2026-10-10T18:00:00-04:00', end: undefined };
  const p = feedback(feedback({}, a, 'favorite'), a, 'hide');
  assert.ok(p.favorites.includes(seriesKey(next)));
  assert.notEqual(occurrenceKey(a), occurrenceKey(next));
  assert.equal(selectBestBets([a, next], { ...opts, preferences: p })[0].event.start, next.start);
  assert.equal(feedback({}, a, 'more').topicAffinities[a.category.toLowerCase()], 10);
  assert.equal(feedback({}, a, 'less').hiddenOccurrences.length, 0);
});

test('storage isolates profiles and supports reload, undo, reset, and corrupt storage', () => {
  const data = new Map(), storage = { getItem: k => data.get(k), setItem: (k, v) => data.set(k, v) };
  const a = preferenceStore(storage, { profileId: 'a' }), b = preferenceStore(storage, { profileId: 'b' });
  a.set(feedback(a.get(), event('x'), 'hide'));
  assert.equal(b.get().hiddenOccurrences.length, 0);
  const reload = preferenceStore(storage, { profileId: 'a' });
  assert.equal(reload.get().hiddenOccurrences.length, 1);
  reload.undo(); assert.equal(reload.get().hiddenOccurrences.length, 0);
  reload.set(feedback(reload.get(), event('x'), 'favorite')); reload.reset();
  assert.equal(reload.get().favorites.length, 0); reload.undo(); assert.equal(reload.get().favorites.length, 1);
  assert.equal(preferenceStore({ getItem() { throw new Error(); } }).get().profileId, 'household');
  assert.equal(preferenceStore(null).set({}), false);
});

test('canonical occurrence IDs de-duplicate and input arrays remain unchanged', () => {
  const events = [event('a', { occurrenceId: 'shared' }), event('b', { occurrenceId: 'shared' })];
  const before = JSON.stringify(events);
  assert.equal(selectBestBets(events, opts).length, 1);
  assert.equal(JSON.stringify(events), before);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { reconcileRecommendationEvents } from '../event-experiences.mjs';
import { selectBestBets, selectEventLanes } from '../ranking.mjs';
import { feedback } from '../preferences.mjs';

const options = { day: '2026-10-12', now: Date.parse('2026-10-12T12:00:00-04:00') };
const make = (id, extra = {}) => ({ id, occurrenceId: id, title: 'A Different Show',
  start: '2026-10-14T20:00:00-04:00', venue: 'The Ark', city: 'Ann Arbor',
  category: 'Music', summary: 'A concise description.', score: 80, ...extra });
const official = extra => ({ sourceAuthority: 'organizer', sourceId: 'the-ark', ...extra });
const ids = rows => rows.map(row => row.event.id);

test('October 12 listings collapse Off Book variants and consecutive Comics dates', () => {
  const events = JSON.parse(readFileSync(new URL('./fixtures/experience-listings.json', import.meta.url)));
  const before = JSON.stringify(events);
  const picks = selectBestBets(events, options);
  assert.equal(picks.filter(row => /off.?book/i.test(row.event.title)).length, 1);
  assert.equal(picks.filter(row => row.event.title === 'Comics Beyond Sight').length, 1);
  const show = picks.find(row => /off.?book/i.test(row.event.title)).event;
  assert.equal(show.id, 'occ-905a0ace1ddf8fc2a0e37d43');
  assert.equal(show.start, '2026-10-14T20:00:00-04:00');
  assert.equal(show.priceMin, 33.5);
  assert.equal(show.url, 'https://theark.org/event/off-book-tour-de-force-a-force-of-tour-261014/');
  assert.equal(JSON.stringify(events), before, 'all original calendar occurrences remain untouched');
  assert.deepEqual(selectBestBets([...events].reverse(), options), picks);
  assert.deepEqual(reconcileRecommendationEvents(reconcileRecommendationEvents(events)), reconcileRecommendationEvents(events));
});

test('different venues, cities and explicit programs remain distinct', () => {
  const events = [make('one'), make('another-venue', { venue: 'Second Theater' }),
    make('another-city', { city: 'Detroit' }),
    make('first-program', { title: 'Comedy Evening: Alice', ...official() }),
    make('second-program', { title: 'Comedy Evening: Bob', ...official() }),
    make('ambiguous-prefix', { title: 'Comedy Evening' })];
  const prepared = reconcileRecommendationEvents(events);
  assert.equal(new Set(prepared.map(event => event.experienceId)).size, events.length);
});

test('generic calendar URLs and unverified title prefixes cannot join experiences', () => {
  const events = [make('one', { url: 'https://example.com/calendar' }),
    make('other-room', { venue: 'The Ark — Studio', url: 'https://example.com/calendar' }),
    make('prefix', { title: 'Live Stories' }), make('longer', { title: 'Live Stories Workshop' })];
  assert.equal(new Set(reconcileRecommendationEvents(events).map(event => event.experienceId)).size, 4);
});

test('missing cities or venues cannot create a shared experience by accident', () => {
  const events = [make('one', { city: undefined }), make('two', { city: undefined }),
    make('three', { venue: undefined }), make('four', { venue: undefined })];
  assert.equal(new Set(reconcileRecommendationEvents(events).map(event => event.experienceId)).size, 4);
});

test('a compact subset cannot undo an ambiguous full-corpus experience decision', () => {
  const events = [make('short', { title: 'Comedy Evening' }),
    make('alice', { title: 'Comedy Evening: Alice', ...official() }),
    make('bob', { title: 'Comedy Evening: Bob', ...official() })];
  const prepared = reconcileRecommendationEvents(events);
  const compact = prepared.filter(event => event.id !== 'bob');
  assert.equal(new Set(reconcileRecommendationEvents(compact).map(event => event.experienceId)).size, 2);
});

test('organizer facts win over a higher-scored conflicting time; description and taste survive', () => {
  const old = make('reported', { start: '2026-10-14T19:30:00-04:00', score: 99,
    summary: 'A fuller description of the music and performers, useful for evaluating whether to attend.' });
  const primary = make('primary', { ...official(), score: 40, priceMin: 30, priceCurrency: 'USD',
    url: 'https://theark.org/event/a-different-show/', lastCheckedAt: '2026-10-12T11:00:00-04:00' });
  const [row] = selectBestBets([old, primary], options);
  assert.equal(row.event.id, 'primary');
  assert.equal(row.event.start, primary.start);
  assert.equal(row.event.summary, old.summary);
  assert.equal(row.components.taste, 99);
  assert.equal(row.event.priceMin, 30);
});

test('cancellations, sold-out slots and hides cannot be bypassed through another listing', () => {
  const reported = make('reported', { start: '2026-10-14T19:30:00-04:00' });
  const primary = make('primary', official());
  for (const notice of [{ status: 'cancelled', statusEvidence: 'Cancelled by the organizer' },
    { title: 'CANCELED BY ARTIST: A Different Show' }, { status: 'postponed' }, { availability: 'SoldOut' }]) {
    assert.deepEqual(selectBestBets([reported, { ...primary, ...notice }], options), []);
  }
  assert.deepEqual(selectBestBets([reported, primary], { ...options, preferences: feedback({}, reported, 'hide') }), []);
  const next = { ...primary, id: 'next', occurrenceId: 'next', start: '2026-10-15T20:00:00-04:00' };
  assert.deepEqual(ids(selectBestBets([reported, primary, next], { ...options, preferences: feedback({}, reported, 'hide') })), ['next']);
});

test('a newer explicit reinstatement can replace cancellation; generic scheduled markup cannot', () => {
  const cancelled = make('cancelled', { ...official(), status: 'cancelled', statusEvidence: 'Cancelled by organizer',
    lastCheckedAt: '2026-10-11T12:00:00-04:00' });
  const current = make('current', { ...official(), status: 'scheduled', lastCheckedAt: '2026-10-12T12:00:00-04:00' });
  assert.deepEqual(selectBestBets([cancelled, { ...current, statusEvidence: 'https://schema.org/EventScheduled' }], options), []);
  assert.deepEqual(ids(selectBestBets([cancelled, { ...current, statusEvidence: 'Organizer: performance reinstated' }], options)), ['current']);
});

test('distinct same-day performances remain available when the earlier slot sells out', () => {
  const early = make('early', { ...official(), start: '2026-10-14T18:00:00-04:00', availability: 'SoldOut' });
  const late = make('late', { ...official(), start: '2026-10-14T21:00:00-04:00' });
  const reported = make('reported', { start: '2026-10-14T17:30:00-04:00' });
  assert.deepEqual(ids(selectBestBets([early, late, reported], options)), ['late']);
  assert.equal(reconcileRecommendationEvents([early, late, reported]).length, 2);
});

test('favorites survive source-specific series IDs, and an experience appears in one lane', () => {
  const today = make('today', { start: '2026-10-12T18:00:00-04:00', seriesId: 'source-one' });
  const later = make('later', { seriesId: 'source-two' });
  const preferences = feedback({}, later, 'favorite');
  const lanes = selectEventLanes([today, later], { ...options, preferences });
  assert.deepEqual(ids(lanes.today), ['today']);
  assert.equal(lanes.today[0].components.taste, 100);
  assert.deepEqual(lanes.future, []);
  const future = selectBestBets([today, later], { ...options, day: '2026-10-13', preferences });
  assert.deepEqual(ids(future), ['later']);
});

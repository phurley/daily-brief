import test from 'node:test';
import assert from 'node:assert/strict';
import { feedback, occurrenceKey, seriesKey } from '../preferences.mjs';
import { eligibility, rankEvent } from '../ranking.mjs';
const old = { id: 'old-id', title: 'Old title', start: '2026-10-10T01:00:00Z', venue: 'Library', score: 60 };
const current = { ...old, id: 'occ-new', occurrenceId: 'occ-new', title: 'Corrected title', start: '2026-10-09T21:00:00-04:00', legacyOccurrenceKeys: [occurrenceKey(old)], legacySeriesKeys: [seriesKey(old)] };
const opts = { day: '2026-10-09', now: Date.parse('2026-10-09T12:00:00-04:00') };
test('old hides survive canonical activation and new hides survive rollback', () => {
  assert.equal(eligibility(current, { ...opts, preferences: feedback({}, old, 'hide') }), 'Hidden occurrence');
  assert.equal(eligibility(old, { ...opts, preferences: feedback({}, current, 'hide') }), 'Hidden occurrence');
  assert.equal(eligibility({ ...old, start: '2026-10-11T01:00:00Z' }, { ...opts, preferences: feedback({}, current, 'hide') }), '');
});
test('favorites and overrides survive activation; unfavorite clears aliases', () => {
  const preferences = feedback({}, old, 'favorite');
  preferences.overrides = { [occurrenceKey(old).toLowerCase()]: 7 };
  assert.equal(rankEvent(current, { ...opts, preferences }).components.taste, 87);
  assert.equal(feedback(preferences, current, 'favorite').favorites.length, 0);
  assert.ok(feedback({}, current, 'favorite').favorites.includes(seriesKey(old)));
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { orderRankedNews, rankNewsStory } from '../news-ranking.mjs';
const now = Date.parse('2026-10-09T12:00:00Z');
const story = (id, extra = {}) => ({ id, publishedAt: '2026-10-09T10:00:00Z', localityIndex: 0, topics: ['science'], ...extra });
test('news ranking reads news topics and ignores all event signals', () => {
  const a = story('a');
  assert.deepEqual(rankNewsStory(a, { now }), rankNewsStory({ ...a, scoring: { score: 100, signals: { live_music: 1 } } }, { now }));
  assert.equal(rankNewsStory(a, { now, preferences: { newsTopicAffinities: { science: 10 } } }).components.affinity, 10);
});
test('recent explicit local alerts lead regardless of taste; stale/remote alerts do not', () => {
  const a = story('alert', { urgentLocal: true, topics: ['water'] });
  const options = { now, preferences: { newsTopicAffinities: { water: -40, science: 40 } } };
  assert.equal(orderRankedNews([story('science'), a], options)[0].id, 'alert');
  assert.equal(rankNewsStory({ ...a, publishedAt: '2026-09-01T10:00:00Z' }, options).urgent, false);
  assert.equal(rankNewsStory({ ...a, localityIndex: 4 }, options).urgent, false);
  assert.equal(rankNewsStory({ id: 'unknown' }, options).components.freshness, 0);
  assert.equal(rankNewsStory(story('future', { publishedAt: '2027-01-01T12:00:00Z' }), options).components.freshness, 0);
  assert.deepEqual(orderRankedNews([story('b'), story('a')], { now }).map(s => s.id), ['a', 'b']);
});

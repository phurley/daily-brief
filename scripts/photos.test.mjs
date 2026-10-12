import test from 'node:test';
import assert from 'node:assert/strict';
import { shufflePhotos } from '../photo-selection.mjs';

test('shuffle exposes every candidate once, preserves IDs and leaves source order intact', () => {
  const photos = Array.from({length:100}, (_, i) => ({id:`smugmug-${i}`}));
  const original = [...photos];
  const shuffled = shufflePhotos(photos, () => 0);
  assert.equal(shuffled.length,100);
  assert.equal(new Set(shuffled.map(p=>p.id)).size,100);
  assert.deepEqual(photos,original);
  assert.notDeepEqual(shuffled,original);
  assert.deepEqual(shuffled.map(p=>p.id).sort(),original.map(p=>p.id).sort());
});

test('empty and single-photo pools remain usable', () => {
  assert.deepEqual(shufflePhotos([]),[]);
  assert.deepEqual(shufflePhotos([{id:'only'}]),[{id:'only'}]);
});

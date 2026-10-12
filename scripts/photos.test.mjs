import test from 'node:test';
import assert from 'node:assert/strict';
import { shufflePhotos, isLandscapePhoto, photoDotWindow, photoDateLabel } from '../photo-selection.mjs';

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

test('landscape eligibility uses dimensions and excludes portraits, squares and unknowns', () => {
  assert.equal(isLandscapePhoto({width:1280,height:853}),true);
  for (const photo of [{width:853,height:1280},{width:1000,height:1000},{},
    {width:1280,height:0},{width:'1280',height:853},{width:Infinity,height:853}]) {
    assert.equal(isLandscapePhoto(photo),false);
  }
});

test('dot window remains bounded, includes the current photo, and reaches both ends', () => {
  for (const total of [0,1,2,8,100]) for (const capacity of [1,3,5,9]) {
    for (let current=0;current<total;current++) {
      const window=photoDotWindow(total,current,capacity);
      assert.equal(window.length,Math.min(total,capacity));
      assert.ok(window.includes(current));
      assert.equal(new Set(window).size,window.length);
      assert.ok(window.every(index=>index>=0 && index<total));
    }
  }
  assert.deepEqual(photoDotWindow(100,99,5),[95,96,97,98,99]);
  assert.deepEqual(photoDotWindow(100,0,5),[0,1,2,3,4]);
  assert.deepEqual(photoDotWindow(0,0,5),[]);
});

test('photo date uses the original calendar day and the requested concise format', () => {
  assert.equal(photoDateLabel('2001-09-20'),'20 Sept 2001');
  assert.equal(photoDateLabel('2000-02-29'),'29 Feb 2000');
  for (const value of ['2001-02-29','not-a-date','','2001-09-20T23:00:00Z',undefined]) {
    assert.equal(photoDateLabel(value),'');
  }
});

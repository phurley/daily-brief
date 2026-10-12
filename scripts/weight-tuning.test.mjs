import test from 'node:test';
import assert from 'node:assert/strict';
import { fitOrder, compareTaste, restoreWeights, saveWeights, tasteScore } from '../weight-tuning.mjs';
const config = { rulesVersion: 2, base: .5, scale: 100, signals: { artsy: { weight: .4, polarity: 'positive' }, sporting: { weight: .2, polarity: 'negative' } } };
const event = (id, signals) => ({ id, start: '2026-10-12T12:00:00-04:00', scoring: { signals } });
test('fits a reversed order, permits polarity changes, and does not mutate inputs', () => {
  const events = [event('sport', { sporting: 1 }), event('neutral', { artsy: .1, sporting: .1 }), event('art', { artsy: 1 })];
  const before = structuredClone(config);
  const result = fitOrder(events, config);
  assert.equal(result.matched, 3);
  assert.deepEqual([...events].sort((a,b) => compareTaste(a,b,result.config)), events);
  assert.deepEqual(config, before);
  assert.ok(Object.values(result.config.signals).every(s => s.weight >= 0 && s.weight <= 1));
});
test('identical signals report unattainable pairs instead of claiming success', () => {
  const result = fitOrder([event('b',{artsy:1}),event('a',{artsy:1})], config);
  assert.equal(result.matched, 0); assert.equal(result.ties, 1); assert.equal(result.total, 1);
});
test('fit uses overlap-adjusted market signals', () => {
  const c = { ...config, signals: { market_or_shop: { weight: .4, polarity:'negative' } } };
  const events = [event('shop',{ market_or_shop:1 }),event('farm',{market_or_shop:1,farmer_market:1})];
  const result = fitOrder(events,c);
  assert.equal(result.matched,1); assert.equal(result.config.signals.market_or_shop.polarity,'positive');
});
test('missing signals preserve published score and cannot be fitted', () => {
  assert.equal(tasteScore({score:77},config),77);
  assert.equal(fitOrder([{score:77},event('a',{artsy:1})],config).unsupported,true);
});
test('raw scores break ties even when clamped; score displays remain bounded', () => {
  const c = {...config,base:1};
  const a=event('a',{artsy:1}), b=event('b',{artsy:.5});
  assert.equal(tasteScore(a,c),100); assert.equal(tasteScore(b,c),100);
  assert.ok(compareTaste(a,b,c)<0);
});
test('saved weights merge defaults and reject malformed and incompatible data', () => {
  let saved; const storage={setItem:(k,v)=>{saved=v},getItem:()=>saved};
  const fitted=fitOrder([event('b',{sporting:1}),event('a',{artsy:1})],config).config;
  assert.equal(saveWeights(fitted,storage),true);
  assert.deepEqual(restoreWeights(config,storage),fitted);
  for (const invalid of ['{', JSON.stringify({...config,rulesVersion:1}),JSON.stringify({...config,signals:{artsy:{weight:'no',polarity:'positive'}}})]) {
    saved=invalid;assert.equal(restoreWeights(config,storage),config);
  }
  const denied={getItem(){throw Error('denied')},setItem(){throw Error('denied')}};
  assert.equal(saveWeights(config,denied),false);assert.equal(restoreWeights(config,denied),config);
});
test('prioritizes the explicitly moved event and reports the full fitted order', () => {
  const events = [event('sport',{sporting:1}),event('art',{artsy:1}),event('neutral',{artsy:.1,sporting:.1})];
  const result = fitOrder(events,config,{movedId:'sport'});
  assert.deepEqual([...events].sort((a,b)=>compareTaste(a,b,result.config)),events);
  assert.equal(result.matched,result.total);
});

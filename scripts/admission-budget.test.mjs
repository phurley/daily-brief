import test from 'node:test';
import assert from 'node:assert/strict';
import { eligibility, admissionPrice, rankEvent } from '../ranking.mjs';
import { normalizePreferences } from '../preferences.mjs';
const options = { day: '2026-11-01', now: Date.parse('2026-10-12T12:00:00-04:00'), preferences: { constraints: { maxPrice: 40 } } };
const event = extra => ({ id: 'concert', title: 'Concert', start: '2026-11-01T15:00:00-05:00', venue: 'Hall', ...extra });
test('budget excludes expensive events and treats free as zero', () => {
  assert.equal(eligibility(event({ priceMin: 1000, priceCurrency: 'USD' }), options), 'Outside admission budget');
  assert.equal(eligibility(event({ price: 'Free', priceMin: 0, priceCurrency: 'USD' }), options), '');
  assert.equal(admissionPrice(event({ price: '$20–$114.66' })), 20);
  assert.equal(admissionPrice(event({ price: 'Donation suggested $20' })), null);
});
test('unknown costs remain visible unless explicitly excluded; currencies are respected', () => {
  assert.equal(eligibility(event({}), options), '');
  assert.equal(eligibility(event({}), { ...options, preferences: { constraints: { excludeUnknownPrice: true } } }), 'Admission price unknown');
  assert.equal(admissionPrice(event({ priceMin: 20, priceCurrency: 'EUR' })), null);
  assert.equal(normalizePreferences({ constraints: { maxPrice: -1 } }).constraints.maxPrice, null);
});
test('sold-out shows remain collected but leave recommendations; a verified check improves freshness', () => {
  assert.equal(eligibility(event({ availability: 'SoldOut' }), options), 'Sold out');
  assert.equal(eligibility(event({ availability: 'SoldOut, InStock' }), options), '');
  const row = rankEvent(event({ price: '$10', lastCheckedAt: '2026-10-12T11:00:00-04:00' }), options);
  assert.ok(row.components.quality > 0);
  assert.ok(row.reasons.some(r => r.includes('budget')));
});

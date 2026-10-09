import test from 'node:test';
import assert from 'node:assert/strict';
import { isEventRecommended, eventDisplayTitle } from '../event-status.mjs';
test('canceled and postponed events are calendar notices, not recommendations', () => {
  for (const status of ['canceled', 'postponed']) {
    assert.equal(isEventRecommended({ status }), false);
    assert.match(eventDisplayTitle({ status, title: 'Storytime' }), /: Storytime$/);
  }
  for (const status of [undefined, 'scheduled', 'rescheduled']) assert.equal(isEventRecommended({ status }), true);
  assert.equal(eventDisplayTitle({ status: 'canceled', title: 'CANCELED Storytime' }), 'CANCELED Storytime');
});

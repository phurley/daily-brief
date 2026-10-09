import test from 'node:test';
import assert from 'node:assert/strict';
import {DEFAULT_PREFERENCES,hasEventPersonalization} from '../preferences.mjs';
test('only event-affecting saved preferences request the full candidate pool',()=>{
 const base=structuredClone(DEFAULT_PREFERENCES);
 assert.equal(hasEventPersonalization({...base,updatedAt:'2026-10-09T12:00:00Z'}),false);
 assert.equal(hasEventPersonalization({...base,newsTopicAffinities:{science:10}}),false);
 for(const change of [{favorites:['outside-pool']},{hiddenOccurrences:['hidden']},{topicAffinities:{theatre:10}},{venueAffinities:{nearby:10}},{overrides:{event:30}},{constraints:{selectedDayOnly:true}},{ranking:{limit:10}}]) assert.equal(hasEventPersonalization({...base,...change}),true);
 assert.equal(hasEventPersonalization({...base,favorites:['public']},{...base,favorites:['public']}),false);
});

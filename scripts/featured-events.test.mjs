import test from 'node:test';
import assert from 'node:assert/strict';
import {verifiedEvents, currentFeaturedEvents, reviewInput, selectFeaturedLanes, featuredBestBets, featuredMasthead} from '../featured-events.mjs';
const now=Date.parse('2026-10-12T16:00:00Z');
function event(id, extra={}) {
  const e={id,title:`Live stories ${id}`,url:`https://example.org/event/${id}`,start:'2026-10-13T17:00:00-04:00',
    venue:'A small theatre',city:'Ann Arbor',timePrecision:'time',price:'Free',status:'confirmed',score:80,
    summary:'Explore African history through beads in a hands-on storytelling workshop. Participants learn how visual materials preserve cultural history, then string their own selection to take home. It combines an unusual art encounter with the pleasure of making something personal.',...extra};
  e.editorial={state:'verified',reviewedAt:'2026-10-12T12:00:00-04:00',expiresAt:'2026-10-13T12:00:00-04:00',sourceUrl:e.url,
    evidence:{date:'October 13, 2026',time:'5:00pm',venue:e.venue,admission:'Free'},inputFacts:reviewInput(e)};
  return e;
}
test('unreviewed, expired, incomplete and elapsed unknown-end events never feature',()=>{
  const valid=event('valid');
  const stale=event('stale');stale.editorial.expiresAt='2026-10-12T11:00:00-04:00';
  const missing=event('missing');delete missing.editorial.evidence.admission;
  const raw=event('raw');delete raw.editorial;
  const elapsed=event('elapsed',{start:'2026-10-12T11:00:00-04:00'});
  assert.deepEqual(verifiedEvents([valid,stale,missing,raw,elapsed],now).map(e=>e.id),['valid']);
});
test('changed organizer facts or cancellation invalidate review immediately',()=>{
  const e=event('one');
  assert.equal(currentFeaturedEvents([e],[e]).length,1);
  for(const extra of [{start:'2026-10-13T18:00:00-04:00'},{price:'$50'},{status:'cancelled'}])
    assert.deepEqual(currentFeaturedEvents([{...e,...extra}],[e]),[]);
});
test('featured lanes cap at three distinct experiences and widgets/headline use same lead',()=>{
  const events=Array.from({length:6},(_,i)=>event(String(i),{venue:`Room ${i}`,score:100-i}));
  events.push(event('repeat',{title:'Live stories 0',venue:'Room 0',start:'2026-10-14T17:00:00-04:00'}));
  const options={now,day:'2026-10-13'};
  const lanes=selectFeaturedLanes(events,options);
  assert.equal(lanes.today.length,3);assert.equal(lanes.future.length,0);
  assert.deepEqual(featuredBestBets(events,options),lanes.today);
  assert.equal(featuredMasthead(events,options).leadId,lanes.today[0].event.id);
});
test('expiry advances the lead and unknown-end events expire at start',()=>{
  const first=event('first',{start:'2026-10-12T13:00:00-04:00'}),next=event('next',{start:'2026-10-12T14:00:00-04:00'});
  assert.equal(featuredMasthead([first,next],{now,day:'2026-10-12'}).leadId,'first');
  assert.equal(featuredMasthead([first,next],{now:Date.parse('2026-10-12T17:00:00Z'),day:'2026-10-12'}).leadId,'next');
});
test('reader count preferences can reduce the selection and cannot exceed the verified cap',()=>{
  const events=Array.from({length:6},(_,i)=>event(String(i),{venue:`Room ${i}`,score:100-i}));
  const options={now,day:'2026-10-13'};
  assert.equal(selectFeaturedLanes(events,{...options,preferences:{ranking:{limit:1}}}).today.length,1);
  assert.equal(selectFeaturedLanes(events,{...options,preferences:{ranking:{limit:20}}}).today.length,3);
});

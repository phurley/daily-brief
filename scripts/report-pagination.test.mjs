import test from 'node:test';
import assert from 'node:assert/strict';
import { appendReportPage, eventBrowseLanes, eventHasTimeRemaining, eventPageKey, storiesOnDate } from '../report-pagination.mjs';

const now=Date.parse('2026-10-14T14:00:00Z'),day='2026-10-14';
const event=(id,extra={})=>({id,title:`Event ${id}`,venue:'The room',city:'Canton',score:10,
  start:'2026-10-14T18:00:00-04:00',end:'2026-10-14T20:00:00-04:00',price:'Free',...extra});

test('repeated pages retain their order, remove duplicate IDs and reach exhaustion',()=>{
  const pool=Array.from({length:67},(_,id)=>({id:String(id)}));
  pool.splice(12,0,pool[0],pool[11]);
  let page=pool.slice(0,3);
  for (const limit of [23,43,63,83]) {
    const previous=[...page];page=appendReportPage(page,pool,limit);
    assert.deepEqual(page.slice(0,previous.length),previous);
    assert.equal(new Set(page.map(item=>item.id)).size,page.length);
  }
  assert.equal(page.length,67);
  assert.deepEqual(appendReportPage(page,pool,100),page);
  assert.deepEqual(appendReportPage([],pool,0),[]);
});

test('publication dates recover older stories from a newer document without showing future stories',()=>{
  const document={editionDate:'2026-10-14',stories:[
    {id:'yesterday',publishedAt:'2026-10-13T15:00:00Z'},
    {id:'today',publishedAt:'2026-10-14T15:00:00Z'},
    {id:'tomorrow-utc',publishedAt:'2026-10-15T01:00:00Z'},
    {id:'future',publishedAt:'2026-10-15T15:00:00Z'},
    {id:'unknown'},
  ]};
  assert.deepEqual(storiesOnDate(document,'2026-10-13').map(story=>story.id),['yesterday']);
  assert.deepEqual(storiesOnDate(document,'2026-10-14').map(story=>story.id),['yesterday','today','tomorrow-utc','unknown']);
});

test('event browsing reaches low-rated events and dates beyond the recommendation horizon',()=>{
  const events=Array.from({length:45},(_,i)=>event(String(i)));
  events.push(event('far',{start:'2027-03-01T18:00:00-05:00',end:'2027-03-01T20:00:00-05:00'}));
  const lanes=eventBrowseLanes(events,{day,now,preferences:{ranking:{limit:1,minScore:100,maxPerVenue:1,horizonDays:1}}});
  assert.equal(lanes.today.length,45);
  assert.equal(lanes.future[0].id,'far');
  assert.equal(appendReportPage([],lanes.today,60,eventPageKey).length,45);
});

test('event lanes preserve separate performances and reconcile duplicate sources',()=>{
  const original=event('original',{title:'The performance',occurrenceId:'one',sources:[{sourceAuthority:'organizer'}]});
  const copy={...original,id:'copy'};
  const later=event('later',{title:'The performance',start:'2026-10-14T21:00:00-04:00',end:'2026-10-14T23:00:00-04:00',sources:[{sourceAuthority:'organizer'}]});
  const ongoing=event('ongoing',{start:'2026-10-13T18:00:00-04:00',end:'2026-10-15T20:00:00-04:00'});
  const lanes=eventBrowseLanes([original,copy,later,ongoing],{day,now});
  assert.equal(lanes.today.length,2);
  assert.equal(lanes.ongoing.length,1);
  assert.equal(new Set(lanes.today.map(eventPageKey)).size,2);
});

test('unavailable events and explicit reader constraints still apply while browsing',()=>{
  const normal=event('normal',{occurrenceId:'hidden'});
  const events=[normal,event('cancelled',{status:'cancelled'}),event('sold',{availability:'SoldOut'}),
    event('ended',{end:'2026-10-14T09:00:00-04:00',start:'2026-10-14T08:00:00-04:00'}),
    event('unknown-end',{start:'2026-10-14T09:00:00-04:00',end:null}),event('too-far',{distanceMiles:90}),
    event('expensive',{price:'$60',distanceMiles:5}),event('unknown-price',{price:null,distanceMiles:5}),
    event('fits',{price:'$10',distanceMiles:5})];
  const lanes=eventBrowseLanes(events,{day,now,preferences:{hiddenOccurrences:['hidden'],constraints:{maxDistanceMiles:20,maxPrice:25,excludeUnknownPrice:true}}});
  assert.deepEqual(lanes.today.map(event=>event.id),['fits']);
  assert.equal(eventHasTimeRemaining(events[4],day,now),false);
});

test('browsing a previous date can recover its finished occurrences',()=>{
  const previous=event('previous',{start:'2026-10-13T18:00:00-04:00',end:'2026-10-13T20:00:00-04:00'});
  assert.equal(eventBrowseLanes([previous],{day:'2026-10-13',now}).today.length,1);
  const next=event('next',{start:'2026-10-15T18:00:00-04:00',end:'2026-10-15T20:00:00-04:00'});
  assert.equal(eventBrowseLanes([next],{day,now,preferences:{constraints:{selectedDayOnly:true}}}).future.length,0);
});

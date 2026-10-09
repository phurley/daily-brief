import test from 'node:test';
import assert from 'node:assert/strict';
import {selectEventLanes} from '../ranking.mjs';

test('date lanes retain eligible picks independently and honor hidden/cancelled events', () => {
 const make = (id,start,end,extra={}) => ({id,title:id,start,end,score:80,category:id,venue:id,...extra});
 const events = [
  make('today','2026-10-09T18:00:00-04:00','2026-10-09T20:00:00-04:00'),
  make('ends-today','2026-10-08T18:00:00-04:00','2026-10-09T20:00:00-04:00'),
  make('ongoing','2026-10-08T18:00:00-04:00','2026-10-11T20:00:00-04:00'),
  make('future','2026-10-10T18:00:00-04:00','2026-10-10T20:00:00-04:00',{score:100}),
  make('cancelled','2026-10-09T18:00:00-04:00',null,{status:'cancelled'}),
  make('ended','2026-10-09T08:00:00-04:00','2026-10-09T09:00:00-04:00'),
 ];
 const options={day:'2026-10-09',now:Date.parse('2026-10-09T12:00:00-04:00')};
 const lanes=selectEventLanes(events,options);
 assert.deepEqual(lanes.today.map(row=>row.event.id),['today','ends-today']);
 assert.deepEqual(lanes.ongoing.map(row=>row.event.id),['ongoing']);
 assert.deepEqual(lanes.future.map(row=>row.event.id),['future']);
 assert.equal(selectEventLanes(events,{...options,preferences:{constraints:{selectedDayOnly:true}}}).future.length,0);
});

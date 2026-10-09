import assert from 'node:assert/strict';
import test from 'node:test';
import {selectScienceDigest,scienceView,scienceRotationSlot,scienceContext,scienceEditorial,scienceFreshness} from '../science.mjs';
const stories = Array.from({length:12},(_,i)=>({id:`s-${i}`,title:`Finding ${i}`,summary:'Measured effect',publishedAt:'2026-10-08T12:00:00Z',verifiedAt:'2026-10-09T12:00:00Z',source:{name:i<8?'NASA':`Other ${i}`},topics:[i<8?'astronomy':'biology'],evidenceType:'preprint',caveat:'Not peer reviewed.'}));
test('cards and editorial share exact IDs, dates and untruncated caveats independent of input order',()=>{
 const d={stories};const a=selectScienceDigest(d,'2026-10-09');
 const b=scienceContext({stories:[...stories].reverse()},null,'2026-10-09',Date.parse('2026-10-09T04:00:00Z'));
 assert.deepEqual(a.selectedIds,b.selectedIds);assert.equal(a.stories.length,6);
 assert.ok(new Set(a.stories.map(s=>s.source.name)).size>=3);
 assert.equal(b.stories[0].caveat,stories[0].caveat);assert.equal(b.stories[0].evidenceType,'preprint');
});
test('persisted daily selection stays stable; next date can select new stories',()=>{
 const d={stories};const a=selectScienceDigest(d,'2026-10-09');
 d.selection=a;d.stories=[...stories,{...stories[0],id:'new',publishedAt:'2026-10-09T12:00:00Z'}];
 assert.deepEqual(selectScienceDigest(d,'2026-10-09').selectedIds,a.selectedIds);
 assert.ok(selectScienceDigest(d,'2026-10-10').selectedIds.includes('new'));
});
test('old and future material does not become recent lead coverage',()=>{
 const d={stories:stories.map(s=>({...s,publishedAt:'2026-08-01T12:00:00Z'}))};
 const c=scienceContext(d,null,'2026-10-09');assert.equal(c.stories.length,0);
 assert.equal(selectScienceDigest(d,'2026-10-09').archive.length,12);
 assert.doesNotMatch(scienceEditorial(c).summary,/new today/i);
 assert.match(scienceEditorial(c).summary,/overdue/);
 assert.equal(selectScienceDigest({stories},'2026-10-07').stories.length,0);
});
test('quiet healthy feeds and overdue feeds are distinguished',()=>{
 const now=Date.parse('2026-10-09T18:00:00Z');const health={status:'ok',generatedAt:'2026-10-09T17:00:00Z',sources:{nsf:{name:'NSF',lastSuccessAt:'2026-10-08T18:00:00Z',staleAfterHours:48}}};
 assert.equal(scienceFreshness({stories},health,now).stale,false);
 assert.equal(scienceFreshness({stories},health,now+49*3600000).stale,true);
 assert.equal(scienceFreshness({stories},{...health,status:'error'},now).stale,true);
});
test('mission history groups milestones without merging distinct telescope findings',()=>{
 const milestone={...stories[0],evidenceType:'mission-milestone',storyClusterId:'test-mission'};
 const old={...milestone,id:'old',publishedAt:'2026-10-07T12:00:00Z'};
 const finding={...milestone,id:'finding',evidenceType:'peer-reviewed-research'};
 const result=selectScienceDigest({stories:[old,milestone,finding]},'2026-10-09');
 assert.deepEqual(result.previously[milestone.id].map(s=>s.id),['old']);
 assert.ok(result.selectedIds.includes('finding'));assert.ok(!result.selectedIds.includes('old'));
});
test('deterministic editorial explicitly preserves date and caveat',()=>{
 const copy=scienceEditorial(scienceContext({stories},null,'2026-10-09'));
 assert.match(copy.summary,/2026-10-08/);assert.match(copy.summary,/Not peer reviewed/);
 assert.doesNotMatch(copy.summary,/new today/i);
});

test('four-hour browser rotation is deterministic, preserves the daily six and matches the intro',()=>{
 const doc={stories};doc.selection=selectScienceDigest(doc,'2026-10-09');
 const original=JSON.stringify(doc);
 const view=stamp=>scienceView(doc,'2026-10-09',Date.parse(stamp));
 const before=view('2026-10-09T11:59:59Z'); // 07:59 Detroit
 const after=view('2026-10-09T12:00:00Z');
 assert.notDeepEqual(before.selectedIds,after.selectedIds);
 assert.deepEqual([...before.selectedIds].sort(),[...after.selectedIds].sort());
 assert.deepEqual(view('2026-10-09T15:59:59Z').selectedIds,after.selectedIds);
 assert.deepEqual(scienceView({...doc,stories:[...stories].reverse()},'2026-10-09',Date.parse('2026-10-09T12:00:00Z')).selectedIds,after.selectedIds);
 const context=scienceContext(doc,null,'2026-10-09',Date.parse('2026-10-09T12:00:00Z'));
 assert.deepEqual(context.selectedIds,after.selectedIds);
 assert.ok(scienceEditorial(context).summary.includes(after.stories[0].title));
 assert.equal(JSON.stringify(doc),original); // dates, caveats and saved selection untouched
 const leads=new Set(Array.from({length:6},(_,slot)=>scienceView(doc,'2026-10-09',Date.parse('2026-10-09T04:00:00Z')+slot*4*3600000).selectedIds[0]));
 assert.equal(leads.size,6);
});
test('rotation handles empty/singleton views, archive dates and DST without random order',()=>{
 const now=Date.parse('2026-10-09T18:00:00Z');
 assert.deepEqual(scienceView({},'2026-10-09',now).selectedIds,[]);
 assert.deepEqual(scienceView({stories:stories.slice(0,1)},'2026-10-09',now).selectedIds,['s-0']);
 assert.deepEqual(scienceView({stories},'2026-10-08',now).selectedIds,selectScienceDigest({stories},'2026-10-08').selectedIds);
 assert.equal(scienceRotationSlot(Date.parse('2026-11-01T05:30:00Z')),scienceRotationSlot(Date.parse('2026-11-01T06:30:00Z')));
});
test('daily health stays healthy overnight, but missed collection and failures surface',()=>{
 const checked='2026-10-09T10:00:00Z';const start=Date.parse(checked);
 const health={status:'ok',generatedAt:checked,sources:{nasa:{name:'NASA',lastSuccessAt:checked,staleAfterHours:36}}};
 assert.equal(scienceFreshness({stories},health,start+26*3600000).stale,false);
 assert.equal(scienceFreshness({stories},health,start+37*3600000).stale,true);
 assert.equal(scienceFreshness({stories},{...health,status:'error'},start).stale,true);
 assert.equal(scienceFreshness({stories},{...health,generatedAt:new Date(start+37*3600000).toISOString()},start+37*3600000).stale,true);
});

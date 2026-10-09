import assert from 'node:assert/strict';
import test from 'node:test';
import {selectScienceDigest,scienceContext,scienceEditorial,scienceFreshness} from '../science.mjs';
const stories = Array.from({length:12},(_,i)=>({id:`s-${i}`,title:`Finding ${i}`,summary:'Measured effect',publishedAt:'2026-10-08T12:00:00Z',verifiedAt:'2026-10-09T12:00:00Z',source:{name:i<8?'NASA':`Other ${i}`},topics:[i<8?'astronomy':'biology'],evidenceType:'preprint',caveat:'Not peer reviewed.'}));
test('cards and editorial share exact IDs, dates and untruncated caveats independent of input order',()=>{
 const d={stories};const a=selectScienceDigest(d,'2026-10-09');
 const b=scienceContext({stories:[...stories].reverse()},null,'2026-10-09');
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

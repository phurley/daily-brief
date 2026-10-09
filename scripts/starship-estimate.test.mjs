import test from 'node:test';
import assert from 'node:assert/strict';
import { collectEstimate, searchSources, validateEstimate, redditURL } from '../starship/estimate.mjs';
import { communityEstimateView, editorialStarship, reconcile } from '../starship.mjs';
const now=new Date('2026-10-09T18:00:00Z');
const record=reconcile({evidence:[],now});
const source={id:'source',url:'https://www.reddit.com/r/spacex/comments/test/outlook/',title:'Development thread',content:'Flight 15 NET November 8th 2026. Updated October 9, 2026.',observedAt:now.toISOString()};
const result={state:'estimated',missionId:'starship-flight-15',summary:'Best guess: no earlier than November 8.',rationale:'The updated development thread reports this tentative target.',windowStart:'2026-11-08',windowEnd:null,caveats:['The date can slip.'],citations:[{sourceId:'source',excerpt:source.content}]};
const estimate={...validateEstimate(result,[source],now,record),generatedAt:now.toISOString(),expiresAt:'2026-10-10T18:00:00Z',model:'fixture',version:'fixture'};
test('only actual r/SpaceX retrieval excerpts are eligible, never generated answer or neighboring subs',()=>{
 assert.equal(redditURL('https://reddit.com.evil/r/spacex/comments/x'),null);
 assert.equal(redditURL('https://www.reddit.com/r/SpaceXLounge/comments/x'),null);
 assert.equal(redditURL('https://user:pass@reddit.com/r/spacex/comments/x'),null);
 assert.equal(searchSources({content:'invented target'},now).length,0);
 const message={annotations:[{url_citation:{url:source.url,title:source.title,content:source.content}}]};
 assert.equal(searchSources(message,now).length,1);
});
test('fabricated citations, past dates, wrong missions and impossible calendar dates are rejected',()=>{
 for(const patch of [{citations:[]},{citations:[{sourceId:'missing',excerpt:source.content}]},{citations:[{sourceId:'source',excerpt:'Flight 15 is definitely confirmed for November 9th'}]},{windowStart:'2026-10-01'},{windowStart:'2027-02-30'},{missionId:null}]) assert.throws(()=>validateEstimate({...result,...patch},[source],now,record));
 assert.throws(()=>validateEstimate(result,[source],now,{outcomes:[{missionId:'starship-flight-15'}]}));
});
test('community estimate does not change official freshness or enter editorial fact input',()=>{
 const r={...record,communityEstimate:estimate};
 assert.equal(communityEstimateView(r,now).summary,estimate.summary);
 assert.equal(r.lastVerifiedAt,null);
 assert.equal(editorialStarship(r,now).communityEstimate,undefined);
 assert.equal(communityEstimateView(r,new Date('2026-10-10T18:00:01Z')),null);
 assert.equal(communityEstimateView({...r,outcomes:[{missionId:'starship-flight-15'}]},now),null);
 assert.equal(communityEstimateView({...r,mission:{id:'starship-flight-16'}},now),null);
 assert.equal(communityEstimateView({...r,status:'underway',statusEffectiveAt:now.toISOString()},now),null);
});
test('outages preserve the old timestamp and expiry, and source failures do not halt official collection',async()=>{
 const previous={communityEstimate:{...estimate,generatedAt:'2026-10-08T18:00:00Z'}};
 const {estimate:retained,health}=await collectEstimate(record,previous,{}, {now,keyProvider:()=> 'test',request:async()=>{throw new Error('Estimate API HTTP 429');}});
 assert.deepEqual(retained,previous.communityEstimate);
 assert.equal(health.state,'error');
 assert.equal(health.lastSuccessAt,null);
});
test('repeated runs reuse the daily estimate without extra API requests',async()=>{
 const r=await collectEstimate(record,{communityEstimate:estimate},{},{now,keyProvider:()=>{throw new Error('should not be called');}});
 assert.deepEqual(r.estimate,estimate);
 assert.equal(r.health,null);
});
test('search without excerpts cannot produce a forecast from model memory',async()=>{
 let calls=0;
 const r=await collectEstimate(record,null,{}, {now,keyProvider:()=> 'test',request:async()=>{calls++;return {choices:[{message:{content:'November 8, certainly'}}]};}});
 assert.equal(calls,1); assert.equal(r.estimate,null); assert.equal(r.health.state,'error');
});
test('search plus grounded synthesis generates an expiring estimate',async()=>{
 const a={choices:[{message:{annotations:[{url_citation:{url:source.url,title:source.title,content:source.content}}]}}]};
 const id=searchSources(a.choices[0].message,now)[0].id;
 const responses=[a,{choices:[{message:{content:JSON.stringify({...result,citations:[{sourceId:id,excerpt:source.content}]})}}]}];
 const r=await collectEstimate(record,null,{}, {now,keyProvider:()=> 'test',request:async()=> responses.shift()});
 assert.equal(r.estimate.windowStart,'2026-11-08'); assert.equal(r.estimate.windowEnd,null); assert.equal(r.health.state,'ok');
});

test('dated source quotations must include the next mission, correct year and date',()=>{
 const wrong={...source,content:'Flight 14 NET November 8th 2026. Updated October 9, 2026.'};
 assert.throws(()=>validateEstimate({...result,citations:[{sourceId:'source',excerpt:wrong.content}]},[wrong],now,record));
 const old={...source,content:'Flight 15 NET November 8th 2025.'};
 assert.throws(()=>validateEstimate({...result,citations:[{sourceId:'source',excerpt:old.content}]},[old],now,record));
});
test('feed retrieval points readers at the embedded development-thread permalink',()=>{
 const msg={annotations:[{url_citation:{url:'https://www.reddit.com/r/SpaceX/.rss',content:`## Starship Development Thread #63\nPublished: 2026-04-22 · Link: ${source.url}\n\n${source.content}\n---\n## Other post`}}]};
 const sources=searchSources(msg,now);
 assert.equal(sources[0].url,source.url);
 assert.equal(sources[0].title,'Starship Development Thread #63');
 assert.equal(sources[0].content,source.content);
});

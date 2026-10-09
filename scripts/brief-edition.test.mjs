import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,readFile,writeFile,cp,readdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve} from 'node:path';
import {execFileSync} from 'node:child_process';
import {gzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {bestBets,detroitDate,eventDay,validateManifest} from '../brief-selection.mjs';
import {selectBestBets} from '../ranking.mjs';
import {readSavedEdition,saveEdition,fetchJSON,SAVED_KEY} from '../edition-client.mjs';
const root=resolve(import.meta.dirname,'..');
const event=(id,start,score,end)=>({id,title:id,url:'https://example.com',start,end,score,summary:'test'});
test('best bets use Detroit dates, canonical IDs, scores, and known expiry',()=>{
 const now=Date.parse('2026-10-09T20:00:00Z');
 const events=[event('late','2026-10-10T01:00:00Z',95),event('early','2026-10-09T21:00:00Z',80),event('ended','2026-10-09T15:00:00Z',99,'2026-10-09T16:00:00Z'),event('long','2026-10-08T15:00:00Z',99,'2026-10-12T16:00:00Z')];
 assert.equal(eventDay(events[0].start),'2026-10-09');
 assert.deepEqual(bestBets([...events,events[0]],'2026-10-09',now).map(e=>e.id),['late','early','long']);
 assert.equal(detroitDate(new Date('2026-10-10T04:00:00Z')),'2026-10-10');
});
test('edition publication is deterministic, dated, bounded, coherent and excludes private data',async()=>{
 const temp=await mkdtemp(resolve(tmpdir(),'brief-edition-'));
 try{
  for(const name of ['weather','events','news','geeknews','vibe','starship','science-health','brief-preferences','scoring-weights']) await cp(resolve(root,name+'.json'),resolve(temp,name+'.json'));
  await writeFile(resolve(temp,'private-preference-history.json'),'SECRET');await writeFile(resolve(temp,'calendar.json'),'PRIVATE CALENDAR');
  const build=now=>execFileSync(process.execPath,[resolve(root,'scripts/build_edition.mjs'),temp],{env:{...process.env,BRIEF_NOW:now}});
  build('2026-10-09T18:00:00Z');
  const text=await readFile(resolve(temp,'brief-manifest.json'),'utf8');const manifest=validateManifest(JSON.parse(text));
  build('2026-10-09T18:00:00Z');assert.equal(await readFile(resolve(temp,'brief-manifest.json'),'utf8'),text);
  let compressed=0;
  for(const descriptor of Object.values(manifest.sections)){
   const data=await readFile(resolve(temp,descriptor.url));compressed+=gzipSync(data).length;
   assert.equal(createHash('sha256').update(data).digest('hex'),descriptor.sha256);
   assert.ok(!data.includes('SECRET'));assert.ok(!data.includes('PRIVATE CALENDAR'));
  }
  assert.ok(compressed<250000);
  const widget=JSON.parse(await readFile(resolve(temp,'widget-events.json'),'utf8'));
  const events=JSON.parse(await readFile(resolve(temp,manifest.sections.events.url),'utf8')).events;
  const config=JSON.parse(await readFile(resolve(temp,manifest.sections.rankingConfig.url),'utf8'));
  assert.deepEqual(widget.events.map(e=>e.id),selectBestBets(events,{day:manifest.editionDate,now:Date.parse('2026-10-09T18:00:00Z'),preferences:config.preferences,weights:config.weights}).map(row=>row.event.id));
  assert.throws(()=>validateManifest({...manifest,schemaVersion:2}));
  build('2026-10-10T04:01:00Z');
  assert.equal(JSON.parse(await readFile(resolve(temp,'widget-events.json'),'utf8')).editionDate,'2026-10-10');
  assert.ok((await readdir(resolve(temp,'editions'))).length<=16);
 }finally{await rm(temp,{recursive:true,force:true})}
});
test('storage denial and incompatible saved editions are recoverable',()=>{
 globalThis.localStorage={getItem(){throw Error('denied')},setItem(){throw Error('quota')}};
 assert.equal(readSavedEdition(),null);assert.equal(saveEdition({},{}),false);
 globalThis.localStorage={getItem:()=>JSON.stringify({manifest:{schemaVersion:100}})};
 assert.equal(readSavedEdition(),null);
 delete globalThis.localStorage;
});
test('bounded fetch rejects failures and hash mismatches',async()=>{
 const old=globalThis.fetch;
 try{
  globalThis.fetch=async()=>new Response('{}',{status:200});
  await assert.rejects(fetchJSON('test',{sha256:'0'.repeat(64)}),/changed/);
  globalThis.fetch=async()=>new Response('',{status:503});await assert.rejects(fetchJSON('test'),/503/);
  globalThis.fetch=(_,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('timeout'))));
  await assert.rejects(fetchJSON('test',{timeout:10}),/timeout/);
 }finally{globalThis.fetch=old}
});

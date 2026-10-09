#!/usr/bin/env node
import {readFile,writeFile,rename,mkdir,readdir,unlink} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {gzipSync} from 'node:zlib';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {BRIEF_VERSION,SECTION_VERSIONS,detroitDate,eventDay,bestBets,rankEvents,validateSection} from '../brief-selection.mjs';
import {selectBestBets,selectEventLanes,rankEvent} from '../ranking.mjs';
import {orderRankedNews} from '../news-ranking.mjs';
import {selectScienceDigest} from '../science.mjs';
const root=resolve(process.argv[2] || dirname(fileURLToPath(import.meta.url))+'/..');
const now=new Date(process.env.BRIEF_NOW || Date.now());
const date=detroitDate(now);
const digest=text=>createHash('sha256').update(text).digest('hex');
const read=async name=>JSON.parse(await readFile(resolve(root,name),'utf8'));
async function atomic(name,text) { const path=resolve(root,name); await writeFile(path+`.tmp-${process.pid}`,text); await rename(path+`.tmp-${process.pid}`,path); }
const source={},sourceText={};
for (const name of Object.keys(SECTION_VERSIONS).filter(name=>name!=='rankingConfig')) {
 sourceText[name]=await readFile(resolve(root,(name==='scienceHealth'?'science-health':name)+'.json'),'utf8');
 source[name]=validateSection(name,JSON.parse(sourceText[name]));
}
const preferences=await read('brief-preferences.json');
const weights=await read('scoring-weights.json');
const selection=(day)=>selectBestBets(source.events.events,{day,now:+now,preferences,weights}).map(row=>row.event);
const dates=[-1,0,1].map(offset=>{const d=new Date(date+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+offset);return d.toISOString().slice(0,10)});
const selected=new Map();
for (const day of dates) for (const event of selection(day)) selected.set(event.id,event);
for (const day of dates) for (const rows of Object.values(selectEventLanes(source.events.events,{day,now:+now,preferences,weights})))
 for (const {event} of rows) selected.set(event.id,event);
// A small candidate pool supports immediate browsing; personalization fetches the full archive on demand.
const candidates=source.events.events.map(event=>rankEvent(event,{day:date,now:+now,preferences,weights})).filter(row=>!row.ineligible).sort((a,b)=>b.score-a.score).slice(0,40);
for(const {event} of candidates) selected.set(event.id,event);
// Only existing public source documents/defaults are read. Browser preference histories never enter publication.
const eventFields=["legacyOccurrenceKeys","legacySeriesKeys","timePrecision",'id','occurrenceId','seriesId','title','url','start','end','venue','city','region','category','summary','score','scoring','price','registration','distanceMiles','deadline','status','statusEvidence','sourceUpdatedAt','localityTier','sourceMentions','canonicalEntityId'];
const pick=(object,fields)=>Object.fromEntries(fields.filter(k=>object[k]!==undefined).map(k=>[k,object[k]]));
const sections={
 weather:source.weather,
 events:{...source.events,events:[...selected.values()].map(e=>pick(e,eventFields))},
 news:{...source.news,stories:orderRankedNews(source.news.stories,{now:+now,preferences}).slice(0,10)},
 geeknews:{...source.geeknews,stories:selectScienceDigest(source.geeknews,date).stories},
 starship:source.starship,
 scienceHealth:source.scienceHealth,
 rankingConfig:{schemaVersion:'1.0.0',generatedAt:source.events.generatedAt,preferences,weights},
 vibe:{...source.vibe,messages:source.vibe.messages.filter(m=>dates.includes(m.date))},
};
await mkdir(resolve(root,'editions'),{recursive:true});
const descriptors={};let bytes=0;
for (const [name,data] of Object.entries(sections)) {
 const text=JSON.stringify(data); const sha256=digest(text);const url=`editions/${name.toLowerCase()}-${sha256}.json`;
 await atomic(url,text);bytes+=gzipSync(text).length;
 descriptors[name]={url,sha256,schemaVersion:data.schemaVersion,generatedAt:data.generatedAt,editionDate:data.editionDate || null};
}
if(bytes>250000) throw new Error(`Compact data budget exceeded: ${bytes} gzip bytes`);
const archives={};
// Hash-checked lazy archives keep old manifests from accidentally consuming new data.
for(const name of ['events','news','geeknews']) {
 const text=sourceText[name];
 archives[name]={url:name+'.json',sha256:digest(text),schemaVersion:source[name].schemaVersion};
}
const content={schemaVersion:BRIEF_VERSION,editionDate:date,sections:descriptors,archives};
const editionId=digest(JSON.stringify(content));
let previous=null;try{previous=await read('brief-manifest.json')}catch{}
const generatedAt=previous?.editionId===editionId?previous.generatedAt:now.toISOString();
const manifest={...content,editionId,generatedAt};
const widget={schemaVersion:BRIEF_VERSION,editionId,editionDate:date,generatedAt,sourceGeneratedAt:source.events.generatedAt,events:selection(date).map(e=>pick(e,eventFields.filter(f=>f!=='scoring')))};
await atomic('widget-events.json',JSON.stringify(widget));
await atomic('recommendations.json',JSON.stringify({schemaVersion:'1.0.0',rankingVersion:1,generatedAt,days:dates.slice(1).map(day=>({date:day,events:selectBestBets(source.events.events,{day,now:+now,preferences,weights}).map(row=>({...row.event,recommendation:{occurrenceId:row.occurrenceId,score:row.score,components:row.components,reasons:row.reasons,unknowns:row.unknowns}}))}))},null,2)+'\n');
// Write manifest last: a reader can only discover completed section files.
await atomic('brief-manifest.json',JSON.stringify(manifest));
// Keep current and previous manifests' section files; clients also save their last complete edition.
const keep=new Set([...Object.values(descriptors),...Object.values(previous?.sections||{})].map(s=>s.url));
for(const file of await readdir(resolve(root,'editions'))) if(file.endsWith('.json')&&!keep.has('editions/'+file)) await unlink(resolve(root,'editions',file));
console.log(`Edition ${date} ${editionId.slice(0,12)}: ${bytes} gzip bytes, ${sections.events.events.length} event cards, ${widget.events.length} widget best bets`);

// Exercise the real browser timer with a controlled, hash-checked edition.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const engines=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),url=process.env.BRIEF_TEST_URL || 'http://localhost:4173';
 const manifest=JSON.parse(fs.readFileSync(path.join(root,'brief-manifest.json'))),now=Date.parse(manifest.generatedAt);
 const doc=JSON.parse(fs.readFileSync(path.join(root,manifest.sections.events.url)));
 const summary='An intimate evening of live music brings an eclectic performer to a small local venue. Listen closely to the songs and enjoy a relaxed room that puts the performance first, with a chance to discover something outside the usual concert circuit.';
 function fixture(id,start){return {id,title:`Verified performance ${id}`,url:`https://example.org/event/${id}`,start:new Date(start).toISOString(),
  venue:`Small room ${id}`,city:'Canton',category:'Music',summary,price:'Free',timePrecision:'time',score:id==='first'?100:90,status:'confirmed',
  editorial:{state:'verified',sourceUrl:`https://example.org/event/${id}`,reviewedAt:new Date(now-1000).toISOString(),expiresAt:new Date(now+86400000).toISOString(),
  evidence:{date:'October 12',time:'9 PM',venue:`Small room ${id}`,admission:'Free'}}};}
 const first=fixture('first',now+1500),backup=fixture('backup',now+3600000);
 doc.featuredEvents=[first,backup];const body=JSON.stringify(doc),hash=crypto.createHash('sha256').update(body).digest('hex');
 const oldUrl=manifest.sections.events.url;manifest.sections.events={...manifest.sections.events,sha256:hash,url:`editions/events-${hash}.json`};
 const browser=await engines[process.env.BRIEF_BROWSER || 'chromium'].launch();
 try{
  const context=await browser.newContext({serviceWorkers:'block',viewport:{width:390,height:844}});
  await context.route(/icanhazdadjoke|rocketlaunch/,r=>r.abort());
  await context.route('**/brief-manifest.json',r=>r.fulfill({json:manifest}));
  await context.route('**/'+manifest.sections.events.url,r=>r.fulfill({body,contentType:'application/json'}));
  const p=await context.newPage();await p.clock.install({time:new Date(now)});await p.goto(url);
  await p.waitForFunction(()=>document.querySelector('#events-list').textContent.includes('Verified performance first'));
  assert.ok(await p.locator('#events-list .card > p:not(.card__footer)').first().evaluate(el=>el.scrollHeight<=el.clientHeight+1), 'Short reviewed copy is fully visible');
  assert.match(await p.locator('#page-title .desktop-title').textContent(),/Verified performance first/);
  await p.clock.fastForward(2000);
  await p.waitForFunction(()=>!document.querySelector('#events-list').textContent.includes('Verified performance first'));
  assert.match(await p.locator('#page-title .desktop-title').textContent(),/Verified performance backup/);
  assert.equal(await p.locator('#events-list > article').count(),1);
  const {featuredBestBets}=await import(pathToFileURL(path.join(root,'featured-events.mjs')));
  assert.equal(featuredBestBets([first,backup],{now:now+2000,day:manifest.editionDate})[0].event.id,'backup');
  await p.clock.fastForward(86400000);
  await p.waitForFunction(()=>document.querySelectorAll('#events-list > article, #claims-list > article').length===0);
  assert.ok(!(await p.locator('#page-title .desktop-title').textContent()).includes('Verified performance'));
  console.log('Unknown-end expiry, lead replacement, shared widget order and review expiry passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});

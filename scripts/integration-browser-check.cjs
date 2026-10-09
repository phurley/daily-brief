// Regression checks for upgrades, failed edition replacement and preference reloads.
const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const engines=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const root=path.resolve(__dirname,'..'),temp=await fs.mkdtemp(path.join(os.tmpdir(),'brief-browser-'));
 let browser,server;
 try {
  for(const file of await fs.readdir(root,{withFileTypes:true})) {
   if(['.git','node_modules','scripts'].includes(file.name)) continue;
   if(file.isDirectory()) await fs.symlink(path.join(root,file.name),path.join(temp,file.name),'dir');
   else await fs.copyFile(path.join(root,file.name),path.join(temp,file.name));
  }
  await fs.mkdir(path.join(temp,'scripts'));await fs.copyFile(path.join(root,'scripts/sw-template.js'),path.join(temp,'scripts/sw-template.js'));
  const {buildShell}=await import(pathToFileURL(path.join(root,'scripts/build_shell.mjs')));
  const app=await fs.readFile(path.join(temp,'app.js'),'utf8');
  await fs.writeFile(path.join(temp,'app.js'),app+'\nglobalThis.__briefUpgrade=1;\n');await buildShell(temp);
  server=http.createServer(async(req,res)=>{
   try {
    const name=decodeURIComponent(new URL(req.url,'http://localhost').pathname).replace(/^\//,'') || 'index.html';
    const file=path.resolve(temp,name);if(!file.startsWith(temp+path.sep))throw Error('path');
    const body=await fs.readFile(file);res.writeHead(200,{'Content-Type':{'.js':'text/javascript','.mjs':'text/javascript','.html':'text/html','.css':'text/css','.json':'application/json','.svg':'image/svg+xml','.png':'image/png'}[path.extname(file)] || 'application/octet-stream','Cache-Control':'no-cache'});res.end(body);
   }catch{res.writeHead(404);res.end();}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const url=`http://127.0.0.1:${server.address().port}/`;
  browser=await engines[process.env.BRIEF_BROWSER || 'chromium'].launch({channel:process.env.BRIEF_CHROME_CHANNEL});
  const manifest=JSON.parse(await fs.readFile(path.join(root,'brief-manifest.json'))),now=Date.parse(manifest.generatedAt);
  const errors=[];
  async function context(options={}) {const c=await browser.newContext(options);await c.route(/icanhazdadjoke|rocketlaunch/,r=>r.abort());return c;}
  async function page(c){const p=await c.newPage();await p.clock.setFixedTime(new Date(now));p.on('pageerror',e=>errors.push(e.message));return p;}
  const c=await context();let p=await page(c);await p.goto(url);await p.waitForFunction(async()=>Boolean((await navigator.serviceWorker.getRegistration())?.active),null,{timeout:15000});await p.reload();await p.waitForFunction(()=>navigator.serviceWorker.controller && globalThis.__briefUpgrade===1);
  await fs.writeFile(path.join(temp,'app.js'),app+'\nglobalThis.__briefUpgrade=2;\n');await buildShell(temp);
  await p.evaluate(async()=>{await (await navigator.serviceWorker.getRegistration()).update()});
  await p.waitForFunction(async()=>Boolean((await navigator.serviceWorker.getRegistration()).waiting));
  // Engines differ in whether a worker starts with no clients. Close all old
  // clients, then reopen; bounded retries allow activation to finish on launch.
  let activated=false;
  for(let attempt=0;attempt<8 && !activated;attempt++) {
    await p.close();await new Promise(resolve=>setTimeout(resolve,250));
    p=await page(c);await p.goto(url);await p.waitForFunction(()=>globalThis.__briefUpgrade!==undefined);
    activated=await p.evaluate(()=>globalThis.__briefUpgrade===2);
  }
  assert.ok(activated,'waiting worker activates on reopening after old clients close');
  await p.waitForFunction(()=>localStorage.getItem('daily-brief-edition:v1'));await c.setOffline(true);await p.reload();await p.waitForFunction(()=>globalThis.__briefUpgrade===2);await c.close();console.log('Generated shell upgrade and offline reopening passed');

  const replacement=await context({serviceWorkers:'block'});p=await page(replacement);await p.goto(url);await p.waitForFunction(()=>localStorage.getItem('daily-brief-edition:v1'));
  const before=await p.locator('#events-list > article').allTextContents();assert.ok(before.length>0);
  const next=structuredClone(manifest);next.editionId='a'.repeat(64);next.sections.events.sha256='b'.repeat(64);next.sections.events.url='editions/events-'+next.sections.events.sha256+'.json';
  await replacement.route('**/brief-manifest.json',r=>r.fulfill({json:next}));await replacement.route('**/'+next.sections.events.url,r=>r.abort());
  await p.locator('#refresh-brief').click();await p.waitForFunction(()=>document.querySelector('#data-errors').textContent.includes('keeping the previous complete edition'));
  assert.deepEqual(await p.locator('#events-list > article').allTextContents(),before);
  assert.equal(await p.evaluate(()=>JSON.parse(localStorage.getItem('daily-brief-edition:v1')).manifest.editionId),manifest.editionId);
  next.sections.events=manifest.sections.events;await p.locator('#refresh-brief').click();await p.waitForFunction(()=>JSON.parse(localStorage.getItem('daily-brief-edition:v1')).manifest.editionId==='a'.repeat(64));await replacement.close();console.log('Failed replacement retains complete edition and recovers on retry');

  const {selectBestBets,rankEvent,seriesKey}=await import(pathToFileURL(path.join(root,'ranking.mjs')));
  const events=JSON.parse(await fs.readFile(path.join(root,'events.json'))).events;
  const compact=JSON.parse(await fs.readFile(path.join(root,manifest.sections.events.url))).events;
  const defaults=JSON.parse(await fs.readFile(path.join(root,'brief-preferences.json'))),weights=JSON.parse(await fs.readFile(path.join(root,'scoring-weights.json')));
  const ids=new Set(compact.map(e=>e.id)),day=manifest.editionDate;
  const candidate=events.find(e=>!ids.has(e.id) && !rankEvent(e,{day,now,preferences:defaults,weights}).ineligible && selectBestBets(events,{day,now,preferences:{...defaults,favorites:[seriesKey(e)]},weights}).some(r=>r.event.id===e.id));
  assert.ok(candidate,'fixture contains a personal favorite outside the public shortlist');
  const current={...defaults,profileId:'household',schemaVersion:'1.0.0',favorites:[seriesKey(candidate)]};
  const personalized=await context({serviceWorkers:'block'});p=await page(personalized);
  await p.addInitScript(current=>localStorage.setItem('daily-brief:preferences:v1:household',JSON.stringify({current,history:[]})),current);
  await p.goto(url);await p.waitForFunction(title=>[...document.querySelectorAll('#events-list, #ongoing-list, #claims-list')].some(lane=>lane.textContent.includes(title)),candidate.title);
  await p.reload();await p.waitForFunction(title=>[...document.querySelectorAll('#events-list, #ongoing-list, #claims-list')].some(lane=>lane.textContent.includes(title)),candidate.title);
  const later=structuredClone(manifest);later.editionId='c'.repeat(64);
  await personalized.route('**/brief-manifest.json',r=>r.fulfill({json:later}));
  await p.locator('#refresh-brief').click();await p.waitForFunction(()=>JSON.parse(localStorage.getItem('daily-brief-edition:v1')).manifest.editionId==='c'.repeat(64));
  await p.waitForFunction(title=>[...document.querySelectorAll('#events-list, #ongoing-list, #claims-list')].some(lane=>lane.textContent.includes(title)),candidate.title);
  await personalized.route('**/events.json',r=>r.abort());await p.reload();await p.waitForFunction(()=>document.querySelector('#recommendation-status')?.textContent.includes('Full candidates unavailable'));
  await personalized.close();console.log('Saved favorite survives reload and edition changes; unavailable full candidates are explained');
  assert.deepEqual(errors,[]);
 } finally {await browser?.close();await new Promise(resolve=>server ? server.close(resolve) : resolve());await fs.rm(temp,{recursive:true,force:true});}
})().catch(error=>{console.error(error);process.exit(1)});

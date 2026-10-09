const engines=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const engine=engines[process.env.BRIEF_BROWSER || 'chromium'];
const testURL=process.env.BRIEF_TEST_URL || 'http://localhost:4173';
const assert=require('node:assert/strict');
(async()=>{
 const browser=await engine.launch();
 const fixture=await (await fetch(new URL('brief-manifest.json',testURL))).json();
 const fixtureTime=new Date(fixture.generatedAt);
 const errors=[];
 const context=await browser.newContext({viewport:{width:390,height:844},reducedMotion:'reduce'});
 await context.route(/icanhazdadjoke|rocketlaunch/,r=>r.abort());
 const page=await context.newPage();await page.clock.setFixedTime(fixtureTime);page.on('pageerror',e=>errors.push(e.message));
 const requests=[];page.on('request',r=>requests.push(r.url()));
 const start=Date.now();await page.goto((process.env.BRIEF_TEST_URL || 'http://localhost:4173'));
 await page.waitForFunction(()=>document.querySelector('#events-list .event-details-button') && document.querySelector('#geek-section').getAttribute('aria-busy')==='false');
 console.log('Initial readyMs',Date.now()-start,'nodes',await page.locator('*').count());
 assert.ok(!requests.some(u=>/\/(events|news|calendar|photos)\.json/.test(u)),'archives are lazy');
 assert.equal(await page.locator('#ongoing-lane').isVisible(),true);
 assert.equal(await page.locator('#plan-ahead').isVisible(),true);
 assert.equal(await page.locator('#events-section .recommendation-details').count(),0);
 assert.equal(await page.locator('[data-more-stories], #starship-status').count(),0);
 assert.match(await page.locator('#rocket-launches').innerText(),/Starship: best guess/);
 const icon=await page.locator('#events-list .event-details-button').first().boundingBox();
 assert.ok(icon.width<=24 && icon.height<=24,'details is a small icon');
 await page.locator('#events-list .event-details-button').first().click();
 await page.waitForFunction(()=>document.querySelector('#event-details-dialog').open);
 assert.equal(await page.locator('#event-details-dialog .event-feedback button').count(),4);
 await page.keyboard.press('Tab');await page.keyboard.press('Tab');
 assert.equal(await page.evaluate(()=>document.querySelector('#event-details-dialog').contains(document.activeElement)),true);
 await page.keyboard.press('Escape');
 assert.equal(await page.evaluate(()=>document.activeElement.classList.contains('event-details-button')),true);
 for(const [width,height,large] of [[320,640,false],[390,844,false],[430,932,false],[844,390,false],[320,640,true]]) {
  await page.setViewportSize({width,height});
  await page.evaluate(large=>document.documentElement.style.fontSize=large?'32px':'',large);
  await page.locator('[data-calendar-open]').click();await page.waitForFunction(()=>document.querySelector('#calendar-load-status').textContent.includes('Complete'));
  await page.locator('.agenda-item strong').first().evaluate(e=>e.textContent='A'.repeat(300));
  const dimensions=await page.evaluate(()=>{
   const dialog=document.querySelector('#calendar-dialog');const close=document.querySelector('#calendar-close').getBoundingClientRect();
   const agenda=document.querySelector('.agenda-item');
   return {width:innerWidth,scroll:dialog.scrollWidth,client:dialog.clientWidth,closeRight:close.right,closeLeft:close.left,closeBottom:close.bottom,height:innerHeight,agenda:agenda.getBoundingClientRect().width,agendaScroll:agenda.scrollWidth,agendaClient:agenda.clientWidth};
  });
  console.log('Layout',width,height,large,dimensions);
  assert.ok(dimensions.scroll<=dimensions.client+1,'dialog does not overflow');assert.ok(dimensions.closeRight<=width && dimensions.closeLeft>=0 && dimensions.closeBottom<=height,'close visible');assert.ok(dimensions.agendaScroll<=dimensions.agendaClient+1,'long title wraps');
  await page.keyboard.press('Escape');await page.waitForFunction(()=>document.activeElement===document.querySelector('[data-calendar-open]'));
 }
 await page.evaluate(()=>document.documentElement.style.fontSize='');await page.setViewportSize({width:390,height:844});
 await page.locator('.section-nav a[href="#geek-section"]').click();
 await page.waitForTimeout(100);
 assert.ok(await page.locator('#geek-section').evaluate(e=>e.getBoundingClientRect().top>=44 && e.getBoundingClientRect().top<200),'one action science navigation');
 await page.locator('#events-all-summary').click();await page.waitForFunction(()=>document.querySelector('#events-all-list .card'));

 // Each shelf extends only after scrolling; appended pages preserve earlier cards.
 for (const [kind,selector] of [['news','#news-list'],['geeknews','#geek-list']]) {
  const ids=await page.locator(`${selector} > .story`).evaluateAll(cards=>cards.map(card=>card.dataset.storyId));
  assert.ok(ids.length>0);
  await page.locator(selector).evaluate(track=>{track.scrollLeft=track.scrollWidth;});
  await page.waitForFunction(([selector,count])=>document.querySelectorAll(`${selector} > .story`).length>count,[selector,ids.length]);
  const after=await page.locator(`${selector} > .story`).evaluateAll(cards=>cards.map(card=>card.dataset.storyId));
  assert.deepEqual(after.slice(0,ids.length),ids,'existing cards stay in order');
  assert.equal(new Set(after).size,after.length,'no duplicate stories');
  assert.ok(await page.locator(selector).evaluate(track=>track.scrollLeft>0),'scroll position retained');
  assert.equal(requests.filter(url=>new URL(url).pathname===`/${kind}.json`).length,1,'one archive request per shelf');
 }
 console.log('Lanes, details icon, compact launch line and scroll pagination passed');
 await page.evaluate(()=>navigator.serviceWorker.ready);await page.reload();
 await page.waitForFunction(()=>document.querySelector('#events-list .event-details-button'));
 await context.setOffline(true);await page.reload();
 await page.waitForFunction(()=>document.querySelector('#live-status').textContent.includes('Saved brief'));
 assert.ok(await page.locator('#events-list .event-details-button').count()>0);console.log('Offline reopen passed');
 await context.setOffline(false);
 // A missing section cache must still recover from the saved complete edition.
 await page.evaluate(async()=>{for(const key of await caches.keys()) if(key.includes('sections')) await caches.delete(key)});
 await context.setOffline(true);await page.reload();
 await page.waitForFunction(()=>document.querySelector('#live-status').textContent.includes('Saved brief'));
 await context.setOffline(false);
 // A future contract in browser storage is ignored, then recovered online.
 await page.evaluate(()=>localStorage.setItem('daily-brief-edition:v1',JSON.stringify({manifest:{schemaVersion:99}})));
 await page.reload();await page.waitForFunction(()=>document.querySelector('#events-list .event-details-button'));

 const blocked=await browser.newContext({serviceWorkers:'block'});await blocked.route(/icanhazdadjoke|rocketlaunch/,r=>r.abort());
 await blocked.addInitScript(()=>{Object.defineProperty(window,'localStorage',{get(){throw new Error('storage denied')}})});
 const bp=await blocked.newPage();await bp.clock.setFixedTime(fixtureTime);await bp.goto((process.env.BRIEF_TEST_URL || 'http://localhost:4173'));await bp.waitForFunction(()=>document.querySelector('#data-errors').textContent.includes('storage unavailable'));console.log('Storage denial passed');await blocked.close();
 const stalled=await browser.newContext({serviceWorkers:'block'});await stalled.route(/icanhazdadjoke|rocketlaunch/,r=>r.abort());
 await stalled.route(/editions\/geeknews-/,async r=>{await new Promise(resolve=>setTimeout(resolve,10000));try{await r.abort()}catch{}});
 const sp=await stalled.newPage();await sp.clock.setFixedTime(fixtureTime);const st=Date.now();await sp.goto((process.env.BRIEF_TEST_URL || 'http://localhost:4173'));await sp.waitForFunction(()=>document.querySelector('#events-list .event-details-button'));
 assert.ok(Date.now()-st<5000);await sp.waitForFunction(()=>document.querySelector('#geek-section').getAttribute('aria-busy')==='false',null,{timeout:12000});console.log('Stalled noncritical section passed');await stalled.close();
 assert.deepEqual(errors,[]);await browser.close();console.log('Browser acceptance passed');
})().catch(e=>{console.error(e);process.exit(1)});

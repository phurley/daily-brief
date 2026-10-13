const assert = require('node:assert/strict');
const fs = require('node:fs');
const {createHash} = require('node:crypto');
const engines = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.BRIEF_TEST_URL || 'http://localhost:4173';
const key = 'daily-brief:weights:v2';
(async () => {
  const browser = await engines[process.env.BRIEF_BROWSER || 'chromium'].launch();
  try {
    const manifest = JSON.parse(fs.readFileSync('brief-manifest.json'));
    const source = JSON.parse(fs.readFileSync('events.json'));
    const day = manifest.editionDate;
    const template = source.events[0];
    source.events = [
      ['art', 'Art fixture', {artsy: 1}],
      ['neutral', 'Neutral fixture', {artsy: .1, sporting: .1}],
      ['sport', 'Sport fixture', {sporting: 1}],
    ].map(([id,title,signals]) => ({ ...template, id, title, start: `${day}T12:00:00-04:00`, end: `${day}T23:00:00-04:00`, scoring: {...template.scoring,signals} }));
    const body = JSON.stringify(source);
    manifest.archives.events.sha256 = createHash('sha256').update(body).digest('hex');
    async function setup(denied = false) {
      const context = await browser.newContext({serviceWorkers:'block',viewport:{width:1440,height:1000}});
      await context.route(/icanhazdadjoke|rocketlaunch/,r=>r.abort());
      await context.route('**/brief-manifest.json',r=>r.fulfill({json:manifest}));
      await context.route(/\/events.json(?:\?|$)/,r=>r.fulfill({body,contentType:'application/json'}));
      if (denied) await context.addInitScript(()=>Object.defineProperty(window,'localStorage',{get(){throw Error('denied')}}));
      const page = await context.newPage();
      await page.clock.setFixedTime(new Date(`${day}T10:00:00-04:00`));
      await page.goto(url);
      await page.locator('[data-calendar-open]').click();
      await page.waitForFunction(()=>document.querySelector('#calendar-load-status').textContent.includes('loaded'));
      assert.equal(await page.locator('.agenda-item').count(),3);
      return {context,page};
    }
    const {context,page} = await setup();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    const ids = () => page.locator('.agenda-item').evaluateAll(nodes=>nodes.map(n=>n.dataset.eventId));
    assert.deepEqual(await ids(),['art','neutral','sport']);
    await page.locator('.calendar-tuning summary').click();
    const artsy = page.getByRole('spinbutton',{name:'artsy weight',exact:true});
    await artsy.fill('-1');
    await page.waitForFunction(()=>document.querySelector('.agenda-item').dataset.eventId==='neutral');
    assert.deepEqual(await ids(),['neutral','sport','art']);
    const saved=await page.evaluate(key=>JSON.parse(localStorage.getItem(key)),key);
    assert.equal(saved.signals.artsy.weight,1);assert.equal(saved.signals.artsy.polarity,'negative');
    // Arrow keys on sliders must not be intercepted by the calendar navigation.
    const slider=page.getByRole('slider',{name:'artsy weight slider'});
    await slider.focus();await page.keyboard.press('ArrowRight');
    assert.equal(await slider.inputValue(),'-0.99');
    await page.reload();await page.locator('[data-calendar-open]').click();
    await page.waitForFunction(()=>document.querySelector('#calendar-load-status').textContent.includes('loaded'));
    assert.deepEqual(await ids(),['neutral','sport','art']);
    await page.getByRole('button',{name:'Reset to defaults',exact:true}).click();
    assert.deepEqual(await ids(),['art','neutral','sport']);
    await page.getByRole('button',{name:'Move Sport fixture up',exact:true}).click();
    assert.deepEqual(await ids(),['art','sport','neutral']);
    assert.match(await page.locator('.calendar-tuning__status').textContent(),/Requested order matched/);
    await page.getByRole('button',{name:'Undo weight change'}).click();
    assert.deepEqual(await ids(),['art','neutral','sport']);
    await page.locator('[data-event-id="sport"].agenda-item').dragTo(page.locator('[data-event-id="art"].agenda-item'));
    assert.deepEqual(await ids(),['sport','art','neutral']);
    // A second tab receives this browser's saved profile.
    const other=await context.newPage();await other.clock.setFixedTime(new Date(`${day}T10:00:00-04:00`));await other.goto(url);
    await other.locator('[data-calendar-open]').click();await other.waitForFunction(()=>document.querySelector('#calendar-load-status').textContent.includes('loaded'));
    await page.getByRole('button',{name:'Reset to defaults',exact:true}).click();
    await other.waitForFunction(()=>document.querySelector('.agenda-item').dataset.eventId==='art');
    await page.locator('.calendar-tuning summary').click();
    for(const width of [1440,390,320]) {
      await page.setViewportSize({width,height:900});
      assert.ok(await page.locator('#calendar-dialog').evaluate(n=>n.scrollWidth<=n.clientWidth+1),`dialog fits ${width}`);
      assert.ok(await page.locator('.calendar-dialog__body').evaluate(n=>n.clientHeight>100),'agenda stays usable with controls expanded');
      if(process.env.BRIEF_TUNING_SCREENSHOTS) await page.screenshot({path:`${process.env.BRIEF_TUNING_SCREENSHOTS}/tuning-${width}.png`});
    }
    await other.close();await context.close();
    const denied=await setup(true);await denied.page.locator('.calendar-tuning summary').click();
    await denied.page.getByRole('spinbutton',{name:'artsy weight',exact:true}).fill('-1');
    assert.match(await denied.page.locator('.calendar-tuning__status').textContent(),/storage unavailable/);
    await denied.context.close();assert.deepEqual(errors,[]);
    console.log('Live sort, slider keyboard input, save/reload, fitting, drag, undo, reset, cross-tab sync, mobile layout, and denied storage passed');
    await require('./scoring-debug-browser-check.cjs')(browser, url);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});

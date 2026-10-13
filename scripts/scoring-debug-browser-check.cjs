const assert = require('node:assert/strict');
const fs = require('node:fs');

module.exports = async function checkScoringDebug(browser, url) {
  const source = JSON.parse(fs.readFileSync('events.json'));
  const day = source.editionDate;
  const tomorrow = new Date(`${day}T12:00:00Z`);
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  const nextDay = tomorrow.toISOString().slice(0, 10);
  const config = { rulesVersion: 2, base: .5, scale: 100, signals: {
    artsy: { weight: .4, polarity: 'positive' },
    sporting: { weight: .2, polarity: 'negative' },
  } };
  const event = (id, title, signals, extra = {}) => ({
    ...source.events[0], id, title, score: 50,
    start: `${day}T12:00:00-04:00`, end: `${day}T23:00:00-04:00`,
    scoring: { signals }, ...extra,
  });
  const fixtures = [
    // High-rated multi-day events participate in the same fitted order.
    event('art', 'Art fixture', { artsy: 1 }, { end: `${nextDay}T23:00:00-04:00` }),
    event('neutral', 'Neutral fixture', { artsy: .1, sporting: .1 }),
    event('sport', 'Sport fixture', { sporting: 1 }),
  ];
  const errors = [];
  const key = 'daily-brief:weights:v2';
  async function setup(events = fixtures, denied = false) {
    const context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } });
    await context.route(/\/events.json(?:\?|$)/, r => r.fulfill({ json: { ...source, events } }));
    await context.route(/\/scoring-weights.json(?:\?|$)/, r => r.fulfill({ json: config }));
    if (denied) await context.addInitScript(() => Object.defineProperty(window, 'localStorage', { get() { throw Error('denied'); } }));
    const page = await context.newPage();
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(`${url}/scoring.html`);
    await page.waitForFunction(() => document.querySelector('#status').textContent.includes('events loaded'));
    return { context, page };
  }
  const { context, page } = await setup();
  const ids = () => page.locator('.score-event').evaluateAll(nodes => nodes.map(n => n.dataset.id));
  const artsy = page.getByRole('spinbutton', { name: 'artsy weight', exact: true });
  const suggest = page.getByRole('button', { name: 'Suggest weights', exact: true });
  const apply = page.getByRole('button', { name: 'Apply suggestion to preview', exact: true });
  const clear = page.getByRole('button', { name: 'Clear requested order', exact: true });
  const undo = page.getByRole('button', { name: 'Undo applied suggestion', exact: true });
  const sportUp = page.getByRole('button', { name: 'Move Sport fixture up', exact: true });
  try {
    assert.deepEqual(await ids(), ['art', 'neutral', 'sport']);
    assert.ok(await suggest.isDisabled());
    assert.ok(await page.getByRole('button', { name: 'Move Art fixture up', exact: true }).isDisabled());
    assert.ok(await page.getByRole('button', { name: 'Move Sport fixture down', exact: true }).isDisabled());
    await page.locator('[data-id="sport"] .score-event__breakdown summary').click();
    await sportUp.click();
    assert.deepEqual(await ids(), ['art', 'sport', 'neutral']);
    assert.equal(await page.locator('#sort-select').inputValue(), 'requested');
    assert.ok(await page.locator('[data-id="sport"] details').evaluate(n => n.open));
    await page.getByRole('button', { name: 'Move Sport fixture down', exact: true }).click();
    assert.deepEqual(await ids(), ['art', 'neutral', 'sport']);
    await sportUp.click();
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), 'Move Sport fixture up');
    await page.keyboard.press('Enter');
    assert.deepEqual(await ids(), ['sport', 'art', 'neutral']);
    assert.equal(await artsy.inputValue(), '0.4', 'rank moves do not change weights');
    await suggest.click();
    assert.match(await page.locator('#suggestion-summary').textContent(), /Requested order matched.*3 of 3/);
    assert.ok(await page.locator('#weight-suggestion').isVisible());
    assert.equal(await page.evaluate(key => localStorage.getItem(key), key), null);
    assert.equal(await artsy.inputValue(), '0.4', 'suggestions do not change weights');
    await page.getByRole('button', { name: 'Discard suggestion', exact: true }).click();
    assert.ok(await page.locator('#weight-suggestion').isHidden());
    assert.deepEqual(await ids(), ['sport', 'art', 'neutral']);
    await suggest.click();
    await apply.click();
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { value: { async writeText(text) { globalThis.__copiedWeights = text; } }, configurable: true }));
    await page.getByRole('button', { name: 'Copy weights JSON', exact: true }).click();
    const copied = await page.evaluate(() => JSON.parse(globalThis.__copiedWeights));
    assert.equal(copied.base, config.base);
    assert.equal(copied.rulesVersion, config.rulesVersion);
    assert.notDeepEqual(copied.signals, config.signals, 'copy exports applied weights');
    assert.equal(await page.locator('#sort-select').inputValue(), 'rating');
    assert.deepEqual(await ids(), ['sport', 'art', 'neutral']);
    assert.ok(await undo.isEnabled());
    assert.equal(await page.evaluate(key => localStorage.getItem(key), key), null, 'applying only changes the preview');
    await undo.click();
    assert.deepEqual(await ids(), ['art', 'neutral', 'sport']);
    assert.equal(await artsy.inputValue(), '0.4');
    await sportUp.click();
    await sportUp.click();
    await suggest.click();
    await apply.click();
    await page.getByRole('button', { name: 'Save weights on this browser', exact: true }).click();
    assert.match(await page.locator('#status').textContent(), /Saved on this browser/);
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#status').textContent.includes('saved browser weights'));
    assert.deepEqual(await ids(), ['sport', 'art', 'neutral']);
    await page.getByRole('button', { name: 'Clear saved weights', exact: true }).click();
    assert.deepEqual(await ids(), ['art', 'neutral', 'sport']);
    await sportUp.click();
    await suggest.click();
    await artsy.fill('0.5');
    await page.waitForFunction(() => document.querySelector('#weight-suggestion').hidden);
    assert.deepEqual(await ids(), ['art', 'sport', 'neutral'], 'manual edits retain the requested order');
    await suggest.click();
    assert.ok(await page.locator('#weight-suggestion').isVisible());
    await clear.click();
    assert.deepEqual(await ids(), ['art', 'neutral', 'sport']);
    await sportUp.click();
    await page.locator('#sort-select').selectOption('published');
    assert.ok(await suggest.isDisabled(), 'changing sort clears the request');
    await sportUp.click();
    await page.locator('#day-input').fill(nextDay);
    await page.locator('#day-input').dispatchEvent('change');
    assert.equal(await page.locator('.score-event').count(), 1);
    assert.ok(await clear.isDisabled(), 'changing day clears the request');
    await page.locator('#day-input').fill(day);
    await page.locator('#day-input').dispatchEvent('change');
    await sportUp.click();
    await page.getByRole('button', { name: 'Reset weights', exact: true }).click();
    assert.deepEqual(await ids(), ['art', 'neutral', 'sport']);
    assert.ok(await clear.isDisabled());
    await sportUp.click();
    await suggest.click();
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `scoring page fits ${width}`);
      const bounds = await page.locator('[data-id="sport"] .score-event__moves button').evaluateAll(nodes => nodes.map(n => ({ width: n.clientWidth, height: n.clientHeight })));
      assert.ok(bounds.every(b => b.width >= 42 && b.height >= 42), 'move controls have touch targets');
      if (process.env.BRIEF_TUNING_SCREENSHOTS) await page.screenshot({ path: `${process.env.BRIEF_TUNING_SCREENSHOTS}/scoring-${width}.png` });
    }
    // Imported editions reset drafts and discover previously unseen signals.
    const imported = { ...source, editionDate: nextDay, events: [
      event('new', 'New signal fixture', { brand_new: 1 }, { start: `${nextDay}T12:00:00-04:00`, end: `${nextDay}T23:00:00-04:00` }),
      event('other', 'Other fixture', { artsy: 1 }, { start: `${nextDay}T12:00:00-04:00`, end: `${nextDay}T23:00:00-04:00` }),
    ] };
    await page.locator('#edition-file').setInputFiles({ name: 'edition.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(imported)) });
    await page.waitForFunction(() => document.querySelector('#status').textContent.includes('Saved edition loaded'));
    assert.ok(await clear.isDisabled());
    assert.equal(await page.getByRole('spinbutton', { name: 'brand new weight', exact: true }).inputValue(), '0');
    await page.getByRole('button', { name: 'Move New signal fixture up', exact: true }).click();
    await suggest.click();
    assert.match(await page.locator('#suggestion-summary').textContent(), /1 of 1/);
    await context.close();

    const tied = await setup([event('a', 'Twin A', { artsy: 1 }), event('b', 'Twin B', { artsy: 1 })]);
    await tied.page.getByRole('button', { name: 'Move Twin B up', exact: true }).click();
    await tied.page.getByRole('button', { name: 'Suggest weights', exact: true }).click();
    assert.match(await tied.page.locator('#suggestion-summary').textContent(), /could not be matched.*0 of 1.*1 tied/);
    assert.ok(await tied.page.getByRole('button', { name: 'Apply suggestion to preview', exact: true }).isDisabled());
    await tied.context.close();

    const missing = await setup([fixtures[0], event('missing', 'Missing signals fixture', null)]);
    assert.match(await missing.page.locator('#ranking-status').textContent(), /no scoring signals/);
    await missing.page.getByRole('button', { name: 'Move Missing signals fixture up', exact: true }).click();
    assert.ok(await missing.page.getByRole('button', { name: 'Suggest weights', exact: true }).isDisabled());
    await missing.context.close();

    const denied = await setup(fixtures, true);
    await denied.page.getByRole('button', { name: 'Move Sport fixture up', exact: true }).click();
    await denied.page.getByRole('button', { name: 'Suggest weights', exact: true }).click();
    await denied.page.getByRole('button', { name: 'Apply suggestion to preview', exact: true }).click();
    await denied.page.getByRole('button', { name: 'Save weights on this browser', exact: true }).click();
    assert.match(await denied.page.locator('#status').textContent(), /storage unavailable/);
    await denied.context.close();

    // Full signal names and real titles exercise widths that small fixtures miss.
    const live = await browser.newContext({ serviceWorkers: 'block' });
    try {
      const realPage = await live.newPage();
      realPage.on('pageerror', e => errors.push(e.message));
      await realPage.goto(`${url}/scoring.html`);
      await realPage.waitForFunction(() => document.querySelector('#status').textContent.includes('events loaded'));
      if (await realPage.locator('.score-event').count() > 1) {
        await realPage.locator('.score-event').nth(1).locator('[data-move-offset="-1"]').click();
        const realSuggest = realPage.getByRole('button', { name: 'Suggest weights', exact: true });
        if (await realSuggest.isEnabled()) await realSuggest.click();
      }
      for (const width of [1440, 390, 320]) {
        await realPage.setViewportSize({ width, height: 900 });
        assert.ok(await realPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `real scoring data fits ${width}`);
      }
      // Wider font metrics expose unbroken venue URLs even on macOS, whose
      // default system font can fit a URL that overflows the Linux CI browser.
      await realPage.evaluate(() => document.documentElement.style.setProperty('--sans', 'Verdana, sans-serif'));
      assert.ok(await realPage.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'real scoring data fits 320 with wider fonts');
    } finally { await live.close(); }
    assert.deepEqual(errors, []);
    console.log('Scoring debug: requested ordering, keyboard moves, suggestion review/apply/undo, save/reload, invalidation, imports, ties, missing signals, mobile layout, and denied storage passed');
  } finally { await context.close(); }
};

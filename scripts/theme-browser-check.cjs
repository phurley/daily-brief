const { strict: assert } = require('node:assert');
const engines = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.BRIEF_TEST_URL || 'http://localhost:4173';
const key = 'daily-brief:theme:v1';
const themes = ['original', 'newspaper', 'infographic', 'star-trek', 'redwood', 'michigan'];
(async () => {
  const browser = await engines[process.env.BRIEF_BROWSER || 'chromium'].launch();
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'reduce' });
    await context.route(/icanhazdadjoke|rocketlaunch/, route => route.abort());
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.waitForFunction(() => document.querySelector('#geek-section').getAttribute('aria-busy') === 'false');
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'original');
    assert.deepEqual(await page.locator('#theme-select option').evaluateAll(options => options.map(o => o.value)), themes);
    const colors = new Set();
    for (const theme of themes) {
      await page.locator('#theme-select').selectOption(theme);
      assert.equal(await page.evaluate(key => localStorage.getItem(key), key), theme);
      await page.reload();
      assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
      assert.equal(await page.locator('#theme-select').inputValue(), theme);
      assert.equal(await page.locator('.theme-signature').isVisible(), theme !== 'original');
      colors.add(await page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--paper').trim()));
      for (const width of [1440, 768, 390, 320]) {
        await page.setViewportSize({ width, height: 1000 });
        const dimensions = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: innerWidth }));
        assert.ok(dimensions.scroll <= dimensions.width + 1, `${theme} overflows at ${width}px`);
        const box = await page.locator('#theme-select').boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width && box.height >= 44, `${theme} picker fits and is touch accessible`);
        if (process.env.BRIEF_THEME_SCREENSHOTS && [1440, 390].includes(width)) {
          await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
          await page.screenshot({ path: `${process.env.BRIEF_THEME_SCREENSHOTS}/${theme}-${width}.png` });
        }
      }
      if (theme !== 'original') {
        const before = await page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--paper'));
        await page.evaluate(() => Object.assign(document.documentElement.dataset, { skyPhase: 'night', weather: 'rain', season: 'winter' }));
        assert.equal(await page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--paper')), before, `${theme} survives live weather updates`);
      }
      await page.locator('[data-calendar-open]').click();
      assert.ok(await page.locator('#calendar-dialog').isVisible());
      await page.keyboard.press('Escape');
      console.log(`${theme}: saved, restored, responsive, calendar usable`);
    }
    assert.equal(colors.size, 6, 'all palettes are distinct');
    await page.evaluate(() => navigator.serviceWorker.ready);
    await page.reload();
    await context.setOffline(true);
    await page.locator('#theme-select').selectOption('redwood');
    await page.reload();
    assert.equal(await page.locator('#theme-select').inputValue(), 'redwood');
    assert.equal(await page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--paper').trim()), '#eeeee2');
    await context.setOffline(false);
    console.log('Offline theme switch and restoration passed');
    const other = await context.newPage();
    await other.goto(url);
    await page.locator('#theme-select').selectOption('redwood');
    await other.waitForFunction(() => document.documentElement.dataset.theme === 'redwood');
    await page.evaluate(key => localStorage.removeItem(key), key);
    await other.waitForFunction(() => document.documentElement.dataset.theme === 'original');
    await page.evaluate(key => localStorage.setItem(key, 'invalid-theme'), key);
    await page.reload();
    assert.equal(await page.locator('#theme-select').inputValue(), 'original');
    // The synchronous head script restores even while application modules are stalled.
    await page.evaluate(key => localStorage.setItem(key, 'michigan'), key);
    await page.route(/app\.js/, route => route.abort());
    await page.reload({ waitUntil: 'domcontentloaded' });
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'michigan');
    assert.deepEqual(errors, []);
    const blocked = await browser.newContext({ serviceWorkers: 'block' });
    await blocked.route(/icanhazdadjoke|rocketlaunch/, route => route.abort());
    await blocked.addInitScript(() => Object.defineProperty(window, 'localStorage', { get() { throw new Error('Storage denied'); } }));
    const blockedPage = await blocked.newPage();
    await blockedPage.goto(url);
    await blockedPage.locator('#theme-select').selectOption('star-trek');
    assert.equal(await blockedPage.locator('html').getAttribute('data-theme'), 'star-trek');
    assert.match(await blockedPage.locator('#theme-status').textContent(), /for this visit/);
    console.log('Cross-tab sync, invalid preference, early restoration, and unavailable storage passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });

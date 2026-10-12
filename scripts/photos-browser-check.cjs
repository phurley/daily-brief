const engines = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const testURL = process.env.BRIEF_TEST_URL || 'http://localhost:4173';

(async () => {
  const browser = await engines[process.env.BRIEF_BROWSER || 'chromium'].launch();
  try {
    const manifest = await (await fetch(new URL('brief-manifest.json', testURL))).json();
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce', serviceWorkers: 'block' });
    await context.route(/icanhazdadjoke|rocketlaunch/, route => route.abort());
    const base = { imageUrl: 'https://photos.smugmug.com/test-landscape.jpg', takenDate: '2001-09-20', location: '', description: 'Landscape test photo', width: 1280, height: 853 };
    const candidates = Array.from({ length: 67 }, (_, index) => ({ ...base, id: `landscape-${index}`, description: `Landscape ${index}` }));
    candidates.push({ ...base, id: 'portrait', width: 853, height: 1280 }, { ...base, id: 'square', width: 853, height: 853 }, { ...base, id: 'unknown', width: undefined, height: undefined });
    await context.route('**/photos.json', route => route.fulfill({ json: {
      schemaVersion: '1.0.0', generatedAt: manifest.generatedAt, timeZone: 'America/Detroit',
      collectionStart: manifest.editionDate, collectionEnd: manifest.editionDate,
      days: [{ date: manifest.editionDate, photos: candidates }],
    } }));
    await context.route(base.imageUrl, route => route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="853"><rect width="1280" height="853" fill="#347066"/></svg>' }));
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.clock.setFixedTime(new Date(manifest.generatedAt));
    await page.goto(testURL);
    await page.locator('#events-all-summary').scrollIntoViewIfNeeded();
    await page.locator('#photo-section').waitFor({ state: 'visible' });
    await page.waitForFunction(() => document.querySelector('.memory__dot[aria-current="true"]'));
    assert.equal(await page.locator('.memory__slide').count(), 67, 'portrait, square and unknown dimensions are excluded');
    assert.equal(await page.locator('.memory__slide img[src]').count(), 1, 'photos remain lazy');
    assert.equal(await page.locator('.is-active .memory__date').innerText(), '20 Sept 2001');
    for (const width of [1280, 620, 430, 390, 320]) {
      await page.setViewportSize({ width, height: 800 });
      await page.waitForTimeout(80);
      const layout = await page.evaluate(() => {
        const frame = document.querySelector('#photo-section').getBoundingClientRect();
        const controls = document.querySelector('#photo-controls').getBoundingClientRect();
        const date = document.querySelector('.is-active .memory__date').getBoundingClientRect();
        return { frame: frame.toJSON(), controls: controls.toJSON(), date: date.toJSON(), dots: document.querySelectorAll('.memory__dot').length };
      });
      assert.ok(layout.dots >= 1 && layout.dots <= 9, 'bounded dot window');
      for (const rect of [layout.controls, layout.date]) {
        assert.ok(rect.left >= layout.frame.left && rect.right <= layout.frame.right, `controls and date fit at ${width}px`);
        assert.ok(rect.bottom <= layout.frame.bottom, 'footer stays inside photo');
      }
      assert.ok(layout.date.left >= layout.controls.right || layout.date.top >= layout.controls.bottom, 'date and dots do not overlap');
      assert.ok(layout.frame.right - layout.date.right < 24, 'date is at bottom right');
      console.log(`Photo footer fits ${width}px with ${layout.dots} dots`);
    }
    await page.evaluate(() => { document.documentElement.style.fontSize = '32px'; });
    await page.waitForTimeout(80);
    assert.ok(await page.locator('#photo-controls').evaluate(element => {
      const frame = document.querySelector('#photo-section').getBoundingClientRect();
      const controls = element.getBoundingClientRect();
      return controls.left >= frame.left && controls.right <= frame.right;
    }), 'dots fit at 320px with enlarged text');
    await page.evaluate(() => { document.documentElement.style.fontSize = ''; });
    await page.setViewportSize({ width: 1280, height: 800 });
    for (let index = 0; index < 66; index++) await page.locator('#photo-next').click();
    assert.equal(await page.locator('.memory__dot[aria-current="true"]').getAttribute('data-photo-index'), '66', 'arrows reach the final photo');
    assert.ok(Number(await page.locator('.memory__dot').first().getAttribute('data-photo-index')) > 0, 'dot window follows navigation');
    await page.locator('#photo-next').click();
    assert.equal(await page.locator('.memory__dot[aria-current="true"]').getAttribute('data-photo-index'), '0', 'last photo wraps to first');
    await page.locator('#photo-previous').click();
    assert.equal(await page.locator('.memory__dot[aria-current="true"]').getAttribute('data-photo-index'), '66', 'previous wraps back');
    await page.locator('.memory__dot').first().focus();
    await page.locator('.memory__dot').first().press('Enter');
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-current')), 'true', 'dot focus survives window movement');
    assert.deepEqual(errors, []);
    console.log('Photo filtering, date, lazy images, full navigation and responsive dots passed');
    await context.close();
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });

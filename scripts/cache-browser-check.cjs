// Exercise production-style caching with old clients still open. This includes
// migration from the pre-update worker, which has no reload message listener.
const fs = require('node:fs/promises'), path = require('node:path'), os = require('node:os');
const http = require('node:http'), assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const engines = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const root = path.resolve(__dirname, '..');
  const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'brief-cache-'));
  let browser, server, failedAsset, failedDownload;
  try {
    for (const file of await fs.readdir(root, {withFileTypes: true})) {
      if (['.git', 'node_modules', 'scripts'].includes(file.name)) continue;
      if (file.isDirectory()) await fs.symlink(path.join(root, file.name), path.join(temp, file.name), 'dir');
      else await fs.copyFile(path.join(root, file.name), path.join(temp, file.name));
    }
    await fs.mkdir(path.join(temp, 'scripts'));
    const template = await fs.readFile(path.join(root, 'scripts/sw-template.js'), 'utf8');
    const legacy = await fs.readFile(path.join(root, 'scripts/fixtures/legacy-sw.js'), 'utf8');
    const app = await fs.readFile(path.join(root, 'app.js'), 'utf8');
    const {buildShell} = await import(pathToFileURL(path.join(root, 'scripts/build_shell.mjs')));
    async function deploy(version, worker = template, code = app) {
      await fs.writeFile(path.join(temp, 'scripts/sw-template.js'), worker);
      await fs.writeFile(path.join(temp, 'app.js'), code + `\nglobalThis.__briefUpgrade=${version};\n`);
      await buildShell(temp);
    }
    const manifest = JSON.parse(await fs.readFile(path.join(root, 'brief-manifest.json')));
    const preferences = JSON.parse(await fs.readFile(path.join(root, 'brief-preferences.json')));
    preferences.topicAffinities.comedy = 12;
    const weights = JSON.parse(await fs.readFile(path.join(root, 'scoring-weights.json')));
    const stored = {
      'daily-brief:preferences:v1:household': JSON.stringify({current: preferences, history: []}),
      'daily-brief:weights:v2': JSON.stringify(weights),
      'daily-brief:theme:v1': 'newspaper',
    };
    const tomorrow = new Date(Date.parse(manifest.editionDate + 'T12:00:00Z') + 86400000).toISOString().slice(0, 10);
    const errors = [];
    server = http.createServer(async (req, res) => {
      try {
        const request = new URL(req.url, 'http://localhost');
        const name = decodeURIComponent(request.pathname).replace(/^\//, '') || 'index.html';
        if (name === failedAsset && request.searchParams.has('shell')) {
          failedDownload?.();
          throw Error('Simulated failed download');
        }
        const file = path.resolve(temp, name);
        if (!file.startsWith(temp + path.sep)) throw Error('path');
        const body = await fs.readFile(file);
        const type = {'.js': 'text/javascript', '.mjs': 'text/javascript', '.html': 'text/html', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png'}[path.extname(file)];
        res.writeHead(200, {'Content-Type': type || 'application/octet-stream', 'Cache-Control': name === 'sw.js' || name.endsWith('.json') ? 'no-cache' : 'public, max-age=3600'});
        res.end(body);
      } catch { res.writeHead(404); res.end(); }
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const url = `http://127.0.0.1:${server.address().port}/`;
    browser = await engines[process.env.BRIEF_BROWSER || 'chromium'].launch();
    async function context() {
      const c = await browser.newContext({viewport: {width: 390, height: 844}});
      await c.route(/icanhazdadjoke|rocketlaunch/, route => route.abort());
      return c;
    }
    async function page(c, address = url) {
      const p = await c.newPage();
      await p.clock.setFixedTime(new Date(manifest.generatedAt));
      p.on('pageerror', error => errors.push(error.message));
      await p.goto(address);
      return p;
    }
    async function version(p, expected) {
      await p.waitForFunction(v => globalThis.__briefUpgrade === v, expected, {timeout: 20000});
    }
    async function ready(p) {
      await p.waitForFunction(() => localStorage.getItem('daily-brief-edition:v1') && document.querySelector('#recommendation-controls') && !document.querySelector('#refresh-brief').disabled);
    }
    async function samePreferences(p) {
      assert.deepEqual(await p.evaluate(keys => Object.fromEntries(keys.map(key => [key, localStorage.getItem(key)])), Object.keys(stored)), stored);
    }

    // A real legacy cache-first worker and a page that knows nothing about the
    // new handshake must upgrade without closing either open tab.
    await deploy(1, legacy, "document.querySelector('#page-title').textContent='Legacy cached report'; navigator.serviceWorker.register('./sw.js',{updateViaCache:'none'});");
    const old = await context();
    const p = await page(old), q = await page(old, url + '?date=' + tomorrow);
    await p.evaluate(values => { for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value); }, stored);
    await p.evaluate(() => navigator.serviceWorker.ready);
    await p.reload(); await q.reload();
    await version(p, 1); await version(q, 1);
    await deploy(2);
    // Registration/update on an old page visit discovers the new worker.
    await p.reload();
    await version(p, 2); await version(q, 2);
    await ready(p); await ready(q); await samePreferences(p);
    assert.equal(new URL(q.url()).searchParams.get('date'), tomorrow);
    assert.notEqual(await p.locator('#page-title').innerText(), 'Legacy cached report');
    const picks = await p.locator('#events-list > article').count();
    assert.ok(picks > 0 && picks <= 3);
    assert.ok(!(await p.locator('#events-list').innerText()).includes('Price unknown'));
    console.log('Legacy cached pages upgrade together; reviewed picks, date, preferences, weights and theme survive');

    // Refresh itself must discover the next code deployment, preserving edits
    // that have not been submitted to the preference store yet.
    await p.locator('#recommendation-controls summary').click();
    await p.getByLabel('Admission budget per person ($, lowest listed ticket):').fill('25');
    await p.getByLabel('Require a known admission price').check();
    await deploy(3);
    await p.locator('#refresh-brief').click();
    await version(p, 3); await version(q, 3); await ready(p);
    assert.ok(await p.getByLabel('Admission budget per person ($, lowest listed ticket):').isVisible());
    assert.equal(await p.getByLabel('Admission budget per person ($, lowest listed ticket):').inputValue(), '25');
    assert.ok(await p.getByLabel('Require a known admission price').isChecked());
    await samePreferences(p);
    console.log('Refresh updates code and data without closing tabs or losing preference drafts');

    // An incomplete shell must never take over the working report.
    failedAsset = 'shell-updates.mjs';
    await deploy(4);
    let failureTimer;
    const downloaded = new Promise((resolve, reject) => {
      failedDownload = resolve;
      failureTimer = setTimeout(() => reject(Error('New worker did not download the failing asset')), 20000);
    });
    await p.locator('#refresh-brief').click();
    try { await downloaded; } finally { clearTimeout(failureTimer); failedDownload = null; }
    await p.waitForFunction(async () => {
      const worker = (await navigator.serviceWorker.getRegistration()).installing;
      if (!worker) return true;
      return new Promise(resolve => worker.addEventListener('statechange', () => { if (worker.state === 'redundant') resolve(true); }));
    });
    await p.waitForFunction(() => !document.querySelector('#refresh-brief').disabled);
    assert.equal(await p.evaluate(() => globalThis.__briefUpgrade), 3);
    assert.equal(await q.evaluate(() => globalThis.__briefUpgrade), 3);
    assert.equal(await p.getByLabel('Admission budget per person ($, lowest listed ticket):').inputValue(), '25');
    failedAsset = null;
    await p.locator('#refresh-brief').click();
    await version(p, 4); await version(q, 4); await ready(p);
    await samePreferences(p);
    console.log('A failed shell download keeps the working version; Refresh recovers when downloads succeed');

    // A background tab defers data refresh until it returns to the foreground.
    // Its draft must restore on that refresh as well as on initial loading.
    await ready(q);
    await q.locator('#recommendation-controls summary').click();
    await q.getByLabel('Maximum miles (unknown distances excluded when set):').fill('12');
    await q.addInitScript(() => Object.defineProperty(document, 'hidden', {configurable: true, value: true}));
    // Cache eviction must not hide the fact that an older renderer is open.
    await p.evaluate(async () => {
      for (const key of await caches.keys()) if (key.startsWith('daily-brief-shell-')) await caches.delete(key);
    });
    await deploy(5);
    await p.locator('#refresh-brief').click();
    await version(p, 5); await version(q, 5); await ready(p);
    await q.evaluate(() => {
      Object.defineProperty(document, 'hidden', {configurable: true, value: false});
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await ready(q);
    assert.equal(await q.getByLabel('Maximum miles (unknown distances excluded when set):').inputValue(), '12');
    assert.ok(await q.getByLabel('Maximum miles (unknown distances excluded when set):').isVisible());
    await samePreferences(p);
    console.log('Open reports upgrade after shell eviction; background-tab drafts restore on foreground refresh');

    await old.setOffline(true); await p.reload(); await version(p, 5);
    await p.waitForFunction(() => document.querySelector('#live-status').textContent.includes('Saved brief'));
    await samePreferences(p);
    assert.ok(await p.locator('#events-section .event-details-button').count() > 0);
    await old.close();
    assert.deepEqual(errors, []);
    console.log('Updated shell and saved report reopen offline');
  } finally {
    await browser?.close();
    await new Promise(resolve => server ? server.close(resolve) : resolve());
    await fs.rm(temp, {recursive: true, force: true});
  }
})().catch(error => { console.error(error); process.exit(1); });

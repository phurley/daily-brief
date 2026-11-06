// Playwright with each browser context's clock starting at BRIEF_CLOCK (ISO time).
// CI runs the browser checks against a pinned data snapshot
// (scripts/pin_browser_data.sh); the clock must match that snapshot or events
// age out and checks fail on some days and not others. Time still flows
// normally after the start, so timers and waits behave as in production.
// Use: PLAYWRIGHT_MODULE=$PWD/scripts/pinned-playwright.cjs BRIEF_CLOCK=... node scripts/x-browser-check.cjs
const playwright = require('playwright');
const start = process.env.BRIEF_CLOCK && new Date(process.env.BRIEF_CLOCK);
if (process.env.BRIEF_CLOCK && Number.isNaN(start.getTime())) throw Error(`Bad BRIEF_CLOCK: ${process.env.BRIEF_CLOCK}`);

async function pin(context) {
  await context.clock.install({time: start});
  await context.clock.resume();
  return context;
}

function wrap(browser) {
  const newContext = browser.newContext.bind(browser), newPage = browser.newPage.bind(browser);
  browser.newContext = async (...args) => pin(await newContext(...args));
  browser.newPage = async (...args) => {
    const page = await newPage(...args);
    await pin(page.context());
    return page;
  };
  return browser;
}

module.exports = start ? new Proxy(playwright, {
  get(target, name) {
    const engine = target[name];
    if (!['chromium', 'firefox', 'webkit'].includes(name)) return engine;
    return new Proxy(engine, {
      get(type, key) {
        const value = type[key];
        if (key === 'launch') return async (...args) => wrap(await value.apply(type, args));
        if (key === 'launchPersistentContext') return async (...args) => pin(await value.apply(type, args));
        return typeof value === 'function' ? value.bind(type) : value;
      },
    });
  },
}) : playwright;

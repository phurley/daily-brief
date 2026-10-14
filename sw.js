// Generated at deployment from every shell asset. Complete the new cache before
// activation, then reload open reports together so code and data stay coherent.
const SHELL='daily-brief-shell-bf1ab5271dc8229d2982ed0614407ba541e09b9cc6899257f952a00e1b369720';
const DATA='daily-brief-sections-v1';
const SHELL_FILES=["almanac-calc.mjs","app.js","assets/moon-waxing-gibbous.png","assets/redwood-canopy.svg","aurora.mjs","brief-selection.mjs","calendar-tuning.css","calendar-tuning.js","edition-client.mjs","event-experiences.mjs","event-status.mjs","event-time.mjs","favicon.svg","featured-events.mjs","index.html","news-ranking.mjs","on-this-date.mjs","photo-selection.mjs","preferences.mjs","ranking.mjs","report-pagination.mjs","science.mjs","scoring-debug.css","scoring-debug.js","scoring.html","scoring.mjs","shell-updates.mjs","sky-events.mjs","starship.mjs","story-editorial.mjs","story-order.mjs","theme.css","themes.css","themes.js","weather-appearance.mjs","weight-tuning.mjs"];
const UPDATE_MARKER=new URL('__shell_update__',self.registration.scope).href;
// Use per-shell download URLs to avoid reusing assets from earlier builds.
// Normal page requests match these entries by path.
self.addEventListener('install',event=>event.waitUntil((async()=>{
 const cache=await caches.open(SHELL);
 await cache.addAll(SHELL_FILES.map(path=>{
 const url=new URL(path,self.registration.scope);url.searchParams.set('shell',SHELL);
 return new Request(url,{cache:'reload'});
 }));
 // Persist this across worker restarts, including eviction of the old cache.
 if(self.registration.active) await cache.put(UPDATE_MARKER,new Response('1'));
 await self.skipWaiting();
})()));

async function reloadReport(client) {
 const url=new URL(client.url),base=new URL(self.registration.scope);
 if(url.origin!==base.origin || !['', 'index.html'].includes(url.pathname.slice(base.pathname.length))) return;
 // Current pages save open preference edits. Older pages cannot acknowledge,
 // so the bounded fallback also upgrades a browser pinned to a legacy shell.
 const channel=new MessageChannel();
 await new Promise(resolve=>{
  const timer=setTimeout(resolve,1000);
  channel.port1.onmessage=()=>{clearTimeout(timer);resolve();};
  client.postMessage({type:'BRIEF_SHELL_RELOAD'},[channel.port2]);
 });
 channel.port1.close();
 const current=await self.clients.get(client.id);
 // Do not await navigation inside activation: its fetch waits for activation.
 if(current?.url===client.url) void current.navigate(client.url).catch(()=>{});
}
self.addEventListener('activate',event=>event.waitUntil((async()=>{
 const keys=await caches.keys();
 const upgrading=Boolean(await (await caches.open(SHELL)).match(UPDATE_MARKER));
 await self.clients.claim();
 if(upgrading) await Promise.allSettled((await self.clients.matchAll({type:'window'})).map(reloadReport));
 for(const key of keys) if(key.startsWith('daily-brief-') && key!==SHELL && key!==DATA) await caches.delete(key);
})()));
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url);const base=new URL(self.registration.scope);
 if(event.request.method!=='GET' || url.origin!==base.origin || !url.pathname.startsWith(base.pathname)) return;
 const relative=url.pathname.slice(base.pathname.length);
 // Manifest remains network-only: a cache hit is never called a successful check.
 if(relative==='brief-manifest.json' || relative==='widget-events.json') return;
 if(event.request.mode==='navigate' && (relative==='' || relative==='index.html')) {
  event.respondWith(caches.open(SHELL).then(cache=>cache.match(new URL('index.html',base).href,{ignoreSearch:true})));return;
 }
 if(SHELL_FILES.some(path=>path.split('?')[0]===relative)) {
  event.respondWith(caches.open(SHELL).then(async cache=>(await cache.match(event.request,{ignoreSearch:true})) || fetch(event.request)));return;
 }
 if(/^editions\/[a-z]+-[a-f0-9]{64}\.json$/.test(relative)) {
  event.respondWith((async()=>{
   const cache=await caches.open(DATA);const saved=await cache.match(event.request);if(saved)return saved;
   const response=await fetch(event.request);if(response.ok) {
    await cache.put(event.request,response.clone());const keys=await cache.keys();
    for(const key of keys.slice(0,Math.max(0,keys.length-32))) await cache.delete(key);
   }return response;
  })());
 }
});

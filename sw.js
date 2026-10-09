// Bump with shell/contract changes. A waiting worker activates on the next launch;
// never swap code beneath an already-open edition.
const SHELL='daily-brief-shell-v1-20261009-rotation-2';
const DATA='daily-brief-sections-v1';
const SHELL_FILES=['./','index.html','theme.css?v=20261009-5','app.js?v=20261009-rotation-2','brief-selection.mjs','edition-client.mjs','almanac-calc.mjs?v=20260920-1','weather-appearance.mjs?v=20260830-1','event-time.mjs?v=20260908-1','story-order.mjs?v=20261009-1','event-status.mjs?v=20261009-1','starship.mjs?v=20261009-1','science.mjs?v=20261009-rotation-1','news-ranking.mjs?v=20261009-2','ranking.mjs?v=20261009-2','preferences.mjs?v=20261009-2','scoring.mjs?v=20261009-2','favicon.svg','assets/moon-waxing-gibbous.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(SHELL).then(cache=>cache.addAll(SHELL_FILES))));
self.addEventListener('activate',event=>event.waitUntil((async()=>{
 for(const key of await caches.keys()) if(key.startsWith('daily-brief-') && key!==SHELL && key!==DATA) await caches.delete(key);
 await self.clients.claim();
})()));
self.addEventListener('fetch',event=>{
 const url=new URL(event.request.url);const base=new URL(self.registration.scope);
 if(event.request.method!=='GET' || url.origin!==base.origin || !url.pathname.startsWith(base.pathname)) return;
 const relative=url.pathname.slice(base.pathname.length);
 // Manifest remains network-only: a cache hit is never called a successful check.
 if(relative==='brief-manifest.json' || relative==='widget-events.json') return;
 if(event.request.mode==='navigate' && (relative==='' || relative==='index.html')) {
  event.respondWith(caches.open(SHELL).then(cache=>cache.match(new URL('index.html',base).href)));return;
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

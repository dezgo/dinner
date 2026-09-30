// Dinner Tab service worker.
//
// Caches ONLY the static shell (CSS, JS, icons) so the organiser app opens
// fast. It never caches /api/*, pages, images of menus or receipts, or anything
// with bill or banking data — those always go to the network. If the network
// is down, pages show the browser's normal offline state rather than stale data.
const CACHE = "dinnertab-static-v2";

self.addEventListener("install", (e) => { self.skipWaiting(); });
self.addEventListener("activate", (e) => {
  e.waitUntil((async () => {
    for (const key of await caches.keys()) if (key !== CACHE) await caches.delete(key);
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (!url.pathname.startsWith("/static/")) return; // network only for everything else
  // Network first, so a deploy is picked up immediately; the cached copy is
  // only used when there's no connection.
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const res = await fetch(e.request);
      if (res.ok) cache.put(e.request, res.clone());
      return res;
    } catch (err) {
      const hit = await cache.match(e.request);
      if (hit) return hit;
      throw err;
    }
  })());
});

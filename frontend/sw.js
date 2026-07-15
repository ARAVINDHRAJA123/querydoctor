/* QueryDoctor service worker: cache the app shell for instant loads and
   offline opens; NEVER cache /api responses — checks stay in memory only. */

// Bump this on every deploy that changes any SHELL file — stale-while-revalidate
// otherwise leaves returning visitors on a mismatched mix of old/new assets.
const CACHE = "querydoctor-v7";
const SHELL = [".", "index.html", "style.css", "app.js", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", (e) => {
  // {cache: "reload"} bypasses the browser's own HTTP cache — a plain fetch()
  // here can silently seed a brand-new SW cache with stale assets if the
  // browser's disk cache still has an old copy (no explicit Cache-Control
  // headers are set on these static files, so browsers cache them heuristically).
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.all(SHELL.map((url) => fetch(url, { cache: "reload" }).then((res) => c.put(url, res)))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.pathname.includes("/api/")) return;

  // Stale-while-revalidate: serve cache fast, refresh in background.
  // The revalidation fetch also bypasses HTTP cache, for the same reason as install.
  e.respondWith(
    caches.match(e.request).then((cached) => {
      const fresh = fetch(e.request, { cache: "reload" })
        .then((res) => {
          if (res.ok && url.origin === location.origin) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return res;
        })
        .catch(() => cached);
      return cached || fresh;
    })
  );
});

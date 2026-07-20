/* QueryDoctor service worker: cache the app shell for instant loads and
   offline opens; NEVER cache /api responses — checks stay in memory only. */

// Bump this on every deploy that changes any SHELL file.
const CACHE = "querydoctor-v14";
const SHELL = [".", "index.html", "style.css", "app.js", "manifest.webmanifest", "fonts/SpaceGrotesk-Variable.woff2", "icons/icon-192.png", "icons/icon-512.png"];

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

/* Network-first: always try the network so a fresh deploy shows up on the
   very next load, not one load behind. Only same-origin GET app assets are
   handled here — third-party embeds (the Razorpay Checkout script, in
   particular) get opaque, sometimes-redirected responses this cache logic
   isn't built for; intercepting them risks the script intermittently
   failing to load. Let the browser fetch those natively instead. */
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;
  e.respondWith(
    fetch(e.request, { cache: "reload" }).then((res) => {
      if (res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
      }
      return res;
    }).catch(() => caches.match(e.request))
  );
});

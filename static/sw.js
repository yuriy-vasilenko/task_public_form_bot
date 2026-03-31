const CACHE_NAME = "task-form-pwa-v2";
const APP_SHELL = [
  "/",
  "/static/style.css",
  "/static/manifest.webmanifest",
  "/static/icon.svg",
  "/static/offline.html"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => (key !== CACHE_NAME ? caches.delete(key) : Promise.resolve()))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  const isStatic = url.pathname.startsWith("/static/");

  if (isStatic) {
    event.respondWith(
      caches.match(req).then((cached) => {
        if (cached) return cached;
        return fetch(req).then((networkResp) => {
          const copy = networkResp.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
          return networkResp;
        });
      })
    );
    return;
  }

  event.respondWith(
    fetch(req)
      .then((networkResp) => {
        const copy = networkResp.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(req, copy));
        return networkResp;
      })
      .catch(() => caches.match(req).then((cached) => cached || caches.match("/static/offline.html")))
  );
});

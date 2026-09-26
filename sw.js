// Lets the player open like an app and show the last-loaded list when there's
// no signal. Pages and text are fetched fresh when online and cached as a
// fallback; audio streams straight from storage.
const CACHE = "read-me-v1";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(
  caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE && /^read-(aloud|me)-/.test(k)).map(k => caches.delete(k))))
    .then(() => self.clients.claim())
));

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  const scope = new URL(self.registration.scope);
  if (request.method !== "GET" || url.origin !== scope.origin || !url.pathname.startsWith(scope.pathname)) return;
  if (url.pathname.endsWith(".mp3")) return;

  event.respondWith(
    fetch(request)
      .then(response => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(CACHE).then(cache => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request).then(hit => hit || Response.error()))
  );
});

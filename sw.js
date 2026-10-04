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

// Something shared to Read Me from the phone's share menu arrives here as a
// form post. Keep it for the page, which uploads it, then open the app.
const SHARE_CACHE = "read-me-share";
async function receiveShare(request, scope) {
  try {
    const form = await request.formData();
    const cache = await caches.open(SHARE_CACHE);
    await cache.put(new URL("shared-pending", scope).href, new Response(form));
  } catch {}
  return Response.redirect(new URL("index.html?shared=1", scope).href, 303);
}

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  const scope = new URL(self.registration.scope);
  if (request.method === "POST" && url.origin === scope.origin && url.pathname === scope.pathname + "share") {
    event.respondWith(receiveShare(request, scope));
    return;
  }
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

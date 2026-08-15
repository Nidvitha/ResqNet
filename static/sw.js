/*
 * ResQNet service worker.
 *
 * A service worker is a script the browser runs *between* the page and the
 * network. It can answer requests from a local cache when the network is gone,
 * which is what lets an officer open the inspection screen inside a disaster
 * zone with no signal.
 *
 * The caching strategy is deliberately split by request type, because getting
 * this wrong in a relief platform is dangerous:
 *
 *   * **App shell** (CSS, JS, key pages)  - cache first. These rarely change and
 *     must be instantly available offline.
 *   * **API GET requests**                - network first, falling back to the
 *     cache. Stale case data is better than nothing, but fresh data always wins.
 *     An officer must never act on a cached status when the real one is available.
 *   * **API writes (POST/PATCH)**         - never cached, never intercepted. The
 *     IndexedDB queue in offline.js owns those, because only it can attach an
 *     idempotency key and guarantee a replay does not create a duplicate case.
 */

const VERSION = "resqnet-v1";
const SHELL_CACHE = VERSION + "-shell";
const DATA_CACHE = VERSION + "-data";

const SHELL_ASSETS = [
  "/",
  "/offline/",
  "/static/css/resqnet.css",
  "/static/js/api.js",
  "/static/js/ui.js",
  "/static/js/offline.js",
  "/static/manifest.json",
  "/citizen/",
  "/citizen/report/",
  "/officer/",
];

/* ------------------------------------------------------------- Install */
self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      // addAll fails the whole install if any single URL 404s, which would leave
      // the user with no service worker at all. Add them individually instead.
      return Promise.all(
        SHELL_ASSETS.map(function (url) {
          return cache.add(url).catch(function () { return null; });
        })
      );
    }).then(function () { return self.skipWaiting(); })
  );
});

/* ------------------------------------------------------------ Activate */
self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(
        keys.filter(function (key) { return key.indexOf(VERSION) !== 0; })
            .map(function (key) { return caches.delete(key); })
      );
    }).then(function () { return self.clients.claim(); })
  );
});

/* --------------------------------------------------------------- Fetch */
self.addEventListener("fetch", function (event) {
  const request = event.request;
  const url = new URL(request.url);

  // Only handle same-origin traffic. Map tiles and anything else cross-origin
  // go straight to the network.
  if (url.origin !== self.location.origin) return;

  // Writes are the offline queue's responsibility, not the cache's.
  if (request.method !== "GET") return;

  // Never cache the admin site or authentication endpoints - serving a stale
  // authenticated response to the wrong person is a real risk.
  if (url.pathname.startsWith("/admin/") || url.pathname.startsWith("/api/auth/")) return;

  if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirst(request));
  } else {
    event.respondWith(cacheFirstWithRefresh(request));
  }
});

/**
 * Fresh data preferred; cached copy used only when the network fails.
 */
async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response && response.ok) {
      const cache = await caches.open(DATA_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(
      JSON.stringify({ detail: "You are offline and this data is not cached on this device." }),
      { status: 503, headers: { "Content-Type": "application/json" } }
    );
  }
}

/**
 * Instant from cache, refreshed in the background for next time. Navigations
 * that miss entirely fall back to the offline page.
 */
async function cacheFirstWithRefresh(request) {
  const cached = await caches.match(request);

  const networkFetch = fetch(request).then(async function (response) {
    if (response && response.ok) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  }).catch(function () { return null; });

  if (cached) return cached;

  const fresh = await networkFetch;
  if (fresh) return fresh;

  if (request.mode === "navigate") {
    const offlinePage = await caches.match("/offline/");
    if (offlinePage) return offlinePage;
  }
  return new Response("Offline", { status: 503, headers: { "Content-Type": "text/plain" } });
}

/* Let the page trigger an immediate activation after an update. */
self.addEventListener("message", function (event) {
  if (event.data === "skipWaiting") self.skipWaiting();
});

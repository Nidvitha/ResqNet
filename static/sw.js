/*
 * ResQNet service worker.
 *
 * A service worker sits between the page and the network and can answer from a
 * local cache when the network is gone — which is what lets an officer open the
 * inspection screen inside a disaster zone with no signal.
 *
 * ---------------------------------------------------------------------------
 * A bug worth remembering (fixed in v2)
 * ---------------------------------------------------------------------------
 * v1 used a cache-first strategy for navigations. Requesting `/citizen/` while
 * logged out returns a 302 to `/login/`; `fetch()` followed it and produced a
 * response with `redirected = true`. Returning that from `respondWith()` for a
 * navigation is illegal — Chrome fails the page with "a redirected response was
 * used for a request whose redirect mode is not 'follow'" — and because it was
 * also written to the cache, every later visit failed the same way. The portals
 * were unreachable until the cache was cleared.
 *
 * Two rules prevent it recurring, and both are enforced below:
 *
 *   1. Navigations are **network-first**, and a redirected response is passed
 *      straight back to the browser, never cached.
 *   2. Authenticated pages are **never** precached or stored. What one user sees
 *      must not be served to the next.
 */

const VERSION = "resqnet-v2";
const SHELL_CACHE = VERSION + "-shell";
const DATA_CACHE = VERSION + "-data";

/*
 * Only genuinely public, non-redirecting assets belong here.
 *
 * Note what is absent: /citizen/, /officer/ and /command/. Those are behind
 * @login_required, so precaching them caches a redirect to the login page — the
 * exact bug described above.
 */
const SHELL_ASSETS = [
  "/",
  "/offline/",
  "/static/css/resqnet.css",
  "/static/js/api.js",
  "/static/js/ui.js",
  "/static/js/offline.js",
  "/static/manifest.json",
];

/** Paths whose HTML must never be cached — they differ per signed-in user. */
const PRIVATE_PREFIXES = ["/citizen/", "/officer/", "/command/", "/admin/", "/login/", "/register/"];

function isPrivatePath(pathname) {
  return PRIVATE_PREFIXES.some(function (prefix) { return pathname.startsWith(prefix); });
}

/* ------------------------------------------------------------- Install */
self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      // Added individually: addAll() fails the entire install if any single URL
      // misses, which would leave the user with no service worker at all.
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
      // Deletes every cache from a previous VERSION, which is what evicts the
      // poisoned v1 entries from browsers that already installed it.
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

  // Only same-origin GETs. Map tiles and other cross-origin traffic go direct,
  // and writes belong to the IndexedDB queue in offline.js, not to the cache.
  if (request.method !== "GET") return;

  let url;
  try { url = new URL(request.url); } catch (e) { return; }
  if (url.origin !== self.location.origin) return;

  // The Django admin and the auth endpoints are never intercepted: serving a
  // stale authenticated response to the wrong person is a real risk.
  if (url.pathname.startsWith("/admin/") || url.pathname.startsWith("/api/auth/")) return;

  if (request.mode === "navigate") {
    event.respondWith(handleNavigation(request, url));
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirst(request));
    return;
  }
  event.respondWith(staticCacheFirst(request));
});

/**
 * Navigations: always try the network, and hand redirects straight back.
 *
 * Returning `response` untouched is essential. A 302 must reach the browser as a
 * 302 so it can follow it itself; anything else reintroduces the v1 failure.
 */
async function handleNavigation(request, url) {
  try {
    const response = await fetch(request);

    // Cache only public, non-redirected, successful HTML. Anything redirected
    // or private is returned but never stored.
    if (response && response.ok && !response.redirected && !isPrivatePath(url.pathname)) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, response.clone()).catch(function () { /* not cacheable */ });
    }
    return response;
  } catch (error) {
    // Genuinely offline. Serve the cached page if we have a safe one, otherwise
    // the offline notice — which explains that queued work is not lost.
    if (!isPrivatePath(url.pathname)) {
      const cached = await caches.match(request);
      if (cached && !cached.redirected) return cached;
    }
    const offlinePage = await caches.match("/offline/");
    if (offlinePage) return offlinePage;

    return new Response(
      "<h1>You are offline</h1><p>Reconnect and try again. Anything you saved is still on this device.</p>",
      { status: 503, headers: { "Content-Type": "text/html" } }
    );
  }
}

/**
 * API GETs: fresh data preferred, cached copy only when the network fails.
 *
 * An officer must never act on stale case data while the real thing is
 * reachable, so the network always wins when it answers.
 */
async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response && response.ok && !response.redirected) {
      const cache = await caches.open(DATA_CACHE);
      cache.put(request, response.clone()).catch(function () {});
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

/** CSS/JS/images: instant from cache, refreshed in the background. */
async function staticCacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) {
    // Refresh for next time without blocking this response.
    fetch(request).then(async function (response) {
      if (response && response.ok && !response.redirected) {
        const cache = await caches.open(SHELL_CACHE);
        cache.put(request, response.clone()).catch(function () {});
      }
    }).catch(function () {});
    return cached;
  }

  try {
    const response = await fetch(request);
    if (response && response.ok && !response.redirected) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, response.clone()).catch(function () {});
    }
    return response;
  } catch (error) {
    return new Response("", { status: 503 });
  }
}

/* Let a page force an immediate takeover after an update. */
self.addEventListener("message", function (event) {
  if (event.data === "skipWaiting") self.skipWaiting();
});

// b03be6a6 — Minimal PWA service worker for the Meridian dashboard.
//
// PURPOSE: this worker exists ONLY to satisfy PWA installability. It is
// deliberately as thin as possible.
//
// HARD REQUIREMENT (b03be6a6) — NETWORK-FIRST, NEVER CACHE-FIRST.
// Dashboard changes MUST appear on the next app open with zero rebuild /
// republish. We therefore do NOT precache the app shell (HTML/JS/CSS) and we
// never serve them from cache when the network is available. Every fetch goes
// to the network first; the cache is only ever a *fallback* for when the
// network fails (offline). If you make this cache-first you will silently break
// the "just dev normally, changes show up" promise the whole sprint depends on.
//
// The only thing we opportunistically cache is a tiny allow-list of truly-static
// assets (the icons + manifest) so an installed app still has its icon/manifest
// while offline. Even those are served network-first.

const CACHE = 'meridian-static-v1';

// Truly-static, safe-to-cache assets ONLY. NEVER add HTML/JS/CSS here.
const STATIC_ASSETS = [
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  // Activate immediately; do NOT precache the app shell.
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    (async () => {
      // Drop any stale caches from older SW versions.
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)));
      await self.clients.claim();
    })(),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;

  // Only handle same-origin GETs. Everything else (POST/PUT, cross-origin
  // CDN scripts, etc.) is passed straight through untouched.
  if (request.method !== 'GET' || new URL(request.url).origin !== self.location.origin) {
    return;
  }

  // NETWORK-FIRST (b03be6a6). Always try the network first so a freshly edited
  // dashboard shows up with no rebuild. Only fall back to cache on failure.
  event.respondWith(
    (async () => {
      const url = new URL(request.url);
      const isStaticAsset = STATIC_ASSETS.includes(url.pathname);
      try {
        const response = await fetch(request);
        // Opportunistically refresh the offline copy of the static allow-list
        // ONLY. HTML/JS/CSS are never written to the cache.
        if (isStaticAsset && response && response.ok) {
          const copy = response.clone();
          const cache = await caches.open(CACHE);
          cache.put(request, copy);
        }
        return response;
      } catch (err) {
        // Network failed (offline). Fall back to cache if we have it.
        const cached = await caches.match(request);
        if (cached) return cached;
        throw err;
      }
    })(),
  );
});

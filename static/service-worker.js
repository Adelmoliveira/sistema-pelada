const CACHE_NAME = "peladeiros-gpcta-v6";
const OFFLINE_URL = "/offline";
const APP_SHELL = [
  OFFLINE_URL,
  "/static/style.css",
  "/static/pwa.js",
  "/static/manifest.webmanifest",
  "/static/icons/pwa-192.png",
  "/static/icons/pwa-512.png",
  "/static/icons/pwa-maskable-512.png",
  "/static/icons/apple-touch-icon.png"
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = {body: event.data ? event.data.text() : ""}; }
  const declarative = data.web_push === 8030 && data.notification ? data.notification : null;
  const notification = declarative || data;
  const badgeCount = Math.max(0, Number(notification.app_badge || notification.badge) || 0);
  const target = notification.navigate || notification.url || "/";
  event.waitUntil(Promise.all([
    self.registration.showNotification(notification.title || "PELADEIROS GPCTA", {
      body: notification.body || "Você tem uma nova atualização.",
      icon: "/static/icons/pwa-192.png",
      badge: "/static/icons/pwa-192.png",
      image: notification.image || undefined,
      data: {url: target},
      vibrate: [200, 100, 200],
      timestamp: Date.now(),
      renotify: false,
      silent: notification.silent === true
    }),
    badgeCount > 0 && self.navigator && "setAppBadge" in self.navigator
      ? self.navigator.setAppBadge(badgeCount).catch(() => undefined)
      : Promise.resolve(),
    self.clients.matchAll({type: "window", includeUncontrolled: true})
      .then(list => Promise.all(list.map(client => client.postMessage({type: "notification-count", count: badgeCount}))))
  ]));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = event.notification.data && event.notification.data.url || "/";
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    const existing = list.find(client => "focus" in client);
    if (existing) { existing.navigate(target); return existing.focus(); }
    return clients.openWindow(target);
  }));
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin || !url.pathname.startsWith("/static/")) return;

  const refreshed = fetch(request).then(response => {
    if (response.ok) {
      const copy = response.clone();
      caches.open(CACHE_NAME).then(cache => cache.put(request, copy));
    }
    return response;
  });
  event.waitUntil(refreshed.catch(() => undefined));
  event.respondWith(caches.match(request).then(cached => cached || refreshed));
});

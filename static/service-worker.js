const CACHE_NAME = "peladeiros-gpcta-v8";
const OFFLINE_URL = "/offline";
const APP_SHELL = [
  OFFLINE_URL,
  "/static/style.css",
  "/static/pwa.js?v=8",
  "/static/pwa-update.js",
  "/static/manifest.webmanifest",
  "/static/icons/pwa-192.png",
  "/static/icons/pwa-512.png",
  "/static/icons/pwa-maskable-512.png",
  "/static/icons/apple-touch-icon.png"
];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));

});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key.startsWith("peladeiros-gpcta-") && key !== CACHE_NAME).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

// Only opaque alert identifiers are stored here, never payloads or private pages.
async function claimPurchaseAlert(id) {
  if (typeof indexedDB === "undefined") return true;
  return new Promise(resolve => {
    const request=indexedDB.open("gpcta-purchase-push",1);
    request.onupgradeneeded=()=>{
      const store=request.result.createObjectStore("seen",{keyPath:"id"});
      store.createIndex("seen_at","seen_at");
    };
    request.onerror=()=>resolve(true); // storage denial must not disable all push
    request.onsuccess=()=>{
      const db=request.result;
      try {
      const transaction=db.transaction("seen","readwrite");
      const store=transaction.objectStore("seen");
      if (typeof IDBKeyRange !== "undefined") {
        const old=store.index("seen_at").openCursor(IDBKeyRange.upperBound(Date.now()-7*86400000));
        old.onsuccess=()=>{const cursor=old.result;if(cursor){cursor.delete();cursor.continue();}};
      }
      const put=store.add({id:String(id),seen_at:Date.now()});
      let claimed=true;
      put.onerror=event=>{
        if (put.error && put.error.name === "ConstraintError") {
          event.preventDefault(); event.stopPropagation(); claimed=false;
        }
      };
      transaction.oncomplete=()=>{db.close();resolve(claimed);};
      transaction.onabort=()=>{db.close();resolve(true);};
      } catch (_) {db.close();resolve(true);}
    };
  });
}

async function forgetPurchaseAlert(id) {
  if (typeof indexedDB === "undefined") return;
  await new Promise(resolve=>{
    const request=indexedDB.open("gpcta-purchase-push",1);
    request.onerror=()=>resolve();
    request.onsuccess=()=>{
      const db=request.result;
      try {
      const transaction=db.transaction("seen","readwrite");
      transaction.objectStore("seen").delete(String(id));
      transaction.oncomplete=transaction.onabort=()=>{db.close();resolve();};
      } catch (_) {db.close();resolve();}
    };
  });
}

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = {body: event.data ? event.data.text() : ""}; }
  const declarative = data.web_push === 8030 && data.notification ? data.notification : null;
  const notification = declarative || data;
  const badgeCount = Math.max(0, Number(notification.app_badge || notification.badge) || 0);
  const target = notification.navigate || notification.url || "/";
  event.waitUntil((async()=>{
    const intelligent=data.kind === "purchase-alert" && data.alert_id;
    if (intelligent && !(await claimPurchaseAlert(data.alert_id))) return;
    try { await Promise.all([
    self.registration.showNotification(notification.title || "PELADEIROS GPCTA", {
      tag: data.kind === "purchase-alert" ? "gpcta-purchase-" + data.alert_id : undefined,
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
    ]); }
    catch(error) { if (intelligent) await forgetPurchaseAlert(data.alert_id); throw error; }
  })());
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

self.addEventListener("message", event => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (request.mode === "navigate") {
    // Authenticated pages are network-only, never written to the offline cache.
    event.respondWith(fetch(request).catch(async () => {
      const cache = await caches.open(CACHE_NAME);
      return (await cache.match(OFFLINE_URL)) || new Response("Sem conexão. Reconecte para continuar.", {
        status: 503, headers: {"Content-Type": "text/plain; charset=utf-8"}
      });
    }));
    return;
  }
  if (!APP_SHELL.includes(url.pathname + url.search) || url.pathname === OFFLINE_URL) return;
  const refreshed = fetch(request).then(async response => {
    if (response.ok && response.type !== "opaque" && !response.redirected) {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(request, response.clone()).catch(() => undefined);
    }
    return response;
  });
  event.waitUntil(refreshed.catch(() => undefined));
  event.respondWith(caches.open(CACHE_NAME).then(async cache => (await cache.match(request)) || refreshed));
});

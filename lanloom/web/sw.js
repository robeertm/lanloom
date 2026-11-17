/* Lanloom service worker: web push messages and the offline shell of the installed app.
   Network first for everything; the cache is only a fallback when the network is gone, so an update is never hidden
   behind an old copy. The API is never cached. */
'use strict';
const CACHE = 'lanloom-shell-1';
const SCOPE = self.registration.scope;
const SHELL = ['', 'index.html', 'manifest.webmanifest', 'img/icon.svg', 'img/icon-180.png'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL.map(p => SCOPE + p))).catch(() => null).then(() => self.skipWaiting()));
});

self.addEventListener('activate', event => {
  // no clients.claim(): an open page stays as it is; the worker takes over from the next navigation
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k.startsWith('lanloom-') && k !== CACHE).map(k => caches.delete(k)))));
});

function isShellAsset(url) {
  if (!url.startsWith(SCOPE)) return false;
  const rest = url.slice(SCOPE.length).split('?')[0];
  if (rest.startsWith('api/')) return false;
  return rest === '' || rest === 'index.html' || /^(css|js|board|img|fonts)\//.test(rest) || rest === 'manifest.webmanifest';
}

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match(SCOPE + 'index.html').then(r => r || Response.error())));
    return;
  }
  if (!isShellAsset(req.url)) return;            // the API and everything else: straight to the network, untouched
  event.respondWith(fetch(req).then(res => {
    if (res && res.ok && res.type === 'basic') {
      const copy = res.clone();
      caches.open(CACHE).then(c => c.put(req, copy)).catch(() => null);
    }
    return res;
  }).catch(() => caches.match(req).then(r => r || Response.error())));
});

self.addEventListener('push', event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: 'Lanloom', body: event.data ? event.data.text() : '' }; }
  const title = data.title || 'Lanloom';
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || '', tag: data.tag || 'lanloom', renotify: true, icon: SCOPE + 'img/icon-180.png', badge: SCOPE + 'img/icon-180.png',
    requireInteraction: data.severity === 'critical', data: { url: data.url || SCOPE },
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || SCOPE;
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (c.url.startsWith(SCOPE) && 'focus' in c) {
        if (target.startsWith(SCOPE) && 'navigate' in c) c.navigate(target).catch(() => null);
        return c.focus();
      }
    }
    return self.clients.openWindow(target);
  }));
});

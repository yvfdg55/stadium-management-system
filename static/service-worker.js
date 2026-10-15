const CACHE_NAME = 'happy-family-resort-static-v10';
const PRECACHE_URLS = [
  '/static/css/style.css',
  '/static/js/theme.js',
  '/static/js/pwa.js',
  '/static/manifest.webmanifest',
  '/static/customer-manifest.webmanifest',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-maskable-512.png'
];
const CACHEABLE_PATHS = new Set(PRECACHE_URLS);

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key.startsWith('happy-family-resort-static-') && key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => new Response(
        '<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>غير متصل</title><body style="font-family:Tahoma,Arial,sans-serif;background:#fff9e5;color:#203329;padding:2rem;text-align:center"><h1>لا يوجد اتصال بالإنترنت</h1><p>بيانات الحجوزات تحتاج اتصالًا آمنًا بالخادم. أعد الاتصال ثم حاول مجددًا.</p></body></html>',
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
      ))
    );
    return;
  }

  if (!CACHEABLE_PATHS.has(url.pathname)) return;
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request).then((response) => {
        if (response.ok && response.type === 'basic') {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      });
    })
  );
});

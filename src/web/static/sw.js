const CACHE = 'review-pwa-v6';
const SHELL = ['/', '/app.js', '/styles.css', '/manifest.webmanifest', '/icon.svg'];

self.addEventListener('install', (event) => {
  // Instalar com a versão nova já garante que o activate remova o cache antigo.
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  // A API nunca entra no cache: contadores e decisões precisam ser sempre atuais.
  if (url.pathname.startsWith('/api/') || event.request.method !== 'GET') return;

  // Network-first: após cada deploy o navegador recebe o HTML/JS novos; o cache
  // só é usado quando a DGX/tailnet estiver indisponível (uso offline da PWA).
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(CACHE)
            .then((cache) => cache.put(event.request, copy))
            .catch(() => {});
        }
        return response;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match('/')))
  );
});

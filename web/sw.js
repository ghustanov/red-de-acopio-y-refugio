// Cache de Red de Acopio y Refugio — sirve la última versión vista aunque no haya señal.
const CACHE = "angeles-v1";
self.addEventListener("install", e => { self.skipWaiting(); });
self.addEventListener("activate", e => { e.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const ruta = new URL(e.request.url).pathname;
  if (ruta.startsWith("/api/institucional/") || ruta === "/panel.html") return;
  e.respondWith(
    fetch(e.request).then(resp => {
      const copia = resp.clone();
      caches.open(CACHE).then(c => c.put(e.request, copia));
      return resp;
    }).catch(() => caches.match(e.request))
  );
});

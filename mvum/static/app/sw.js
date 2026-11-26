// Offline shell: the app's own files are served from cache (and refreshed in the background when online).
// Downloaded forest files are cached by the page itself ("mvum-files") and served from there.
const SHELL = "mvum-shell-v1";
const ASSETS = ["/", "/app.js", "/manifest.webmanifest", "/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k.startsWith("mvum-shell-") && k !== SHELL)
    .map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/files/")) {
    e.respondWith(caches.open("mvum-files").then((c) => c.match(url.pathname)).then((r) => r || fetch(e.request)));
    return;
  }
  const path = e.request.mode === "navigate" ? "/" : url.pathname;
  if (!ASSETS.includes(path)) return; // API calls go to the network; app.js falls back to its cached copy
  e.respondWith(caches.open(SHELL).then(async (c) => {
    const hit = await c.match(path);
    const fresh = fetch(path).then((r) => { if (r.ok) c.put(path, r.clone()); return r; }).catch(() => null);
    return hit || (await fresh) || new Response("offline", { status: 503 });
  }));
});

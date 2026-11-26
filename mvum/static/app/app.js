// mvumgrab phone app. Files live in Cache Storage ("mvum-files"); the forest list is cached too, so the app
// works with no connection once a forest is downloaded.
const FILES = "mvum-files";
const DATA = "mvum-data";
const $ = (s) => document.querySelector(s);
let forests = [], tab = "phone", here = null, have = new Set(), busy = {}, online = true;

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
if (navigator.storage && navigator.storage.persist) navigator.storage.persist();

const mb = (n) => (n == null ? "" : n < 1 << 20 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1048576).toFixed(1)} MB`);
const fileUrl = (name) => "/files/" + encodeURIComponent(name);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

async function load() {
  let data;
  online = true;
  try {
    const r = await fetch("/api/forests", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    data = await r.json();
    (await caches.open(DATA)).put("/api/forests", new Response(JSON.stringify(data)));
  } catch {
    online = false;
    const r = await (await caches.open(DATA)).match("/api/forests");
    data = r ? await r.json() : { forests: [] };
  }
  forests = data.forests;
  $("#net").textContent = online ? "" : "offline";
  have = new Set((await (await caches.open(FILES)).keys()).map((r) => decodeURIComponent(new URL(r.url).pathname.slice(7))));
  if (tab === "phone" && !have.size && online) tab = "all";
  render();
  const pending = forests.some((f) => f.raster && ["queued", "running"].includes(f.raster.state));
  if (online && pending) setTimeout(load, 15000);
}

function distance(f) {
  if (!here || !f.bbox) return Infinity;
  const [x0, y0, x1, y1] = f.bbox;
  const dx = Math.max(x0 - here[0], 0, here[0] - x1) * Math.cos((here[1] * Math.PI) / 180);
  const dy = Math.max(y0 - here[1], 0, here[1] - y1);
  return Math.hypot(dx, dy);
}

function fileRow(f, kind) {
  const v = kind === "vector" ? f.vector : f.raster;
  if (!v) return "";
  const title = kind === "vector" ? "Roads &amp; trails" : "Official map";
  let sub = kind === "vector" ? `${v.roads} roads, ${v.trails} trails` : `from ${f.pdf_maps} PDF map${f.pdf_maps === 1 ? "" : "s"}`;
  if (kind === "vector" && v.missing) sub += ` · <span class="warn">${v.missing} trails have no line in USFS data</span>`;
  const n = v.file, cached = have.has(n), b = busy[n];
  let act;
  if (b != null) act = `<progress max="1" value="${b}"></progress>`;
  else if (cached) act = `<span class="have">✓ on phone</span> <button class="primary" data-open="${esc(n)}">Open in OsmAnd</button>
                          <button data-del="${esc(n)}">Remove</button>`;
  else if (v.size) act = `<button class="primary" data-get="${esc(n)}" ${online ? "" : "disabled"}>Download ${mb(v.size)}</button>`;
  else if (kind === "raster" && v.state === "queued") act = `<span class="warn">queued on server…</span>`;
  else if (kind === "raster" && v.state === "running") act = `<span class="warn">converting on server (~3 min per map)…</span>`;
  else if (kind === "raster") act = `${v.state && v.state.startsWith("error") ? `<span class="warn">${esc(v.state)}</span>` : ""}
                                     <button data-make="${esc(f.key)}" ${online ? "" : "disabled"}>Make on server</button>`;
  else act = `<span class="warn">not built yet</span>`;
  return `<div class="file"><div class="what">${title}<small>${sub}</small></div>${act}</div>`;
}

function render() {
  $("#tab-phone").classList.toggle("on", tab === "phone");
  $("#tab-all").classList.toggle("on", tab === "all");
  const q = $("#q").value.trim().toLowerCase();
  let list = forests.filter((f) => f.vector || f.raster);
  if (tab === "phone") list = list.filter((f) => [f.vector, f.raster].some((v) => v && have.has(v.file)));
  if (q) list = list.filter((f) => f.name.toLowerCase().includes(q));
  if (here) list.sort((a, b) => distance(a) - distance(b));
  if (!list.length) {
    $("#list").innerHTML = `<p class="empty">${tab === "phone" ? "Nothing downloaded yet. Open “All forests”." : "No forests match."}</p>`;
    return;
  }
  const groups = here ? [["Nearest first", list]] : Object.entries(list.reduce((g, f) => ((g[f.region || "Other"] ||= []).push(f), g), {}));
  $("#list").innerHTML = groups.map(([g, fs]) => `<h2>${esc(g)}</h2>` + fs.map((f) => `
    <div class="forest"><div class="name">${esc(f.name)}</div>
      <div class="meta">${here && f.bbox ? `${Math.round(distance(f) * 111)} km away` : esc(f.region || "")}</div>
      ${fileRow(f, "vector")}${fileRow(f, "raster")}</div>`).join("")).join("");
}

async function download(name) {
  busy[name] = 0; render();
  try {
    const r = await fetch(fileUrl(name), { cache: "no-store" });
    if (!r.ok) throw new Error(`server said ${r.status}`);
    const total = +r.headers.get("Content-Length") || 0, reader = r.body.getReader(), parts = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value); got += value.length;
      if (total) { busy[name] = got / total; render(); }
    }
    const blob = new Blob(parts, { type: r.headers.get("Content-Type") || "application/octet-stream" });
    await (await caches.open(FILES)).put(fileUrl(name), new Response(blob, { headers: { "Content-Type": blob.type, "Content-Length": blob.size } }));
    have.add(name);
  } catch (e) {
    alertLine(`Download failed: ${e.message}`);
  }
  delete busy[name]; render();
}

async function openInOsmAnd(name) {
  const r = await (await caches.open(FILES)).match(fileUrl(name));
  if (!r) return alertLine("Not on this phone any more; download it again.");
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function alertLine(msg) {
  $("#net").textContent = msg;
  setTimeout(() => ($("#net").textContent = navigator.onLine ? "" : "offline"), 6000);
}

$("#list").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.get) download(b.dataset.get);
  else if (b.dataset.open) openInOsmAnd(b.dataset.open);
  else if (b.dataset.del) { await (await caches.open(FILES)).delete(fileUrl(b.dataset.del)); have.delete(b.dataset.del); render(); }
  else if (b.dataset.make) {
    b.disabled = true;
    try {
      const r = await fetch(`/api/forests/${b.dataset.make}/raster`, { method: "POST" });
      if (!r.ok) throw new Error(r.status);
      load();
    } catch { alertLine("Server not reachable"); b.disabled = false; }
  }
});
$("#q").addEventListener("input", render);
$("#tab-phone").onclick = () => { tab = "phone"; render(); };
$("#tab-all").onclick = () => { tab = "all"; render(); };
$("#near").onclick = () => {
  if (here) { here = null; $("#near").textContent = "Near me"; return render(); }
  navigator.geolocation.getCurrentPosition(
    (p) => { here = [p.coords.longitude, p.coords.latitude]; $("#near").textContent = "A–Z"; render(); },
    () => alertLine("No location"), { maximumAge: 600000, timeout: 20000 });
};
window.addEventListener("online", load);
window.addEventListener("offline", () => { online = false; $("#net").textContent = "offline"; render(); });
load();

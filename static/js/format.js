/* Shared formatters + HTML escaping helpers. */

export function fmtSize(b) {
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; let n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
export function fmtDate(s) {
  if (!s) return "—";
  return s.slice(0, 10);
}
/* Rotten Tomatoes / Metacritic tier bands: top tier blue, then green,
   yellow, red for the very lowest (RT itself has no such color scale). */
export function rtTier(s) {
  return s >= 75 ? "t-high" : s >= 60 ? "t-good" : s >= 40 ? "t-mid" : "t-low";
}
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    "&" + { "&": "amp", "<": "lt", ">": "gt", '"': "quot", "'": "#39" }[c] + ";");
}
/* Route remote images through the server's local cache: the browser never
   talks to image.tmdb.org, so posters come from disk and load instantly. */
function imgSrc(u) {
  if (!u) return "";
  return u.startsWith("https://image.tmdb.org/")
    ? "/img?u=" + encodeURIComponent(u)
    : u;
}
export function imgTag(cls, u, lazy = true) {
  return u ? `<img class="${cls}"${lazy ? ' loading="lazy"' : ""} src="${esc(imgSrc(u))}">`
           : `<div class="${cls} ph">🎬</div>`;
}
export function locBadge(path) {
  return path.replace(/^\/home\/[^/]+/, "~").replace(/\/$/, "");
}

/* clickable person chips fill the search box */
export function personChip(name, prefix) {
  if (!name) return "";
  return `<span class="person" data-person="${esc(name)}" title="Search ${esc(prefix)}: ${esc(name)}">${esc(name)}</span>`;
}

export function fmtRuntime(t) {
  if (!t.runtime) return "—";
  return t.kind === "series" ? `${t.runtime}m/ep` : `${t.runtime}m`;
}

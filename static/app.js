/* Film Organizer frontend */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  q: "", kind: "", watched: "", genre: "", root: "", match: "!not_found", missing: false,
  wanted: "", hidden: "",
  sort: "title", dir: "asc",
  sec: "", secDir: "asc", // previous sort — the silent tie-breaker
  titles: [], genres: [],
};

/* ---------- UI state persistence (survives refresh & restart) ---------- */
const STATE_KEY = "film_organizer_ui_state";
function saveState() {
  const { q, kind, watched, genre, root, match, missing, wanted, hidden, sort, dir, sec, secDir } = state;
  try {
    localStorage.setItem(STATE_KEY,
      JSON.stringify({ q, kind, watched, genre, root, match, missing, wanted, hidden, sort, dir, sec, secDir }));
  } catch (e) { /* private mode etc. — persistence is best-effort */ }
}
function restoreState() {
  try { Object.assign(state, JSON.parse(localStorage.getItem(STATE_KEY) || "{}")); }
  catch (e) { /* corrupt entry -> defaults */ }
}
function applySortIndicators() {
  $$("th.sortable").forEach(t => {
    t.classList.remove("sorted-asc", "sorted-desc");
    if (t.dataset.sort === state.sort)
      t.classList.add(state.dir === "asc" ? "sorted-asc" : "sorted-desc");
  });
}

function fmtSize(b) {
  if (!b) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0; let n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}
function fmtDate(s) {
  if (!s) return "—";
  return s.slice(0, 10);
}
/* Rotten Tomatoes / Metacritic tier bands: top tier blue, then green,
   yellow, red for the very lowest (RT itself has no such color scale). */
function rtTier(s) {
  return s >= 75 ? "t-high" : s >= 60 ? "t-good" : s >= 40 ? "t-mid" : "t-low";
}
function esc(s) {
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
function imgTag(cls, u, lazy = true) {
  return u ? `<img class="${cls}"${lazy ? ' loading="lazy"' : ""} src="${esc(imgSrc(u))}">`
           : `<div class="${cls} ph">🎬</div>`;
}

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", ...opts };
  if (opts.body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.body);
  } else {
    delete init.body; // no body -> no JSON content-type (avoids 422 on DELETE)
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* ignore */ }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}

/* ---------- table ---------- */
async function load() {
  saveState(); // every filter/sort change funnels through here
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  // the Mini-series chip is kind=series + the Miniseries pseudo-genre
  if (state.kind === "miniseries") {
    p.set("kind", "series"); p.set("genre", "Miniseries");
  } else if (state.kind) {
    p.set("kind", state.kind);
  }
  if (state.genre && state.kind !== "miniseries") p.set("genre", state.genre);
  if (state.watched) p.set("watched", state.watched);
  if (state.genre) p.set("genre", state.genre);
  if (state.root) p.set("root", state.root);
  if (state.match) p.set("match", state.match);
  if (state.missing) p.set("missing_on", "1");
  if (state.wanted) p.set("wanted", state.wanted);
  if (state.hidden) p.set("hidden", state.hidden);
  p.set("sort", state.sort); p.set("direction", state.dir);
  if (state.sec && state.sec !== state.sort) {
    p.set("secondary", state.sec); p.set("secondary_dir", state.secDir);
  }
  // stats use the SAME filter params (minus sort) so the top bar reflects
  // everything that made it through the current filter
  const ps = new URLSearchParams(p);
  ps.delete("sort"); ps.delete("direction");
  ps.delete("secondary"); ps.delete("secondary_dir");
  const [data, st] = await Promise.all([
    api(`/api/titles?${p}`), api(`/api/stats?${ps}`),
  ]);
  state.titles = data.titles;
  renderGenres(data.genres);
  renderRows();
  $("#statline").textContent =
    `Through filter: ${st.n} titles · ${st.movies} movies · ${st.series} series · ` +
    `${st.watched} watched · ${st.unwatched} unwatched · ${fmtSize(st.bytes)}`;
  $("#mToggleSub").textContent = `${st.n} titles`; // mobile collapse button
}

/* ---------- mobile collapsible header (stats + search + filters) ---------- */
const MTOGGLE_KEY = "film_organizer_mhdr_open";
function setMobileHeader(open) {
  document.body.classList.toggle("mExpanded", open);
  $("#mToggle").setAttribute("aria-expanded", open ? "true" : "false");
  try { localStorage.setItem(MTOGGLE_KEY, open ? "1" : "0"); }
  catch (e) { /* private mode — best effort */ }
}
$("#mToggle").onclick = () =>
  setMobileHeader(!document.body.classList.contains("mExpanded"));
try { // start collapsed on each visit; the button is only visible on mobile
  if (localStorage.getItem(MTOGGLE_KEY) === "1") setMobileHeader(true);
} catch (e) { /* ignore */ }

function renderGenres(genres) {
  const sel = $("#genreSel");
  const cur = sel.value;
  // "Miniseries" pseudo-genre is filterable here but never shown as a plain
  // genre chip option twice; it comes from the server facet already
  sel.innerHTML = '<option value="">Genre…</option>' +
    genres.map(g => `<option${g === cur ? " selected" : ""}>${esc(g)}</option>`).join("");
}

async function renderRootOptions() {
  const { roots } = await api("/api/roots");
  $("#rootSel").innerHTML = '<option value="">All locations</option>' +
    roots.map(r => `<option value="${esc(r.path)}">${esc(r.label || r.path)}${r.exists ? "" : " (offline)"}</option>`).join("");
  $("#rootSel").value = state.root;
}

function locBadge(path) {
  return path.replace(/^\/home\/[^/]+/, "~").replace(/\/$/, "");
}

/* clickable person chips fill the search box */
function personChip(name, prefix) {
  if (!name) return "";
  return `<span class="person" data-person="${esc(name)}" title="Search ${esc(prefix)}: ${esc(name)}">${esc(name)}</span>`;
}

function fmtRuntime(t) {
  if (!t.runtime) return "—";
  return t.kind === "series" ? `${t.runtime}m/ep` : `${t.runtime}m`;
}

function renderRows() {
  const tb = $("#rows");
  $("#empty").classList.toggle("hidden", state.titles.length > 0);
  tb.innerHTML = state.titles.map(t => {
    const rating = t.rating_imdb || t.rating_tmdb;
    const starChips = (t.stars || []).slice(0, 3).map(s => personChip(s, "actor")).join(", ");
    const whoPerson = t.kind === "series"
      ? (t.creator ? `Created by ${personChip(t.creator, "creator")}` : "")
      : (t.director ? `Dir. ${personChip(t.director, "director")}` : "");
    // miniseries replaces the plain series badge
    const kindLabel = t.is_miniseries ? "miniseries" : t.kind;
    const kindTitle = t.is_miniseries ? "Miniseries — one closed season" : t.kind;
    const metas = [];
    if (t.kind === "series" && t.seasons) metas.push(`${t.seasons} season${t.seasons > 1 ? "s" : ""}`);
    if (t.episode_count) metas.push(`${t.episode_count} ep file${t.episode_count > 1 ? "s" : ""}`);
    if (t.missing_files) metas.push(`${t.missing_files} missing`);
    const matchBadge = t.match_status === "matched" ? ""
      : t.match_status === "no_provider" ? '<span class="badge warn">no key</span>'
      : t.match_status === "error" ? `<span class="badge err" title="${esc(t.match_error)}">error</span>`
      : t.match_status === "not_found" ? `<span class="badge dim2" title="${esc(t.match_error)}">not a movie/show</span>`
      : '<span class="badge">unmatched</span>';
    const watched = t.watched;
    const genreTags = (t.genres || []).slice(0, 3).map(g =>
      `<span class="badge genre">${esc(g)}</span>`).join("");
    const wantedBadge = t.wanted ? '<span class="badge wanted" title="On the wishlist — submitted via API or toggled here">★ wanted</span>' : "";
    const hiddenBadge = t.hidden ? '<span class="badge hiddenbadge" title="Hidden — out of the default list, not deleted">🙈 hidden</span>' : "";
    // season calendar chips: upcoming season announced/vague, or finished
    const calUpcoming = t.season_upcoming
      ? `<span class="badge cal" title="Next season on the season calendar (📅 in the top bar)">${esc(t.season_upcoming)}</span>` : "";
    const calFinished = t.season_finished
      ? '<span class="badge finished" title="Series finished — the finale has aired">🏁 finished</span>' : "";
    // a title is OFFLINE when every location it lives on is currently
    // disconnected (e.g. its only copy sits on an unplugged drive);
    // ONLINE when at least one location is connected (ready to watch)
    const offlineSet = t.offline_locations || [];
    const allOff = (t.locations || []).length > 0 &&
      (t.locations || []).every(l => offlineSet.includes(l));
    const isOnline = (t.locations || []).length > 0 && !allOff;
    const isNext = !!t.watch_next;
    // offline is signaled by the amber row edge + amber "Where" chips —
    // a dedicated badge just cluttered the title line
    const nextBadge = isNext
      ? '<span class="badge next" title="Pinned as Watch Next — copy sits in the aaNext folder inside the internal storage">▶ next</span>' : "";
    return `<tr data-id="${t.id}" class="${isNext ? "is-next" : ""}${isOnline ? " is-online" : ""}${t.wanted ? " is-wanted" : ""}${t.history ? " is-history" : ""}${allOff ? " is-offline" : ""}${t.hidden ? " is-hidden" : ""}">
      <td><div class="tcell">
        ${imgTag("thumb", t.poster)}
        <div>
          <div class="tname">${esc(t.title)}<button class="copybtn" data-copy="${esc(t.title)}"
            title="Copy title to clipboard">⧉</button></div>
          <div style="margin-top:3px">
            <span class="badge ${t.kind}${t.is_miniseries ? " mini" : ""}" title="${kindTitle}">${kindLabel}</span>${nextBadge}${wantedBadge}${hiddenBadge}${calUpcoming}${calFinished}${matchBadge}
            ${metas.map(m => `<span class="badge">${esc(m)}</span>`).join("")}
            ${genreTags}
          </div>
        </div>
      </div></td>
      <td>${t.year || "—"}</td>
      <td class="c">${fmtRuntime(t)}</td>
      <td class="c">${t.cert ? `<span class="cert">${esc(t.cert)}</span>` : "—"}</td>
      <td class="c">${rating ? `<span class="rating">★ ${rating.toFixed(1)}</span>` : "—"}</td>
      <td class="c">${t.rating_rt != null ? `<span class="rt ${rtTier(t.rating_rt)}">${t.rating_rt}%</span>` : "—"}</td>
      <td class="c">${t.avg_rating != null ? `<span class="avgrating" title="Average of IMDb / TMDB / RT / Metacritic (those that have a score)">${t.avg_rating.toFixed(1)}</span>` : "—"}</td>
      <td class="meta-col">${starChips}${starChips && whoPerson ? "<br>" : ""}${whoPerson}</td>
      <td class="where">${(t.locations || []).map(l => {
        const off = (t.offline_locations || []).includes(l);
        return `<span class="loc${off ? " off" : ""}" title="${esc(l)}${off ? " — drive disconnected" : ""}">${esc(locBadge(l))}${off ? " ⚠" : ""}</span>`;
      }).join("")}</td>
      <td class="r">${(t.wanted || t.history) && !(t.size_bytes) ? "—" : fmtSize(t.size_bytes)}</td>
      <td title="${t.created_at ? "created" : "created unknown — showing catalog date (files on offline drive)"}">${fmtDate(t.created_at || t.cataloged_at)}</td>
      <td class="actions">
        <button class="wbtn fav ${t.favorite ? "on" : ""}" data-fav="${t.id}"
          title="${t.favorite ? "Remove from favorites" : "Mark as favorite"}">♥</button>
        <button class="wbtn hide ${t.hidden ? "on" : ""}" data-hide="${t.id}"
          title="${t.hidden ? "Un-hide — back in the list" : "Hide — tuck away without deleting (Hidden filter shows it)"}">${t.hidden ? "👁" : "🙈"}</button>
        <button class="wbtn next ${isNext ? "on" : ""}${watched ? " watched" : ""}" data-next="${t.id}"
          title="${isNext ? "Unpin from Watch Next" : "Set as Watch Next — copies it to the aaNext folder on internal storage and pins it to the top"}">▶</button>
        <button class="wbtn ${watched ? "on" : ""}" data-watched="${t.id}"
          title="${watched ? "Mark unwatched" : "Mark watched"}">✓</button>
        <button class="wbtn del" data-deltitle="${t.id}" data-name="${esc(t.title)}"
          title="Delete this ${t.kind} and its files from disk">🗑</button>
      </td>
    </tr>`;
  }).join("");
}

/* ---------- sorting / filtering ---------- */
/* Clicking a new column keeps the PREVIOUS sort as a silent tie-breaker:
   ties on the new key (year, rating...) stay ordered by whatever the user
   was looking at before. No UI indication — it just behaves that way. */
$$("th.sortable").forEach(th => th.addEventListener("click", () => {
  const k = th.dataset.sort;
  if (state.sort === k) {
    state.dir = state.dir === "asc" ? "desc" : "asc";
  } else {
    state.sec = state.sort; state.secDir = state.dir;
    state.sort = k; state.dir = "asc";
  }
  applySortIndicators();
  load();
}));

let searchTimer;
$("#search").addEventListener("input", e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = e.target.value.trim(); load(); }, 250);
});
$$("#kindChips .chip").forEach(c => c.addEventListener("click", () => {
  $$("#kindChips .chip").forEach(x => x.classList.remove("on")); c.classList.add("on");
  state.kind = c.dataset.kind; load();
}));
$$("#watchChips .chip").forEach(c => c.addEventListener("click", () => {
  $$("#watchChips .chip").forEach(x => x.classList.remove("on")); c.classList.add("on");
  state.watched = c.dataset.w; load();
}));
$("#genreSel").addEventListener("change", e => { state.genre = e.target.value; load(); });
$("#rootSel").addEventListener("change", e => { state.root = e.target.value; load(); });
$("#wantedSel").addEventListener("change", e => { state.wanted = e.target.value; load(); });
$("#hiddenSel").addEventListener("change", e => { state.hidden = e.target.value; load(); });
$("#matchSel").addEventListener("change", e => { state.match = e.target.value; load(); });
$("#onlyMissing").addEventListener("change", e => { state.missing = e.target.checked; load(); });

/* click a director/actor/creator chip -> fills the search box */
function searchPerson(name) {
  $("#search").value = name;
  state.q = name;
  load();
}

/* ---------- watched toggle + watch-next + delete + row click (delegation) ---------- */
document.addEventListener("click", async e => {
  const nbtn = e.target.closest("[data-next]");
  if (nbtn) {
    e.stopPropagation();
    const id = nbtn.dataset.next;
    const t = state.titles.find(x => String(x.id) === String(id));
    nbtn.disabled = true;
    try {
      if (t.watch_next) {
        await api(`/api/titles/${id}/watch-next`, { method: "DELETE" });
        t.watch_next = null;
        renderRows();
      } else {
        // instant visual confirmation: color the button + row NOW — the
        // actual pin only lands in the DB when the copy job finishes, so
        // a plain load() here would repaint the button as un-clicked
        t.watch_next = t.kind;          // optimistic; load() corrects later
        renderRows();
        const r = await api(`/api/titles/${id}/watch-next`, { method: "POST" });
        // watchJob reloads the list itself once the copy job completes
        watchJob(r.job_id, `Watch Next — ${t.title}`);
      }
    } catch (err) {
      // 409 names the drive to connect; 400 explains missing settings
      t.watch_next = null;              // roll the optimistic pin back
      renderRows();
      alert(err.message);
    }
    nbtn.disabled = false;
    return;
  }
  const wbtn = e.target.closest("[data-watched]");
  if (wbtn) {
    e.stopPropagation();
    const id = wbtn.dataset.watched;
    const t = state.titles.find(x => String(x.id) === String(id));
    const next = !t.watched;
    try {
      await api(`/api/titles/${id}/watched`, { method: "POST", body: { watched: next } });
      t.watched = next;
      renderRows();
    } catch (err) { alert(err.message); }
    return;
  }
  const del = e.target.closest("[data-deltitle]");
  if (del) {
    e.stopPropagation();
    const id = del.dataset.deltitle;
    const t = state.titles.find(x => String(x.id) === String(id));
    openDeleteChoice(t);
    return;
  }
  const fav = e.target.closest("[data-fav]");
  if (fav) {
    e.stopPropagation();
    const id = fav.dataset.fav;
    const t = state.titles.find(x => String(x.id) === String(id));
    try {
      await api(`/api/titles/${id}/favorite`, { method: "POST", body: { value: !t.favorite } });
      t.favorite = !t.favorite;
      renderRows();
    } catch (err) { alert(err.message); }
    return;
  }
  const hbtn = e.target.closest("[data-hide]");
  if (hbtn) {
    e.stopPropagation();
    const id = hbtn.dataset.hide;
    const t = state.titles.find(x => String(x.id) === String(id));
    try {
      const r = await api(`/api/titles/${id}/hidden`, { method: "POST", body: { value: !t.hidden } });
      t.hidden = r.hidden;
      // hiding in the default view makes the row drop out (server filter
      // excludes hidden) -> full reload; in Hidden-only view it stays put
      if (t.hidden && state.hidden !== "only") load();
      else renderRows();
    } catch (err) { alert(err.message); }
    return;
  }
  const chip = e.target.closest("[data-person]");
  if (chip) {
    e.stopPropagation();
    searchPerson(chip.dataset.person);
    return;
  }
  const cp = e.target.closest("[data-copy]");
  if (cp) {
    e.stopPropagation();
    copyTitle(cp);
    return;
  }
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDrawer(+tr.dataset.id);
});

/* copy a title name to the clipboard (with fallback for non-secure origins) */
async function copyTitle(btn) {
  const text = btn.dataset.copy;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    btn.classList.add("ok");
    btn.textContent = "✓";
    setTimeout(() => { btn.classList.remove("ok"); btn.textContent = "⧉"; }, 1200);
  } catch (err) { alert(`Could not copy "${text}": ${err.message}`); }
}

/* ---------- delete-choice popup ---------- */
let delTarget = null;

async function refreshDriveQueue() {
  if ($("#modal").classList.contains("hidden")) return;
  const { groups, pending } = await api("/api/drive-queue");
  $("#queueHint").textContent = pending ? `${pending} queued` : "";
  $("#queueList").innerHTML = groups.length ? groups.map(g => `
    <li class="qdrive">
      <span class="path" title="${esc(g.drive)}">💾 ${esc(g.drive)}</span>
      ${g.mounted
        ? '<span class="st" style="color:var(--ok)">connected</span>'
        : '<span class="off">waiting for connection</span>'}
    </li>
    ${g.items.map(it => {
      // an entry may wait for MORE drives than this group's primary one —
      // for moves this includes the drive its FILES sit on and the liked-
      // copy drive, so `missing` names exactly what to plug in
      const itemDrives = it.drives || [g.drive];
      const missing = it.missing || [];
      const wait = it.ready ? "" : missing.length
        ? `<div class="dim" style="font-size:11.5px">⏳ waiting for: ${esc(missing.join(" and "))} — plug it in to run</div>`
        : `<div class="dim" style="font-size:11.5px">⏳ waiting for: ${esc(itemDrives.join(", "))}</div>`;
      // an error entry below the retry cap comes back by itself ("retrying");
      // past the cap it never will — say so, with the reason at hand
      const errBadge = it.exhausted
        ? `<span class="badge err" title="${esc(it.last_error || "failed")}">⚠ failed</span>`
        : `<span class="badge err" title="${esc(it.last_error || "failed — will retry")}">⚠ retrying</span>`;
      return `
    <li class="qitem">
      <div style="flex:1;min-width:0">
        <span class="path" title="${esc(it.description)}${it.last_error ? `\nLast error: ${it.last_error}` : ""}">${esc(it.description)}</span>
        ${wait}
      </div>
      ${it.status === "error"
        ? errBadge
        : it.ready && g.mounted
          ? '<span class="st" style="color:var(--ok)">will run now</span>'
          : ""}
      <span class="st">${fmtDate(it.created_at)}</span>
      <button class="btn mini ghost" data-cancel-queue="${it.id}" title="Remove from queue">✕</button>
    </li>`;}).join("")}`).join("")
    : '<li><span class="dim">Nothing queued — every drive is caught up. 🎉</span></li>';
}

$("#queueList").addEventListener("click", async e => {
  const b = e.target.closest("[data-cancel-queue]");
  if (!b) return;
  try {
    await api(`/api/drive-queue/${b.dataset.cancelQueue}`, { method: "DELETE" });
    refreshDriveQueue();
  } catch (err) { alert(err.message); }
});
$("#btnRunQueue").onclick = async () => {
  try {
    const r = await api("/api/drive-queue/run", { method: "POST" });
    $("#queueHint").textContent = r.started.length
      ? `Started ${r.started.length} queued job(s)…`
      : "Nothing to run — connect the drives first.";
    setTimeout(refreshDriveQueue, 1500);
  } catch (err) { alert(err.message); }
};

function openDeleteChoice(t) {
  delTarget = t;
  $("#delName").textContent = `"${t.title}"`;
  const gb = (t.size_bytes / 1e9).toFixed(1);
  const hasFiles = (t.locations || []).length > 0 || !!t.size_bytes;
  const info = $("#delInfo");
  if (!hasFiles) {
    info.innerHTML = `No file is on disk for this entry — this removes only
      the list entry (its watched memory goes with it).`;
    $("#delKeep").classList.add("hidden");
  } else {
    info.innerHTML = `This ${t.kind} currently uses <b>${gb} GB</b> on disk
      ${t.watched ? "and is marked <b>watched</b>" : ""}.
      What should happen to its file(s)?`;
    $("#delKeep").classList.remove("hidden");
  }
  $("#delModal").classList.remove("hidden");
}

function closeDeleteChoice() {
  delTarget = null;
  $("#delModal").classList.add("hidden");
}

async function runDelete(keepRecord) {
  const t = delTarget;
  if (!t) return;
  closeDeleteChoice();
  try {
    const q = keepRecord ? "?keep_record=true" : "";
    const r = await api(`/api/titles/${t.id}/files${q}`, { method: "DELETE" });
    if (r.queued) {
      alert(`"${r.title}" needs ${r.drives.length > 1 ? "these drives" : "this drive"}: ${r.drives.join(", ")}.
The deletion was queued and runs automatically when ${r.drives.length > 1 ? "they are" : "it is"} connected (Settings → Drive queues).`);
    } else if (r.kept_record) {
      alert(`Deleted the file(s) of "${r.title}" (${r.removed_files} removed).
The entry stays in your list as a record.`);
    } else {
      alert(r.removed_files > 0
        ? `Deleted "${r.title}": ${r.removed_files} file(s) removed from disk.`
        : `Removed "${r.title}" from the list (no files on disk).`);
    }
    load();
  } catch (err) {
    alert(err.message); // includes "Connect these drives first: …" on 409 (queue=off)
  }
}

$("#delClose").onclick = closeDeleteChoice;
$("#delCancel").onclick = closeDeleteChoice;
$("#delModal").addEventListener("click", e => {
  if (e.target === $("#delModal")) closeDeleteChoice();
});
$("#delAll").onclick = () => runDelete(false);
$("#delKeep").onclick = () => runDelete(true);

/* ---------- drawer ---------- */

// friendly label for a move destination (drawer buttons, toasts, confirm)
function destLabel(root, target) {
  if (!root) return target === "internal" ? "💻 Internal" : "🔌 External";
  const last = root.split("/").filter(Boolean).pop() || root;
  return last.charAt(0).toUpperCase() + last.slice(1);
}

// one 'Move to' button per configured destination (internal + per-kind
// backup + liked roots); unavailable drives still render — clicking them
// queues the move, and the drive-queue gate now waits for every drive
// the move actually needs
async function moveDestButtons(t) {
  let dests;
  try {
    ({ destinations: dests } = await api("/api/move-destinations"));
  } catch { dests = []; }
  // exactly TWO canonical side buttons (💻 internal / 🔌 external backup);
  // every other configured folder lives behind the "Other folder…" popup
  const side = (d) => {
    if (!d) return "";
    const here = t.storage_side === "external"
      ? d.role === "internal"
      : d.role !== "internal";
    const mark = d.mounted ? (here ? " •" : "") : " ⏳";
    const title = d.mounted
      ? `Move to ${d.root}`
      : `${d.root} is not connected — the move will be queued until it is`;
    return `<button class="btn mini" data-dest data-target="${d.role === "internal" ? "internal" : "external"}" data-root="${esc(d.root)}" title="${esc(title)}">${esc(destLabel(d.root, d.role))}${mark}</button>`;
  };
  const int = dests.find(d => d.role === "internal");
  const ext = dests.find(d => d.role === "backup" && d.kind === t.kind)
    || dests.find(d => d.role === "backup");
  // MUST be an array — the drawer template does .join("") on the result
  return [side(int), side(ext)].filter(Boolean);
}

const DM_ROLE_BADGE = {
  internal: '<span class="badge">internal</span>',
  backup: '<span class="badge">backup</span>',
  liked: '<span class="badge wanted">liked</span>',
  library: '<span class="badge genre">library</span>',
};

// folder picker for moves: EVERY configured destination with its FULL PATH —
// internal, backup/liked drives AND the scanned library roots, so a title
// can even move from one library folder to another on the same drive
// (~/Videos -> ~/Downloads). onPick(target, root) receives the choice.
async function openDestPicker(name, onPick) {
  let dests;
  try {
    ({ destinations: dests } = await api("/api/move-destinations"));
  } catch { dests = []; }
  $("#dmName").textContent = name;
  $("#dmList").innerHTML = dests.map(d => `
    <div class="dmitem${d.mounted ? "" : " off"}"
      data-dmroot="${esc(d.root)}"
      data-dmtarget="${d.role === "internal" ? "internal" : "external"}"
      title="${d.mounted ? `Move to ${esc(d.root)}` : esc(d.root) + " is not connected — the move will be queued until it is"}">
      <span class="dmpath">📁 ${esc(d.root)}</span>
      ${DM_ROLE_BADGE[d.role] || ""}${d.mounted ? "" : '<span class="badge warn">⏳ queued</span>'}
    </div>`).join("");
  $$("#dmList .dmitem").forEach(el => {
    el.onclick = () => {
      $("#destModal").classList.add("hidden");
      onPick(el.dataset.dmtarget, el.dataset.dmroot);
    };
  });
  $("#destModal").classList.remove("hidden");
}

const EDIT_FIELDS = [
  ["runtime", "Runtime (min) — episode length for series", "number"],
  ["rating_imdb", "IMDb rating (0-10)", "number", "0.1"],
  ["rating_tmdb", "TMDB rating (0-10)", "number", "0.1"],
  ["rating_rt", "Rotten Tomatoes %", "number", "1"],
  ["rating_mc", "Metacritic Metascore (0-100)", "number", "1"],
  ["votes_imdb", "IMDb votes", "number", "1"],
  ["cert", "Content rating (e.g. PG-13)", "text"],
  ["seasons", "Seasons", "number"],
  ["episodes", "Episodes", "number"],
];

async function openDrawer(id) {
  const t = await api(`/api/titles/${id}`);
  const facts = [
    t.year && `Year ${t.year}`,
    t.runtime && `${t.runtime} min`,
    t.status,
    t.network && `Network: ${t.network}`,
    t.tmdb_id && `TMDB ${t.tmdb_id}`,
    t.imdb_id && `IMDb ${t.imdb_id}`,
    t.data_source,
  ].filter(Boolean).join(" · ");
  const descText = t.overview || "No description fetched yet — run Enrich.";
  $("#dBody").innerHTML = `
    ${imgTag("back", t.backdrop, false)}
    <h2>${esc(t.title)} ${t.year ? `<span style="color:var(--dim)">(${t.year})</span>` : ""}</h2>
    <div class="facts">${esc(facts)}</div>
    <div class="flagrow">
      <button class="btn mini ${t.favorite ? "primary" : ""}" id="dFav">${t.favorite ? "♥ Favorite" : "♡ Favorite"}</button>
      <button class="btn mini ${t.wanted ? "primary" : ""}" id="dWanted">${t.wanted ? "★ Wanted" : "☆ Not wanted"}</button>
      <button class="btn mini" id="dWatch">${t.watched ? "✓ Watched" : "Mark watched"}</button>
      ${t.kind === "series" ? `<button class="btn mini ${t.is_miniseries ? "primary" : ""}" id="dMini"
        title="Mark as miniseries (limited series) — still a series, but tagged and filterable as miniseries">${t.is_miniseries ? "🎬 Miniseries ✓" : "🎬 Mark miniseries"}</button>` : ""}
      <button class="btn mini ${t.watch_next ? "primary" : ""}" id="dNext"
        title="Copies the files into the aaNext folder on internal storage and pins this title to the top of the list">${t.watch_next ? "▶ Watch Next ✓" : "▶ Set Watch Next"}</button>
    </div>
    ${(t.genres || []).map(g => `<span class="badge genre">${esc(g)}</span>`).join(" ")}
    ${t.cert ? `<span class="cert" title="Content rating">${esc(t.cert)}</span>` : ""}
    ${t.rating_rt != null ? `<span class="rt ${rtTier(t.rating_rt)}" title="Rotten Tomatoes">🍅 ${t.rating_rt}%</span>` : ""}
    ${t.rating_mc != null ? `<span class="rt ${rtTier(t.rating_mc)}" title="Metacritic">🟩 MC ${t.rating_mc}</span>` : ""}
    ${t.avg_rating != null ? `<span class="avgrating" title="Average of IMDb / TMDB / RT / Metacritic (those that have a score)">⚖ avg ${t.avg_rating.toFixed(1)}</span>` : ""}
    ${t.wanted && t.wanted_note ? `<p class="hint">Wanted — ${esc(t.wanted_note)}${t.wanted_by ? ` (via ${esc(t.wanted_by)})` : ""}</p>` : ""}
    <div class="descwrap">
      <button class="btn mini" id="dDescBtn">Show description</button>
      <p class="overview hidden" id="dDesc">${esc(descText)}</p>
    </div>
    ${t.kind === "series" && t.episodes_tracked ? `
    <details class="editbox epbox" id="epBox">
      <summary>
        Mark episodes watched
        <span class="epprog"><span class="epprogbar"><span style="width:${t.episodes_tracked ? Math.round(100 * t.episodes_watched / t.episodes_tracked) : 0}%"></span></span>
        <span class="epcount">${t.episodes_watched}/${t.episodes_tracked}</span></span>
      </summary>
      <div class="epall">
        <button class="btn mini ghost" id="epAll" title="Tick every episode">all</button>
        <button class="btn mini ghost" id="epNone" title="Untick every episode">none</button>
      </div>
      <div class="eplist">
        ${t.files.filter(f => f.episode !== null && f.episode !== undefined && !f.missing)
          .sort((a, b) => ((a.season ?? 1) - (b.season ?? 1)) || (a.episode - b.episode) || a.path.localeCompare(b.path))
          .map(f => `
          <label class="epitem ${f.watched_manual ? "seen" : ""}" data-epid="${f.id}">
            <input type="checkbox" ${f.watched_manual ? "checked" : ""}>
            <span class="epname">S${f.season ?? 1}E${f.episode}</span>
            <span class="eppath" title="${esc(f.path)}">${esc(f.path.split("/").pop())}</span>
          </label>`).join("")}
      </div>
    </details>` : ""}
    <div class="moverow">
      <span class="mlab">Move to:</span>
      ${(await moveDestButtons(t)).join("")}
      <button class="btn mini" id="dAnyFolder"
        title="Pick ANY configured folder — full paths, including other library folders on the same drive">📁 Other folder…</button>
      ${t.kind === "series" && t.files.some(f => !f.missing) ? `
      <button class="btn mini seasonsbtn" id="dSeasons"
        title="Choose seasons — move only the seasons you pick instead of the whole series">✏️</button>` : ""}
    </div>
    <dl>
      ${t.rating_imdb ? `<dt>IMDb</dt><dd>★ ${t.rating_imdb} (${(t.votes_imdb || 0).toLocaleString()} votes)</dd>` : ""}
      ${t.rating_tmdb ? `<dt>TMDB</dt><dd>★ ${t.rating_tmdb}</dd>` : ""}
      ${t.director ? `<dt>Director</dt><dd><span class="person" data-person="${esc(t.director)}">${esc(t.director)}</span></dd>` : ""}
      ${t.creator ? `<dt>Created by</dt><dd><span class="person" data-person="${esc(t.creator)}">${esc(t.creator)}</span></dd>` : ""}
      ${(t.stars || []).length ? `<dt>Stars</dt><dd>${(t.stars || []).map(s => `<span class="person" data-person="${esc(s)}">${esc(s)}</span>`).join(", ")}</dd>` : ""}
      <dt>Created on disk</dt><dd>${fmtDate(t.created_at)}</dd>
      <dt>Cataloged</dt><dd>${fmtDate(t.cataloged_at)}</dd>
    </dl>

    <details class="editbox" id="editBox">
      <summary>Edit details (fix wrong metadata)</summary>
      <p class="hint">Edited fields are locked — <b>Enrich</b> will not overwrite them. Clearing a field unlocks it for refetch.</p>
      <div class="egrid">
        <label style="grid-column:1/-1">Title<input id="e_title" value="${esc(t.title)}"></label>
        <label>Kind
          <select id="e_kind">
            <option value="movie" ${t.kind === "movie" ? "selected" : ""}>movie</option>
            <option value="series" ${t.kind === "series" ? "selected" : ""}>series</option>
          </select>
        </label>
        <label>Year<input id="e_year" type="number" value="${t.year ?? ""}"></label>
        ${EDIT_FIELDS.map(([k, lab, ty, step]) => `
          <label>${lab}${(t.manual_edits || []).includes(k) ? ' <span class="locked" title="Manually edited — Enrich skips this field">🔒</span>' : ""}<input id="e_${k}" type="${ty}"${step ? ` step="${step}"` : ""} value="${t[k] ?? ""}"></label>`).join("")}
        <label style="grid-column:1/-1">Director<input id="e_director" value="${esc(t.director || "")}"></label>
        <label style="grid-column:1/-1">Created by<input id="e_creator" value="${esc(t.creator || "")}"></label>
        <label style="grid-column:1/-1">Network<input id="e_network" value="${esc(t.network || "")}"></label>
        <label style="grid-column:1/-1">Stars (comma-separated)<input id="e_stars" value="${esc((t.stars || []).join(", "))}"></label>
        <label style="grid-column:1/-1">Genres (comma-separated)<input id="e_genres" value="${esc((t.genres || []).join(", "))}"></label>
        <label style="grid-column:1/-1">Description<textarea id="e_overview" rows="3">${esc(t.overview || "")}</textarea></label>
      </div>
      <div class="editrow">
        <button class="btn mini primary" id="eSave">Save changes</button>
        <button class="btn mini ghost" id="eRevert">Revert</button>
        <span class="hint">Changing title/kind/year re-keys the row and clears enrichment for a fresh match.</span>
      </div>
    </details>

    <h3 style="margin-bottom:6px">Files on disk</h3>
    <div class="filelist">
      ${t.files.map(f => `
        <div class="f ${f.missing ? "missing" : ""}">
          <span class="path" title="${esc(f.path)}">${esc(f.path)}${f.missing ? " (missing)" : ""}</span>
          <span>${fmtSize(f.size_bytes)}</span>
          ${f.missing ? "" : `<a href="#" data-open="${t.id}:${f.id}">open folder</a>`}
        </div>`).join("")}
    </div>`;
  $("#drawer").classList.add("open");

  // description toggle (hidden by default)
  $("#dDescBtn").onclick = () => {
    const p = $("#dDesc");
    const show = p.classList.toggle("hidden") === false;
    $("#dDescBtn").textContent = show ? "Hide description" : "Show description";
  };
  $("#dWatch").onclick = async () => {
    await api(`/api/titles/${id}/watched`, { method: "POST", body: { watched: !t.watched } });
    openDrawer(id); load();
  };
  $("#dFav").onclick = async () => {
    await api(`/api/titles/${id}/favorite`, { method: "POST", body: { value: !t.favorite } });
    openDrawer(id); load();
  };
  $("#dWanted").onclick = async () => {
    await api(`/api/titles/${id}/wanted`, { method: "POST", body: { value: !t.wanted } });
    openDrawer(id); load();
  };
  const dMini = $("#dMini");
  if (dMini) dMini.onclick = async () => {
    await api(`/api/titles/${id}`, { method: "PATCH", body: { is_miniseries: !t.is_miniseries } });
    openDrawer(id); load();
  };
  $("#dNext").onclick = async () => {
    const btn = $("#dNext");
    try {
      if (t.watch_next) {
        await api(`/api/titles/${id}/watch-next`, { method: "DELETE" });
        openDrawer(id); load();
      } else {
        // instant confirmation: color the drawer button + keep the list row
        // in sync — the pin only lands in the DB when the copy job finishes,
        // so refetching now would repaint both as un-clicked
        t.watch_next = t.kind;
        const st = state.titles.find(x => String(x.id) === String(id));
        if (st) st.watch_next = t.kind;
        btn.classList.add("primary");
        btn.textContent = "▶ Watch Next ✓";
        const r = await api(`/api/titles/${id}/watch-next`, { method: "POST" });
        // watchJob reloads the list itself once the copy job completes
        watchJob(r.job_id, `Watch Next — ${t.title}`);
      }
    } catch (err) {
      // roll the optimistic confirmation back on failure
      t.watch_next = null;
      const st = state.titles.find(x => String(x.id) === String(id));
      if (st) st.watch_next = null;
      btn.classList.remove("primary");
      btn.textContent = "▶ Set Watch Next";
      alert(err.message);
    }
  };
  // ---- episode checklist (series) ----
  $("#drawer").dataset.tid = id;
  const epAll = $("#epAll");
  if (epAll) epAll.onclick = async () => {
    try {
      const r = await api(`/api/titles/${id}/episodes-watched`,
        { method: "POST", body: { watched: true, season: null } });
      $$("#dBody .epitem").forEach(x => { x.classList.add("seen"); $("input", x).checked = true; });
      updateEpProgress(r);
      if (r.promoted) markDrawerWatched();
    } catch (err) { alert(err.message); }
  };
  const epNone = $("#epNone");
  if (epNone) epNone.onclick = async () => {
    try {
      const r = await api(`/api/titles/${id}/episodes-watched`,
        { method: "POST", body: { watched: false, season: null } });
      $$("#dBody .epitem").forEach(x => { x.classList.remove("seen"); $("input", x).checked = false; });
      updateEpProgress(r);
    } catch (err) { alert(err.message); }
  };
  const doMove = async (target, destRoot) => {
    const body = { target, purge_others: true };
    if (destRoot) body.dest_root = destRoot;
    try {
      const r = await api(`/api/titles/${id}/move`, { method: "POST", body });
      if (r.queued) {
        alert(`The destination drive (${r.drive}) is not connected right now.
The move was queued and runs automatically when it is connected (Settings → Drive queues).`);
      } else {
        watchJob(r.job_id, `Move → ${destLabel(destRoot, target)}`);
      }
    } catch (err) { alert(err.message); }
  };
  $("#dAnyFolder").onclick = () => openDestPicker(t.title, (target, root) => {
    const dupNote = t.on_both_drives
      ? `\n\nThis title is duplicated on BOTH sides — after the move every copy outside the destination is deleted.` : "";
    if (!confirm(`Move "${t.title}" (${t.files.length} file(s)) to ${root}?${dupNote}`)) return;
    doMove(target, root);
  });
  $$("#dBody [data-dest]").forEach(b => {
    const { target, root } = b.dataset;
    b.onclick = async () => {
      const dupNote = t.on_both_drives
        ? `\n\nThis title is duplicated on BOTH sides — after the move every copy outside the destination is deleted.` : "";
      if (!confirm(`Move "${t.title}" (${t.files.length} file(s)) to ${destLabel(root, target)}?${dupNote}`)) return;
      await doMove(target, root || null);
    };
  });

  // ---- season picker (series: move only the checked seasons) ----
  const dSeasons = $("#dSeasons");
  if (dSeasons) {
    // same convention as the backend: a file without a parsed season is S1
    const seasons = [...new Set(t.files.filter(f => !f.missing)
      .map(f => f.season ?? 1))].sort((a, b) => a - b);
    const epCount = s => t.files.filter(f => !f.missing && (f.season ?? 1) === s).length;
    $("#mvName").textContent = t.title;
    $("#mvList").innerHTML = seasons.map(s => `
      <label class="mvitem">
        <input type="checkbox" data-season="${s}" checked>
        <span class="mvname">Season ${s}</span>
        <span class="mvcount">${epCount(s)} file(s)</span>
      </label>`).join("");
    $("#mvAll").onclick = () => $$("#mvList input").forEach(i => i.checked = true);
    $("#mvNone").onclick = () => $$("#mvList input").forEach(i => i.checked = false);
    const moveChecked = async (target, destRoot) => {
      const sel = $$("#mvList input:checked").map(i => Number(i.dataset.season));
      if (!sel.length) { alert("Tick at least one season to move."); return; }
      const body = { target, purge_others: true, seasons: sel };
      if (destRoot) body.dest_root = destRoot;
      if (!confirm(`Move season(s) ${sel.join(", ")} of "${t.title}" to ${destLabel(destRoot, target)}? Unticked seasons stay where they are.`)) return;
      try {
        const r = await api(`/api/titles/${id}/move`, { method: "POST", body });
        $("#mvModal").classList.add("hidden");
        if (r.queued) {
          alert(`The destination drive (${r.drive}) is not connected right now.
The move was queued and runs automatically when it is connected (Settings → Drive queues).`);
        } else {
          watchJob(r.job_id, `Move S${sel.join(",S")} → ${destLabel(destRoot, target)}`);
        }
      } catch (err) { alert(err.message); }
    };
    // canonical side buttons stay; every other folder lives in the popup
    api("/api/move-destinations").then(({ destinations: dests }) => {
      const byRole = role => dests.find(d => d.role === role);
      const ext = dests.find(d => d.role === "backup" && d.kind === t.kind)
        || byRole("backup");
      const wire = (el, d) => {
        if (!el || !d) return;
        el.dataset.target = d.role === "internal" ? "internal" : "external";
        el.dataset.root = d.root;
        el.title = d.mounted
          ? d.root
          : `${d.root} — not connected, move will be queued`;
        el.textContent = `${destLabel(d.root, d.role)}${d.mounted ? "" : " ⏳"}`;
      };
      wire($("#mvInt"), byRole("internal"));
      wire($("#mvExt"), ext);
      $$("#mvDests [data-target]").forEach(b => {
        b.onclick = () => moveChecked(b.dataset.target, b.dataset.root || null);
      });
    }).catch(() => {
      $("#mvInt").onclick = () => moveChecked("internal");
      $("#mvExt").onclick = () => moveChecked("external");
    });
    // season picker can also target ANY configured folder, full paths —
    // created once, but re-bound on every drawer open (t is per-open)
    let dAnySeasons = $("#mvDests [data-anyfolder]");
    if (!dAnySeasons) {
      dAnySeasons = document.createElement("button");
      dAnySeasons.className = "btn mini";
      dAnySeasons.dataset.anyfolder = "1";
      dAnySeasons.textContent = "📁 Other folder…";
      dAnySeasons.title = "Pick ANY configured folder — full paths, including other library folders on the same drive";
      $("#mvDests").appendChild(dAnySeasons);
    }
    dAnySeasons.onclick = () => openDestPicker(t.title, (target, root) => {
      if (!confirm(`Move checked season(s) of "${t.title}" to ${root}? Unticked seasons stay where they are.`)) return;
      moveChecked(target, root);
    });
    dSeasons.onclick = () => $("#mvModal").classList.remove("hidden");
  }

  // ---- edit details ----
  $("#eSave").onclick = async () => {
    const val = k => { const el = $("#e_" + k); return el ? el.value.trim() : null; };
    const numOrNull = k => { const v = val(k); return v === "" || v === null ? null : Number(v); };
    const textOrNull = k => { const v = val(k); return v === "" ? null : v; };
    const patch = {
      title: textOrNull("title"),
      kind: $("#e_kind").value,
      year: numOrNull("year"),
      overview: textOrNull("overview"),
      director: textOrNull("director"),
      creator: textOrNull("creator"),
      network: textOrNull("network"),
      stars: val("stars").split(",").map(s => s.trim()).filter(Boolean),
      genres: val("genres").split(",").map(s => s.trim()).filter(Boolean),
    };
    for (const [k] of EDIT_FIELDS) {
      const v = numOrNull(k);
      if (v !== null) patch[k] = v;
    }
    // identity change -> wipe enrichment server-side (fresh match next Enrich)
    try {
      await api(`/api/titles/${id}`, { method: "PATCH", body: patch });
      openDrawer(id); load();
    } catch (err) { alert(err.message); }
  };
  $("#eRevert").onclick = () => openDrawer(id);
}
// in-place updates so the checklist never collapses while ticking
function updateEpProgress(r) {
  const bar = $("#dBody .epprogbar > span");
  const count = $("#dBody .epcount");
  if (bar) bar.style.width = `${r.episodes_total ? Math.round(100 * r.episodes_watched / r.episodes_total) : 0}%`;
  if (count) count.textContent = `${r.episodes_watched}/${r.episodes_total}`;
}
function markDrawerWatched() {
  const btn = $("#dWatch");
  if (btn) { btn.textContent = "✓ Watched"; btn.classList.add("primary"); }
  const t = state.titles.find(x => String(x.id) === $("#drawer").dataset.tid);
  if (t) { t.watched = true; renderRows(); }
}
$("#dBody").addEventListener("click", async e => {
  const p = e.target.closest("[data-person]");
  if (p) {
    $("#drawer").classList.remove("open");
    searchPerson(p.dataset.person);
    return;
  }
  const a = e.target.closest("[data-open]");
  if (!a) return;
  e.preventDefault();
  const parts = a.dataset.open.split(":");
  try { await api(`/api/titles/${parts[0]}/files/${parts[1]}/open`); }
  catch (err) { alert(err.message); }
});
// episode checklist: change (not click) so the label's synthetic event
// cannot double-fire the API call
$("#dBody").addEventListener("change", async e => {
  const input = e.target.closest('.epitem input[type="checkbox"]');
  if (!input) return;
  const ep = input.closest(".epitem");
  const tid = $("#drawer").dataset.tid;
  const watched = input.checked;
  try {
    const r = await api(`/api/titles/${tid}/files/${ep.dataset.epid}/watched`,
      { method: "POST", body: { watched } });
    ep.classList.toggle("seen", watched);
    updateEpProgress(r);
    if (r.promoted) markDrawerWatched(); // last episode ticked -> series watched
  } catch (err) {
    input.checked = !watched; // revert on failure
    alert(err.message);
  }
});
$("#dClose").onclick = () => $("#drawer").classList.remove("open");
$("#mvClose").onclick = () => $("#mvModal").classList.add("hidden");
$("#mvModal").addEventListener("click", e => {
  if (e.target === $("#mvModal")) $("#mvModal").classList.add("hidden");
});
$("#dmClose").onclick = () => $("#destModal").classList.add("hidden");
$("#destModal").addEventListener("click", e => {
  if (e.target === $("#destModal")) $("#destModal").classList.add("hidden");
});

/* ---------- settings ---------- */
async function openSettings() {
  const s = await api("/api/settings");
  $("#s_tmdb_api_key").value = s.tmdb_api_key_set ? s.tmdb_api_key : "";
  $("#s_omdb_api_key").value = s.omdb_api_key_set ? s.omdb_api_key : "";
  $("#s_llm_api_key").value = s.llm_api_key_set ? s.llm_api_key : "";
  $("#s_llm_base_url").value = s.llm_base_url || "";
  $("#s_llm_model").value = s.llm_model || "";
  $("#s_internal_root").value = s.internal_root || "";
  $("#s_backup_movies_root").value = s.backup_movies_root || "";
  $("#s_backup_series_root").value = s.backup_series_root || "";
  $("#s_liked_movies_root").value = s.liked_movies_root || "";
  $("#s_liked_series_root").value = s.liked_series_root || "";
  $("#s_external_root").value = s.external_root || "";
  $("#s_move_native_dialog").checked = s.move_native_dialog !== "0";
  const { roots } = await api("/api/roots");
  $("#rootList").innerHTML = roots.map(r => `
    <li>
      <span class="path" title="${esc(r.path)}">${esc(r.path)}</span>
      ${r.exists ? "" : '<span class="off">offline</span>'}
      <span class="st">${(r.stats && r.stats.titles) || 0} titles</span>
      <button class="btn mini" data-toggle-root="${r.id}">${r.enabled ? "enabled" : "disabled"}</button>
      <button class="btn mini ghost" data-del-root="${r.id}">✕</button>
    </li>`).join("");
  $("#modal").classList.remove("hidden");
  refreshDriveQueue(); // per-drive pending operations (moves/deletes)
  refreshPairing();    // QR + paired device list (Devices section)
}

/* ---------- Devices (QR pairing) ---------- */
let pairTimer = null;
async function refreshPairing() {
  try {
    const info = await api("/api/pairing/info");
    $("#pairQr").src = "/api/pairing/qr.svg";
    $("#pairCode").textContent = info.code;
    $("#pairLan").textContent = info.lan_url ? `address: ${info.lan_url}` : "";
    updatePairExpiry(info.expires_in);
    clearInterval(pairTimer);
    pairTimer = setInterval(() => updatePairExpiry(-1), 1000);
    $("#deviceList").innerHTML = info.devices.map(d => `
      <li>
        <span class="path">📱 ${esc(d.name)}</span>
        <span class="st">paired ${fmtDate(d.created_at)}${d.last_seen ? " · last seen " + fmtDate(d.last_seen) : ""}</span>
        <button class="btn mini ghost" data-revoke-device="${d.id}" title="Remove this device's access">Revoke</button>
      </li>`).join("") || '<li><span class="st">No other devices paired.</span></li>';
  } catch (e) { /* section is cosmetic; never block settings */ }
}
function updatePairExpiry(secsLeft) {
  const el = $("#pairExpiry");
  if (secsLeft === -1) {           // tick: recompute from displayed deadline
    secsLeft = Math.max(0, (updatePairExpiry.deadline || 0) - Date.now() / 1000);
    if (secsLeft === 0) { refreshPairing(); return; }
  } else {
    updatePairExpiry.deadline = Date.now() / 1000 + secsLeft;
  }
  const m = Math.floor(secsLeft / 60), s = Math.floor(secsLeft % 60);
  el.textContent = `code expires in ${m}:${String(s).padStart(2, "0")}`;
}
$("#btnPairRegen").onclick = async () => {
  await api("/api/pairing/regenerate", { method: "POST" });
  refreshPairing();
};
$("#btnPairRefresh").onclick = refreshPairing;
$("#deviceList").addEventListener("click", async e => {
  const id = e.target.dataset && e.target.dataset.revokeDevice;
  if (!id) return;
  await api(`/api/pairing/devices/${id}`, { method: "DELETE" });
  refreshPairing();
});
$("#btnSettings").onclick = openSettings;
function closeModal() {
  $("#modal").classList.add("hidden");
  clearInterval(pairTimer); // stop the pairing-code countdown
}
$("#mClose").onclick = closeModal;
$("#modal").addEventListener("click", e => {
  if (e.target === $("#modal")) closeModal();
});
async function saveSettings() {
  await api("/api/settings", { method: "POST", body: { values: {
    tmdb_api_key: $("#s_tmdb_api_key").value.trim(),
    omdb_api_key: $("#s_omdb_api_key").value.trim(),
    llm_api_key: $("#s_llm_api_key").value.trim(),
    llm_base_url: $("#s_llm_base_url").value.trim(),
    llm_model: $("#s_llm_model").value.trim(),
    internal_root: $("#s_internal_root").value.trim(),
    backup_movies_root: $("#s_backup_movies_root").value.trim(),
    backup_series_root: $("#s_backup_series_root").value.trim(),
    liked_movies_root: $("#s_liked_movies_root").value.trim(),
    liked_series_root: $("#s_liked_series_root").value.trim(),
    external_root: $("#s_external_root").value.trim(),
    move_native_dialog: $("#s_move_native_dialog").checked ? "1" : "0",
  } } });
}
async function testProvider(kind) {
  const out = $("#testOut");
  out.className = "testout";
  out.textContent = "testing…";
  try {
    await saveSettings(); // persist first so the probe uses what you typed
    const r = await api(`/api/test/${kind}`, { method: "POST" });
    out.className = "testout " + (r.ok ? "ok" : "bad");
    out.textContent = (r.ok ? "✓ " : "✗ ") + r.detail;
  } catch (err) {
    out.className = "testout bad";
    out.textContent = "✗ " + err.message;
  }
}
$("#btnTestTmdb").onclick = () => testProvider("tmdb");
$("#btnTestLlm").onclick = () => testProvider("llm");
$("#mSave").onclick = async () => {
  await saveSettings();
  $("#modal").classList.add("hidden");
};
$("#addRoot").onclick = async () => {
  const p = $("#newRoot").value.trim();
  if (!p) return;
  try {
    await api("/api/roots", { method: "POST", body: { path: p } });
    $("#newRoot").value = "";
    openSettings(); renderRootOptions();
  } catch (err) { alert(err.message); }
};
$("#rootList").addEventListener("click", async e => {
  const del = e.target.closest("[data-del-root]");
  const tog = e.target.closest("[data-toggle-root]");
  if (del) { await api(`/api/roots/${del.dataset.delRoot}`, { method: "DELETE" }); openSettings(); renderRootOptions(); }
  if (tog) { await api(`/api/roots/${tog.dataset.toggleRoot}/toggle`, { method: "POST" }); openSettings(); renderRootOptions(); }
});

/* ---------- jobs / toast ---------- */
let jobTimer = null;
let currentJobId = null;
function watchJob(jid, label, onDone) {
  currentJobId = jid;
  $("#tTitle").textContent = label;
  $("#toast").classList.remove("hidden");
  clearInterval(jobTimer);
  const poll = async () => {
    const j = await api(`/api/jobs/${jid}`);
    const pct = j.total ? Math.round(100 * j.progress / j.total) : (j.status === "done" ? 100 : 5);
    $("#tFill").style.width = pct + "%";
    // live job message: names the title + file path currently being worked
    // on — this is how you spot a run stuck on a junk entry (it also shows
    // the "LLM cleanup running…" warnings for the slowest step)
    $("#tMsg").textContent = j.message || "";
    $("#tLog").textContent = j.log.join("\n");
    $("#tLog").scrollTop = $("#tLog").scrollHeight;
    if (j.status !== "running") {
      clearInterval(jobTimer);
      $("#tTitle").textContent = `${label} — ${j.status}`;
      $("#tMsg").textContent = j.message || "";
      load();
      if (onDone && j.status === "done") onDone();
    }
  };
  poll();
  jobTimer = setInterval(poll, 1200);
}

$("#btnScan").onclick = async () => {
  try {
    const r = await api("/api/scan", { method: "POST", body: {} });
    watchJob(r.job_id, "Scan");
  } catch (err) { alert(err.message); }
};
$("#btnEnrich").onclick = async () => {
  try {
    const r = await api("/api/enrich", { method: "POST", body: {} });
    watchJob(r.job_id, "Enrich", async () => {
      // chain the light backfill pass: cert / RT / miniseries tags for
      // ALREADY-matched rows the default Enrich skips
      try {
        const b = await api("/api/backfill-ratings", { method: "POST" });
        watchJob(b.job_id, "Backfill");
      } catch (e) { /* non-fatal */ }
    });
  } catch (err) { alert(err.message); }
};
$("#tClose").onclick = async () => {
  clearInterval(jobTimer);
  $("#toast").classList.add("hidden");
  // X actually CANCELS a running job (cooperative stop between work
  // items) — hiding the toast alone left jobs burning API calls forever
  if (currentJobId) {
    try { await api(`/api/jobs/${currentJobId}/cancel`, { method: "POST" }); } catch (e) {}
    currentJobId = null;
    load();
  }
};

/* ---------- folder picker ---------- */
let fpTargetInput = null;
let fpCurrentPath = null;
let fpParentPath = null;

async function fpBrowse(path) {
  try {
    const d = await api(`/api/browse?path=${encodeURIComponent(path || "")}`);
    fpCurrentPath = d.path;
    fpParentPath = d.parent;
    $("#fpPath").value = d.path;
    $("#fpUp").disabled = !d.parent;
    $("#fpShortcuts").innerHTML = d.shortcuts.map(s =>
      `<button class="btn mini" data-fpgo="${esc(s.path)}">${esc(s.label)}</button>`).join("");
    $("#fpDirs").innerHTML = d.dirs.length ? d.dirs.map(dir => `
      <div class="fpdir" data-fpgo="${esc(dir.path)}" data-locked="${dir.locked ? 1 : 0}">
        <span>📁 ${esc(dir.name)}</span>
      </div>`).join("")
      : '<p class="hint">No subfolders — you can select this folder.</p>';
  } catch (err) {
    alert(err.message);
  }
}
function openPicker(inputId) {
  fpTargetInput = inputId;
  $("#fpModal").classList.remove("hidden");
  const cur = $("#" + inputId).value.trim();
  fpBrowse(cur || null);
}
document.addEventListener("click", e => {
  const b = e.target.closest("[data-browse]");
  if (b) { openPicker(b.dataset.browse); return; }
  const go = e.target.closest("[data-fpgo]");
  if (go && go.dataset.locked !== "1") fpBrowse(go.dataset.fpgo);
});
$("#fpUp").onclick = () => { if (fpParentPath) fpBrowse(fpParentPath); };
$("#fpClose").onclick = () => $("#fpModal").classList.add("hidden");
$("#fpModal").addEventListener("click", e => {
  if (e.target === $("#fpModal")) $("#fpModal").classList.add("hidden");
});
$("#fpSelect").onclick = () => {
  if (fpTargetInput && fpCurrentPath) $("#" + fpTargetInput).value = fpCurrentPath;
  $("#fpModal").classList.add("hidden");
};

/* ---------- duplicates ---------- */
let dupGroups = [];      // last loaded duplicate groups
let dupOffline = new Set(); // roots whose drives are not mounted
let bulk = null;         // bulk-wizard state

async function openDupes() {
  $("#dupList").innerHTML = '<p class="hint">Scanning…</p>';
  $("#dupModal").classList.remove("hidden");
  try {
    const [{ groups }, rootsInfo] = await Promise.all([
      api("/api/duplicates"), api("/api/roots"),
    ]);
    dupGroups = groups;
    dupOffline = new Set(rootsInfo.roots.filter(r => !r.exists).map(r => r.path));
    if (!groups.length) {
      $("#dupList").innerHTML = '<p class="hint">No duplicates found. 🎉 Every title lives in exactly one place.</p>';
      return;
    }
    $("#dupList").innerHTML = groups.map(g => `
      <div class="dupgroup">
        <div class="duphead">
          <b>${esc(g.title)}</b> ${g.year ? `<span class="dim">(${g.year})</span>` : ""}
          <span class="badge ${g.kind}">${g.kind}</span>
          ${g.scope === "episodes" ? `<span class="badge">${g.dup_episodes} ep(s) doubled</span>` : ""}
          ${g.scope === "two_rows" ? `<span class="badge warn">cataloged twice — two library rows for the same title</span>` : ""}
          <span class="dupdelta">${fmtSize(g.size_delta)} size difference</span>
        </div>
        ${g.copies.map((c, i) => `
          <div class="dupcopy">
            <span class="badge">${i === 0 ? "★ biggest" : "copy"}</span>
            <span>${esc(c.label)}</span>
            <span class="dim">${fmtSize(c.size_bytes)}${c.file_count ? ` · ${c.file_count} file(s)` : ""}</span>
            ${g.scope === "two_rows"
              ? `<button class="btn mini ghost" data-delrow="${c.title_id}">🗑 delete this row</button>`
              : `<button class="btn mini ghost" data-delroot="${c.title_id}" data-root="${esc(c.root)}">🗑 delete this copy</button>`}
          </div>`).join("")}
      </div>`).join("");
  } catch (err) {
    $("#dupList").innerHTML = `<p class="hint">Failed: ${esc(err.message)}</p>`;
  }
}
/* bulk wizard: only IDENTICAL copies (byte-for-byte same size) */
function identicalGroups() {
  return dupGroups.filter(g =>
    g.scope !== "two_rows" &&
    g.copies.length > 1 &&
    g.copies.every(c => c.size_bytes === g.copies[0].size_bytes) &&
    g.copies.some(c => c.root && !dupOffline.has(c.root))); // something deletable online
}

function startBulk() {
  const groups = identicalGroups();
  if (!groups.length) {
    alert("No identical-size duplicates to bulk-delete. (Different-size copies are kept out of bulk mode on purpose — pick those manually.)");
    return;
  }
  // group by location-signature so the user decides once per combination
  const combos = new Map();
  for (const g of groups) {
    const sig = g.copies.map(c => c.root).sort().join(" | ");
    if (!combos.has(sig)) combos.set(sig, []);
    combos.get(sig).push(g);
  }
  bulk = { combos: [...combos.entries()], step: 0, plan: [], freed: 0, count: 0 };
  $("#dupList").innerHTML = '<p class="hint">Bulk delete — deciding…</p>';
  bulkStep();
}

function bulkStep() {
  if (bulk.step >= bulk.combos.length) return bulkRun();
  const [sig, groups] = bulk.combos[bulk.step];
  const roots = sig.split(" | ");
  const keepRoot = roots.find(r => !dupOffline.has(r)) || roots[0];
  const totalMb = groups.reduce((s, g) =>
    s + (g.copies.find(c => c.root !== keepRoot)?.size_bytes || 0) / 1e6, 0);
  $("#dupList").innerHTML = `
    <div class="bulkbox">
      <h3>Combination ${bulk.step + 1} of ${bulk.combos.length}</h3>
      <p class="hint">
        ${groups.length} title(s) have <b>identical copies</b> (same byte size) in these locations:
        ${roots.map(r => `<b>${esc(locBadge(r))}</b>${dupOffline.has(r) ? " (offline)" : ""}`).join(" and ")}.
      </p>
      <p class="hint">Which location's copies should be deleted? The others are kept. Offline drives are skipped automatically.</p>
      <div class="bulkchoices">
        ${roots.map(r => `
          <button class="btn ${r === keepRoot ? "" : "primary"}" data-bulkpick="${esc(r)}"
            ${dupOffline.has(r) ? "disabled" : ""}>
            Delete copies on ${esc(locBadge(r))}${dupOffline.has(r) ? " (offline)" : ""}
          </button>`).join("")}
        <button class="btn ghost" data-bulkskip>Skip this combination</button>
      </div>
      <p class="hint">~${totalMb.toFixed(0)} MB would be freed by deleting from "${esc(locBadge(keepRoot))}".</p>
    </div>`;
}
$("#dupList").addEventListener("click", e => {
  if (!bulk) return;
  const pick = e.target.closest("[data-bulkpick]");
  if (pick) {
    const root = pick.dataset.bulkpick;
    const [, groups] = bulk.combos[bulk.step];
    for (const g of groups) {
      for (const c of g.copies) {
        if (c.root === root && !dupOffline.has(c.root)) {
          bulk.plan.push({ title_id: c.title_id, root: c.root });
          bulk.freed += c.size_bytes || 0;
          bulk.count += c.file_count || 1;
        }
      }
    }
    bulk.step++;
    bulkStep();
    return;
  }
  if (e.target.closest("[data-bulkskip]")) { bulk.step++; bulkStep(); }
});

async function bulkRun() {
  const plan = bulk.plan;
  const freed = bulk.freed;
  const count = bulk.count;
  bulk = null;
  if (!plan.length) { openDupes(); return; }
  if (!confirm(`Delete ${plan.length} copies (${count} files, ~${fmtSize(freed)}) from disk?\nAre you sure?`)) {
    openDupes(); return;
  }
  $("#dupList").innerHTML = `<p class="hint">Deleting ${plan.length} copies…</p>`;
  let ok = 0;
  const failed = [];
  for (const p of plan) {
    try {
      const q = p.root ? `?root=${encodeURIComponent(p.root)}` : "";
      await api(`/api/duplicates/${p.title_id}${q}`, { method: "DELETE" });
      ok++;
    } catch (err) {
      failed.push(`title ${p.title_id}: ${err.message}`);
    }
  }
  const msg = `Bulk delete finished: ${ok} of ${plan.length} copies deleted.` +
    (failed.length ? `\n\nSome failed:\n${failed.join("\n")}` : "");
  alert(msg);
  openDupes(); load();
}

/* ---------- watched log (titles watched without owning a file) ---------- */
async function openHistory() {
  $("#histModal").classList.remove("hidden");
  await renderHistoryList();
}

async function renderHistoryList() {
  const { history } = await api("/api/history");
  const el = $("#histList");
  el.innerHTML = history.length
    ? history.map(t => `
      <div class="histrow" data-id="${t.id}">
        <div>
          <b>${esc(t.title)}</b>${t.year ? ` <span class="dim">(${t.year})</span>` : ""}
          <span class="badge ${t.kind}">${t.kind}</span>
          ${t.watched_at ? `<span class="dim">· seen ${fmtDate(t.watched_at)}</span>` : ""}
          ${t.wanted_note ? `<div class="dim">${esc(t.wanted_note)}</div>` : ""}
        </div>
        <button class="btn mini" data-histdel="${t.id}" data-name="${esc(t.title)}">✕ Remove</button>
      </div>`).join("")
    : '<p class="hint">Nothing recorded yet. Add a title above — e.g. a film you saw at a friend\'s place or in a cinema.</p>';
}

$("#btnHistory").onclick = openHistory;
$("#hClose").onclick = () => $("#histModal").classList.add("hidden");
$("#histModal").addEventListener("click", e => {
  if (e.target === $("#histModal")) $("#histModal").classList.add("hidden");
});

/* gather the current form values as the payload shared by check/add */
function histFormPayload() {
  const title = $("#hTitle").value.trim();
  if (!title) { $("#hTitle").focus(); return null; }
  const body = { title, kind: $("#hKind").value };
  const year = parseInt($("#hYear").value, 10);
  if (year) body.year = year;
  const note = $("#hNote").value.trim();
  if (note) body.note = note;
  return body;
}

function histResetForm() {
  $("#hTitle").value = ""; $("#hYear").value = ""; $("#hNote").value = "";
  $("#hResults").innerHTML = "";
}

/* Check: local catalog first, then TMDB candidates to confirm */
$("#hAdd").onclick = async () => {
  const body = histFormPayload();
  if (!body) return;
  $("#hAdd").disabled = true;
  $("#hResults").innerHTML = '<p class="hint">Searching…</p>';
  try {
    const r = await api("/api/history/search", { method: "POST", body });
    renderHistCandidates(r, body);
  } catch (err) {
    $("#hResults").innerHTML = "";
    alert(err.message);
  }
  $("#hAdd").disabled = false;
};

function renderHistCandidates(r, formBody) {
  const el = $("#hResults");
  const cards = [];
  if (r.local.length) {
    cards.push('<p class="hint">Already in your catalog:</p>');
    for (const t of r.local) {
      cards.push(`
        <div class="cand">
          ${imgTag("candimg", t.poster)}
          <div class="candtxt">
            <b>${esc(t.title)}</b>${t.year ? ` <span class="dim">(${t.year})</span>` : ""}
            <span class="badge ${t.kind}">${t.kind}</span>
            ${t.history ? '<span class="badge seen">✓ watched</span>' : ""}
            <div class="dim">${(t.genres || []).slice(0, 3).join(" · ")
              || esc(t.overview || "").slice(0, 120)}</div>
          </div>
          <button class="btn mini" data-picklocal="${t.id}" data-title="${esc(t.title)}"
            data-kind="${esc(t.kind)}" data-year="${t.year || ""}">${t.history ? "Already logged" : "Mark watched"}</button>
        </div>`);
    }
  }
  if (r.tmdb.length) {
    cards.push(`<p class="hint">${r.local.length ? "Or is it one of these?" : "Is it one of these?"} Confirm to fetch poster/ratings:</p>`);
    for (const c of r.tmdb) {
      cards.push(`
        <div class="cand">
          ${imgTag("candimg", c.poster)}
          <div class="candtxt">
            <b>${esc(c.name)}</b>${c.year ? ` <span class="dim">(${c.year})</span>` : ""}
            <div class="dim">${esc(c.overview || "")}</div>
          </div>
          <button class="btn mini" data-picktmdb="${c.tmdb_id}"
            data-name="${esc(c.name)}" data-year="${c.year || ""}">This one</button>
        </div>`);
    }
  }
  if (!r.local.length && !r.tmdb.length) {
    cards.push(`<p class="hint">No match found${r.tmdb_error ? ` (${esc(r.tmdb_error)})` : ""}.
      You can still record it without details:</p>`);
  }
  cards.push('<button class="btn mini ghost" data-skiplookup>Add without lookup</button>');
  el.innerHTML = cards.join("");
}

/* pick handler: mark an existing row seen, or add with the pinned TMDB id */
async function histPick(body, tmdbId, reuseTitle, reuseYear) {
  const payload = { ...body };
  if (tmdbId) {
    payload.tmdb_id = tmdbId;
    if (reuseTitle) payload.title = reuseTitle;
    if (reuseYear) payload.year = parseInt(reuseYear, 10) || payload.year;
  }
  try {
    await api("/api/history", { method: "POST", body: payload });
    histResetForm();
    await renderHistoryList();
    load();
  } catch (err) { alert(err.message); }
}

$("#hResults").addEventListener("click", e => {
  const localBtn = e.target.closest("[data-picklocal]");
  if (localBtn) {
    if (localBtn.disabled || localBtn.textContent.trim() === "Already logged") return;
    localBtn.disabled = true;
    // reuse the backend's dedupe matching by sending the row's own identity
    histPick({ title: localBtn.dataset.title, kind: localBtn.dataset.kind,
               year: localBtn.dataset.year ? parseInt(localBtn.dataset.year, 10) : undefined,
               note: (histFormPayload() || {}).note }, null);
    return;
  }
  const tmdbBtn = e.target.closest("[data-picktmdb]");
  if (tmdbBtn) {
    tmdbBtn.disabled = true;
    histPick(histFormPayload() || {}, tmdbBtn.dataset.picktmdb,
             tmdbBtn.dataset.name, tmdbBtn.dataset.year);
    return;
  }
  if (e.target.closest("[data-skiplookup]")) {
    histPick(histFormPayload() || {}, null);
  }
});

$("#histList").addEventListener("click", async e => {
  const btn = e.target.closest("[data-histdel]");
  if (!btn) return;
  if (!confirm(`Remove "${btn.dataset.name}" from the watched log?`)) return;
  btn.disabled = true;
  try {
    await api(`/api/history/${btn.dataset.histdel}`, { method: "DELETE" });
    await renderHistoryList();
    load();
  } catch (err) {
    btn.disabled = false;
    alert(err.message);
  }
});

$("#btnDupes").onclick = () => {
  // launcher lives inside the settings sheet now — close it first
  $("#modal").classList.add("hidden");
  openDupes();
};
$("#btnBulk").onclick = startBulk;
$("#dClose2").onclick = () => { bulk = null; $("#dupModal").classList.add("hidden"); };
$("#dupModal").addEventListener("click", e => {
  if (e.target === $("#dupModal")) { bulk = null; $("#dupModal").classList.add("hidden"); }
});
async function delCopy(btn, payload, what) {
  if (!payload.title_id || payload.title_id === "undefined") {
    alert("Internal error: missing title id — please reload the page (Ctrl+Shift+R) and try again.");
    return;
  }
  if (!confirm(`Are you sure? This deletes ${what} from disk.`)) return;
  btn.disabled = true;
  try {
    // root as query param: immune to content-type/422 quirks
    const q = payload.root ? `?root=${encodeURIComponent(payload.root)}` : "";
    const r = await api(`/api/duplicates/${payload.title_id}${q}`, { method: "DELETE" });
    if (r.queued) {
      btn.textContent = "queued for drive connect";
      alert(`That copy's drive (${r.drives.join(", ")}) is not connected.
The deletion was queued and runs automatically when it is connected (Settings → Drive queues).`);
      return;
    }
    // instant feedback: remove the copy row, then re-check the group
    const copyRow = btn.closest(".dupcopy");
    if (copyRow) copyRow.remove();
    btn.textContent = `deleted (${r.removed_files} files)`;
    // remaining duplicate state may have changed: refresh view + library
    setTimeout(() => { openDupes(); load(); }, 600);
  } catch (err) {
    btn.disabled = false;
    alert(err.message);
  }
}
$("#dupList").addEventListener("click", e => {
  const byRoot = e.target.closest("[data-delroot]");
  if (byRoot) return delCopy(byRoot, { title_id: byRoot.dataset.delroot, root: byRoot.dataset.root }, "this copy");
  const byRow = e.target.closest("[data-delrow]");
  if (byRow) return delCopy(byRow, { title_id: byRow.dataset.delrow }, "this library row");
});

/* ---------- film recommender (AI-researched picks) ---------- */
let recCurrent = null;   // the recommendation currently in the popup
let recBusy = false;

async function refreshRecPill() {
  try {
    const s = await api("/api/recommendations/status");
    const pill = $("#recCount");
    if (s.pending > 0) {
      pill.textContent = s.pending;
      pill.classList.remove("hidden");
    } else {
      pill.classList.add("hidden");
    }
    if (s.enabled && s.low && !s.running_job && !s.refill_job_id) {
      // crossed the low-water line while the user was browsing — server
      // status endpoint already tried; nothing more to do here
    }
  } catch (e) { /* non-fatal */ }
}

async function openRecommend() {
  if (recBusy) return;
  recBusy = true;
  $("#recModal").classList.remove("hidden");
  $("#recBody").innerHTML = '<p class="hint">Fetching your next recommendation…</p>';
  try {
    const r = await api("/api/recommendations/next");
    if (!r.recommendation) {
      const hasKey = (await api("/api/recommendations/status")).enabled;
      $("#recBody").innerHTML = hasKey
        ? `<p class="hint">The queue is empty — the researcher is on it.
           New picks appear automatically; check back in a minute.</p>
           <div class="recactions"><button class="btn ghost" id="recWaitClose">Close</button></div>`
        : `<p class="hint">The recommender needs your <b>z.ai API key</b> —
           add it in <b>⚙ Settings</b> and it will research picks for you
           automatically.</p>
           <div class="recactions"><button class="btn ghost" id="recWaitClose">Close</button></div>`;
      $("#recWaitClose").onclick = () => $("#recModal").classList.add("hidden");
      return;
    }
    recCurrent = r.recommendation;
    renderRec(recCurrent, r.pending);
    refreshRecPill();
  } catch (err) {
    $("#recBody").innerHTML = `<p class="hint">Failed: ${esc(err.message)}</p>`;
  } finally {
    recBusy = false;
  }
}

function recMetaFacts(m) {
  return [
    m.year && `${m.year}`,
    m.kind === "series" && (m.seasons ? `${m.seasons} season${m.seasons > 1 ? "s" : ""}` : "series"),
    m.kind === "series" && m.episodes && `${m.episodes} episodes`,
    m.runtime && (m.kind === "series" ? `${m.runtime} min/ep` : `${m.runtime} min`),
    m.cert,
    m.rating_imdb && `★ ${m.rating_imdb} IMDb`,
    m.rating_tmdb && `★ ${m.rating_tmdb} TMDB`,
    m.rating_rt != null && `🍅 ${m.rating_rt}%`,
  ].filter(Boolean).join(" · ");
}

function renderRec(m, pending) {
  const kindLabel = m.is_miniseries ? "miniseries" : m.kind;
  $("#recBody").innerHTML = `
    <div class="reccard">
      ${imgTag("recback", m.backdrop, false)}
      <div class="recmain">
        ${imgTag("recposter", m.poster, false)}
        <div class="recinfo">
          <h3>${esc(m.title)} ${m.year ? `<span class="dim">(${m.year})</span>` : ""}</h3>
          <div class="facts">${esc(recMetaFacts(m))}</div>
          <div class="recbadges">
            <span class="badge ${m.kind}${m.is_miniseries ? " mini" : ""}">${kindLabel}</span>
            ${(m.genres || []).map(g => `<span class="badge genre">${esc(g)}</span>`).join("")}
          </div>
          <p class="recwhy">💡 ${esc(m.why || "")}</p>
          ${m.overview ? `<p class="recover">${esc(m.overview)}</p>` : ""}
          ${m.where_watch ? `<p class="recwhere">📺 Where: <b>${esc(m.where_watch)}</b></p>` : ""}
          ${(m.stars || []).length ? `<p class="recstars">${(m.stars || []).map(s => esc(s)).join(" · ")}</p>` : ""}
          ${m.director ? `<p class="recstars">Dir. ${esc(m.director)}</p>` : ""}
          ${m.creator ? `<p class="recstars">Created by ${esc(m.creator)}</p>` : ""}
        </div>
      </div>
      <div class="recfeedback">
        <textarea id="recNote" rows="2" placeholder="Why you like / dislike it (optional — helps future picks)"></textarea>
        <div class="recactions">
          <button class="btn primary" id="recAccept">✓ Want it</button>
          <button class="btn" id="recSeen" title="Logs it as watched (you own no file) and moves on">👁 Already seen it</button>
          <button class="btn" id="recReject">✕ Not for me</button>
          <span class="dim recleft">${pending} in queue</span>
        </div>
      </div>
    </div>`;
  $("#recClose").onclick = () => $("#recModal").classList.add("hidden");
  $("#recAccept").onclick = () => decideRec("accepted");
  $("#recSeen").onclick = () => decideRec("seen");
  $("#recReject").onclick = () => decideRec("rejected");
}

async function decideRec(decision) {
  const m = recCurrent;
  if (!m) return;
  const note = $("#recNote").value.trim();
  const seen = decision === "seen";
  ["#recAccept", "#recSeen", "#recReject"].forEach(s => { $(s).disabled = true; });
  try {
    const r = await api(`/api/recommendations/${m.id}/decide`, {
      method: "POST",
      body: { decision: seen ? "rejected" : decision, seen, note: note || null },
    });
    const msgs = {
      accepted: `Added "${m.title}" to your wanted list.`,
      seen: `"${m.title}" logged as seen (you own no file).`,
      rejected: `Dismissed "${m.title}".`,
    };
    $("#recBody").innerHTML = `<p class="hint">${esc(msgs[decision] || msgs.rejected)}</p>`;
    recCurrent = null;
    refreshRecPill();
    load(); // wanted/seen state may have changed in the main list
    openRecommend(); // straight on to the next pick — no extra click
  } catch (err) {
    alert(err.message);
    ["#recAccept", "#recSeen", "#recReject"].forEach(s => { $(s).disabled = false; });
  }
}

$("#btnRecommend").onclick = openRecommend;
$("#recClose").onclick = () => $("#recModal").classList.add("hidden");
$("#recModal").addEventListener("click", e => {
  if (e.target === $("#recModal")) $("#recModal").classList.add("hidden");
});

/* ---------- season calendar (upcoming seasons of watched series) ---------- */
function fmtCalDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map(Number);
  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  return `${d} ${months[(m || 1) - 1]} ${y}`;
}

function calWhen(e) {
  if (e.status === "announced" && e.release_start) {
    const start = fmtCalDate(e.release_start);
    if (e.release_end && e.release_end !== e.release_start)
      return `${start} → ${fmtCalDate(e.release_end)}`;
    return start;
  }
  return e.window_hint || "date TBA";
}

// recent-entry line: "S3 · 12 Aug 2026 → 7 Oct 2026 · airing" / "… · 18 days ago"
function calRecentWhen(e) {
  const start = fmtCalDate(e.release_start);
  const range = e.release_end && e.release_end !== e.release_start
    ? `${start} → ${fmtCalDate(e.release_end)}` : start;
  const age = e.days_ago === 0 ? "started today"
    : e.days_ago === 1 ? "started yesterday"
    : `${e.days_ago} days ago`;
  return `${range} · ${e.airing ? "airing now" : age}`;
}

function calPattern(e) {
  if (e.status === "done" || e.finished) return "series finished";
  if (e.release_kind === "all_at_once") return "all at once";
  if (e.release_kind === "weekly") return "weekly episodes";
  return "";
}

function renderRecentItem(e) {
  const poster = imgTag("thumb", e.poster);
  return `<div class="notifitem calitem cal-recent${e.is_new ? " cal-new" : ""}" data-open-title="${e.title_id}">
    <div class="ntop">
      ${poster}
      <div>
        <div class="ntitle">${esc(e.title)}${e.is_new ? ' <span class="newchip">NEW</span>' : ""}</div>
        <div class="nmeta">S${e.season} · <b>${esc(calRecentWhen(e))}</b>
          ${e.release_kind === "all_at_once" ? " · all at once" : ""}</div>
        ${e.note ? `<div class="nmeta dim">${esc(e.note)}</div>` : ""}
      </div>
      <button class="btn mini seenbtn" data-seen-id="${e.id}"
        title="Mark this season watched — removes it from this list">👁</button>
    </div>
  </div>`;
}

function renderCalItem(e) {
  const when = calWhen(e);
  const pattern = calPattern(e);
  const days = (e.status === "announced" && typeof e.days_until === "number")
    ? (e.days_until === 0 ? "today" : e.days_until < 0 ? "airing/ended"
       : `in ${e.days_until} day${e.days_until === 1 ? "" : "s"}`) : "";
  const poster = imgTag("thumb", e.poster);
  return `<div class="notifitem calitem cal-${esc(e.status)}${e.is_new ? " cal-new" : ""}" data-open-title="${e.title_id}">
    <div class="ntop">
      ${poster}
      <div>
        <div class="ntitle">${esc(e.title)}${e.is_new ? ' <span class="newchip">NEW</span>' : ""}</div>
        <div class="nmeta">S${e.season} · <b>${esc(when)}</b>
          ${pattern ? `· ${esc(pattern)}` : ""}${days ? ` · ${esc(days)}` : ""}</div>
        ${e.note ? `<div class="nmeta dim">${esc(e.note)}</div>` : ""}
      </div>
    </div>
  </div>`;
}

async function toggleCalPanel() {
  const panel = $("#calPanel");
  if (!panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  panel.innerHTML = '<div class="notifdone">Loading…</div>';
  // fetch FIRST, stamp the looked-at baseline AFTER: this open still shows
  // everything that arrived since the last look; anything that changes
  // from now on lights the pill + NEW highlights again
  const [{ entries }, recent] = await Promise.all([
    api("/api/seasons/calendar"), api("/api/seasons/recent"),
  ]);
  api("/api/seasons/calendar/opened", { method: "POST" }).catch(() => null);
  // upcoming = announced rows whose date is still in the future (past-dated
  // announced rows are recently-released seasons — the section above), plus
  // vague windows; done rows go to their own section
  const open = entries.filter(e => e.status === "vague"
    || (e.status === "announced"
        && (e.days_until === undefined || e.days_until >= 0)));
  const done = entries.filter(e => e.status === "done" || e.finished);
  const recents = recent.entries || [];
  panel.innerHTML = recents.length || open.length || done.length
    ? (recents.length ? '<div class="notifhead">Recently released</div>'
       + recents.map(renderRecentItem).join("") : "")
      + (open.length ? '<div class="notifhead" style="margin-top:6px">Upcoming seasons</div>'
       + open.map(renderCalItem).join("") : "")
      + (done.length ? '<div class="notifhead" style="margin-top:6px">Finished</div>'
       + done.map(renderCalItem).join("") : "")
      + `<div class="calfoot">Every watched series is re-checked once a week ·
         <button class="btn mini ghost" id="calPoll">check all now</button></div>`
    : '<div class="notifdone">No seasons tracked yet — mark a series watched and it gets checked.</div>';
  const pollBtn = $("#calPoll");
  if (pollBtn) pollBtn.onclick = async () => {
    pollBtn.disabled = true; pollBtn.textContent = "checking…";
    try { await api("/api/seasons/poll", { method: "POST" }); } catch (e) {}
    setTimeout(toggleCalPanel, 1500); // reopen = refresh
  };
}

$("#btnCalendar").onclick = () => toggleCalPanel().catch(err => alert(err.message));
document.addEventListener("click", e => {
  // click-outside closes the calendar panel (its own clicks keep it open)
  if (!e.target.closest("#btnCalendar") && !e.target.closest("#calPanel"))
    $("#calPanel").classList.add("hidden");
});
$("#calPanel").addEventListener("click", async e => {
  const seen = e.target.closest("[data-seen-id]");
  if (seen) {
    seen.disabled = true;
    try {
      await api(`/api/seasons/${seen.dataset.seenId}/seen`, { method: "POST", body: { seen: true } });
      await toggleCalPanel(); // reopen = refresh without the seen entry
    } catch (err) { seen.disabled = false; alert(err.message); }
    refreshCalPill();
    return;
  }
  const t = e.target.closest("[data-open-title]");
  if (t && t.dataset.openTitle) {
    $("#calPanel").classList.add("hidden");
    openDrawer(Number(t.dataset.openTitle));
  }
});

async function refreshCalPill() {
  try {
    const [{ entries }, recent] = await Promise.all([
      api("/api/seasons/calendar"), api("/api/seasons/recent"),
    ]);
    // the pill counts NEW information only (vague -> dated, shifted dates,
    // new season, finished...) — info that arrived since the calendar was
    // last opened. Stale-but-unwatched entries do not nag.
    const n = entries.filter(e => e.is_new).length
      + (recent.entries || []).filter(e => e.is_new).length;
    const pill = $("#calCount");
    pill.textContent = n > 9 ? "9+" : n;
    pill.classList.toggle("hidden", n === 0);
  } catch (e) { /* non-fatal */ }
}

/* ---------- notifications (watched -> backup decisions) ---------- */
async function refreshBellPill() {
  try {
    const { pending } = await api("/api/notifications/count");
    const pill = $("#bellCount");
    pill.textContent = pending > 99 ? "99+" : pending;
    pill.classList.toggle("hidden", pending === 0);
  } catch (e) { /* server restart race — next poll fixes it */ }
}

function renderNotifItem(n) {
  const t = n.title || {};
  const poster = imgTag("thumb", t.poster);
  const kindLabel = t.is_miniseries ? "miniseries" : (t.kind || "");
  return `<div class="notifitem" data-nid="${n.id}">
    <div class="ntop">
      ${poster}
      <div>
        <div class="ntitle" data-open-title="${t.id || ""}">${esc(t.title || "(deleted title)")}</div>
        <div class="nmeta"><span class="badge ${t.kind === "series" ? "series" : "movie"}">${esc(kindLabel)}</span>
          ${t.year || ""} · watched ${fmtDate(n.created_at)}</div>
      </div>
    </div>
    <div class="nactions">
      <button class="btn mini primary" data-nacc="${n.id}" title="Move the file(s) to the backup drive now">✓ Move to backup</button>
      <button class="btn mini ghost" data-nrej="${n.id}" title="Keep it where it is">✕ Keep here</button>
    </div>
    <div class="nerr hidden"></div>
  </div>`;
}

async function toggleNotifPanel() {
  const panel = $("#notifPanel");
  if (!panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  panel.innerHTML = '<div class="notifdone">Loading…</div>';
  const { notifications: items } = await api("/api/notifications?status=pending");
  panel.innerHTML = items.length
    ? '<div class="notifhead">Watched — move to backup?</div>' + items.map(renderNotifItem).join("")
    : '<div class="notifdone">No pending notifications 🎉</div>';
}

$("#btnBell").onclick = () => toggleNotifPanel().catch(err => alert(err.message));
document.addEventListener("click", e => {
  // click-outside closes the panel
  if (!e.target.closest(".bellwrap")) $("#notifPanel").classList.add("hidden");
});

$("#notifPanel").addEventListener("click", async e => {
  const titleBtn = e.target.closest("[data-open-title]");
  if (titleBtn && titleBtn.dataset.openTitle) {
    $("#notifPanel").classList.add("hidden");
    openDrawer(Number(titleBtn.dataset.openTitle));
    return;
  }
  const accBtn = e.target.closest("[data-nacc]");
  const rejBtn = e.target.closest("[data-nrej]");
  if (!accBtn && !rejBtn) return;
  const nid = Number((accBtn || rejBtn).dataset.nacc || (accBtn || rejBtn).dataset.nrej);
  const item = $(`#notifPanel .notifitem[data-nid="${nid}"]`);
  const errBox = item.querySelector(".nerr");
  [accBtn, rejBtn].forEach(b => b && (b.disabled = true));
  try {
    const r = await api(`/api/notifications/${nid}/decide`, {
      method: "POST",
      body: { decision: accBtn ? "accept" : "reject" },
    });
    if (r.ok) {
      item.remove();
      if (!$$("#notifPanel .notifitem").length)
        $("#notifPanel").innerHTML = '<div class="notifdone">All caught up 🎉</div>';
      if (r.status === "started" && r.job_id) {
        // background move — progress shows in the job toast immediately
        watchJob(r.job_id, "Move to backup", () => refreshBellPill());
      } else if (r.status === "queued") {
        alert(`The backup drive is not connected right now.
The move was queued and runs automatically when it is connected (Settings → Drive queues).`);
      }
      refreshBellPill(); load();
    } else {
      // drive offline (or move failed): stay pending, show which drive to plug in
      errBox.textContent = `🔌 ${r.error}`;
      errBox.classList.remove("hidden");
      [accBtn, rejBtn].forEach(b => b && (b.disabled = false));
    }
  } catch (err) {
    errBox.textContent = err.message;
    errBox.classList.remove("hidden");
    [accBtn, rejBtn].forEach(b => b && (b.disabled = false));
  }
});

/* ---------- live drive connect / disconnect (SSE) ---------- */
let driveReloadTimer = null;
function watchDrives() {
  try {
    const es = new EventSource("/api/events/drives");
    es.onmessage = () => {
      clearTimeout(driveReloadTimer);
      // debounce: a single plug/unplug can emit several signature flips
      driveReloadTimer = setTimeout(() => {
        load(); renderRootOptions(); refreshBellPill(); refreshCalPill();
        refreshDriveQueue(); // a queue entry may have just become runnable
      }, 400);
    };
  } catch (e) { /* EventSource unavailable — manual refresh still works */ }
}

/* ---------- boot ---------- */
restoreState();
// reflect the restored state in the controls before the first load
$("#search").value = state.q;
$("#matchSel").value = state.match;
$("#wantedSel").value = state.wanted;
$("#hiddenSel").value = state.hidden || "";
$("#onlyMissing").checked = !!state.missing;
$$("#kindChips .chip").forEach(c => c.classList.toggle("on", (c.dataset.kind || "") === state.kind));
$$("#watchChips .chip").forEach(c => c.classList.toggle("on", (c.dataset.w || "") === state.watched));
applySortIndicators();
renderRootOptions() // sets #rootSel to state.root once options exist
  .then(load)
  .then(() => { $("#genreSel").value = state.genre; }) // options exist only after first load
  .catch(err => {
    $("#empty").classList.remove("hidden");
    $("#empty").textContent = `Failed to load: ${err.message}`;
  });
watchDrives();
refreshRecPill(); // show how many recommendations are waiting
refreshBellPill(); // show pending watched->backup decisions
refreshCalPill(); // show how many announced upcoming seasons are on the calendar

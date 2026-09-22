/* Title drawer: detail view, edit form, episode checklist, move controls. */
import { $, $$ } from "./dom.js";
import { api } from "./api.js";
import { state } from "./state.js";
import { load, renderRows, searchPerson } from "./list.js";
import { esc, imgTag, rtTier, fmtSize, fmtDate } from "./format.js";
import { watchJob } from "./jobs.js";
import { openDestPicker } from "./destpicker.js";

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

export async function openDrawer(id) {
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
export function updateEpProgress(r) {
  const bar = $("#dBody .epprogbar > span");
  const count = $("#dBody .epcount");
  if (bar) bar.style.width = `${r.episodes_total ? Math.round(100 * r.episodes_watched / r.episodes_total) : 0}%`;
  if (count) count.textContent = `${r.episodes_watched}/${r.episodes_total}`;
}
export function markDrawerWatched() {
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

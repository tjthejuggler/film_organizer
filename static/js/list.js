/* Main catalog list: loading, filters/sort wiring, row rendering. */
import { $, $$ } from "./dom.js";
import { api } from "./api.js";
import { state, saveState, applySortIndicators } from "./state.js";
import { esc, imgTag, rtTier, fmtSize, fmtDate, personChip, fmtRuntime, locBadge } from "./format.js";

/* ---------- table ---------- */
export async function load() {
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

export function renderGenres(genres) {
  const sel = $("#genreSel");
  const cur = sel.value;
  // "Miniseries" pseudo-genre is filterable here but never shown as a plain
  // genre chip option twice; it comes from the server facet already
  sel.innerHTML = '<option value="">Genre…</option>' +
    genres.map(g => `<option${g === cur ? " selected" : ""}>${esc(g)}</option>`).join("");
}

export async function renderRootOptions() {
  const { roots } = await api("/api/roots");
  $("#rootSel").innerHTML = '<option value="">All locations</option>' +
    roots.map(r => `<option value="${esc(r.path)}">${esc(r.label || r.path)}${r.exists ? "" : " (offline)"}</option>`).join("");
  $("#rootSel").value = state.root;
}

export function renderRows() {
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
export function searchPerson(name) {
  $("#search").value = name;
  state.q = name;
  load();
}

/* Watched log: titles seen without owning a file (manual entries). */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { load } from "./list.js";
import { esc, fmtDate, imgTag } from "./format.js";

export async function openHistory() {
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

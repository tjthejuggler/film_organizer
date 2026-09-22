/* Season calendar panel: upcoming seasons of watched series. */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { esc, imgTag } from "./format.js";
import { openDrawer } from "./drawer.js";
import { refreshCalPill } from "./pills.js";

export function fmtCalDate(iso) {
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

export async function toggleCalPanel() {
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

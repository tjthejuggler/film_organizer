/* Header pills: calendar badge + bell (backup decisions) panel. */
import { $, $$ } from "./dom.js";
import { api } from "./api.js";
import { load } from "./list.js";
import { esc, fmtDate, imgTag } from "./format.js";
import { openDrawer } from "./drawer.js";
import { watchJob } from "./jobs.js";

/* ---------- calendar pill (announced seasons) ---------- */
export async function refreshCalPill() {
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
export async function refreshBellPill() {
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

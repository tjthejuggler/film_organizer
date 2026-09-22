/* Drive-queue panel inside Settings: pending per-drive operations. */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { esc, fmtDate } from "./format.js";

export async function refreshDriveQueue() {
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

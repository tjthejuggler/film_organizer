/* Settings sheet: keys, storage roots, devices (QR pairing), drive queue. */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { esc, fmtDate } from "./format.js";
import { refreshDriveQueue } from "./drivequeue.js";
import { renderRootOptions } from "./list.js";

export async function openSettings() {
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
export async function refreshPairing() {
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
export function closeModal() {
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

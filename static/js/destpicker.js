/* Destination picker popup: EVERY configured move destination (full paths),
   used by both the drawer's move row and the season picker. */
import { $, $$ } from "./dom.js";
import { api } from "./api.js";
import { esc } from "./format.js";

export const DM_ROLE_BADGE = {
  internal: '<span class="badge">internal</span>',
  backup: '<span class="badge">backup</span>',
  liked: '<span class="badge wanted">liked</span>',
  library: '<span class="badge genre">library</span>',
};

// folder picker for moves: EVERY configured destination with its FULL PATH —
// internal, backup/liked drives AND the scanned library roots, so a title
// can even move from one library folder to another on the same drive
// (~/Videos -> ~/Downloads). onPick(target, root) receives the choice.
export async function openDestPicker(name, onPick) {
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

$("#dmClose").onclick = () => $("#destModal").classList.add("hidden");
$("#destModal").addEventListener("click", e => {
  if (e.target === $("#destModal")) $("#destModal").classList.add("hidden");
});

/* 'Remove from which places?' step of the move flow: when a title (or the
   ticked seasons) exists in more than one place, ask which copies to keep
   before triggering the move. Standard move = everything ticked (all
   copies outside the destination go); untick a place to preserve it. */
import { $, $$ } from "./dom.js";
import { api } from "./api.js";
import { esc } from "./format.js";

const fmtPlace = root => {
  const parts = root.split("/").filter(Boolean);
  if (parts[0] === "run" && parts[1] === "media") return `💾 ${parts.slice(3).join("/")}`;
  if (parts[0] === "home") return `💻 ~/${parts.slice(2).join("/")}`;
  return `📁 /${parts.join("/")}`;
};

/**
 * Ask which source places to remove the copy from.
 * @param name     display title for the popup
 * @param titleId  catalog id
 * @param seasons  array of ticked seasons or null (= whole title)
 * @param destRoot absolute destination root (never offered for removal)
 * @param onGo     (purgeRoots|null) called with the ticked roots — null
 *                 means 'user kept everything: pure copy, no purge'
 */
export async function openSrcPicker(name, titleId, seasons, destRoot, onGo) {
  let locs = [];
  try {
    const q = seasons && seasons.length ? `?seasons=${seasons.join(",")}` : "";
    ({ locations: locs } = await api(`/api/titles/${titleId}/copy-locations${q}`));
  } catch { locs = []; }
  // only places OTHER than the destination are candidates for removal
  const destAbs = destRoot ? destRoot.replace(/\/+$/, "") : null;
  const sources = locs.filter(l => !destAbs || l.root.replace(/\/+$/, "") !== destAbs);
  if (sources.length < 1) {          // single copy (the destination itself)
    onGo(null);
    return;
  }
  if (sources.length === 1 && sources[0].files === 0) { onGo(null); return; }

  $("#srcName").textContent = name;
  $("#srcList").innerHTML = sources.map(l => `
    <label class="mvitem">
      <input type="checkbox" data-srcroot="${esc(l.root)}" checked
             ${l.mounted ? "" : 'data-unmounted="1"'}>
      <span class="mvname">${esc(fmtPlace(l.root))}</span>
      <span class="mvcount">${l.files} file(s)${l.mounted ? "" : " · drive not connected"}</span>
    </label>`).join("");
  const summary = () => {
    const on = $$("#srcList input:checked").length;
    $("#srcGo").textContent = on ? `Move — remove from ${on} place${on > 1 ? "s" : ""}` : "Move (copy only)";
  };
  $$("#srcList input").forEach(i => i.onchange = summary);
  $("#srcAll").onclick = () => $$("#srcList input").forEach(i => i.checked = true) || summary();
  $("#srcNone").onclick = () => $$("#srcList input").forEach(i => i.checked = false) || summary();
  $("#srcGo").onclick = () => {
    const roots = $$("#srcList input:checked").map(i => i.dataset.srcroot);
    $("#srcModal").classList.add("hidden");
    onGo(roots.length ? roots : null);   // null = keep every copy
  };
  $("#srcModal").classList.remove("hidden");
}

$("#srcClose").onclick = () => $("#srcModal").classList.add("hidden");
$("#srcModal").addEventListener("click", e => {
  if (e.target === $("#srcModal")) $("#srcModal").classList.add("hidden");
});

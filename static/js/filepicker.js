/* Filesystem folder-picker dialog (backs the Settings path inputs). */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { esc } from "./format.js";

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
export function openPicker(inputId) {
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

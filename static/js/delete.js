/* Delete-choice popup: remove files, keep a record, or drop the entry. */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { load } from "./list.js";

let delTarget = null;

export function openDeleteChoice(t) {
  delTarget = t;
  $("#delName").textContent = `"${t.title}"`;
  const gb = (t.size_bytes / 1e9).toFixed(1);
  const hasFiles = (t.locations || []).length > 0 || !!t.size_bytes;
  const info = $("#delInfo");
  if (!hasFiles) {
    info.innerHTML = `No file is on disk for this entry — this removes only
      the list entry (its watched memory goes with it).`;
    $("#delKeep").classList.add("hidden");
  } else {
    info.innerHTML = `This ${t.kind} currently uses <b>${gb} GB</b> on disk
      ${t.watched ? "and is marked <b>watched</b>" : ""}.
      What should happen to its file(s)?`;
    $("#delKeep").classList.remove("hidden");
  }
  $("#delModal").classList.remove("hidden");
}

function closeDeleteChoice() {
  delTarget = null;
  $("#delModal").classList.add("hidden");
}

async function runDelete(keepRecord) {
  const t = delTarget;
  if (!t) return;
  closeDeleteChoice();
  try {
    const q = keepRecord ? "?keep_record=true" : "";
    const r = await api(`/api/titles/${t.id}/files${q}`, { method: "DELETE" });
    if (r.queued) {
      alert(`"${r.title}" needs ${r.drives.length > 1 ? "these drives" : "this drive"}: ${r.drives.join(", ")}.
The deletion was queued and runs automatically when ${r.drives.length > 1 ? "they are" : "it is"} connected (Settings → Drive queues).`);
    } else if (r.kept_record) {
      alert(`Deleted the file(s) of "${r.title}" (${r.removed_files} removed).
The entry stays in your list as a record.`);
    } else {
      alert(r.removed_files > 0
        ? `Deleted "${r.title}": ${r.removed_files} file(s) removed from disk.`
        : `Removed "${r.title}" from the list (no files on disk).`);
    }
    load();
  } catch (err) {
    alert(err.message); // includes "Connect these drives first: …" on 409 (queue=off)
  }
}

$("#delClose").onclick = closeDeleteChoice;
$("#delCancel").onclick = closeDeleteChoice;
$("#delModal").addEventListener("click", e => {
  if (e.target === $("#delModal")) closeDeleteChoice();
});
$("#delAll").onclick = () => runDelete(false);
$("#delKeep").onclick = () => runDelete(true);

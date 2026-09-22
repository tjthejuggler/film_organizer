/* Duplicates modal + bulk-delete wizard (identical-size copies). */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { load } from "./list.js";
import { esc, fmtSize, locBadge } from "./format.js";

let dupGroups = [];      // last loaded duplicate groups
let dupOffline = new Set(); // roots whose drives are not mounted
let bulk = null;         // bulk-wizard state

export async function openDupes() {
  $("#dupList").innerHTML = '<p class="hint">Scanning…</p>';
  $("#dupModal").classList.remove("hidden");
  try {
    const [{ groups }, rootsInfo] = await Promise.all([
      api("/api/duplicates"), api("/api/roots"),
    ]);
    dupGroups = groups;
    dupOffline = new Set(rootsInfo.roots.filter(r => !r.exists).map(r => r.path));
    if (!groups.length) {
      $("#dupList").innerHTML = '<p class="hint">No duplicates found. 🎉 Every title lives in exactly one place.</p>';
      return;
    }
    $("#dupList").innerHTML = groups.map(g => `
      <div class="dupgroup">
        <div class="duphead">
          <b>${esc(g.title)}</b> ${g.year ? `<span class="dim">(${g.year})</span>` : ""}
          <span class="badge ${g.kind}">${g.kind}</span>
          ${g.scope === "episodes" ? `<span class="badge">${g.dup_episodes} ep(s) doubled</span>` : ""}
          ${g.scope === "two_rows" ? `<span class="badge warn">cataloged twice — two library rows for the same title</span>` : ""}
          <span class="dupdelta">${fmtSize(g.size_delta)} size difference</span>
        </div>
        ${g.copies.map((c, i) => `
          <div class="dupcopy">
            <span class="badge">${i === 0 ? "★ biggest" : "copy"}</span>
            <span>${esc(c.label)}</span>
            <span class="dim">${fmtSize(c.size_bytes)}${c.file_count ? ` · ${c.file_count} file(s)` : ""}</span>
            ${g.scope === "two_rows"
              ? `<button class="btn mini ghost" data-delrow="${c.title_id}">🗑 delete this row</button>`
              : `<button class="btn mini ghost" data-delroot="${c.title_id}" data-root="${esc(c.root)}">🗑 delete this copy</button>`}
          </div>`).join("")}
      </div>`).join("");
  } catch (err) {
    $("#dupList").innerHTML = `<p class="hint">Failed: ${esc(err.message)}</p>`;
  }
}
/* bulk wizard: only IDENTICAL copies (byte-for-byte same size) */
function identicalGroups() {
  return dupGroups.filter(g =>
    g.scope !== "two_rows" &&
    g.copies.length > 1 &&
    g.copies.every(c => c.size_bytes === g.copies[0].size_bytes) &&
    g.copies.some(c => c.root && !dupOffline.has(c.root))); // something deletable online
}

function startBulk() {
  const groups = identicalGroups();
  if (!groups.length) {
    alert("No identical-size duplicates to bulk-delete. (Different-size copies are kept out of bulk mode on purpose — pick those manually.)");
    return;
  }
  // group by location-signature so the user decides once per combination
  const combos = new Map();
  for (const g of groups) {
    const sig = g.copies.map(c => c.root).sort().join(" | ");
    if (!combos.has(sig)) combos.set(sig, []);
    combos.get(sig).push(g);
  }
  bulk = { combos: [...combos.entries()], step: 0, plan: [], freed: 0, count: 0 };
  $("#dupList").innerHTML = '<p class="hint">Bulk delete — deciding…</p>';
  bulkStep();
}

function bulkStep() {
  if (bulk.step >= bulk.combos.length) return bulkRun();
  const [sig, groups] = bulk.combos[bulk.step];
  const roots = sig.split(" | ");
  const keepRoot = roots.find(r => !dupOffline.has(r)) || roots[0];
  const totalMb = groups.reduce((s, g) =>
    s + (g.copies.find(c => c.root !== keepRoot)?.size_bytes || 0) / 1e6, 0);
  $("#dupList").innerHTML = `
    <div class="bulkbox">
      <h3>Combination ${bulk.step + 1} of ${bulk.combos.length}</h3>
      <p class="hint">
        ${groups.length} title(s) have <b>identical copies</b> (same byte size) in these locations:
        ${roots.map(r => `<b>${esc(locBadge(r))}</b>${dupOffline.has(r) ? " (offline)" : ""}`).join(" and ")}.
      </p>
      <p class="hint">Which location's copies should be deleted? The others are kept. Offline drives are skipped automatically.</p>
      <div class="bulkchoices">
        ${roots.map(r => `
          <button class="btn ${r === keepRoot ? "" : "primary"}" data-bulkpick="${esc(r)}"
            ${dupOffline.has(r) ? "disabled" : ""}>
            Delete copies on ${esc(locBadge(r))}${dupOffline.has(r) ? " (offline)" : ""}
          </button>`).join("")}
        <button class="btn ghost" data-bulkskip>Skip this combination</button>
      </div>
      <p class="hint">~${totalMb.toFixed(0)} MB would be freed by deleting from "${esc(locBadge(keepRoot))}".</p>
    </div>`;
}
$("#dupList").addEventListener("click", e => {
  if (!bulk) return;
  const pick = e.target.closest("[data-bulkpick]");
  if (pick) {
    const root = pick.dataset.bulkpick;
    const [, groups] = bulk.combos[bulk.step];
    for (const g of groups) {
      for (const c of g.copies) {
        if (c.root === root && !dupOffline.has(c.root)) {
          bulk.plan.push({ title_id: c.title_id, root: c.root });
          bulk.freed += c.size_bytes || 0;
          bulk.count += c.file_count || 1;
        }
      }
    }
    bulk.step++;
    bulkStep();
    return;
  }
  if (e.target.closest("[data-bulkskip]")) { bulk.step++; bulkStep(); }
});

async function bulkRun() {
  const plan = bulk.plan;
  const freed = bulk.freed;
  const count = bulk.count;
  bulk = null;
  if (!plan.length) { openDupes(); return; }
  if (!confirm(`Delete ${plan.length} copies (${count} files, ~${fmtSize(freed)}) from disk?\nAre you sure?`)) {
    openDupes(); return;
  }
  $("#dupList").innerHTML = `<p class="hint">Deleting ${plan.length} copies…</p>`;
  let ok = 0;
  const failed = [];
  for (const p of plan) {
    try {
      const q = p.root ? `?root=${encodeURIComponent(p.root)}` : "";
      await api(`/api/duplicates/${p.title_id}${q}`, { method: "DELETE" });
      ok++;
    } catch (err) {
      failed.push(`title ${p.title_id}: ${err.message}`);
    }
  }
  const msg = `Bulk delete finished: ${ok} of ${plan.length} copies deleted.` +
    (failed.length ? `\n\nSome failed:\n${failed.join("\n")}` : "");
  alert(msg);
  openDupes(); load();
}

export { startBulk };

async function delCopy(btn, payload, what) {
  if (!payload.title_id || payload.title_id === "undefined") {
    alert("Internal error: missing title id — please reload the page (Ctrl+Shift+R) and try again.");
    return;
  }
  if (!confirm(`Are you sure? This deletes ${what} from disk.`)) return;
  btn.disabled = true;
  try {
    // root as query param: immune to content-type/422 quirks
    const q = payload.root ? `?root=${encodeURIComponent(payload.root)}` : "";
    const r = await api(`/api/duplicates/${payload.title_id}${q}`, { method: "DELETE" });
    if (r.queued) {
      btn.textContent = "queued for drive connect";
      alert(`That copy's drive (${r.drives.join(", ")}) is not connected.
The deletion was queued and runs automatically when it is connected (Settings → Drive queues).`);
      return;
    }
    // instant feedback: remove the copy row, then re-check the group
    const copyRow = btn.closest(".dupcopy");
    if (copyRow) copyRow.remove();
    btn.textContent = `deleted (${r.removed_files} files)`;
    // remaining duplicate state may have changed: refresh view + library
    setTimeout(() => { openDupes(); load(); }, 600);
  } catch (err) {
    btn.disabled = false;
    alert(err.message);
  }
}
$("#dupList").addEventListener("click", e => {
  const byRoot = e.target.closest("[data-delroot]");
  if (byRoot) return delCopy(byRoot, { title_id: byRoot.dataset.delroot, root: byRoot.dataset.root }, "this copy");
  const byRow = e.target.closest("[data-delrow]");
  if (byRow) return delCopy(byRow, { title_id: byRow.dataset.delrow }, "this library row");
});

$("#btnDupes").onclick = () => {
  // launcher lives inside the settings sheet now — close it first
  $("#modal").classList.add("hidden");
  openDupes();
};
$("#btnBulk").onclick = startBulk;
$("#dClose2").onclick = () => { bulk = null; $("#dupModal").classList.add("hidden"); };
$("#dupModal").addEventListener("click", e => {
  if (e.target === $("#dupModal")) { bulk = null; $("#dupModal").classList.add("hidden"); }
});

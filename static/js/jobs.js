/* Job toast: live progress for scan / enrich / move / backup jobs. */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { load } from "./list.js";

let jobTimer = null;
let currentJobId = null;

export function watchJob(jid, label, onDone) {
  currentJobId = jid;
  $("#tTitle").textContent = label;
  $("#toast").classList.remove("hidden");
  clearInterval(jobTimer);
  const poll = async () => {
    const j = await api(`/api/jobs/${jid}`);
    const pct = j.total ? Math.round(100 * j.progress / j.total) : (j.status === "done" ? 100 : 5);
    $("#tFill").style.width = pct + "%";
    // live job message: names the title + file path currently being worked
    // on — this is how you spot a run stuck on a junk entry (it also shows
    // the "LLM cleanup running…" warnings for the slowest step)
    $("#tMsg").textContent = j.message || "";
    $("#tLog").textContent = j.log.join("\n");
    $("#tLog").scrollTop = $("#tLog").scrollHeight;
    if (j.status !== "running") {
      clearInterval(jobTimer);
      $("#tTitle").textContent = `${label} — ${j.status}`;
      $("#tMsg").textContent = j.message || "";
      load();
      if (onDone && j.status === "done") onDone();
    }
  };
  poll();
  jobTimer = setInterval(poll, 1200);
}

$("#btnScan").onclick = async () => {
  try {
    const r = await api("/api/scan", { method: "POST", body: {} });
    watchJob(r.job_id, "Scan");
  } catch (err) { alert(err.message); }
};
$("#btnEnrich").onclick = async () => {
  try {
    const r = await api("/api/enrich", { method: "POST", body: {} });
    watchJob(r.job_id, "Enrich", async () => {
      // chain the light backfill pass: cert / RT / miniseries tags for
      // ALREADY-matched rows the default Enrich skips
      try {
        const b = await api("/api/backfill-ratings", { method: "POST" });
        watchJob(b.job_id, "Backfill");
      } catch (e) { /* non-fatal */ }
    });
  } catch (err) { alert(err.message); }
};
$("#tClose").onclick = async () => {
  clearInterval(jobTimer);
  $("#toast").classList.add("hidden");
  // X actually CANCELS a running job (cooperative stop between work
  // items) — hiding the toast alone left jobs burning API calls forever
  if (currentJobId) {
    try { await api(`/api/jobs/${currentJobId}/cancel`, { method: "POST" }); } catch (e) {}
    currentJobId = null;
    load();
  }
};

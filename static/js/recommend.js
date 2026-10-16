/* Film recommender popup: AI-researched picks with accept/reject/seen. */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { load } from "./list.js";
import { esc, imgTag } from "./format.js";

let recCurrent = null;   // the recommendation currently in the popup
let recBusy = false;

export async function refreshRecPill() {
  try {
    const s = await api("/api/recommendations/status");
    const pill = $("#recCount");
    if (s.pending > 0) {
      pill.textContent = s.pending;
      pill.classList.remove("hidden");
    } else {
      pill.classList.add("hidden");
    }
    if (s.enabled && s.low && !s.running_job && !s.refill_job_id) {
      // crossed the low-water line while the user was browsing — server
      // status endpoint already tried; nothing more to do here
    }
  } catch (e) { /* non-fatal */ }
}

export async function openRecommend() {
  if (recBusy) return;
  recBusy = true;
  $("#recModal").classList.remove("hidden");
  $("#recBody").innerHTML = '<p class="hint">Fetching your next recommendation…</p>';
  try {
    const r = await api("/api/recommendations/next");
    if (!r.recommendation) {
      const s = await api("/api/recommendations/status");
      renderRecEmpty(s.enabled, s.running_job);
      return;
    }
    recCurrent = r.recommendation;
    renderRec(recCurrent, r.pending);
    refreshRecPill();
  } catch (err) {
    $("#recBody").innerHTML = `<p class="hint">Failed: ${esc(err.message)}</p>`;
  } finally {
    recBusy = false;
  }
}

function renderRecEmpty(hasKey, runningJob) {
  if (!hasKey) {
    $("#recBody").innerHTML = `<p class="hint">The recommender needs your
       <b>z.ai API key</b> — add it in <b>⚙ Settings</b> and it will research
       picks for you automatically.</p>
       <div class="recactions"><button class="btn ghost" id="recWaitClose">Close</button></div>`;
    $("#recWaitClose").onclick = () => $("#recModal").classList.add("hidden");
    return;
  }
  // The composer (guidance input + button) ALWAYS shows on an empty queue.
  // If a low-water auto-refill happens to be running, note it below — the
  // user can still type guidance and click; that attaches to the run.
  guidedSearchPanel();
  if (runningJob) {
    const line = document.createElement("p");
    line.id = "recLiveStatus";
    line.className = "dim";
    line.style.fontSize = "12px";
    line.style.margin = "8px 0 0";
    line.textContent = `\u2115 A research run is already in progress (${runningJob.message || "working\u2026"}) — picks will appear here shortly.`;
    $("#recBody").querySelector(".recfeedback").after(line);
    if (recPollTimer) clearInterval(recPollTimer);
    recPollTimer = setInterval(async () => {
      if ($("#recModal").classList.contains("hidden")) { stopRecPoll(); return; }
      try {
        const s = await api("/api/recommendations/status");
        const el = $("#recLiveStatus");
        if (!el) { stopRecPoll(); return; }
        if (!s.running_job) {
          stopRecPoll();
          el.textContent = s.pending > 0
            ? "\u2713 Fresh picks are in — hit Back to start reviewing them."
            : "\u2713 The background run finished.";
          return;
        }
        if (s.running_job.message) {
          el.textContent = `\u2115 A research run is in progress (${s.running_job.message}) — picks will appear here shortly.`;
        }
      } catch (e) { /* transient */ }
    }, 4000);
  }
}

let recPollTimer = null;

async function researchNow() {
  const btn = $("#recResearchNow");
  const guidance = ($("#recGuide")?.value || "").trim();
  btn.disabled = true;
  btn.textContent = "Starting research…";
  try {
    const r = await api("/api/recommendations/refill", {
      method: "POST",
      body: { guidance: guidance || null, force: true },
    });
    pollResearch(r.job_id, guidance);
  } catch (err) {
    // 409: a research job is already running — just attach to its progress
    try {
      const s = await api("/api/recommendations/status");
      if (s.running_job) { pollResearch(s.running_job.id, guidance); return; }
    } catch (e2) { /* fall through */ }
    btn.disabled = false;
    btn.textContent = "🔎 Go research now";
    $("#recBody").querySelector(".hint").textContent = `Failed: ${err.message}`;
  }
}

function pollResearch(jobId, guidance) {
  $("#recBody").innerHTML = `
    <p class="hint" id="recPollMsg">🔎 Researching the web${guidance ? ` for <b>${esc(guidance)}</b>` : ""}…
       this usually takes a minute or two. You can close this and check back.</p>
    <div class="recactions"><button class="btn ghost" id="recWaitClose">Close</button></div>`;
  $("#recWaitClose").onclick = () => { stopRecPoll(); $("#recModal").classList.add("hidden"); };
  const t0 = Date.now();
  if (recPollTimer) clearInterval(recPollTimer);
  recPollTimer = setInterval(async () => {
    try {
      const s = await api("/api/recommendations/status");
      const msg = $("#recPollMsg");
      if (msg && s.running_job?.message) {
        msg.textContent = `🔎 ${s.running_job.message}`;
      }
      if (!s.running_job) {
        stopRecPoll();
        refreshRecPill();
        if (s.pending > 0) {
          openRecommend();           // fresh picks are in — show the first
        } else {
          $("#recPollMsg").innerHTML =
            `The researcher came back empty-handed — no new picks could be stored.
             Try again, maybe with different guidance.`;
          renderRetryAfterFail();
        }
        return;
      }
      if (Date.now() - t0 > 8 * 60 * 1000) { // safety valve
        stopRecPoll();
        if (msg) msg.textContent = "Still researching… close this and check back in a bit.";
      }
    } catch (e) { /* transient — keep polling */ }
  }, 3000);
}

function stopRecPoll() {
  if (recPollTimer) { clearInterval(recPollTimer); recPollTimer = null; }
}

function renderRetryAfterFail() {
  const actions = $("#recBody").querySelector(".recactions");
  if (!actions) return;
  const b = document.createElement("button");
  b.className = "btn";
  b.textContent = "↻ Try again";
  b.onclick = () => openRecommend();
  actions.prepend(b);
}

function recMetaFacts(m) {
  return [
    m.year && `${m.year}`,
    m.kind === "series" && (m.seasons ? `${m.seasons} season${m.seasons > 1 ? "s" : ""}` : "series"),
    m.kind === "series" && m.episodes && `${m.episodes} episodes`,
    m.runtime && (m.kind === "series" ? `${m.runtime} min/ep` : `${m.runtime} min`),
    m.cert,
    m.rating_imdb && `★ ${m.rating_imdb} IMDb`,
    m.rating_tmdb && `★ ${m.rating_tmdb} TMDB`,
    m.rating_rt != null && `🍅 ${m.rating_rt}%`,
  ].filter(Boolean).join(" · ");
}

function renderRec(m, pending) {
  const kindLabel = m.is_miniseries ? "miniseries" : m.kind;
  $("#recBody").innerHTML = `
    <div class="reccard">
      ${imgTag("recback", m.backdrop, false)}
      <div class="recmain">
        ${imgTag("recposter", m.poster, false)}
        <div class="recinfo">
          <h3>${esc(m.title)} ${m.year ? `<span class="dim">(${m.year})</span>` : ""}</h3>
          <div class="facts">${esc(recMetaFacts(m))}</div>
          <div class="recbadges">
            <span class="badge ${m.kind}${m.is_miniseries ? " mini" : ""}">${kindLabel}</span>
            ${(m.genres || []).map(g => `<span class="badge genre">${esc(g)}</span>`).join("")}
          </div>
          <p class="recwhy">💡 ${esc(m.why || "")}</p>
          ${m.overview ? `<p class="recover">${esc(m.overview)}</p>` : ""}
          ${m.where_watch ? `<p class="recwhere">📺 Where: <b>${esc(m.where_watch)}</b></p>` : ""}
          ${(m.stars || []).length ? `<p class="recstars">${(m.stars || []).map(s => esc(s)).join(" · ")}</p>` : ""}
          ${m.director ? `<p class="recstars">Dir. ${esc(m.director)}</p>` : ""}
          ${m.creator ? `<p class="recstars">Created by ${esc(m.creator)}</p>` : ""}
        </div>
      </div>
      <div class="recfeedback">
        <textarea id="recNote" rows="2" placeholder="Why you like / dislike it (optional — helps future picks)"></textarea>
        <div class="recactions">
          <button class="btn primary" id="recAccept">✓ Want it</button>
          <button class="btn" id="recSeen" title="Logs it as watched (you own no file) and moves on">👁 Already seen it</button>
          <button class="btn" id="recReject">✕ Not for me</button>
          <button class="btn ghost" id="recGuided" title="Send the researcher out right now with your own instructions">🎯 Guide a search…</button>
          <span class="dim recleft">${pending} in queue</span>
        </div>
      </div>
    </div>`;
  $("#recClose").onclick = () => $("#recModal").classList.add("hidden");
  $("#recAccept").onclick = () => decideRec("accepted");
  $("#recSeen").onclick = () => decideRec("seen");
  $("#recReject").onclick = () => decideRec("rejected");
  $("#recGuided").onclick = () => guidedSearchPanel(
    recCurrent ? `more like ${recCurrent.title}` : "");
}

/* The guided-search composer: shown when the queue is empty AND from the
   "🎯 Guide a search…" button on any card. prefill seeds the textarea. */
function guidedSearchPanel(prefill = "") {
  stopRecPoll();
  recCurrent = null;
  $("#recBody").innerHTML = `
    <p class="hint">Tell the researcher what to look for — as specific or as
       loose as you like, or leave it blank for anything.</p>
    <div class="recfeedback">
      <textarea id="recGuide" rows="3">${esc(prefill)}</textarea>
      <div class="recguideexamples dim">e.g. “slow-burn Korean thrillers” · “series from the last 3 months” ·
        “a cozy comedy under 30 min an episode” · “something like Breaking Bad but European”</div>
      <div class="recactions">
        <button class="btn primary" id="recResearchNow">🔎 Go research now</button>
        <button class="btn ghost" id="recGuideCancel">Back</button>
      </div>
    </div>`;
  $("#recGuideCancel").onclick = () => openRecommend();
  $("#recResearchNow").onclick = researchNow;
  $("#recGuide").focus();
}

async function decideRec(decision) {
  const m = recCurrent;
  if (!m) return;
  const note = $("#recNote").value.trim();
  const seen = decision === "seen";
  ["#recAccept", "#recSeen", "#recReject", "#recGuided"].forEach(s => { $(s).disabled = true; });
  try {
    const r = await api(`/api/recommendations/${m.id}/decide`, {
      method: "POST",
      body: { decision: seen ? "rejected" : decision, seen, note: note || null },
    });
    const msgs = {
      accepted: `Added "${m.title}" to your wanted list.`,
      seen: `"${m.title}" logged as seen (you own no file).`,
      rejected: `Dismissed "${m.title}".`,
    };
    $("#recBody").innerHTML = `<p class="hint">${esc(msgs[decision] || msgs.rejected)}</p>`;
    recCurrent = null;
    refreshRecPill();
    load(); // wanted/seen state may have changed in the main list
    openRecommend(); // straight on to the next pick — no extra click
  } catch (err) {
    alert(err.message);
    ["#recAccept", "#recSeen", "#recReject"].forEach(s => { $(s).disabled = false; });
  }
}

$("#btnRecommend").onclick = openRecommend;
$("#recClose").onclick = () => $("#recModal").classList.add("hidden");
$("#recModal").addEventListener("click", e => {
  if (e.target === $("#recModal")) $("#recModal").classList.add("hidden");
});

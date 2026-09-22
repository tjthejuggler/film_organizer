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
      const hasKey = (await api("/api/recommendations/status")).enabled;
      $("#recBody").innerHTML = hasKey
        ? `<p class="hint">The queue is empty — the researcher is on it.
           New picks appear automatically; check back in a minute.</p>
           <div class="recactions"><button class="btn ghost" id="recWaitClose">Close</button></div>`
        : `<p class="hint">The recommender needs your <b>z.ai API key</b> —
           add it in <b>⚙ Settings</b> and it will research picks for you
           automatically.</p>
           <div class="recactions"><button class="btn ghost" id="recWaitClose">Close</button></div>`;
      $("#recWaitClose").onclick = () => $("#recModal").classList.add("hidden");
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
          <span class="dim recleft">${pending} in queue</span>
        </div>
      </div>
    </div>`;
  $("#recClose").onclick = () => $("#recModal").classList.add("hidden");
  $("#recAccept").onclick = () => decideRec("accepted");
  $("#recSeen").onclick = () => decideRec("seen");
  $("#recReject").onclick = () => decideRec("rejected");
}

async function decideRec(decision) {
  const m = recCurrent;
  if (!m) return;
  const note = $("#recNote").value.trim();
  const seen = decision === "seen";
  ["#recAccept", "#recSeen", "#recReject"].forEach(s => { $(s).disabled = true; });
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

/* Global UI state (filters / sorting) + persistence across refreshes. */
import { $$ } from "./dom.js";

export const state = {
  q: "", kind: "", watched: "", genre: "", root: "", match: "!not_found", missing: false,
  wanted: "", hidden: "",
  sort: "title", dir: "asc",
  sec: "", secDir: "asc", // previous sort — the silent tie-breaker
  titles: [], genres: [],
};

/* ---------- UI state persistence (survives refresh & restart) ---------- */
const STATE_KEY = "film_organizer_ui_state";
export function saveState() {
  const { q, kind, watched, genre, root, match, missing, wanted, hidden, sort, dir, sec, secDir } = state;
  try {
    localStorage.setItem(STATE_KEY,
      JSON.stringify({ q, kind, watched, genre, root, match, missing, wanted, hidden, sort, dir, sec, secDir }));
  } catch (e) { /* private mode etc. — persistence is best-effort */ }
}
export function restoreState() {
  try { Object.assign(state, JSON.parse(localStorage.getItem(STATE_KEY) || "{}")); }
  catch (e) { /* corrupt entry -> defaults */ }
}
export function applySortIndicators() {
  $$("th.sortable").forEach(t => {
    t.classList.remove("sorted-asc", "sorted-desc");
    if (t.dataset.sort === state.sort)
      t.classList.add(state.dir === "asc" ? "sorted-asc" : "sorted-desc");
  });
}

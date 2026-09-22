/* Film Organizer — boot entry point.
   Importing the feature modules wires their event handlers; this file then
   restores UI state and performs the first load. */
import { $, $$ } from "./dom.js";
import { restoreState, state, applySortIndicators } from "./state.js";
import { load, renderRootOptions } from "./list.js";
import "./rowactions.js";   // delegated row clicks (watched/fav/hide/next/del)
import "./delete.js";       // delete-choice popup bindings
import "./drivequeue.js";   // settings drive-queue panel bindings
import "./jobs.js";         // scan/enrich buttons + job toast
import "./drawer.js";       // drawer static bindings (dBody handlers)
import "./destpicker.js";   // destination popup bindings
import "./filepicker.js";   // folder picker bindings
import "./settings.js";     // settings sheet + pairing bindings
import "./duplicates.js";   // duplicates modal bindings
import "./history.js";      // watched log bindings
import "./recommend.js";    // recommender popup bindings
import "./calendar.js";     // season calendar panel bindings
import { refreshRecPill } from "./recommend.js";
import { watchDrives } from "./drives.js";  // SSE drive watcher
import { refreshBellPill, refreshCalPill } from "./pills.js";

/* ---------- boot ---------- */
restoreState();
// reflect the restored state in the controls before the first load
$("#search").value = state.q;
$("#matchSel").value = state.match;
$("#wantedSel").value = state.wanted;
$("#hiddenSel").value = state.hidden || "";
$("#onlyMissing").checked = !!state.missing;
$$("#kindChips .chip").forEach(c => c.classList.toggle("on", (c.dataset.kind || "") === state.kind));
$$("#watchChips .chip").forEach(c => c.classList.toggle("on", (c.dataset.w || "") === state.watched));
applySortIndicators();
renderRootOptions() // sets #rootSel to state.root once options exist
  .then(load)
  .then(() => { $("#genreSel").value = state.genre; }) // options exist only after first load
  .catch(err => {
    $("#empty").classList.remove("hidden");
    $("#empty").textContent = `Failed to load: ${err.message}`;
  });
watchDrives();
refreshRecPill(); // show how many recommendations are waiting
refreshBellPill(); // show pending watched->backup decisions
refreshCalPill(); // show how many announced upcoming seasons are on the calendar

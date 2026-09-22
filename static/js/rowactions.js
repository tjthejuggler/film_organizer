/* Row-level actions (watched / favorite / hide / next / delete / copy)
   via a single delegated click handler on the document. */
import { $ } from "./dom.js";
import { api } from "./api.js";
import { state } from "./state.js";
import { renderRows, load, searchPerson } from "./list.js";
import { openDeleteChoice } from "./delete.js";
import { copyTitle } from "./clipboard.js";
import { openDrawer } from "./drawer.js";
import { watchJob } from "./jobs.js";

document.addEventListener("click", async e => {
  const nbtn = e.target.closest("[data-next]");
  if (nbtn) {
    e.stopPropagation();
    const id = nbtn.dataset.next;
    const t = state.titles.find(x => String(x.id) === String(id));
    nbtn.disabled = true;
    try {
      if (t.watch_next) {
        await api(`/api/titles/${id}/watch-next`, { method: "DELETE" });
        t.watch_next = null;
        renderRows();
      } else {
        // instant visual confirmation: color the button + row NOW — the
        // actual pin only lands in the DB when the copy job finishes, so
        // a plain load() here would repaint the button as un-clicked
        t.watch_next = t.kind;          // optimistic; load() corrects later
        renderRows();
        const r = await api(`/api/titles/${id}/watch-next`, { method: "POST" });
        // watchJob reloads the list itself once the copy job completes
        watchJob(r.job_id, `Watch Next — ${t.title}`);
      }
    } catch (err) {
      // 409 names the drive to connect; 400 explains missing settings
      t.watch_next = null;              // roll the optimistic pin back
      renderRows();
      alert(err.message);
    }
    nbtn.disabled = false;
    return;
  }
  const wbtn = e.target.closest("[data-watched]");
  if (wbtn) {
    e.stopPropagation();
    const id = wbtn.dataset.watched;
    const t = state.titles.find(x => String(x.id) === String(id));
    const next = !t.watched;
    try {
      await api(`/api/titles/${id}/watched`, { method: "POST", body: { watched: next } });
      t.watched = next;
      renderRows();
    } catch (err) { alert(err.message); }
    return;
  }
  const del = e.target.closest("[data-deltitle]");
  if (del) {
    e.stopPropagation();
    const id = del.dataset.deltitle;
    const t = state.titles.find(x => String(x.id) === String(id));
    openDeleteChoice(t);
    return;
  }
  const fav = e.target.closest("[data-fav]");
  if (fav) {
    e.stopPropagation();
    const id = fav.dataset.fav;
    const t = state.titles.find(x => String(x.id) === String(id));
    try {
      await api(`/api/titles/${id}/favorite`, { method: "POST", body: { value: !t.favorite } });
      t.favorite = !t.favorite;
      renderRows();
    } catch (err) { alert(err.message); }
    return;
  }
  const hbtn = e.target.closest("[data-hide]");
  if (hbtn) {
    e.stopPropagation();
    const id = hbtn.dataset.hide;
    const t = state.titles.find(x => String(x.id) === String(id));
    try {
      const r = await api(`/api/titles/${id}/hidden`, { method: "POST", body: { value: !t.hidden } });
      t.hidden = r.hidden;
      // hiding in the default view makes the row drop out (server filter
      // excludes hidden) -> full reload; in Hidden-only view it stays put
      if (t.hidden && state.hidden !== "only") load();
      else renderRows();
    } catch (err) { alert(err.message); }
    return;
  }
  const chip = e.target.closest("[data-person]");
  if (chip) {
    e.stopPropagation();
    searchPerson(chip.dataset.person);
    return;
  }
  const cp = e.target.closest("[data-copy]");
  if (cp) {
    e.stopPropagation();
    copyTitle(cp);
    return;
  }
  const tr = e.target.closest("tr[data-id]");
  if (tr) openDrawer(+tr.dataset.id);
});

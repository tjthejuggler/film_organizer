/* Live drive connect/disconnect feed (SSE) — reloads the UI on change. */
import { load, renderRootOptions } from "./list.js";
import { refreshBellPill, refreshCalPill } from "./pills.js";
import { refreshDriveQueue } from "./drivequeue.js";

let driveReloadTimer = null;
export function watchDrives() {
  try {
    const es = new EventSource("/api/events/drives");
    es.onmessage = () => {
      clearTimeout(driveReloadTimer);
      // debounce: a single plug/unplug can emit several signature flips
      driveReloadTimer = setTimeout(() => {
        load(); renderRootOptions(); refreshBellPill(); refreshCalPill();
        refreshDriveQueue(); // a queue entry may have just become runnable
      }, 400);
    };
  } catch (e) { /* EventSource unavailable — manual refresh still works */ }
}

"""Storage moves: destinations, title moves, Watch Next pinning."""
import os
import threading
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, drivequeue, fileops, jobs, lut_sync, mover, watchnext
from .common import _get_title_or_404

router = APIRouter()


class MoveIn(BaseModel):
    target: str = "external"  # "internal" | "external" (ignored when dest_root is set)
    dest_root: Optional[str] = None  # explicit destination folder (Move to list)
    purge_others: bool = False  # consolidation: also delete copies on the other side
    seasons: Optional[List[int]] = None  # series: move only these seasons (None = all)
    purge_roots: Optional[List[str]] = None  # exact places to remove the copy from


# ---- move between internal / external storage -----------------------------
@router.get("/api/move-destinations")
def move_destinations():
    """Configured move destinations (internal + per-kind backup + liked
    roots) for the drawer's 'Move to' buttons, each flagged with whether
    the drive is currently mounted."""
    return {"destinations": fileops.move_destinations()}


@router.post("/api/titles/{tid}/move")
def move_title(tid: int, body: MoveIn):
    if body.target not in ("internal", "external"):
        raise HTTPException(400, "target must be 'internal' or 'external'")
    if body.dest_root:
        # must be one of the CONFIGURED destinations — never an arbitrary path
        allowed = {d["root"] for d in fileops.move_destinations()}
        if os.path.abspath(body.dest_root) not in allowed:
            raise HTTPException(400, "dest_root is not a configured destination")
    row = _get_title_or_404(tid)  # 404 early; mover raises ValueError for config issues
    seasons = sorted(set(body.seasons)) if body.seasons else None
    sel = f" (seasons {', '.join(map(str, seasons))} only)" if seasons else ""
    # destination resolved once here: dest_root override wins, else the
    # title's kind decides the external destination (backup_movies_root vs
    # backup_series_root; legacy external_root drive otherwise)
    if body.dest_root:
        dest = os.path.abspath(body.dest_root)
    elif body.target == "external":
        dest = fileops.regular_root(row["kind"])
    else:
        dest = db.settings_get("internal_root")
    if dest and not os.path.isdir(os.path.abspath(dest)):
        dest = os.path.abspath(dest)
        qid, created = drivequeue.enqueue(
            "move", tid, dest,
            {"target": body.target, "dest_root": dest,
             "purge_others": body.purge_others, "seasons": seasons,
             "purge_roots": body.purge_roots},
            f"Move '{row['title']}' to {dest}{sel}")
        return {"job_id": None, "queued": True, "queue_id": qid,
                "queued_now": created, "drive": dest}
    jid = jobs.create("move", total=0)
    jobs.log(jid, f"Move requested: title {tid} -> {dest or body.target}{sel}")

    def _run(job):
        error = None
        try:
            mover.move_title(job, tid, body.target,
                             purge_others=body.purge_others, seasons=seasons,
                             dest_root=body.dest_root,
                             purge_roots=body.purge_roots)
            lut_sync.request_sync("move")   # voice fast-path follows the file
        except Exception as e:
            jobs.log(job, f"FAILED: {e}")
            error = str(e)
        jobs.finish(job, error=error)

    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return {"job_id": jid}


@router.get("/api/titles/{tid}/copy-locations")
def copy_locations(tid: int, seasons: str = None):
    """Where this title's (or the given seasons') live copies sit right
    now: one entry per distinct top-level place (drive root / library
    root), with file counts and mount state. Powers the move dialog's
    'remove from which places?' checklist."""
    _get_title_or_404(tid)
    want = None
    if seasons:
        try:
            want = {int(s) for s in seasons.split(",")}
        except ValueError:
            raise HTTPException(400, "seasons must be comma-separated ints")
    roots = sorted((r["path"] for r in db.q(
        "SELECT path FROM roots WHERE enabled=1")),
        key=len, reverse=True)
    internal = db.settings_get("internal_root")
    places: dict = {}

    def _place_of(p: str) -> Optional[str]:
        ap = os.path.abspath(p)
        for r in roots:
            if ap == r or ap.startswith(r.rstrip(os.sep) + os.sep):
                return r
        return None

    out = {}
    for f in db.q("SELECT season, path FROM files WHERE title_id=? AND missing=0",
                  (tid,)):
        if want is not None and (f["season"] if f["season"] is not None
                                 else 1) not in want:
            continue
        place = _place_of(f["path"])
        if not place or not os.path.exists(f["path"]):
            continue
        e = out.setdefault(place, {"root": place, "files": 0,
                                   "mounted": os.path.isdir(place)})
        e["files"] += 1
    return {"locations": sorted(out.values(), key=lambda e: e["root"])}


# ---- watch next (pin to top + copy into the aaNext_* slot) -----------------
@router.post("/api/titles/{tid}/watch-next")
def set_watch_next(tid: int):
    """Pin a title as Watch Next: copies its files into
    <internal>/aaNext_Movie|aaNext_Series, wiping the previous pin's copy,
    and sticks it to the top of the list no matter the sort."""
    title = _get_title_or_404(tid)
    # fail before any copying when a needed source drive is offline —
    # the error names the drive so the user knows what to plug in
    offline = watchnext.offline_roots(tid)
    if offline:
        raise HTTPException(
            409, "Connect this drive first to set Watch Next: "
            + ", ".join(sorted(offline)))
    try:
        watchnext.slot_dir(title["kind"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    jid = jobs.create("watchnext", total=0)
    jobs.log(jid, f"Watch Next requested: title {tid}")

    def _run(job):
        error = None
        try:
            watchnext.set_next(job, tid)
        except Exception as e:
            jobs.log(job, f"FAILED: {e}")
            error = str(e)
        jobs.finish(job, error=error)

    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return {"job_id": jid}


@router.delete("/api/titles/{tid}/watch-next")
def clear_watch_next(tid: int):
    _get_title_or_404(tid)
    watchnext.clear_next(tid)
    return {"ok": True}

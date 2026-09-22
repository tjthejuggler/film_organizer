"""Duplicates + consolidation review, drive queue management."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import consolidate, db, drivequeue, duplicates

router = APIRouter()


class DeleteCopyIn(BaseModel):
    root: str = None


@router.get("/api/duplicates")
def list_duplicates():
    return {"groups": duplicates.find_duplicates()}


# ---- consolidation ----------------------------------------------------------
@router.get("/api/consolidation/suspects")
def list_consolidation_suspects():
    """Programmatic duplicate check: title groups the auto-merge leaves
    alone (user-locked same-tmdb rows, conflicting TMDB matches, ambiguous
    no-year movies) — review these by hand instead of case-by-case hunting."""
    return {"groups": consolidate.suspects()}


@router.post("/api/consolidation/run")
def run_consolidation():
    """Manual trigger: heal duplicate title rows now (no restart/scan needed)."""
    return {"merged": consolidate.run()}


@router.delete("/api/duplicates/{tid}")
def delete_duplicate_copy(tid: int, root: str = None, queue: bool = True,
                          body: DeleteCopyIn = None):
    # root as query param preferred (robust against stale/cached clients);
    # JSON body accepted too
    r = root if root is not None else (body.root if body else None)
    try:
        return duplicates.delete_copy(tid, root=r)
    except ValueError as e:
        msg = str(e)
        # offline-drive refusal -> queue the deletion for that drive
        if queue and msg.startswith("Connect these drives first:") and r:
            drives = [d.strip() for d in msg.split(":", 1)[1].split(",") if d.strip()]
            title = db.q1("SELECT title FROM titles WHERE id=?", (tid,))
            queued = []
            for d in drives:
                qid, created = drivequeue.enqueue(
                    "delete_copy", tid, d, {"root": r, "drives": drives},
                    f"Delete duplicate copy of '{title['title'] if title else tid}' on {d}")
                if created:
                    queued.append(qid)
            return {"ok": True, "queued": True, "queue_ids": queued,
                    "drives": drives}
        raise HTTPException(400, msg)


@router.get("/api/drive-queue")
def list_drive_queue():
    """Pending drive-queue entries grouped per drive (Settings panel)."""
    return {"groups": drivequeue.list_pending(),
            "pending": drivequeue.pending_count()}


@router.delete("/api/drive-queue/{qid}")
def cancel_drive_queue(qid: int):
    """Remove a pending entry before its drive gets connected."""
    try:
        drivequeue.cancel(qid)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/drive-queue/run")
def run_drive_queue():
    """Manual drain: runs every queued entry whose drive is connected."""
    return {"ok": True, "started": drivequeue.run_due()}

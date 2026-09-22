"""Background job endpoints: scan / enrich / backfill / status / cancel."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, enrich, jobs, lut_sync, scanner

router = APIRouter()


class JobIn(BaseModel):
    root_ids: list = None
    title_ids: list = None
    force: bool = False


@router.post("/api/scan")
def start_scan(body: JobIn = None):
    body = body or JobIn()
    roots = scanner.root_dirs_for_scan(body.root_ids)
    if not roots:
        raise HTTPException(400, "no enabled roots to scan")
    jid = jobs.create("scan", total=0)
    jobs.log(jid, f"Scan requested for: {', '.join(roots)}")

    def _scan(job):
        scanner.scan_roots(job, roots)
        lut_sync.request_sync("scan")   # voice fast-path follows the catalog

    jobs.run_background(jid, _scan)
    return {"job_id": jid}


@router.post("/api/enrich")
def start_enrich(body: JobIn = None):
    # Always allowed: rows are marked 'no_provider' when no TMDB key is set.
    body = body or JobIn()
    jid = jobs.create("enrich", total=0)
    jobs.run_background(
        jid, lambda j: enrich.run_enrich(j, title_ids=body.title_ids,
                                         force=body.force, only_unmatched=not body.force))
    return {"job_id": jid}


@router.post("/api/backfill-ratings")
def backfill_ratings():
    """Fill cert + Rotten Tomatoes for already-matched titles (light pass)."""
    jid = jobs.create("backfill", total=0)
    jobs.run_background(jid, lambda j: enrich.run_backfill(j))
    return {"job_id": jid}


@router.get("/api/jobs/{jid}")
def get_job(jid: str):
    row = db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
    if not row:
        raise HTTPException(404, "job not found")
    d = dict(row)
    d["log"] = (d["log"] or "").strip().split("\n")[-30:]
    return d


@router.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    """Cooperative cancel: the job's worker loop checks the flag between
    work items and stops; nothing is killed mid-write."""
    ok = jobs.request_cancel(jid)
    if not ok:
        row = db.q1("SELECT status FROM jobs WHERE id=?", (jid,))
        if not row:
            raise HTTPException(404, "job not found")
        if row["status"] != "running":
            return {"ok": True, "status": row["status"]}  # nothing to cancel
    return {"ok": True, "status": "cancelled"}

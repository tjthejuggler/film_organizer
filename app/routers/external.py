"""External program API: submit wanted titles / report watched."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, enrich, jobs
from ..scanner import dedupe_key
from .common import _get_title_or_404, _title_payload

router = APIRouter()


class WantedIn(BaseModel):
    """External submission of a film/show we want but don't have yet."""
    title: str
    kind: str = "movie"
    year: Optional[int] = None
    note: Optional[str] = None
    by: Optional[str] = None   # submitting program, shown in the UI
    watched: bool = False


class ExtWatchedIn(BaseModel):
    """External 'this was watched' report (by id or by title)."""
    title: Optional[str] = None
    kind: str = "movie"
    year: Optional[int] = None
    tmdb_id: Optional[int] = None
    imdb_id: Optional[str] = None
    watched: bool = True
    create_missing: bool = False  # add to wanted list when not found
    by: Optional[str] = None


def _find_title(body_t: str = None, kind: str = "movie", year: int = None,
                tmdb_id: int = None, imdb_id: str = None):
    """Locate a catalog row by external id first, then by dedupe key."""
    if tmdb_id:
        r = db.q1("SELECT * FROM titles WHERE tmdb_id=?", (tmdb_id,))
        if r:
            return r
    if imdb_id:
        r = db.q1("SELECT * FROM titles WHERE imdb_id=?", (imdb_id,))
        if r:
            return r
    if body_t:
        return db.q1("SELECT * FROM titles WHERE dedupe_key=?",
                     (dedupe_key(kind, body_t, year),))
    return None


@router.get("/api/wanted")
def list_wanted():
    rows = db.q("SELECT * FROM titles WHERE wanted=1 ORDER BY title COLLATE NOCASE")
    return {"wanted": [_title_payload(r) for r in rows]}


@router.post("/api/wanted")
def add_wanted(body: WantedIn):
    """Submit a film/show we want. Idempotent: re-submits update the note.
    The row is stored WITH wanted=1 and immediately enriched (provider keys
    permitting) so the list shows ratings, poster and runtime right away.
    When the files later appear on disk, the scanner adopts this row
    (same dedupe_key) and the wanted flag flips off automatically."""
    if body.kind not in ("movie", "series"):
        raise HTTPException(400, "kind must be 'movie' or 'series'")
    key = dedupe_key(body.kind, body.title, body.year)
    existing = db.q1("SELECT * FROM titles WHERE dedupe_key=?", (key,))
    if existing:
        with db.tx() as c:
            c.execute("UPDATE titles SET wanted=1, "
                      "wanted_note=COALESCE(?, wanted_note), "
                      "wanted_by=COALESCE(?, wanted_by) WHERE id=?",
                      (body.note, body.by, existing["id"]))
        tid = existing["id"]
        created = False
    else:
        with db.tx() as c:
            c.execute(
                """INSERT INTO titles(dedupe_key, kind, title, year, match_status,
                   wanted, wanted_note, wanted_by, cataloged_at, watched_manual, watched_at)
                   VALUES(?,?,?,?, 'unmatched', 1, ?, ?, ?, ?, ?)""",
                (key, body.kind, body.title, body.year, body.note, body.by,
                 jobs.now_iso(), 1 if body.watched else 0,
                 jobs.now_iso() if body.watched else None))
        tid = db.q1("SELECT id FROM titles WHERE dedupe_key=?", (key,))["id"]
        created = True
        # best-effort background enrich so the wanted row shows real data
        try:
            jid = jobs.create("enrich", total=1)
            jobs.run_background(jid, lambda j: enrich.run_enrich(j, title_ids=[tid]))
        except Exception:
            pass  # enrichment is optional; the wanted row is already stored
    return {"ok": True, "id": tid, "created": created}


@router.delete("/api/wanted/{tid}")
def remove_wanted(tid: int):
    """Un-want. Rows WITH files keep their catalog entry (flag cleared);
    rows WITHOUT files (pure wishlist) are deleted entirely."""
    row = _get_title_or_404(tid)
    has_files = db.q1("SELECT 1 FROM files WHERE title_id=? AND missing=0 LIMIT 1", (tid,))
    with db.tx() as c:
        if has_files:
            c.execute("UPDATE titles SET wanted=0 WHERE id=?", (tid,))
        else:
            c.execute("DELETE FROM titles WHERE id=?", (tid,))
    return {"ok": True, "removed": not bool(has_files), "title": row["title"]}


@router.post("/api/watched")
def ext_watched(body: ExtWatchedIn):
    """External 'I watched this' report. Matches by tmdb/imdb id, then by
    dedupe key (title+kind+year). Returns matched id, or 404 with the
    would-be dedupe info; pass create_missing=true to add it to the wanted
    list instead of failing."""
    row = _find_title(body.title, body.kind, body.year, body.tmdb_id, body.imdb_id)
    if row is None:
        if body.create_missing and body.title:
            add_wanted(WantedIn(title=body.title, kind=body.kind, year=body.year,
                                note=f"watched report from {body.by or 'external'}",
                                by=body.by, watched=body.watched))
            row = _find_title(body.title, body.kind, body.year, body.tmdb_id, body.imdb_id)
            if row:
                return {"ok": True, "id": row["id"], "created": True,
                        "watched": body.watched}
        raise HTTPException(404, "title not in catalog — retry with create_missing=true to add it")
    with db.tx() as c:
        c.execute(
            "UPDATE titles SET watched_manual=?, watched_at=? WHERE id=?",
            (1 if body.watched else 0,
             jobs.now_iso() if body.watched else None, row["id"]),
        )
    # programmatic watched reports (no user present) queue the backup
    # decision for the NEXT time the web app is opened
    if body.watched:
        try:
            from .. import notifications
            notifications.notify_watched_backup(row["id"])
        except Exception:
            pass  # notifications are never allowed to break the report
        # external watch reports also trigger the next-season lookup
        try:
            from .. import seasons
            seasons.on_watched(row["id"])
        except Exception:
            pass  # the season calendar is never allowed to break watching
    return {"ok": True, "id": row["id"], "watched": body.watched}

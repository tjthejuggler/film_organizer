"""Watched log: titles seen without owning a file (manual history)."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, enrich, jobs, tmdb
from ..scanner import dedupe_key
from .common import _get_title_or_404, _title_payload

router = APIRouter()


class HistoryIn(BaseModel):
    """Manual watch-history entry: seen, but no file is owned anymore."""
    title: str
    kind: str = "movie"
    year: Optional[int] = None
    note: Optional[str] = None
    watched_at: Optional[str] = None  # ISO date the user saw it (optional)
    tmdb_id: Optional[int] = None     # pins the user-confirmed TMDB candidate
    imdb_id: Optional[str] = None


class HistorySearchIn(BaseModel):
    """Candidate lookup for the seen-log confirm step: checks the local
    catalog first, then queries TMDB so the user can confirm the match."""
    title: str
    kind: str = "movie"
    year: Optional[int] = None


@router.get("/api/history")
def list_history():
    """All seen-history entries (watched titles we no longer own)."""
    rows = db.q("SELECT * FROM titles WHERE history=1 ORDER BY title COLLATE NOCASE")
    return {"history": [_title_payload(r) for r in rows]}


@router.post("/api/history/search")
def search_history_candidates(body: HistorySearchIn):
    """Pre-add lookup: (1) rows already in the catalog (any flag), then
    (2) up to 6 TMDB candidates with poster/year/overview so the user can
    confirm what they watched before anything is written."""
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "title required")

    # 1) local catalog matches (exact dedupe key + fuzzy title contains)
    local = []
    exact = db.q1("SELECT * FROM titles WHERE dedupe_key=?",
                  (dedupe_key(body.kind, title, body.year),))
    if exact:
        local.append(_title_payload(exact))
    else:
        for r in db.q(
            "SELECT * FROM titles WHERE kind=? AND title LIKE ? LIMIT 5",
            (body.kind, f"%{title}%"),
        ):
            local.append(_title_payload(r))

    # 2) TMDB candidates (posters + overview for the confirm cards)
    tmdb_candidates, tmdb_error = [], None
    if tmdb.enabled():
        try:
            cands = (tmdb.search_tv(title, body.year) if body.kind == "series"
                     else tmdb.search_movie(title, body.year))
            cands = [c for c in cands if c.get("name")]
            cands.sort(key=lambda c: (c.get("score", 0), c.get("popularity", 0)),
                       reverse=True)
            for c in cands[:6]:
                tmdb_candidates.append({
                    "tmdb_id": c["id"], "name": c["name"],
                    "year": (c.get("date") or "")[:4] or None,
                    "poster": c.get("poster"),
                    "overview": c.get("overview"),
                    "score": c.get("score", 0),
                })
        except Exception as e:
            tmdb_error = str(e)[:200]
    else:
        tmdb_error = "no TMDB key configured (add one in Settings)"

    return {"local": local, "tmdb": tmdb_candidates, "tmdb_error": tmdb_error}


@router.post("/api/history")
def add_history(body: HistoryIn):
    """Record a movie/series as seen WITHOUT owning a file — pure watch log.
    Idempotent per title+kind+year: re-adding refreshes the note/date. If a
    matching catalog row already exists it is simply flagged history=1; the
    row then keeps working normally if its files ever come back (history is
    cleared by the scanner when files appear)."""
    if body.kind not in ("movie", "series"):
        raise HTTPException(400, "kind must be 'movie' or 'series'")
    key = dedupe_key(body.kind, body.title, body.year)
    watched_at = body.watched_at or jobs.now_iso()
    existing = db.q1("SELECT * FROM titles WHERE dedupe_key=?", (key,))
    if existing:
        tid = existing["id"]
        with db.tx() as c:
            c.execute(
                "UPDATE titles SET history=1, watched_manual=1, watched_at=?, "
                "wanted_note=COALESCE(?, wanted_note) WHERE id=?",
                (watched_at, body.note, tid))
    else:
        with db.tx() as c:
            c.execute(
                """INSERT INTO titles(dedupe_key, kind, title, year, match_status,
                   history, wanted, watched_manual, watched_at, cataloged_at, wanted_note,
                   tmdb_id, imdb_id)
                   VALUES(?,?,?,?,'unmatched',1,0,1,?,?,?,?,?)""",
                (key, body.kind, body.title, body.year, watched_at,
                 jobs.now_iso(), body.note, body.tmdb_id, body.imdb_id))
        tid = db.q1("SELECT id FROM titles WHERE dedupe_key=?", (key,))["id"]
    # enrichment: when the user confirmed a specific TMDB candidate, fetch
    # THAT one synchronously (fast, and no risk of the auto-matcher picking
    # a different title); otherwise fall back to the background auto-enrich
    if body.tmdb_id:
        try:
            row = _get_title_or_404(tid)
            if body.tmdb_id != row["tmdb_id"] or row["match_status"] != "matched":
                detail = (tmdb.tv_detail(body.tmdb_id) if body.kind == "series"
                          else tmdb.movie_detail(body.tmdb_id))
                enrich._apply(tid, detail, "tmdb")
        except Exception:
            pass  # never fail the add over enrichment; auto-enrich may retry
    else:
        try:
            jid = jobs.create("enrich", total=1)
            jobs.run_background(jid, lambda j: enrich.run_enrich(j, title_ids=[tid]))
        except Exception:
            pass  # enrichment is optional; the entry is already stored
    return {"ok": True, "id": tid}


@router.delete("/api/history/{tid}")
def remove_history(tid: int):
    """Drop a seen-history entry. Entries WITHOUT files are deleted entirely;
    entries that turned out to also exist on disk just lose the flag."""
    _get_title_or_404(tid)
    has_files = db.q1("SELECT 1 FROM files WHERE title_id=? AND missing=0 LIMIT 1", (tid,))
    with db.tx() as c:
        if has_files:
            c.execute("UPDATE titles SET history=0 WHERE id=?", (tid,))
        else:
            c.execute("DELETE FROM titles WHERE id=?", (tid,))
    return {"ok": True, "removed": not bool(has_files)}

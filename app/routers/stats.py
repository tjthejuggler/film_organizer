"""Disk-usage stats (respects the same filters as /api/titles)."""
from fastapi import APIRouter

from .common import _filter_clause

router = APIRouter()


@router.get("/api/stats")
def stats(
    q: str = None, kind: str = None, watched: str = None,
    match: str = None, genre: str = None,
    root: str = None, missing_on: str = None, cert: str = None,
    person: str = None, wanted: str = None, seen: str = None,
    hidden: str = None,
):
    """Counts reflect the CURRENT FILTER (same params as /api/titles),
    with a watched/unwatched breakdown of the filtered set."""
    from .. import db
    where, params = _filter_clause(q, kind, watched, match, genre,
                                   root, missing_on, cert, person, wanted,
                                   seen, hidden)
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    r = db.q1(
        f"""SELECT COUNT(*) n,
                  SUM(CASE WHEN kind='movie' THEN 1 ELSE 0 END) movies,
                  SUM(CASE WHEN kind='series' THEN 1 ELSE 0 END) series,
                  SUM(CASE WHEN (CASE WHEN watched_manual IS NOT NULL THEN watched_manual ELSE watched_folder END)=1 THEN 1 ELSE 0 END) watched,
                  SUM(CASE WHEN (CASE WHEN watched_manual IS NOT NULL THEN watched_manual ELSE watched_folder END)=0 THEN 1 ELSE 0 END) unwatched,
                  SUM(size_bytes) bytes
           FROM titles t{wsql}""", params)
    d = dict(r)
    d["watched"] = d["watched"] or 0
    d["unwatched"] = d["unwatched"] or 0
    return d

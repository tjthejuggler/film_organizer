"""Catalog titles: list / detail / edit / flags / delete / open-location."""
import json
import os
import subprocess
import threading
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, drivequeue, duplicates, episodes, fileops, jobs, liked, \
    lut_sync, notifications, scanner, seasons
from .common import _AVG_SCORE_SQL, _filter_clause, _get_title_or_404, \
    _title_payload

router = APIRouter()


# ---- models ---------------------------------------------------------------
class WatchedIn(BaseModel):
    watched: bool


class FlagIn(BaseModel):
    value: bool


class TitlePatch(BaseModel):
    # identity (re-keys the row; a title/kind change also wipes enrichment)
    title: Optional[str] = None
    kind: Optional[str] = None
    year: Optional[int] = None
    is_miniseries: Optional[bool] = None
    # detail corrections (safe edits, no enrichment reset)
    overview: Optional[str] = None
    director: Optional[str] = None
    creator: Optional[str] = None
    network: Optional[str] = None
    status: Optional[str] = None
    cert: Optional[str] = None
    runtime: Optional[int] = None
    seasons: Optional[int] = None
    episodes: Optional[int] = None
    rating_imdb: Optional[float] = None
    votes_imdb: Optional[int] = None
    rating_tmdb: Optional[float] = None
    rating_rt: Optional[int] = None
    rating_mc: Optional[int] = None
    stars: Optional[list] = None
    genres: Optional[list] = None
    clear: Optional[list] = None  # field names to NULL out (e.g. wrong rating)


class EpisodeWatchedIn(BaseModel):
    watched: bool


class SeasonWatchedIn(BaseModel):
    watched: bool
    season: Optional[int] = None  # None = every season


# ---- library / media ------------------------------------------------------
@router.get("/api/titles")
def list_titles(
    q: str = None, kind: str = None, watched: str = None,
    match: str = None, genre: str = None,
    root: str = None, missing_on: str = None, cert: str = None,
    person: str = None, wanted: str = None, seen: str = None,
    hidden: str = None,
    sort: str = "title", direction: str = "asc",
    # tie-breaker the frontend derives from the PREVIOUS sort the user had
    # active: when the primary key has ties (year, rating...), those groups
    # stay ordered by the earlier sort instead of an arbitrary order
    secondary: str = None, secondary_dir: str = None,
    limit: int = 10000, offset: int = 0,
):
    where, params = _filter_clause(q, kind, watched, match, genre,
                                   root, missing_on, cert, person, wanted,
                                   seen, hidden)

    ORDER = {
        "title": "t.title COLLATE NOCASE", "year": "t.year",
        "rating": "COALESCE(t.rating_imdb, t.rating_tmdb)",
        "rt": "t.rating_rt",
        "mc": "t.rating_mc",
        "avg": f"({_AVG_SCORE_SQL})",
        "runtime": "t.runtime",
        "cataloged": "t.cataloged_at",
        "created": "COALESCE(t.created_at, t.cataloged_at)",
        "size": "t.size_bytes",
        "watched": "(CASE WHEN t.watched_manual IS NOT NULL THEN t.watched_manual ELSE t.watched_folder END)",
        "match": "t.match_status",
    }
    order = ORDER.get(sort, ORDER["title"])
    direction = "DESC" if direction.lower() == "desc" else "ASC"
    # unknown key / same-as-primary / absent -> no secondary clause at all
    sec_order = ORDER.get(secondary or "")
    sec_dir = "DESC" if (secondary_dir or "").lower() == "desc" else "ASC"
    sec_sql = (f", {sec_order} {sec_dir}"
               if sec_order and (secondary or "") != sort else "")

    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.q(
        f"""SELECT t.* FROM titles t{wsql}
            ORDER BY (t.watch_next IS NOT NULL) DESC,
                     {order} {direction}{sec_sql}, t.title COLLATE NOCASE
            LIMIT ? OFFSET ?""",
        params + [limit, offset],
    )
    total = db.q1(f"SELECT COUNT(*) n FROM titles t{wsql}", params)["n"]

    # genre facet + per-title roots summary; "Miniseries" is a pseudo-genre
    # backed by the is_miniseries flag, always offered for filtering
    genres = sorted({
        g for r in db.q("SELECT genres FROM titles WHERE genres IS NOT NULL")
        for g in (json.loads(r["genres"] or "[]"))
    } | {"Miniseries"})
    out = [_title_payload(r) for r in rows]
    # season-calendar badges: upcoming-season / finished chips under the name
    try:
        flags = seasons.flags_for_titles([t["id"] for t in out])
        for t in out:
            f = flags.get(t["id"])
            if f:
                t["season_upcoming"] = f["upcoming"]
                t["season_finished"] = f["finished"]
    except Exception:
        pass  # badges are cosmetic — never fail the list over them
    # live drive connectivity: isdir per distinct location (cached per request)
    online = {}
    for t in out:
        roots = db.q(
            """SELECT DISTINCT r.path p FROM files f
               JOIN roots r ON f.path LIKE r.path || '%'
               WHERE f.title_id=? AND f.missing=0 ORDER BY r.path""",
            (t["id"],),
        )
        locs = [r["p"] for r in roots]
        t["locations"] = locs
        t["offline_locations"] = [
            p for p in locs
            if online.setdefault(p, os.path.isdir(p)) is False
        ]
        missing = db.q1("SELECT COUNT(*) n FROM files WHERE title_id=? AND missing=1", (t["id"],))["n"]
        t["missing_files"] = missing
    return {"total": total, "titles": out, "genres": genres}


def _storage_sides(files: list) -> set:
    """{'internal','external'} sides where this title's accessible files
    live — both sides at once means a duplicated title, and the drawer
    then offers BOTH move buttons (either click consolidates). 'external'
    means any backup destination (the legacy drive, a per-kind regular
    backup root or a liked drive)."""
    roots = [os.path.abspath(r).rstrip(os.sep) + os.sep
             for r in fileops.all_backup_roots()]
    sides = set()
    for f in files:
        if f["missing"]:
            continue
        p = os.path.abspath(f["path"]) + os.sep
        sides.add("external" if any(p.startswith(r) for r in roots)
                  else "internal")
    return sides


def _storage_side(files: list) -> str:
    """'external' when any accessible file already sits on the backup drive,
    else 'internal' — tells the drawer which move button makes sense."""
    return "external" if "external" in _storage_sides(files) else "internal"


@router.get("/api/titles/{tid}")
def get_title(tid: int):
    row = _get_title_or_404(tid)
    d = _title_payload(row)
    d["files"] = [dict(f) for f in db.q(
        "SELECT id, path, size_bytes, season, episode, watched_folder, watched_manual, missing FROM files WHERE title_id=? ORDER BY path",
        (tid,))]
    # drawer checklist progress: how many parsed episode files are ticked
    d["episodes_watched"] = sum(
        1 for f in d["files"] if f["episode"] is not None
        and not f["missing"] and f["watched_manual"] == 1)
    d["episodes_tracked"] = sum(
        1 for f in d["files"] if f["episode"] is not None and not f["missing"])
    d["locations"] = [r["p"] for r in db.q(
        """SELECT DISTINCT r.path p FROM files f
           JOIN roots r ON f.path LIKE r.path || '%'
           WHERE f.title_id=? AND f.missing=0 ORDER BY r.path""", (tid,))]
    d["storage_side"] = _storage_side(d["files"])
    d["on_both_drives"] = len(_storage_sides(d["files"])) > 1
    return d


DETAIL_FIELDS = {
    "overview": str, "director": str, "creator": str, "network": str,
    "status": str, "cert": str, "runtime": int, "seasons": int,
    "episodes": int, "rating_imdb": float, "votes_imdb": int,
    "rating_tmdb": float, "rating_rt": int, "rating_mc": int,
}
LIST_FIELDS = {"stars", "genres"}


@router.patch("/api/titles/{tid}")
def patch_title(tid: int, body: TitlePatch):
    row = _get_title_or_404(tid)
    sets, vals = [], []
    touched = []  # fields the user manually set/cleared -> enrich must skip

    # identity values are only "changed" when they actually DIFFER from the
    # stored row. The edit form always submits title/kind/year, so comparing
    # raw request fields treated EVERY save as a re-key, wiping enrichment
    # AND the per-field manual_edit locks — user-corrected ratings then
    # reverted on the next Enrich.
    identity_change = ((body.title and body.title != row["title"])
                       or (body.kind and body.kind != row["kind"]
                           and body.kind in ("movie", "series"))
                       or (body.year is not None and body.year != row["year"]))

    # miniseries flag: dedicated control outside the enrich-lock system
    # (a later Enrich re-derives it from TMDB's TV type for series rows)
    if body.is_miniseries is not None:
        sets.append("is_miniseries=?")
        vals.append(1 if body.is_miniseries else 0)

    # ---- detail corrections: direct, surgical, keep enrichment ----------
    for fname in DETAIL_FIELDS:
        v = getattr(body, fname)
        if v is not None:
            sets.append(f"{fname}=?")
            vals.append(v)
            touched.append(fname)
    for fname in LIST_FIELDS:
        v = getattr(body, fname)
        if v is not None:
            sets.append(f"{fname}=?")
            vals.append(json.dumps(v))
            touched.append(fname)
    cleared = []
    for fname in (body.clear or []):
        if fname in LIST_FIELDS:
            sets.append(f"{fname}='[]'")
            cleared.append(fname)
        elif fname in DETAIL_FIELDS:
            sets.append(f"{fname}=NULL")
            cleared.append(fname)
    if sets:
        # lock every SET field so a later Enrich never reverts the fix;
        # CLEARED fields are unlocked so enrichment refills them cleanly
        locked = (set(json.loads(row["manual_edits"] or "[]")) - set(cleared)) \
            | set(touched)
        sets.append("manual_edits=?")
        vals.append(json.dumps(sorted(locked)))
        with db.tx() as c:
            c.execute(f"UPDATE titles SET {', '.join(sets)} WHERE id=?", vals + [tid])
        if not identity_change:
            return get_title(tid)  # detail-only edit: done

    sets, vals = [], []
    if body.title:
        sets.append("title=?")
        vals.append(body.title)
    if body.kind in ("movie", "series"):
        sets.append("kind=?")
        vals.append(body.kind)
    if body.year is not None:
        sets.append("year=?")
        vals.append(body.year)
    if identity_change:
        row = db.q1("SELECT * FROM titles WHERE id=?", (tid,))
        new_title = body.title or row["title"]
        # a kind flip usually means the old year came from a wrong match;
        # unless the user explicitly sets a year, clear it so the next
        # search isn't filtered to the wrong release
        if body.kind and body.year is None:
            sets.append("year=NULL")
            new_year = None
        else:
            new_year = body.year if body.year is not None else row["year"]
        new_kind = body.kind or row["kind"]
        key = scanner.dedupe_key(new_kind, new_title, new_year)
        sets.append("dedupe_key=?")
        vals.append(key)
        # wipe polluted enrichment so the next Enrich starts clean;
        # also drop per-field edit locks and reset the retry counters —
        # fresh identity, fresh values, full attempt budget again
        sets.append(
            "match_status='unmatched', match_error=NULL, enriched_at=NULL, "
            "data_source=NULL, tmdb_id=NULL, imdb_id=NULL, overview=NULL, "
            "rating_imdb=NULL, rating_tmdb=NULL, poster=NULL, backdrop=NULL, "
            "stars='[]', genres='[]', manual_edits='[]', "
            "enrich_attempts=0, backfill_miss=0"
        )
        sets.append("title_locked=1, kind_locked=1")
        vals.append(tid)
        with db.tx() as c:
            c.execute(f"UPDATE titles SET {', '.join(sets)} WHERE id=?", vals)
    return get_title(tid)


@router.post("/api/titles/{tid}/watched")
def set_watched(tid: int, body: WatchedIn):
    _get_title_or_404(tid)
    # a fileless row marked watched becomes a remembered record (history=1)
    # so a later scan can't prune it — this is the "delete file, keep memory"
    # promise; rows WITH files don't need it (files keep them alive)
    has_files = db.q1("SELECT 1 FROM files WHERE title_id=? AND missing=0 LIMIT 1", (tid,))
    with db.tx() as c:
        c.execute(
            "UPDATE titles SET watched_manual=?, watched_at=?, "
            "history=CASE WHEN ? AND ? THEN 1 ELSE history END WHERE id=?",
            (1 if body.watched else 0,
             jobs.now_iso() if body.watched else None,
             1 if body.watched else 0, 0 if has_files else 1, tid),
        )
    # first watch of an owned title queues the "move to backup?" decision
    if body.watched and has_files:
        try:
            notifications.notify_watched_backup(tid)
        except Exception:
            pass  # notifications are never allowed to break the watch toggle
    # watched a series? check right away whether the next season is announced
    if body.watched:
        try:
            seasons.on_watched(tid)
        except Exception:
            pass  # the season calendar is never allowed to break watching
    return {"ok": True, "watched": body.watched}


@router.post("/api/titles/{tid}/files/{fid}/watched")
def set_file_watched(tid: int, fid: int, body: EpisodeWatchedIn):
    """Tick one episode in the drawer checklist. When this tick completes
    the set, the whole series is auto-marked watched (promoted=true)."""
    _get_title_or_404(tid)
    try:
        return episodes.set_episode_watched(tid, fid, body.watched)
    except episodes.EpisodeNotFound:
        raise HTTPException(404, "episode file not found")


@router.post("/api/titles/{tid}/episodes-watched")
def set_episodes_watched(tid: int, body: SeasonWatchedIn):
    """Bulk tick/untick one season (season=null: all seasons) of the
    drawer checklist; auto-promotes the series on the completing tick."""
    _get_title_or_404(tid)
    return episodes.set_season_watched(tid, body.season, body.watched)


@router.post("/api/titles/{tid}/favorite")
def set_favorite(tid: int, body: FlagIn):
    """Toggle the like flag. LIKING a title keeps its two-place backup
    contract: a background job tops up the additional liked-drive copy
    (no-op when no liked folder is configured for this kind). UN-LIKING
    removes that secondary copy again — the regular backup stays."""
    row = _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("UPDATE titles SET favorite=? WHERE id=?",
                  (1 if body.value else 0, tid))
    if body.value:
        try:
            if fileops.liked_root(row["kind"]):
                jid = jobs.create("liked_sync", total=0)
                jobs.log(jid, f"Liked backup: '{row['title']}'")

                def _run(job):
                    error = None
                    try:
                        liked.sync_liked_copy(
                            title_id=tid, log=lambda m: jobs.log(job, m))
                    except Exception as e:
                        jobs.log(job, f"FAILED: {e}")
                        error = str(e)
                    jobs.finish(job, error=error)

                threading.Thread(target=_run, args=(jid,), daemon=True).start()
        except Exception:
            pass  # liking must succeed even when the backup cannot
    else:
        try:
            liked.remove_liked_copy(tid)
        except Exception:
            pass  # never block the toggle on backup cleanup
    return {"ok": True, "favorite": body.value}


@router.post("/api/titles/{tid}/wanted")
def set_wanted(tid: int, body: FlagIn):
    """Toggle the wanted flag on an EXISTING row (UI convenience)."""
    _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("UPDATE titles SET wanted=? WHERE id=?", (1 if body.value else 0, tid))
    return {"ok": True, "wanted": body.value}


@router.post("/api/titles/{tid}/hidden")
def set_hidden(tid: int, body: FlagIn):
    """Tuck a title away: excluded from the default list (the 'Hidden'
    filter shows only these). Never deletes anything — files, watched
    state and details all stay; un-hide any time."""
    _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("UPDATE titles SET hidden=? WHERE id=?", (1 if body.value else 0, tid))
    return {"ok": True, "hidden": body.value}


@router.delete("/api/titles/{tid}")
def delete_title(tid: int):
    """Remove a title from the catalog WITHOUT touching files on disk."""
    _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("DELETE FROM titles WHERE id=?", (tid,))
    lut_sync.request_sync("delete_title")   # voice fast-path drops dead entries
    # the pin's slot copy still exists on disk but no row pins it anymore —
    # setting a new Watch Next will wipe the slot as usual
    return {"ok": True}


@router.delete("/api/titles/{tid}/files")
def delete_title_files(tid: int, keep_record: bool = False, queue: bool = True):
    """Delete a title's files from disk (main-list trash button).

    keep_record=false: the catalog entry is removed too (full delete).
    keep_record=true:  the entry STAYS as a fileless record (history=1) —
    this is how 'watched it, then deleted it' keeps its watched state, and
    also how unwatched titles can be recorded as owned-but-discarded.

    When any source drive is offline the deletion is placed in the drive
    queue (one entry per offline drive) and runs automatically once that
    drive is connected; queue=false restores the legacy 409 refusal."""
    row = _get_title_or_404(tid)
    # full rows needed: _folder_plan() reads size_bytes (folder-vs-catalog
    # byte comparison); selecting a subset here caused a KeyError 500 on
    # every folder-based delete
    files = [dict(f) for f in db.q(
        "SELECT * FROM files WHERE title_id=?", (tid,))]
    if not files:
        # wanted-list-only entry (or files never recorded): just drop the
        # catalog row — there is nothing on disk to touch
        with db.tx() as c:
            c.execute("DELETE FROM titles WHERE id=?", (tid,))
        return {"ok": True, "title": row["title"], "removed_files": 0,
                "kept_record": False}

    roots = [r["path"] for r in db.q("SELECT path FROM roots ORDER BY length(path) DESC")]

    def root_of(p):
        for r in roots:
            if p == r or p.startswith(r.rstrip("/") + "/"):
                return r
        return None

    offline = set()
    for f in files:
        if f["missing"]:
            continue
        rt = root_of(f["path"])
        if rt and not os.path.isdir(rt):
            offline.add(rt)
    if offline:
        if not queue:
            raise HTTPException(
                409, "Connect these drives first, then retry: "
                + ", ".join(sorted(offline)))
        drives = sorted(offline)
        queued = []
        for d in drives:
            qid, created = drivequeue.enqueue(
                "delete", tid, d,
                {"keep_record": keep_record, "drives": drives},
                f"Delete '{row['title']}' files on {d}")
            if created:
                queued.append(qid)
        # files on connected drives (and missing ghosts) go right away;
        # the queued entries handle their drives and the last one finalizes
        # the catalog entry
        connected = [f for f in files if root_of(f["path"]) not in offline]
        if connected:
            duplicates._delete_title_files(connected, tid, delete_row=False)
        return {"ok": True, "queued": True, "queue_ids": queued,
                "removed_files": len(connected),
                "title": row["title"], "drives": drives}

    result = duplicates._delete_title_files(files, tid, delete_row=not keep_record)
    if keep_record:
        with db.tx() as c:
            c.execute(
                "UPDATE titles SET history=1, size_bytes=0, episode_count=0, "
                "seasons=NULL, last_seen=? WHERE id=?",
                (jobs.now_iso(), tid))
    lut_sync.request_sync("delete_files")   # voice fast-path drops dead entries
    return {"ok": True, "title": row["title"], "kept_record": keep_record, **result}


@router.get("/api/titles/{tid}/files/{fid}/open")
def open_location(tid: int, fid: int):
    """Reveal the file's folder in the OS file manager."""
    f = db.q1("SELECT * FROM files WHERE id=? AND title_id=?", (fid, tid))
    if not f:
        raise HTTPException(404, "file not found")
    target = f["path"] if f["is_dir"] else os.path.dirname(f["path"])
    if not os.path.exists(target):
        raise HTTPException(410, "path no longer exists on disk")
    for cmd in (("xdg-open", target),):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True}
        except OSError:
            continue
    raise HTTPException(500, "could not open file manager")

"""FastAPI application: API routes + static frontend."""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, drivequeue, duplicates, enrich, episodes, fileops, jobs, llm, lut_sync, mover, notifications, recommender, scanner, seasons, tmdb, watchnext

db.init()

app = FastAPI(title="Film Organizer", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.on_event("startup")
def _startup_refill():
    """Automatically top the recommendation queue back up when the app boots
    (silently skipped when no LLM key is configured or a job already runs)."""
    try:
        recommender.maybe_refill("startup")
    except Exception:
        pass  # never block boot over recommendations
    # season calendar: seed once (web-researched facts), then poll weekly
    try:
        if not db.q1("SELECT 1 FROM season_watch LIMIT 1"):
            jid = jobs.create("season_poll", total=0)
            jobs.log(jid, "first boot: loading web-researched season seeds")
            threading.Thread(target=seasons.seed, args=(jid,), daemon=True).start()
        seasons.maybe_poll_weekly()
    except Exception:
        pass  # the season calendar must never block boot
    # drive-queue worker: runs queued moves/deletes whenever their drive
    # gets connected — also drains anything queued while the app was off
    threading.Thread(target=drivequeue.worker_loop, daemon=True).start()


@app.middleware("http")
async def no_stale_static(request, call_next):
    """Never let the browser run old frontend code: stale cached JS caused
    'button clicked but nothing happened' reports. API responses must never
    be cached either, so lists reflect deletes instantly."""
    resp = await call_next(request)
    if request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    elif "cache-control" not in resp.headers:
        # routes may set a stronger policy themselves (favicon: no-store)
        resp.headers["Cache-Control"] = "no-cache"
    return resp


from typing import List, Optional

# ---- models ---------------------------------------------------------------
class RootIn(BaseModel):
    path: str
    label: Optional[str] = None


class SettingsIn(BaseModel):
    values: dict


class JobIn(BaseModel):
    root_ids: list = None
    title_ids: list = None
    force: bool = False


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
    stars: Optional[list] = None
    genres: Optional[list] = None
    clear: Optional[list] = None  # field names to NULL out (e.g. wrong rating)


class WantedIn(BaseModel):
    """External submission of a film/show we want but don't have yet."""
    title: str
    kind: str = "movie"
    year: Optional[int] = None
    note: Optional[str] = None
    by: Optional[str] = None   # submitting program, shown in the UI
    watched: bool = False


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


class EpisodeWatchedIn(BaseModel):
    watched: bool


class SeasonWatchedIn(BaseModel):
    watched: bool
    season: Optional[int] = None  # None = every season


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


class MoveIn(BaseModel):
    target: str  # "internal" | "external"
    purge_others: bool = False  # consolidation: also delete copies on the other side
    seasons: Optional[List[int]] = None  # series: move only these seasons (None = all)


class RecDecideIn(BaseModel):
    """Verdict on a recommendation: accept (-> wanted) or reject.
    note: free-text why they liked/disliked it (feeds future research).
    liked/seen: optional flags recorded with the decision."""
    decision: str            # "accepted" | "rejected"
    note: Optional[str] = None
    liked: Optional[bool] = None
    seen: bool = False


# ---- helpers --------------------------------------------------------------
def _title_payload(row) -> dict:
    d = dict(row)
    for k in ("genres", "stars", "manual_edits"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except (json.JSONDecodeError, TypeError):
            d[k] = []
    d["watched"] = bool(d["watched_manual"]) if d["watched_manual"] is not None \
        else bool(d["watched_folder"])
    d["wanted"] = bool(d.get("wanted"))
    d["history"] = bool(d.get("history"))
    d["favorite"] = bool(d.get("favorite"))
    d["watch_next"] = d.get("watch_next")  # 'movie' | 'series' | None
    d["is_miniseries"] = bool(d.get("is_miniseries"))
    d["hidden"] = bool(d.get("hidden"))
    return d


def _get_title_or_404(tid: int):
    row = db.q1("SELECT * FROM titles WHERE id=?", (tid,))
    if not row:
        raise HTTPException(404, "title not found")
    return row


def _secrets_masked(all_settings: dict) -> dict:
    out = {}
    for k, v in all_settings.items():
        if k in config.SECRET_KEYS and v:
            out[k] = ("*" * 6) + v[-4:] if len(v) > 4 else "******"
            out[k + "_set"] = True
        else:
            out[k] = v
            out[k + "_set"] = bool(v)
    return out


# ---- static frontend ------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))


@app.get("/favicon.ico")
def favicon():
    # no-store: browsers otherwise pin the tab icon per origin for weeks,
    # surviving tab closes and server restarts
    path = os.path.join(config.STATIC_DIR, "favicon.svg")
    return FileResponse(path, media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"}) \
        if os.path.exists(path) else JSONResponse({}, status_code=204)


def _filter_clause(q=None, kind=None, watched=None, match=None, genre=None,
                   root=None, missing_on=None, cert=None, person=None,
                   wanted=None, seen=None, hidden=None):
    """Shared WHERE builder so /api/titles and /api/stats agree exactly."""
    where, params = [], []
    # hidden titles stay out of the default view; 'hidden=only' shows just
    # them (how un-hiding works), 'hidden=all' mixes them back in. Nothing
    # is ever deleted by hiding.
    if hidden == "only":
        where.append("t.hidden=1")
    elif hidden != "all":
        where.append("t.hidden=0")
    if q:
        # free-text spans identity AND people: "nolan" finds his films
        # (overview/description text is deliberately NOT searched)
        where.append("(t.title LIKE ? OR t.original_title LIKE ? "
                     "OR t.director LIKE ? OR t.creator LIKE ? OR t.stars LIKE ? "
                     "OR t.network LIKE ?)")
        params += [f"%{q}%"] * 6
    if person:
        where.append("(t.director LIKE ? OR t.creator LIKE ? OR t.stars LIKE ?)")
        params += [f"%{person}%"] * 3
    if wanted == "only":
        where.append("t.wanted=1")
    elif wanted == "hide":
        where.append("t.wanted=0")
    if kind in ("movie", "series"):
        where.append("t.kind=?")
        params.append(kind)
    if watched == "watched":
        where.append("(CASE WHEN t.watched_manual IS NOT NULL THEN t.watched_manual ELSE t.watched_folder END)=1")
    elif watched == "unwatched":
        where.append("(CASE WHEN t.watched_manual IS NOT NULL THEN t.watched_manual ELSE t.watched_folder END)=0")
    if match == "!not_found":
        where.append("(t.match_status IS NULL OR t.match_status != 'not_found')")
    elif match:
        where.append("t.match_status=?")
        params.append(match)
    if genre == "Miniseries":
        # pseudo-genre backed by the is_miniseries flag (still kind='series')
        where.append("t.is_miniseries=1")
    elif genre:
        where.append("t.genres LIKE ?")
        params.append(f'%"{genre}"%')
    if root:
        where.append("EXISTS (SELECT 1 FROM files f WHERE f.title_id=t.id AND f.path LIKE ?)")
        params.append(f"{root}%")
    if missing_on == "1":
        where.append("EXISTS (SELECT 1 FROM files f WHERE f.title_id=t.id AND f.missing=1)")
    if cert:
        where.append("t.cert=?")
        params.append(cert)
    if seen == "history":
        where.append("t.history=1")
    return where, params


# ---- library / media ------------------------------------------------------
@app.get("/api/titles")
def list_titles(
    q: str = None, kind: str = None, watched: str = None,
    match: str = None, genre: str = None,
    root: str = None, missing_on: str = None, cert: str = None,
    person: str = None, wanted: str = None, seen: str = None,
    hidden: str = None,
    sort: str = "title", direction: str = "asc",
    limit: int = 10000, offset: int = 0,
):
    where, params = _filter_clause(q, kind, watched, match, genre,
                                   root, missing_on, cert, person, wanted,
                                   seen, hidden)

    ORDER = {
        "title": "t.title COLLATE NOCASE", "year": "t.year",
        "rating": "COALESCE(t.rating_imdb, t.rating_tmdb)",
        "rt": "t.rating_rt",
        "runtime": "t.runtime",
        "cataloged": "t.cataloged_at",
        "created": "COALESCE(t.created_at, t.cataloged_at)",
        "size": "t.size_bytes",
        "watched": "(CASE WHEN t.watched_manual IS NOT NULL THEN t.watched_manual ELSE t.watched_folder END)",
        "match": "t.match_status",
    }
    order = ORDER.get(sort, ORDER["title"])
    direction = "DESC" if direction.lower() == "desc" else "ASC"

    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    rows = db.q(
        f"""SELECT t.* FROM titles t{wsql}
            ORDER BY (t.watch_next IS NOT NULL) DESC,
                     {order} {direction}, t.title COLLATE NOCASE
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
    then offers BOTH move buttons (either click consolidates)."""
    ext = fileops.backup_root()
    if ext:
        ext = ext.rstrip(os.sep) + os.sep
    sides = set()
    for f in files:
        if f["missing"]:
            continue
        on_ext = bool(ext) and (os.path.abspath(f["path"]) + os.sep).startswith(ext)
        sides.add("external" if on_ext else "internal")
    return sides


def _storage_side(files: list) -> str:
    """'external' when any accessible file already sits on the backup drive,
    else 'internal' — tells the drawer which move button makes sense."""
    return "external" if "external" in _storage_sides(files) else "internal"


@app.get("/api/titles/{tid}")
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
    "rating_tmdb": float, "rating_rt": int,
}
LIST_FIELDS = {"stars", "genres"}


@app.patch("/api/titles/{tid}")
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
        # also drop per-field edit locks — fresh identity, fresh values
        sets.append(
            "match_status='unmatched', match_error=NULL, enriched_at=NULL, "
            "data_source=NULL, tmdb_id=NULL, imdb_id=NULL, overview=NULL, "
            "rating_imdb=NULL, rating_tmdb=NULL, poster=NULL, backdrop=NULL, "
            "stars='[]', genres='[]', manual_edits='[]'"
        )
        sets.append("title_locked=1, kind_locked=1")
        vals.append(tid)
        with db.tx() as c:
            c.execute(f"UPDATE titles SET {', '.join(sets)} WHERE id=?", vals)
    return get_title(tid)


@app.post("/api/titles/{tid}/watched")
def set_watched(tid: int, body: WatchedIn):
    _get_title_or_404(tid)
    from .jobs import now_iso
    # a fileless row marked watched becomes a remembered record (history=1)
    # so a later scan can't prune it — this is the "delete file, keep memory"
    # promise; rows WITH files don't need it (files keep them alive)
    has_files = db.q1("SELECT 1 FROM files WHERE title_id=? AND missing=0 LIMIT 1", (tid,))
    with db.tx() as c:
        c.execute(
            "UPDATE titles SET watched_manual=?, watched_at=?, "
            "history=CASE WHEN ? AND ? THEN 1 ELSE history END WHERE id=?",
            (1 if body.watched else 0, now_iso() if body.watched else None,
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


@app.post("/api/titles/{tid}/files/{fid}/watched")
def set_file_watched(tid: int, fid: int, body: EpisodeWatchedIn):
    """Tick one episode in the drawer checklist. When this tick completes
    the set, the whole series is auto-marked watched (promoted=true)."""
    _get_title_or_404(tid)
    try:
        return episodes.set_episode_watched(tid, fid, body.watched)
    except episodes.EpisodeNotFound:
        raise HTTPException(404, "episode file not found")


@app.post("/api/titles/{tid}/episodes-watched")
def set_episodes_watched(tid: int, body: SeasonWatchedIn):
    """Bulk tick/untick one season (season=null: all seasons) of the
    drawer checklist; auto-promotes the series on the completing tick."""
    _get_title_or_404(tid)
    return episodes.set_season_watched(tid, body.season, body.watched)


@app.post("/api/titles/{tid}/favorite")
def set_favorite(tid: int, body: FlagIn):
    _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("UPDATE titles SET favorite=? WHERE id=?", (1 if body.value else 0, tid))
    return {"ok": True, "favorite": body.value}


@app.post("/api/titles/{tid}/wanted")
def set_wanted(tid: int, body: FlagIn):
    """Toggle the wanted flag on an EXISTING row (UI convenience)."""
    _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("UPDATE titles SET wanted=? WHERE id=?", (1 if body.value else 0, tid))
    return {"ok": True, "wanted": body.value}


@app.post("/api/titles/{tid}/hidden")
def set_hidden(tid: int, body: FlagIn):
    """Tuck a title away: excluded from the default list (the 'Hidden'
    filter shows only these). Never deletes anything — files, watched
    state and details all stay; un-hide any time."""
    _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("UPDATE titles SET hidden=? WHERE id=?", (1 if body.value else 0, tid))
    return {"ok": True, "hidden": body.value}


@app.get("/api/history")
def list_history():
    """All seen-history entries (watched titles we no longer own)."""
    rows = db.q("SELECT * FROM titles WHERE history=1 ORDER BY title COLLATE NOCASE")
    return {"history": [_title_payload(r) for r in rows]}


@app.post("/api/history/search")
def search_history_candidates(body: HistorySearchIn):
    """Pre-add lookup: (1) rows already in the catalog (any flag), then
    (2) up to 6 TMDB candidates with poster/year/overview so the user can
    confirm what they watched before anything is written."""
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "title required")

    # 1) local catalog matches (exact dedupe key + fuzzy title contains)
    from .scanner import dedupe_key
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


@app.post("/api/history")
def add_history(body: HistoryIn):
    """Record a movie/series as seen WITHOUT owning a file — pure watch log.
    Idempotent per title+kind+year: re-adding refreshes the note/date. If a
    matching catalog row already exists it is simply flagged history=1; the
    row then keeps working normally if its files ever come back (history is
    cleared by the scanner when files appear)."""
    if body.kind not in ("movie", "series"):
        raise HTTPException(400, "kind must be 'movie' or 'series'")
    from .scanner import dedupe_key
    from .jobs import now_iso
    key = dedupe_key(body.kind, body.title, body.year)
    watched_at = body.watched_at or now_iso()
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
                 now_iso(), body.note, body.tmdb_id, body.imdb_id))
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


@app.delete("/api/history/{tid}")
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


# ---- external program API (submit wanted / report watched) -----------------
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
        from .scanner import dedupe_key
        return db.q1("SELECT * FROM titles WHERE dedupe_key=?",
                     (dedupe_key(kind, body_t, year),))
    return None


@app.get("/api/wanted")
def list_wanted():
    rows = db.q("SELECT * FROM titles WHERE wanted=1 ORDER BY title COLLATE NOCASE")
    return {"wanted": [_title_payload(r) for r in rows]}


@app.post("/api/wanted")
def add_wanted(body: WantedIn):
    """Submit a film/show we want. Idempotent: re-submits update the note.
    The row is stored WITH wanted=1 and immediately enriched (provider keys
    permitting) so the list shows ratings, poster and runtime right away.
    When the files later appear on disk, the scanner adopts this row
    (same dedupe_key) and the wanted flag flips off automatically."""
    if body.kind not in ("movie", "series"):
        raise HTTPException(400, "kind must be 'movie' or 'series'")
    from .scanner import dedupe_key
    key = dedupe_key(body.kind, body.title, body.year)
    from .jobs import now_iso
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
                 now_iso(), 1 if body.watched else 0,
                 now_iso() if body.watched else None))
        tid = db.q1("SELECT id FROM titles WHERE dedupe_key=?", (key,))["id"]
        created = True
        # best-effort background enrich so the wanted row shows real data
        try:
            jid = jobs.create("enrich", total=1)
            jobs.run_background(jid, lambda j: enrich.run_enrich(j, title_ids=[tid]))
        except Exception:
            pass  # enrichment is optional; the wanted row is already stored
    return {"ok": True, "id": tid, "created": created}


@app.delete("/api/wanted/{tid}")
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


@app.post("/api/watched")
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
    from .jobs import now_iso
    with db.tx() as c:
        c.execute(
            "UPDATE titles SET watched_manual=?, watched_at=? WHERE id=?",
            (1 if body.watched else 0, now_iso() if body.watched else None, row["id"]),
        )
    # programmatic watched reports (no user present) queue the backup
    # decision for the NEXT time the web app is opened
    if body.watched:
        try:
            notifications.notify_watched_backup(row["id"])
        except Exception:
            pass  # notifications are never allowed to break the report
        # external watch reports also trigger the next-season lookup
        try:
            seasons.on_watched(row["id"])
        except Exception:
            pass  # the season calendar is never allowed to break watching
    return {"ok": True, "id": row["id"], "watched": body.watched}


# ---- notifications (watched -> backup decisions) ---------------------------
@app.get("/api/notifications")
def list_notifications(status: str = "pending", limit: int = 50):
    if status not in ("pending", "queued", "done", "rejected", "failed", "all"):
        raise HTTPException(
            400, "status must be pending|queued|done|rejected|failed|all")
    return {"notifications": notifications.list_notifications(status, limit),
            "pending": notifications.pending_count()}


@app.get("/api/notifications/count")
def notification_count():
    return {"pending": notifications.pending_count()}


class NotificationDecisionIn(BaseModel):
    decision: str  # 'accept' | 'reject'


@app.post("/api/notifications/{nid}/decide")
def decide_notification(nid: int, body: NotificationDecisionIn):
    """Accept = start a background job that moves the title's files to the
    backup drive (response carries the job_id); reject = leave them where
    they are. When the backup drive is offline the move goes into the
    persistent drive queue and runs the moment it is connected."""
    try:
        return notifications.decide(nid, body.decision)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---- season calendar (upcoming seasons of watched series) ------------------
@app.get("/api/seasons/calendar")
def seasons_calendar():
    """Everything the calendar popup shows: announced dates (with release
    pattern all-at-once vs weekly + the full date range), vague windows
    waiting for an exact date, and finished series. Entries changed since
    the last calendar open carry is_new=true."""
    return {"entries": seasons.calendar_entries()}


@app.post("/api/seasons/calendar/opened")
def seasons_calendar_opened():
    """Baseline for 'what is new': everything the calendar shows right now
    counts as looked-at; only later changes light the badge again."""
    return seasons.mark_calendar_opened()


@app.get("/api/seasons/recent")
def seasons_recent():
    """Catch-up list: seasons that started airing in the last weeks for
    series the user watches (or partly watched), latest season per show,
    until the season is marked seen."""
    return {"entries": seasons.recent_entries()}


class SeasonSeenIn(BaseModel):
    seen: bool = True


@app.post("/api/seasons/{entry_id}/seen")
def seasons_seen(entry_id: int, body: SeasonSeenIn):
    """'I watched this season' — removes it from the recently-released
    list (seen=false puts it back)."""
    return seasons.mark_season_seen(entry_id, body.seen)


@app.post("/api/seasons/poll")
def seasons_poll():
    """Manual 'check now' — the full sweep (every watched series), run now."""
    jid = jobs.create("season_poll", total=0)
    jobs.log(jid, "manual season sweep requested")
    jobs.run_background(jid, seasons.sweep)
    return {"ok": True, "job_id": jid}


@app.delete("/api/titles/{tid}")
def delete_title(tid: int):
    """Remove a title from the catalog WITHOUT touching files on disk."""
    _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("DELETE FROM titles WHERE id=?", (tid,))
    lut_sync.request_sync("delete_title")   # voice fast-path drops dead entries
    # the pin's slot copy still exists on disk but no row pins it anymore —
    # setting a new Watch Next will wipe the slot as usual
    return {"ok": True}


@app.delete("/api/titles/{tid}/files")
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
    # id needed for the keep-record path (per-row DELETE in _delete_title_files)
    files = [dict(f) for f in db.q(
        "SELECT id, path, missing FROM files WHERE title_id=?", (tid,))]
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
        from .jobs import now_iso
        with db.tx() as c:
            c.execute(
                "UPDATE titles SET history=1, size_bytes=0, episode_count=0, "
                "seasons=NULL, last_seen=? WHERE id=?",
                (now_iso(), tid))
    lut_sync.request_sync("delete_files")   # voice fast-path drops dead entries
    return {"ok": True, "title": row["title"], "kept_record": keep_record, **result}


@app.get("/api/titles/{tid}/files/{fid}/open")
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


# ---- roots ----------------------------------------------------------------
@app.get("/api/roots")
def list_roots():
    rows = db.q("SELECT * FROM roots ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        d["exists"] = os.path.isdir(r["path"])
        d["stats"] = dict(db.q1(
            """SELECT COUNT(DISTINCT t.id) titles,
                      SUM(t.size_bytes) bytes
               FROM titles t JOIN files f ON f.title_id=t.id
               WHERE f.path LIKE ? || '%'""", (r["path"].rstrip("/") + "/",)) or {})
        out.append(d)
    return {"roots": out}


@app.post("/api/roots")
def add_root(body: RootIn):
    p = os.path.abspath(os.path.expanduser(body.path))
    if not os.path.isdir(p):
        raise HTTPException(400, f"not a directory: {p}")
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO roots(path, label) VALUES(?,?)", (p, body.label))
    return {"ok": True, "path": p}


@app.delete("/api/roots/{rid}")
def delete_root(rid: int):
    with db.tx() as c:
        c.execute("DELETE FROM roots WHERE id=?", (rid,))
    return {"ok": True}


@app.post("/api/roots/{rid}/toggle")
def toggle_root(rid: int):
    with db.tx() as c:
        c.execute("UPDATE roots SET enabled = 1 - enabled WHERE id=?", (rid,))
    return {"ok": True}


# ---- settings -------------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    return _secrets_masked(db.settings_all())


@app.post("/api/settings")
def save_settings(body: SettingsIn):
    for k, v in body.values.items():
        if k in config.SECRET_KEYS:
            if v is None or ("*" in str(v)):
                continue  # masked placeholder sent back: keep existing
        db.settings_set(k, str(v) if v is not None else "")
    return get_settings()


@app.post("/api/test/llm")
def test_llm():
    """Saves nothing; probes the STORED config. UI saves fields first."""
    ok, detail = llm.probe()
    return {"ok": ok, "detail": detail}


@app.post("/api/test/tmdb")
def test_tmdb():
    ok, detail = tmdb.probe()
    return {"ok": ok, "detail": detail}


# ---- jobs (scan / enrich) -------------------------------------------------
@app.post("/api/scan")
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


@app.post("/api/enrich")
def start_enrich(body: JobIn = None):
    # Always allowed: rows are marked 'no_provider' when no TMDB key is set.
    body = body or JobIn()
    jid = jobs.create("enrich", total=0)
    jobs.run_background(
        jid, lambda j: enrich.run_enrich(j, title_ids=body.title_ids,
                                         force=body.force, only_unmatched=not body.force))
    return {"job_id": jid}


@app.post("/api/backfill-ratings")
def backfill_ratings():
    """Fill cert + Rotten Tomatoes for already-matched titles (light pass)."""
    jid = jobs.create("backfill", total=0)
    jobs.run_background(jid, lambda j: enrich.run_backfill(j))
    return {"job_id": jid}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    row = db.q1("SELECT * FROM jobs WHERE id=?", (jid,))
    if not row:
        raise HTTPException(404, "job not found")
    d = dict(row)
    d["log"] = (d["log"] or "").strip().split("\n")[-30:]
    return d


@app.post("/api/jobs/{jid}/cancel")
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


# ---- move between internal / external storage -----------------------------
@app.post("/api/titles/{tid}/move")
def move_title(tid: int, body: MoveIn):
    if body.target not in ("internal", "external"):
        raise HTTPException(400, "target must be 'internal' or 'external'")
    row = _get_title_or_404(tid)  # 404 early; mover raises ValueError for config issues
    seasons = sorted(set(body.seasons)) if body.seasons else None
    sel = f" (seasons {', '.join(map(str, seasons))} only)" if seasons else ""
    # target drive not connected right now? queue it — runs automatically
    # the moment the drive is plugged in (Settings shows what is waiting)
    dest = db.settings_get(f"{body.target}_root")
    if dest and not os.path.isdir(os.path.abspath(dest)):
        dest = os.path.abspath(dest)
        qid, created = drivequeue.enqueue(
            "move", tid, dest,
            {"target": body.target, "purge_others": body.purge_others,
             "seasons": seasons},
            f"Move '{row['title']}' to {dest}{sel}")
        return {"job_id": None, "queued": True, "queue_id": qid,
                "queued_now": created, "drive": dest}
    # (reachable-target moves sync via drivequeue._execute / mover hook;
    # the queued branch syncs when the drive finally connects)
    sel = f" (seasons {', '.join(map(str, seasons))} only)" if seasons else ""
    jid = jobs.create("move", total=0)
    jobs.log(jid, f"Move requested: title {tid} -> {body.target}{sel}")

    def _run(job):
        error = None
        try:
            mover.move_title(job, tid, body.target,
                             purge_others=body.purge_others, seasons=seasons)
            lut_sync.request_sync("move")   # voice fast-path follows the file
        except Exception as e:
            jobs.log(job, f"FAILED: {e}")
            error = str(e)
        jobs.finish(job, error=error)

    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return {"job_id": jid}


# ---- watch next (pin to top + copy into the aaNext_* slot) -----------------
@app.post("/api/titles/{tid}/watch-next")
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


@app.delete("/api/titles/{tid}/watch-next")
def clear_watch_next(tid: int):
    _get_title_or_404(tid)
    watchnext.clear_next(tid)
    return {"ok": True}


# ---- duplicates ------------------------------------------------------------
@app.get("/api/duplicates")
def list_duplicates():
    return {"groups": duplicates.find_duplicates()}


class DeleteCopyIn(BaseModel):
    root: str = None


@app.delete("/api/duplicates/{tid}")
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


@app.get("/api/drive-queue")
def list_drive_queue():
    """Pending drive-queue entries grouped per drive (Settings panel)."""
    return {"groups": drivequeue.list_pending(),
            "pending": drivequeue.pending_count()}


@app.delete("/api/drive-queue/{qid}")
def cancel_drive_queue(qid: int):
    """Remove a pending entry before its drive gets connected."""
    try:
        drivequeue.cancel(qid)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/drive-queue/run")
def run_drive_queue():
    """Manual drain: runs every queued entry whose drive is connected."""
    return {"ok": True, "started": drivequeue.run_due()}


# ---- film recommender (LLM researcher + queue) -----------------------------
@app.get("/api/recommendations/status")
def rec_status():
    """Queue health for the header badge; also opportunistically triggers
    the background refill when the queue ran low."""
    pending = recommender.pending_count()
    refill_job = recommender.maybe_refill(f"served status (pending={pending})")
    running = db.q1(
        """SELECT id, status, message FROM jobs WHERE type='recommend'
           AND status='running' ORDER BY created_at DESC LIMIT 1""")
    return {
        "pending": pending,
        "enabled": recommender.enabled(),
        "low": pending <= recommender.QUEUE_LOW,
        "refill_job_id": refill_job,
        "running_job": dict(running) if running else None,
    }


@app.get("/api/recommendations/next")
def rec_next():
    """Serve the oldest pending recommendation (FIFO) — the popup card.
    Final catalog guard: any pending row that collides with a title the
    user already has (name / tmdb / imdb match) is purged on the way out,
    so a served card is always something NEW to them."""
    # lazy purge of pending rows that became 'known' after they were queued
    known = recommender._known_title_keys()
    for r in db.q("SELECT id, title, tmdb_id, imdb_id FROM recommendations "
                  "WHERE status='pending'"):
        if recommender._is_known(r["title"], tmdb_id=r["tmdb_id"],
                                 imdb_id=r["imdb_id"], known=known):
            with db.tx() as c:
                c.execute("DELETE FROM recommendations WHERE id=?", (r["id"],))
    row = db.q1(
        """SELECT * FROM recommendations WHERE status='pending'
           ORDER BY id LIMIT 1""")
    if not row:
        recommender.maybe_refill("queue empty")
        return {"recommendation": None, "pending": recommender.pending_count()}
    d = recommender.rec_payload(row)
    behind = db.q1(
        "SELECT COUNT(*) n FROM recommendations WHERE status='pending' AND id>?",
        (row["id"],))["n"]
    return {"recommendation": d, "pending": recommender.pending_count(),
            "remaining_behind": behind}


@app.post("/api/recommendations/refill")
def rec_refill():
    """Manual 'research now' trigger (also used automatically)."""
    if not recommender.enabled():
        raise HTTPException(400, "no LLM API key configured (Settings)")
    pending = recommender.pending_count()
    jid = recommender.maybe_refill(f"manual (pending={pending})")
    if jid:
        return {"ok": True, "job_id": jid, "pending": pending}
    if pending > recommender.QUEUE_LOW:
        return {"ok": True, "job_id": None,
                "detail": f"queue healthy ({pending} pending — no refill needed)"}
    raise HTTPException(409, "a research job is already running")


class RecPeekIn(BaseModel):
    count: int = 10


@app.get("/api/recommendations")
def rec_list(status: str = "pending", limit: int = 50):
    """Queue / decision history listing."""
    if status not in ("pending", "accepted", "rejected", "all"):
        raise HTTPException(400, "status must be pending|accepted|rejected|all")
    wsql = "" if status == "all" else " WHERE status=?"
    rows = db.q(f"SELECT * FROM recommendations{wsql} "
                "ORDER BY id DESC LIMIT ?", ((status,) if status != "all" else ()) + (limit,))
    return {"recommendations": [recommender.rec_payload(r) for r in rows]}


@app.post("/api/recommendations/{rid}/decide")
def rec_decide(rid: int, body: RecDecideIn):
    """Accept (-> wanted list) or reject. note/liked/seen are stored as
    feedback for future research batches. Reject + seen creates a seen-log
    row ('watched it, own no file'); accept + seen also marks it watched."""
    try:
        rec, title_id, created = recommender.decide(
            rid, body.decision, note=body.note, liked=body.liked, seen=body.seen)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # decisions shrink the queue — kick a refill when we crossed the line
    recommender.maybe_refill("after decision")
    return {"ok": True, "recommendation": recommender.rec_payload(rec),
            "title_id": title_id, "created": created}


@app.post("/api/test/recommender")
def test_recommender():
    """Dry probe: checks LLM key + that the MCP servers are attached. Does
    NOT run a research batch."""
    if not recommender.enabled():
        return {"ok": False, "detail": "no LLM API key configured"}
    ok, detail = llm.probe()
    return {"ok": ok, "detail": detail + " (researcher uses the same key for web-search-prime + web-reader MCP)"}


# ---- live drive connect / disconnect events --------------------------------
def _drives_signature() -> str:
    """Fingerprint of what is mounted + which library roots are reachable.
    /proc/mounts changes whenever a drive is plugged or unplugged; the
    per-root liveness bit catches mount points that silently disappear."""
    try:
        with open("/proc/mounts") as f:
            mounts = f.read()
    except OSError:
        mounts = ""
    roots = ";".join(
        f"{r['path']}={1 if os.path.isdir(r['path']) else 0}"
        for r in db.q("SELECT path FROM roots ORDER BY id"))
    return hashlib.sha1((mounts + "|" + roots).encode()).hexdigest()


def _safe_queue_drain():
    try:
        drivequeue.run_due()
        # a drive just plugged in / out: reachability changed -> regenerate
        # the voice fast-path (parked series return, new movies go live)
        lut_sync.request_sync("drive_change")
    except Exception:
        pass  # the poller thread will retry


@app.get("/api/events/drives")
async def drive_events():
    """Server-sent events fired whenever a drive connects or disconnects.
    The frontend reloads the table so rows show connected/disconnected
    locations immediately — no manual page refresh needed."""
    async def gen():
        last = _drives_signature()
        yield f"data: {json.dumps({'signature': last})}\n\n"
        while True:
            await asyncio.sleep(2)
            cur = _drives_signature()
            if cur != last:
                last = cur
                # a drive may have just connected: kick the queue in the
                # background (a move can take minutes — never block SSE)
                threading.Thread(target=_safe_queue_drain, daemon=True).start()
                yield f"data: {json.dumps({'signature': cur})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


def _unquote_mnt(p: str) -> str:
    """Decode /proc/mounts octal escapes: \\040 space, \\011 tab,
    \\012 newline, \\134 backslash."""
    for esc, ch in (("\\134", "\\"), ("\\040", " "), ("\\011", "\t"), ("\\012", "\n")):
        p = p.replace(esc, ch)
    return p


def _mounted_drives() -> list:
    """Real disk mounts (external USB drives etc.) for the folder picker.
    Reads /proc/mounts; excludes loop/zram/system mounts. Needed because the
    udisks parent dirs (/run/media/<user>) are root-owned and not listable
    by the app user - we shortcut straight to each mounted drive instead."""
    good_fs = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat",
               "ntfs", "ntfs3", "ntfs-3g", "fuseblk", "f2fs"}
    drives = []
    seen = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mnt, fstype = parts[0], parts[1], parts[2]
                if not dev.startswith("/dev/"):
                    continue
                if dev.startswith("/dev/loop") or dev.startswith("/dev/zram"):
                    continue
                if fstype not in good_fs:
                    continue
                if mnt == "/" or mnt.startswith("/boot"):
                    continue
                if mnt in seen:
                    continue
                seen.add(mnt)
                mnt = _unquote_mnt(mnt)
                drives.append({"label": f"💾 {os.path.basename(mnt)}", "path": mnt})
    except OSError:
        pass
    return drives


@app.get("/api/browse")
def browse(path: str = None):
    """List directories at `path` for the folder-picker dialog.
    Local single-user app: full filesystem visibility is intentional."""
    home = os.path.expanduser("~")
    target = os.path.abspath(os.path.expanduser(path)) if path else home
    if not os.path.isdir(target):
        raise HTTPException(400, f"not a directory: {target}")

    dirs = []
    denied = False
    try:
        entries = os.listdir(target)
    except PermissionError:
        denied = True
        entries = []

    for name in sorted(entries, key=str.lower):
        if name.startswith("."):
            continue
        full = os.path.join(target, name)
        if os.path.isdir(full):
            try:
                os.listdir(full)  # readable?
                dirs.append({"name": name, "path": full})
            except PermissionError:
                dirs.append({"name": name + " (locked)", "path": full, "locked": True})

    shortcuts = [{"label": "🏠 Home", "path": home}]
    shortcuts += _mounted_drives()
    shortcuts.append({"label": "🖥  / (root)", "path": "/"})
    parent = os.path.dirname(target) if target != "/" else None
    return {"path": target, "parent": parent, "dirs": dirs,
            "shortcuts": shortcuts, "denied": denied}


# ---- disk usage -----------------------------------------------------------
@app.get("/api/stats")
def stats(
    q: str = None, kind: str = None, watched: str = None,
    match: str = None, genre: str = None,
    root: str = None, missing_on: str = None, cert: str = None,
    person: str = None, wanted: str = None, seen: str = None,
    hidden: str = None,
):
    """Counts reflect the CURRENT FILTER (same params as /api/titles),
    with a watched/unwatched breakdown of the filtered set."""
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


# ---- static frontend assets — MUST stay last so /api/* routes win ---------
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")

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

from . import config, db, duplicates, enrich, jobs, llm, mover, scanner, tmdb, watchnext

db.init()

app = FastAPI(title="Film Organizer", docs_url="/api/docs", openapi_url="/api/openapi.json")


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


from typing import Optional

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
    d["favorite"] = bool(d.get("favorite"))
    d["watch_next"] = d.get("watch_next")  # 'movie' | 'series' | None
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
                   wanted=None):
    """Shared WHERE builder so /api/titles and /api/stats agree exactly."""
    where, params = [], []
    if q:
        # free-text spans identity AND people: "nolan" finds his films
        where.append("(t.title LIKE ? OR t.original_title LIKE ? OR t.overview LIKE ? "
                     "OR t.director LIKE ? OR t.creator LIKE ? OR t.stars LIKE ? "
                     "OR t.network LIKE ?)")
        params += [f"%{q}%"] * 7
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
    if genre:
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
    return where, params


# ---- library / media ------------------------------------------------------
@app.get("/api/titles")
def list_titles(
    q: str = None, kind: str = None, watched: str = None,
    match: str = None, genre: str = None,
    root: str = None, missing_on: str = None, cert: str = None,
    person: str = None, wanted: str = None,
    sort: str = "title", direction: str = "asc",
    limit: int = 10000, offset: int = 0,
):
    where, params = _filter_clause(q, kind, watched, match, genre,
                                   root, missing_on, cert, person, wanted)

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

    # genre facet + per-title roots summary
    genres = sorted({
        g for r in db.q("SELECT genres FROM titles WHERE genres IS NOT NULL")
        for g in (json.loads(r["genres"] or "[]"))
    })
    out = [_title_payload(r) for r in rows]
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


@app.get("/api/titles/{tid}")
def get_title(tid: int):
    row = _get_title_or_404(tid)
    d = _title_payload(row)
    d["files"] = [dict(f) for f in db.q(
        "SELECT id, path, size_bytes, season, episode, watched_folder, missing FROM files WHERE title_id=? ORDER BY path",
        (tid,))]
    d["locations"] = [r["p"] for r in db.q(
        """SELECT DISTINCT r.path p FROM files f
           JOIN roots r ON f.path LIKE r.path || '%'
           WHERE f.title_id=? AND f.missing=0 ORDER BY r.path""", (tid,))]
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
        if not (body.title or body.kind or body.year is not None):
            return get_title(tid)  # detail-only edit: done

    sets, vals = [], []
    identity_change = body.title or body.kind or body.year is not None
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
    with db.tx() as c:
        c.execute(
            "UPDATE titles SET watched_manual=?, watched_at=? WHERE id=?",
            (1 if body.watched else 0, now_iso() if body.watched else None, tid),
        )
    return {"ok": True, "watched": body.watched}


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
    return {"ok": True, "id": row["id"], "watched": body.watched}


@app.delete("/api/titles/{tid}")
def delete_title(tid: int):
    """Remove a title from the catalog WITHOUT touching files on disk."""
    _get_title_or_404(tid)
    with db.tx() as c:
        c.execute("DELETE FROM titles WHERE id=?", (tid,))
    # the pin's slot copy still exists on disk but no row pins it anymore —
    # setting a new Watch Next will wipe the slot as usual
    return {"ok": True}


@app.delete("/api/titles/{tid}/files")
def delete_title_files(tid: int):
    """Delete a title AND its files from disk (main-list trash button).
    Refuses with 409 + drive list when any source drive is offline, so the
    catalog never claims 'deleted' while bytes remain on a sleeping disk."""
    row = _get_title_or_404(tid)
    files = [dict(f) for f in db.q(
        "SELECT path, missing FROM files WHERE title_id=?", (tid,))]
    if not files:
        raise HTTPException(404, "no files recorded for this title")

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
        raise HTTPException(
            409, "Connect these drives first, then retry: "
            + ", ".join(sorted(offline)))

    result = duplicates._delete_title_files(files, tid, delete_row=True)
    return {"ok": True, "title": row["title"], **result}


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
    jobs.run_background(jid, lambda j: scanner.scan_roots(j, roots))
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


# ---- move between internal / external storage -----------------------------
@app.post("/api/titles/{tid}/move")
def move_title(tid: int, body: MoveIn):
    if body.target not in ("internal", "external"):
        raise HTTPException(400, "target must be 'internal' or 'external'")
    _get_title_or_404(tid)  # 404 early; mover raises ValueError for config issues
    jid = jobs.create("move", total=0)
    jobs.log(jid, f"Move requested: title {tid} -> {body.target}")

    def _run(job):
        error = None
        try:
            mover.move_title(job, tid, body.target)
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
def delete_duplicate_copy(tid: int, root: str = None, body: DeleteCopyIn = None):
    # root as query param preferred (robust against stale/cached clients);
    # JSON body accepted too
    r = root if root is not None else (body.root if body else None)
    try:
        return duplicates.delete_copy(tid, root=r)
    except ValueError as e:
        raise HTTPException(400, str(e))


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
    person: str = None, wanted: str = None,
):
    """Counts reflect the CURRENT FILTER (same params as /api/titles),
    with a watched/unwatched breakdown of the filtered set."""
    where, params = _filter_clause(q, kind, watched, match, genre,
                                   root, missing_on, cert, person, wanted)
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

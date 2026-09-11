"""Season calendar: track upcoming seasons of watched series.

What it does
------------
* WATCH EVENT: when a series is marked watched, `on_watched(title_id)` makes
  one immediate TMDB lookup: is the next season announced?
* WEEKLY POLL: `poll(job_id)` (background thread, once per week) re-checks
  every tracked season whose info is still vague/TBA — the ones with exact
  dates are left alone (nothing more to learn from TMDB).
* FINISHED: when TMDB says the show is 'Ended' and the last episode aired,
  the row flips to done (finished=1) — the main list then shows the
  'finished' chip instead of an upcoming-season chip.

Data lives in the `season_watch` table: one row per (title, season).
`announce_start` is the first episode's date; `release_end` covers weekly
ranges (finale) or equals the start for all-at-once drops.
"""
import json
import threading
from datetime import datetime, timedelta, timezone

from . import db, tmdb
from .db import q, q1, tx
from . import season_seed

WEEK_SECONDS = 7 * 24 * 3600
# vague rows are re-checked weekly; rows with no info at all get the same
# cadence — TMDB status updates arrive whenever they arrive


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _in_days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat(timespec="seconds")


def _next_week() -> str:
    return _in_days(7)


def upsert(title_id: int, season: int, force: bool = False, **fields):
    """Insert or update one season_watch row. By default a firmer status
    never clobbers (done > announced > vague); force=True bypasses that for
    revivals (a 'finished' show whose next season just got announced)."""
    rank = {"done": 2, "announced": 1, "vague": 0}
    existing = q1("SELECT * FROM season_watch WHERE title_id=? AND season=?",
                  (title_id, season))
    cols = {"status", "release_kind", "release_start", "release_end",
            "window_hint", "finished", "source", "note", "next_check_at",
            "checked_at"}
    fields = {k: v for k, v in fields.items() if k in cols}
    with tx() as c:
        if existing:
            # only a firmer or equal status may overwrite (unless forced)
            if force or rank.get(fields.get("status", ""), -1) \
                    >= rank.get(existing["status"], -1):
                sets, vals = [], []
                for k, v in fields.items():
                    sets.append(f"{k}=?")
                    vals.append(v)
                if sets:
                    c.execute(f"UPDATE season_watch SET {', '.join(sets)} "
                              f"WHERE id=?", vals + [existing["id"]])
        else:
            keys = ["title_id", "season", "created_at"] + list(fields)
            vals = [title_id, season, _now()] + list(fields.values())
            c.execute(
                f"INSERT INTO season_watch({', '.join(keys)}) "
                f"VALUES({', '.join('?' * len(keys))})", vals)


def clear_title(title_id: int):
    """Forget tracked seasons when a title is deleted / un-watched."""
    with tx() as c:
        c.execute("DELETE FROM season_watch WHERE title_id=?", (title_id,))


# ---- TMDB -> row mapping ----------------------------------------------------

def _probe(title_id: int) -> dict:
    """Ask TMDB about a title's current airing state. Returns
    {found, next_season, release_start, release_end, release_kind, finished}"""
    row = q1("SELECT tmdb_id, seasons, status FROM titles WHERE id=?", (title_id,))
    if not row or not row["tmdb_id"]:
        return {"found": False}
    try:
        info = tmdb.tv_calendar(row["tmdb_id"])
    except Exception:
        return {"found": False}

    seasons = {s["season"]: s for s in info.get("seasons", []) if s["season"]}
    next_ep = info.get("next_episode") or {}
    last_ep = info.get("last_episode") or {}
    # baseline = highest CONCRETE season (announced/done rows or the title's
    # own season count); vague 'window' rows must not inflate it, or the
    # promotion of that very row would be blocked by `nxt > base`
    known = q1("SELECT MAX(season) m FROM season_watch "
               "WHERE title_id=? AND status!='vague'", (title_id,))
    base = known["m"] or row["seasons"] or 1

    out = {"found": True, "status": info.get("status"),
           "network": info.get("network")}

    nxt = next_ep.get("season_number")
    if nxt and nxt > base:
        # TMDB already schedules episodes of the next season
        out.update(next_season=nxt, release_start=(next_ep.get("air_date") or "")[:10])
        # season fully scheduled? last episode of that season gives the range
        s_info = seasons.get(nxt) or {}
        last = last_ep.get("season_number")
        if last == nxt and last_ep.get("air_date"):
            out["release_end"] = last_ep["air_date"][:10]
        elif s_info.get("episodes"):
            # weekly cadence assumption: (episodes-1) weeks after the start
            try:
                start = datetime.strptime(out["release_start"], "%Y-%m-%d")
                out["release_end"] = (start + timedelta(weeks=s_info["episodes"] - 1)) \
                    .strftime("%Y-%m-%d")
            except ValueError:
                pass
        out["release_kind"] = "weekly" if out.get("release_end") \
            and out["release_end"] != out["release_start"] else "unknown"
        return out

    # no next episode: either between seasons (vague) or finished
    status = (info.get("status") or "").lower()
    ended = status in ("ended", "canceled", "cancelled")
    if ended and (last_ep.get("air_date") or "") < _now()[:10]:
        out.update(next_season=base, finished=1)
    return out


def on_watched(title_id: int):
    """Watch-event hook: probe TMDB once for the next season right away."""
    row = q1("SELECT kind FROM titles WHERE id=?", (title_id,))
    if not row or row["kind"] != "series":
        return
    r = _probe(title_id)
    if not r.get("found"):
        return
    if r.get("next_season"):
        upsert(title_id, r["next_season"],
               status="announced" if r.get("release_start") else "vague",
               release_kind=r.get("release_kind"),
               release_start=r.get("release_start"),
               release_end=r.get("release_end"),
               window_hint=None if r.get("release_start") else None,
               source="tmdb",
               next_check_at=None if r.get("release_start") else _next_week(),
               checked_at=_now())
    elif r.get("finished"):
        _mark_finished(title_id)


def _mark_finished(title_id: int):
    rows = q("SELECT id, season, status FROM season_watch WHERE title_id=? "
             "AND finished=0", (title_id,))
    with tx() as c:
        for r in rows:
            # finished rows keep a SLOW monthly re-check: if the show is
            # ever revived (TMDB announces a new season) the poll revives
            # the row instead of staying finished forever
            c.execute("UPDATE season_watch SET status='done', finished=1, "
                      "next_check_at=?, checked_at=? WHERE id=?",
                      (_in_days(30), _now(), r["id"]))


# ---- weekly poll ------------------------------------------------------------

def poll(job_id: str = None):
    """Re-check every open season_watch row. Announced rows are skipped
    (they already have their dates); vague rows are refreshed. Also sweeps
    shows that now count as finished."""
    from .jobs import log

    def _log(msg):
        if job_id:
            log(job_id, msg)

    rows = q("""SELECT s.*, t.tmdb_id, t.title FROM season_watch s
                JOIN titles t ON t.id=s.title_id
                WHERE s.status='vague'
                   OR (s.next_check_at IS NOT NULL AND s.next_check_at<=?)""",
             (_now(),))
    # catch titles tracked nowhere yet whose TMDB next_episode already points
    # at a future season (between-seasons gap filler on weekly runs)
    _log(f"polling {len(rows)} tracked season(s)")
    checked = promoted = finished = 0
    for r in rows:
        res = _probe(r["title_id"])
        checked += 1
        if res.get("finished"):
            _mark_finished(r["title_id"])
            finished += 1
            _log(f"finished: {r['title']}")
            continue
        if res.get("next_season") and res.get("release_start"):
            # force=True also RESURRECTS done rows when TMDB announces a
            # season for a show we had marked finished
            upsert(r["title_id"], res["next_season"],
                   force=bool(r["status"] == "done"),
                   status="announced", release_kind=res.get("release_kind"),
                   release_start=res["release_start"],
                   release_end=res.get("release_end"),
                   source="tmdb", next_check_at=None, checked_at=_now(),
                   finished=0)
            promoted += 1
            _log(f"announced: {r['title']} S{res['next_season']} "
                 f"starts {res['release_start']}")
        else:
            upsert(r["title_id"], r["season"], checked_at=_now(),
                   next_check_at=_next_week())
    _log(f"poll done: {checked} checked, {promoted} promoted to announced, "
         f"{finished} finished")
    return {"checked": checked, "promoted": promoted, "finished": finished}


def poll_async():
    """Fire the weekly poll in a background thread (used at startup)."""
    threading.Thread(target=lambda: _safe_poll(), daemon=True).start()


def _safe_poll():
    try:
        poll()
    except Exception:
        pass  # the next weekly tick retries


def sweep(job_id: str = None):
    """Exhaustive pass over EVERY watched series in the catalog (not just
    rows already in season_watch): probes TMDB per title and records what
    it finds — an announced next season, or 'done' for shows whose finale
    has aired (that is how Ended shows get their finished chip). Shows
    returning without a scheduled date get NO placeholder row; they are
    simply re-probed on every weekly sweep. Rate-limited by tmdb._get
    (~4 req/s)."""
    from .jobs import log, update

    def _log(msg):
        if job_id:
            log(job_id, msg)

    rows = q("""SELECT t.id, t.title FROM titles t
                WHERE t.kind='series' AND (
                    (CASE WHEN t.watched_manual IS NOT NULL
                          THEN t.watched_manual ELSE t.watched_folder END)=1
                    OR t.history=1)""")
    _log(f"sweeping {len(rows)} watched series against TMDB")
    announced = done = covered = errors = 0
    for i, t in enumerate(rows, 1):
        if job_id:
            update(job_id, progress=i, total=len(rows),
                   message=f"Sweep {i}/{len(rows)} — {t['title']}")
        # rows without a TMDB id (messy parsed names like episode rips):
        # try one best-match search so they are never permanently invisible
        row_full = q1("SELECT tmdb_id, year FROM titles WHERE id=?", (t["id"],))
        if not row_full["tmdb_id"]:
            try:
                cands = tmdb.search_tv(t["title"], row_full["year"])
                best = tmdb.best_candidate(cands, threshold=0.75)
                if best:
                    with tx() as c:
                        c.execute("UPDATE titles SET tmdb_id=? WHERE id=?",
                                  (best["id"], t["id"]))
            except Exception:
                pass
            if not q1("SELECT tmdb_id FROM titles WHERE id=?", (t["id"],))["tmdb_id"]:
                errors += 1
                continue  # unmatchable junk (episode rips etc.) — skip
        # duplicate rows of the same show (episode rips, renames): covered
        # when ANOTHER row with this tmdb_id is already tracked — skip to
        # avoid flooding the calendar with the same season N times
        dup = q1("""SELECT 1 FROM season_watch s JOIN titles t2 ON t2.id=s.title_id
                    JOIN titles t3 ON t3.tmdb_id=t2.tmdb_id
                    WHERE t3.id=? AND t3.tmdb_id IS NOT NULL AND t2.id!=t3.id LIMIT 1""",
                 (t["id"],))
        if dup:
            covered += 1
            continue
        try:
            res = _probe(t["id"])
        except Exception:
            errors += 1
            continue
        if not res.get("found"):
            errors += 1
            continue
        if res.get("finished"):
            _mark_finished(t["id"])
            # untracked show: record its last season as done so the
            # finished chip has something to stand on
            known = q1("SELECT COUNT(*) n FROM season_watch WHERE title_id=?",
                       (t["id"],))
            if not known["n"]:
                title_row = q1("SELECT seasons FROM titles WHERE id=?", (t["id"],))
                upsert(t["id"], title_row["seasons"] or 1,
                       status="done", finished=1, source="tmdb",
                       next_check_at=_in_days(30), checked_at=_now())
            done += 1
        elif res.get("next_season") and res.get("release_start"):
            upsert(t["id"], res["next_season"], force=True,
                   status="announced", release_kind=res.get("release_kind"),
                   release_start=res["release_start"],
                   release_end=res.get("release_end"),
                   source="tmdb", next_check_at=None, checked_at=_now(),
                   finished=0)
            announced += 1
    _log(f"sweep done: {announced} announced, {done} finished, "
         f"{covered} duplicates, {errors} not probeable")
    return {"total": len(rows), "announced": announced, "done": done,
            "duplicates": covered, "errors": errors}


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        pass  # season tracking must never crash a startup thread


def sweep_async():
    """Fire the full catalog sweep in a background thread (startup)."""
    threading.Thread(target=lambda: _safe(sweep), daemon=True).start()


def maybe_poll_weekly():
    """Run the FULL SWEEP when the last run is >7 days old (startup hook).
    The sweep covers everything the poll does plus every watched series
    that has no row yet — so the calendar self-heals completely each week."""
    last = db.settings_get("season_poll_last")
    if last:
        try:
            if (datetime.now(timezone.utc)
                    - datetime.fromisoformat(last)).total_seconds() < WEEK_SECONDS:
                return
        except ValueError:
            pass
    db.settings_set("season_poll_last", _now())
    sweep_async()


# ---- seeding (web-researched facts) -----------------------------------------

def seed(job_id: str = None):
    """Load season_seed.SEEDS / SEEDS_BY_TITLE into season_watch.
    Idempotent: existing rows are never downgraded (see upsert)."""
    from .jobs import log

    def _log(msg):
        if job_id:
            log(job_id, msg)

    n = 0
    for tmdb_id, seasons in season_seed.SEEDS.items():
        row = q1("SELECT id FROM titles WHERE tmdb_id=?", (tmdb_id,))
        if not row:
            continue  # title gone / never existed — skip silently
        for s in seasons:
            upsert(row["id"], s["season"],
                   status=s.get("status", "vague"),
                   release_kind=s.get("release_kind"),
                   release_start=s.get("release_start"),
                   release_end=s.get("release_end"),
                   window_hint=s.get("window_hint"),
                   finished=s.get("finished", 0),
                   source=s.get("source", "seed"),
                   note=s.get("note"),
                   next_check_at=s.get("next_check_at"),
                   checked_at=_now())
            n += 1
    # by-title seeds (rows whose tmdb_id may be missing locally; also match
    # renamed rows like 'The Diplomat US' via a prefix LIKE fallback)
    for title, seasons in season_seed.SEEDS_BY_TITLE.items():
        row = q1("SELECT id FROM titles WHERE title=? COLLATE NOCASE AND kind='series'",
                 (title,))
        if not row:
            row = q1("SELECT id FROM titles WHERE kind='series' "
                     "AND title LIKE ? COLLATE NOCASE ORDER BY length(title) LIMIT 1",
                     (f"{title}%",))
        if not row:
            continue
        for s in seasons:
            upsert(row["id"], s["season"],
                   status=s.get("status", "vague"),
                   release_kind=s.get("release_kind"),
                   release_start=s.get("release_start"),
                   release_end=s.get("release_end"),
                   window_hint=s.get("window_hint"),
                   finished=s.get("finished", 0),
                   source=s.get("source", "seed"),
                   note=s.get("note"),
                   next_check_at=s.get("next_check_at"),
                   checked_at=_now())
            n += 1
    _log(f"seeded {n} season entries from web research")
    return n


# ---- API payloads -----------------------------------------------------------

def calendar_entries() -> list:
    """Everything for the calendar popup, sorted by date (vague at the end)."""
    rows = q("""SELECT s.*, t.title, t.poster, t.year, t.network, t.tmdb_id
                FROM season_watch s JOIN titles t ON t.id=s.title_id
                ORDER BY CASE s.status WHEN 'announced' THEN 0
                                       WHEN 'vague' THEN 1 ELSE 2 END,
                         s.release_start, t.title COLLATE NOCASE""")
    today = datetime.now(timezone.utc).date()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("release_start"):
            try:
                rs = datetime.strptime(d["release_start"], "%Y-%m-%d").date()
                d["days_until"] = (rs - today).days
            except ValueError:
                pass
        out.append(d)
    return out


def flags_for_titles(title_ids: list) -> dict:
    """Per-title badge info for the main list: upcoming season chip and/or
    finished chip. Duplicate rows of the same show (episode rips, renames)
    inherit the badges via their shared tmdb_id.
    {title_id: {upcoming: 'S4 · 18 Sep 2026'|'S2 · 2027',
    finished: bool, next_date: iso|None}}"""
    if not title_ids:
        return {}
    marks = ",".join("?" * len(title_ids))
    rows = q(f"""SELECT s.title_id, s.season, s.status, s.finished,
                        s.release_start, s.window_hint, s.release_end,
                        s.release_kind
                 FROM season_watch s
                 WHERE s.title_id IN ({marks})
                    OR s.title_id IN (
                        SELECT a.id FROM titles a
                        WHERE a.tmdb_id IS NOT NULL AND a.tmdb_id IN (
                            SELECT b.tmdb_id FROM titles b
                            WHERE b.id IN ({marks}) AND b.tmdb_id IS NOT NULL)
                        AND a.id NOT IN ({marks}))""",
             title_ids + title_ids + title_ids)
    out = {}
    for r in rows:
        e = out.setdefault(r["title_id"],
                           {"upcoming": None, "finished": False, "next": None})
        if r["finished"] or r["status"] == "done":
            e["finished"] = True
            continue
        if r["status"] == "announced" and r["release_start"]:
            try:
                d = datetime.strptime(r["release_start"], "%Y-%m-%d")
                label = f"S{r['season']} · {d.strftime('%-d %b %Y')}"
            except ValueError:
                label = f"S{r['season']}"
            if not e["next"] or (r["release_start"] or "") < e["next"]:
                e["upcoming"] = label
                e["next"] = r["release_start"]
        elif r["status"] == "vague":
            hint = r["window_hint"] or "date TBA"
            if not e["upcoming"]:
                e["upcoming"] = f"S{r['season']} · {hint}"
    return out

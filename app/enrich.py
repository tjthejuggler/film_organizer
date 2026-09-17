"""Enrichment pipeline: TMDB search -> details -> OMDb, with optional
LLM-assisted name cleanup for stubborn releases."""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db, llm, omdb, parser, tmdb
from .db import q, q1, tx


def _apply(row_id: int, data: dict, source: str, status="matched", manual_edits=None):
    """Write enrichment data. Fields listed in manual_edits (user-corrected
    via the edit form) are NEVER overwritten — a later Enrich must not
    revert manual fixes."""
    sets, vals = [], []
    colmap = {
        "tmdb_id": "tmdb_id", "imdb_id": "imdb_id", "title": "title",
        "original_title": "original_title", "year": "year",
        "overview": "overview", "tagline": "tagline", "genres": "genres",
        "stars": "stars", "director": "director", "creator": "creator",
        "network": "network", "seasons": "seasons", "episodes": "episodes",
        "runtime": "runtime", "status": "status",
        "rating_imdb": "rating_imdb", "votes_imdb": "votes_imdb",
        "rating_tmdb": "rating_tmdb", "poster": "poster",
        "backdrop": "backdrop",
    }
    locked = set(manual_edits or [])
    # miniseries flag comes from TMDB's TV "type" field; never locked via
    # manual_edits (it is toggled with the dedicated UI control)
    tv_type = (data.pop("tv_type", None) or "")
    if tv_type:
        is_mini = 1 if "miniseries" in tv_type.lower() else 0
        sets.append("is_miniseries=?")
        vals.append(is_mini)
    for k, v in data.items():
        if k not in colmap or k in locked:
            continue
        if k in ("genres", "stars"):
            v = json.dumps(v or [])
        if v is None:
            continue
        sets.append(f"{colmap[k]}=?")
        vals.append(v)
    sets.append("data_source=?")
    vals.append(source)
    sets.append("match_status=?")
    vals.append(status)
    sets.append("match_error=NULL")
    sets.append("enriched_at=datetime('now')")
    vals.append(row_id)
    with tx() as c:
        c.execute(f"UPDATE titles SET {', '.join(sets)} WHERE id=?", vals)


def _search_variants(title: str, year, kind: str) -> list:
    """Try the plain title first; for squashed-together names also try a
    CamelCase-split variant (e.g. 'TheChairCompany' -> 'The Chair Company')."""
    variants = [title]
    split = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", title)
    if split != title:
        variants.append(split)
    cands = []
    for v in variants:
        cands = tmdb.search_tv(v, year) if kind == "series" else tmdb.search_movie(v, year)
        if tmdb.best_candidate(cands) is not None:
            break
    return cands


def _llm_futile(title: str) -> bool:
    """Heuristic: default camera/phone recording names (timestamp stamps,
    IMG_/VID_ codes) — or names that are mostly digits — are personal
    recordings, not catalogued releases. The LLM can never turn them into
    a real title, so an API call on them is pure waste."""
    t = (title or "").strip()
    if not t:
        return True
    if parser.is_camera_name(t):
        return True
    key = parser.normalize_key(t)
    if len(key) >= 8:
        digits = sum(ch.isdigit() for ch in key)
        if digits / len(key) >= 0.5:
            return True
    return False


def _first_path(tid: int) -> str:
    """On-disk location of the title's first file, for job messages: the
    user watches the toast to spot junk entries (personal recordings,
    courses) worth deleting, and a bare name is not enough to find them."""
    f = q1("SELECT path FROM files WHERE title_id=? AND missing=0 ORDER BY id LIMIT 1", (tid,))
    return (f["path"] if f else "") or ""


MAX_ENRICH_ATTEMPTS = 3    # failed full-enrich passes before a row is left alone
MAX_BACKFILL_MISS = 2      # empty backfill passes before a row is left alone


def enrich_one(row, job_log=None, force=False, respect_cap=True):
    """Enrich a single titles row. Returns status string."""
    tid = row["id"]
    if not force and row["match_status"] == "matched":
        return "skip"
    # a row that keeps failing (no TMDB match, provider errors) gets a
    # bounded number of tries; force runs (and explicit single-row
    # enriches) ignore the cap so the user can always retry by hand
    if force is False and respect_cap and \
            (row["enrich_attempts"] or 0) >= MAX_ENRICH_ATTEMPTS:
        return "capped"

    title = row["title"]
    year = row["year"]
    kind = row["kind"]

    if not tmdb.enabled():
        with tx() as c:
            c.execute(
                "UPDATE titles SET match_status='no_provider', "
                "match_error='Add a TMDB key in Settings to fetch metadata' WHERE id=?",
                (tid,),
            )
        return "no_provider"

    cands = []
    try:
        cands = _search_variants(title, year, kind)
        best = tmdb.best_candidate(cands)

        # LLM cleanup pass — at most ONE attempt per title, ever. If it
        # already failed once (llm_attempts >= 1), don't burn time on it
        # again: the title is almost certainly not a catalogued movie/show
        # (courses, YouTube rips, personal recordings...).
        llm_used = False
        futile = _llm_futile(title)
        # never LLM-rewrite an identity the user locked via the edit form,
        # or burn a call on a hopeless camera-recording name
        if best is None and llm.enabled() and title and not futile \
                and not (row["llm_attempts"] or 0) and not row["title_locked"]:
            if job_log:
                # logged BEFORE the call: this is the single slowest step
                # (up to 15s), it must be visible while it runs, not after
                job_log(f"LLM name cleanup running for: {title!r} (can take ~15s)")
            llm_used = True
            cleaned = llm.clean_title(title, hint_kind=kind)
            if cleaned and parser.normalize_key(cleaned["title"]) != parser.normalize_key(title):
                new_title = cleaned["title"]
                new_year = cleaned["year"]
                new_kind = cleaned["kind"]
                # kind flips only when the kind isn't user-locked; the cleaned
                # TITLE is persisted and used either way — without this a junk
                # name whose kind was already right could never recover
                flip = new_kind != kind and not row["kind_locked"]
                eff_kind = new_kind if flip else kind
                new_key = (
                    f"s:{parser.normalize_key(new_title)}"
                    if eff_kind == "series"
                    else f"m:{parser.normalize_key(new_title)}:{new_year or 0}"
                )
                try:
                    with tx() as c:
                        if flip:
                            c.execute(
                                "UPDATE titles SET kind=?, title=?, year=COALESCE(?, year), "
                                "dedupe_key=? WHERE id=?",
                                (new_kind, new_title, new_year, new_key, tid),
                            )
                        else:
                            c.execute(
                                "UPDATE titles SET title=?, year=COALESCE(?, year), "
                                "dedupe_key=? WHERE id=?",
                                (new_title, new_year, new_key, tid),
                            )
                except Exception:
                    pass  # dedupe collision: row identity stays, retry still uses the clean name
                kind, title, year = eff_kind, new_title, new_year or year
                try:
                    cands = (tmdb.search_tv(title, year) if kind == "series"
                             else tmdb.search_movie(title, year))
                    best = tmdb.best_candidate(cands)
                except Exception as e:
                    with tx() as c:
                        c.execute(
                            "UPDATE titles SET match_status='error', match_error=? WHERE id=?",
                            (str(e), tid))
                    return "error"
    except Exception as e:
        with tx() as c:
            c.execute("UPDATE titles SET match_status='error', match_error=?, "
                      "enrich_attempts=enrich_attempts+1 WHERE id=?", (str(e), tid))
        return "error"

    if best is None:
        # 'not_found' is terminal for the default enrich run: futile names
        # (camera recordings) get the same treatment — tried once, done
        status = "not_found" if (llm_used or (row["llm_attempts"] or 0) or futile) \
            else "unmatched"
        err = ("default recording name (camera clip) — not enriched" if futile and not llm_used
               else "not found in TMDB even after LLM name cleanup — likely not a "
                    "catalogued movie/show" if status == "not_found" else "no confident match")
        with tx() as c:
            c.execute(
                "UPDATE titles SET match_status=?, match_error=?, "
                "llm_attempts=llm_attempts+?, "
                "enrich_attempts=enrich_attempts+1 WHERE id=?",
                (status, err, 1 if llm_used else 0, tid))
        return status

    try:
        detail = tmdb.tv_detail(best["id"]) if kind == "series" else tmdb.movie_detail(best["id"])
    except Exception as e:
        with tx() as c:
            c.execute("UPDATE titles SET match_status='error', match_error=? WHERE id=?", (str(e), tid))
        return "error"

    # US certification + OMDb ratings are two independent provider calls;
    # fetch them concurrently (each is a rate-limited HTTP round-trip)
    def _cert():
        try:
            return tmdb.cert(best["id"], kind) or None
        except Exception:
            return None

    def _omdb():
        if not (detail.get("imdb_id") and omdb.enabled()):
            return None
        try:
            return omdb.fetch(detail["imdb_id"])
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as ex:
        fc, fo = ex.submit(_cert), ex.submit(_omdb)
        detail["cert"] = fc.result()
        od = fo.result()

    source = "tmdb"
    if od:
        detail["rating_imdb"] = od["rating_imdb"]
        detail["votes_imdb"] = od["votes_imdb"]
        if od.get("rating_rt") is not None:
            detail["rating_rt"] = od["rating_rt"]
        if od.get("rated") and od["rated"] not in ("N/A", None):
            detail["cert"] = detail.get("cert") or od["rated"]
        if od.get("seasons_omdb") and kind == "series" and not detail.get("seasons"):
            detail["seasons"] = od["seasons_omdb"]
        source = "tmdb+omdb"

    _apply(tid, detail, source, manual_edits=json.loads(row["manual_edits"] or "[]"))
    return "matched"


def run_enrich(job_id: str, title_ids=None, force=False, only_unmatched=True):
    from .jobs import is_cancelled, log, update

    if title_ids:
        ph = ",".join("?" * len(title_ids))
        rows = q(f"SELECT * FROM titles WHERE id IN ({ph})", title_ids)
    elif only_unmatched:
        # default run: skip matched AND not_found (the LLM already tried
        # once and failed — re-checking every enrich would be wasted work).
        # ALSO skip rows that failed too many times (enrich_attempts cap):
        # a re-run must not redo old hopeless entries — only genuinely new
        # or recently-failed rows are retried. Force Enrich ignores this.
        rows = q("SELECT * FROM titles WHERE match_status NOT IN ('matched','not_found') "
                 "AND (enrich_attempts IS NULL OR enrich_attempts < ?) ORDER BY id",
                 (MAX_ENRICH_ATTEMPTS,))
    else:
        rows = q("SELECT * FROM titles ORDER BY id")

    total = len(rows)
    update(job_id, total=total, message=f"Enriching {total} title(s)")
    log(job_id, f"Enrichment started for {total} title(s) (force={force})")

    counts = {}
    t_start = time.monotonic()
    done_n = 0
    lock = threading.Lock()
    cancelled = threading.Event()

    def _work(row):
        nonlocal done_n
        if cancelled.is_set():
            return
        # live status: name the title AND its on-disk path BEFORE working on
        # it, so the toast always shows what the run is currently sitting on
        update(job_id, message=f"looking up: {row['title']}  ({_first_path(row['id'])})")
        t_row = time.monotonic()
        try:
            st = enrich_one(row, job_log=lambda m: log(job_id, m), force=force)
        except Exception as e:
            from .jobs import log as jlog
            jlog(job_id, f"{row['title']}: FATAL {e}")
            st = "error"
        # one log line per title with its duration: slow titles (LLM cleanup,
        # provider hangs) show up here instead of a silently frozen bar
        log(job_id, f"{row['title']} -> {st} ({time.monotonic() - t_row:.1f}s)")
        with lock:
            counts[st] = counts.get(st, 0) + 1
            done_n += 1
            n = done_n
        update(job_id, progress=n, message=f"[{n}/{total}] {row['title']} -> {st}")
        if st != "skip":  # nothing was fetched for skips: no politeness pause
            time.sleep(0.15)

    # parallel workers: most of the wall time is network round-trips, so 4
    # concurrent titles cut the run ~4x while the shared TMDB rate limiter
    # keeps total request rate polite (~4 req/s max)
    workers = min(4, max(1, total))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_work, row) for row in rows]
        # cooperative cancel: poll the flag and stop feeding new titles
        while True:
            alive = [f for f in futures if f.running() or not f.done()]
            if all(f.done() for f in futures):
                break
            if not cancelled.is_set() and is_cancelled(job_id):
                cancelled.set()
                log(job_id, f"Enrichment cancelled — stopping after in-flight titles")
            time.sleep(0.2)
        for f in futures:  # surface worker crashes (already caught inside)
            f.result()

    if cancelled.is_set():
        update(job_id, message=f"Cancelled at {done_n}/{total}")
    else:
        log(job_id, "Enrichment done: " + json.dumps(counts))
        update(job_id, message=f"Done in {time.monotonic() - t_start:.0f}s: {json.dumps(counts)}")
    return counts


def run_backfill(job_id: str):
    """Light pass over ALREADY-matched titles that lack cert / RT score or
    the miniseries flag. Fills those columns without a full re-enrich:
    TMDB cert + TV-type lookup, plus an OMDb ratings refresh.

    Anti-redo: a row that keeps coming up empty (the providers simply have
    no cert / RT / miniseries data for it) is only retried a bounded number
    of times (backfill_miss cap). Any successful write resets its counter,
    so rows self-heal if providers later add the data."""
    from .jobs import log, update

    rows = q("""SELECT id, title, tmdb_id, imdb_id, kind, cert, rating_rt, is_miniseries, manual_edits, backfill_miss FROM titles
                WHERE match_status='matched' AND tmdb_id IS NOT NULL
                  AND (cert IS NULL
                       OR (kind='series' AND is_miniseries=0)
                       OR (imdb_id IS NOT NULL AND rating_rt IS NULL))
                  AND (backfill_miss IS NULL OR backfill_miss < ?)""",
             (MAX_BACKFILL_MISS,))
    total = len(rows)
    update(job_id, total=total, message=f"Backfilling ratings for {total} title(s)")
    log(job_id, f"Backfill started: {total} title(s) missing cert/RT/miniseries")
    done = 0
    for bi, row in enumerate(rows, 1):
        update(job_id, message=f"[{bi}/{total}] refreshing: {row['title']}  ({_first_path(row['id'])})")
        sets, vals = [], []
        goals_met = []  # which of the row's actual goals got filled this pass
        locked = set(json.loads(row["manual_edits"] or "[]"))
        try:
            if row["cert"] is None and "cert" not in locked and tmdb.enabled():
                c = tmdb.cert(row["tmdb_id"], row["kind"])
                if c:
                    sets.append("cert=?"); vals.append(c)
                    goals_met.append("cert")
            # miniseries catch-up: TMDB marks limited series via its TV
            # 'type' field; already-matched rows never got this because the
            # default Enrich skips them (and the scanner used to overwrite
            # the flag with the folder-marker value each scan)
            if (row["kind"] == "series" and not row["is_miniseries"]
                    and "is_miniseries" not in locked and tmdb.enabled()):
                tv_type = tmdb.tv_type(row["tmdb_id"])
                if tv_type and "miniseries" in tv_type.lower():
                    sets.append("is_miniseries=1")
                    goals_met.append("mini")
            if row["imdb_id"] and omdb.enabled() and row["rating_rt"] is None:
                od = omdb.fetch(row["imdb_id"])
                if od:
                    if od.get("rating_rt") is not None and "rating_rt" not in locked:
                        sets.append("rating_rt=?"); vals.append(od["rating_rt"])
                        goals_met.append("rt")
                    if od.get("rating_imdb") is not None and "rating_imdb" not in locked:
                        sets.append("rating_imdb=?"); vals.append(od["rating_imdb"])
                    if od.get("votes_imdb") is not None and "votes_imdb" not in locked:
                        sets.append("votes_imdb=?"); vals.append(od["votes_imdb"])
                    if not row["cert"] and od.get("rated") and od["rated"] != "N/A":
                        sets.append("cert=?"); vals.append(od["rated"])
                        goals_met.append("cert")
        except Exception as e:
            from .jobs import log as jlog
            jlog(job_id, f"row {row['id']}: {e}")
        if sets:
            sets.append("enriched_at=datetime('now')")
            vals.append(row["id"])
            with tx() as c:
                c.execute(f"UPDATE titles SET {', '.join(sets)} WHERE id=?", vals)
            done += 1
        # the miss counter tracks the row's GOALS, not generic activity:
        # a row counts as 'found nothing' only when none of the missing
        # fields it was selected for got filled (incidental extras like an
        # OMDb rating refresh must NOT reset the countdown — those rows
        # would otherwise match the selection forever and redo every run)
        if goals_met:
            with tx() as c:
                c.execute("UPDATE titles SET backfill_miss=0 WHERE id=?", (row["id"],))
        else:
            with tx() as c:
                c.execute("UPDATE titles SET backfill_miss=backfill_miss+1 WHERE id=?",
                          (row["id"],))
        update(job_id, progress=bi, message=f"Backfilled {done}/{total}")
    log(job_id, f"Backfill done: {done}/{total} updated")
    update(job_id, message=f"Backfill done: {done}/{total} updated")
    return done

"""Enrichment pipeline: TMDB search -> details -> OMDb, with optional
LLM-assisted name cleanup for stubborn releases."""
import json
import re
import time

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


def enrich_one(row, job_log=None, force=False):
    """Enrich a single titles row. Returns status string."""
    tid = row["id"]
    if not force and row["match_status"] == "matched":
        return "skip"

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
        # never LLM-rewrite an identity the user locked via the edit form
        if best is None and llm.enabled() and title and not (row["llm_attempts"] or 0) \
                and not row["title_locked"]:
            if job_log:
                job_log(f"LLM cleanup for: {title!r}")
            llm_used = True
            cleaned = llm.clean_title(title, hint_kind=kind)
            if cleaned and parser.normalize_key(cleaned["title"]) != parser.normalize_key(title):
                new_title = cleaned["title"]
                new_year = cleaned["year"]
                new_kind = cleaned["kind"]
                # persist kind correction only when the kind isn't user-locked
                if new_kind != kind and not row["kind_locked"]:
                    new_key = (
                        f"s:{parser.normalize_key(new_title)}"
                        if new_kind == "series"
                        else f"m:{parser.normalize_key(new_title)}:{new_year or 0}"
                    )
                    with tx() as c:
                        try:
                            c.execute(
                                "UPDATE titles SET kind=?, title=?, year=COALESCE(?, year), "
                                "dedupe_key=? WHERE id=?",
                                (new_kind, new_title, new_year, new_key, tid),
                            )
                            kind, title, year = new_kind, new_title, new_year or year
                        except Exception:
                            pass  # dedupe collision: keep original identity
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
            c.execute("UPDATE titles SET match_status='error', match_error=? WHERE id=?", (str(e), tid))
        return "error"

    if best is None:
        status = "not_found" if (llm_used or (row["llm_attempts"] or 0)) else "unmatched"
        err = ("not found in TMDB even after LLM name cleanup — likely not a "
               "catalogued movie/show" if status == "not_found" else "no confident match")
        with tx() as c:
            c.execute(
                "UPDATE titles SET match_status=?, match_error=?, "
                "llm_attempts=llm_attempts+? WHERE id=?",
                (status, err, 1 if llm_used else 0, tid))
        return status

    try:
        detail = tmdb.tv_detail(best["id"]) if kind == "series" else tmdb.movie_detail(best["id"])
    except Exception as e:
        with tx() as c:
            c.execute("UPDATE titles SET match_status='error', match_error=? WHERE id=?", (str(e), tid))
        return "error"

    # US content certification (PG-13 / TV-MA / ...)
    try:
        detail["cert"] = tmdb.cert(best["id"], kind) or None
    except Exception:
        detail["cert"] = None

    source = "tmdb"
    # refresh IMDb rating + Rotten Tomatoes via OMDb when available
    if detail.get("imdb_id") and omdb.enabled():
        try:
            od = omdb.fetch(detail["imdb_id"])
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
        except Exception:
            pass

    _apply(tid, detail, source, manual_edits=json.loads(row["manual_edits"] or "[]"))
    return "matched"


def run_enrich(job_id: str, title_ids=None, force=False, only_unmatched=True):
    from .jobs import log, update

    if title_ids:
        ph = ",".join("?" * len(title_ids))
        rows = q(f"SELECT * FROM titles WHERE id IN ({ph})", title_ids)
    elif only_unmatched:
        # default run: skip matched AND not_found (the LLM already tried
        # once and failed — re-checking every enrich would be wasted work)
        rows = q("SELECT * FROM titles WHERE match_status NOT IN ('matched','not_found') ORDER BY id")
    else:
        rows = q("SELECT * FROM titles ORDER BY id")

    total = len(rows)
    update(job_id, total=total, message=f"Enriching {total} title(s)")
    log(job_id, f"Enrichment started for {total} title(s) (force={force})")

    counts = {}
    for i, row in enumerate(rows, 1):
        st = enrich_one(row, job_log=lambda m: log(job_id, m), force=force)
        counts[st] = counts.get(st, 0) + 1
        update(job_id, progress=i, message=f"Enriched {i}/{total}")
        time.sleep(0.15)

    log(job_id, "Enrichment done: " + json.dumps(counts))
    update(job_id, message=f"Done: {json.dumps(counts)}")
    return counts


def run_backfill(job_id: str):
    """Light pass over ALREADY-matched titles that lack cert / RT score.
    Fills the new columns without a full re-enrich: TMDB cert lookup +
    OMDb ratings refresh only."""
    from .jobs import log, update

    rows = q("""SELECT id, tmdb_id, imdb_id, kind, cert, rating_rt, manual_edits FROM titles
                WHERE match_status='matched' AND tmdb_id IS NOT NULL
                  AND (cert IS NULL OR (imdb_id IS NOT NULL AND rating_rt IS NULL))""")
    total = len(rows)
    update(job_id, total=total, message=f"Backfilling ratings for {total} title(s)")
    log(job_id, f"Backfill started: {total} title(s) missing cert/RT")
    done = 0
    for row in rows:
        sets, vals = [], []
        locked = set(json.loads(row["manual_edits"] or "[]"))
        try:
            if row["cert"] is None and "cert" not in locked and tmdb.enabled():
                c = tmdb.cert(row["tmdb_id"], row["kind"])
                if c:
                    sets.append("cert=?"); vals.append(c)
            if row["imdb_id"] and omdb.enabled() and row["rating_rt"] is None:
                od = omdb.fetch(row["imdb_id"])
                if od:
                    if od.get("rating_rt") is not None and "rating_rt" not in locked:
                        sets.append("rating_rt=?"); vals.append(od["rating_rt"])
                    if od.get("rating_imdb") is not None and "rating_imdb" not in locked:
                        sets.append("rating_imdb=?"); vals.append(od["rating_imdb"])
                    if od.get("votes_imdb") is not None and "votes_imdb" not in locked:
                        sets.append("votes_imdb=?"); vals.append(od["votes_imdb"])
                    if not row["cert"] and od.get("rated") and od["rated"] != "N/A":
                        sets.append("cert=?"); vals.append(od["rated"])
        except Exception as e:
            from .jobs import log as jlog
            jlog(job_id, f"row {row['id']}: {e}")
        if sets:
            sets.append("enriched_at=datetime('now')")
            vals.append(row["id"])
            with tx() as c:
                c.execute(f"UPDATE titles SET {', '.join(sets)} WHERE id=?", vals)
            done += 1
        update(job_id, progress=done, message=f"Backfilled {done}/{total}")
    log(job_id, f"Backfill done: {done}/{total} updated")
    update(job_id, message=f"Backfill done: {done}/{total} updated")
    return done

"""Per-episode watch checklist (series drawer) + series auto-promotion.

The drawer lists every parsed episode file of a series as a checkbox.
Ticking one stores files.watched_manual=1 (NULL = never touched — scanner
upserts leave the column alone, so flags survive rescans). When the tick
that completes the set lands, the whole series is promoted to watched via
the exact same path as the manual watch button (watched_manual=1 +
watched_at + backup notification + next-season probe) — the main list then
shows it as watched with no extra click.

Unticking episodes never un-watches the series: the manual watch state is
the user's call via the normal watched toggle. Promotion fires only on the
request that transitions "some unticked" -> "all ticked", so re-ticking
after a manual un-watch does not resurrect the flag by surprise.
"""
from . import db, notifications, seasons
from .jobs import now_iso


class EpisodeNotFound(KeyError):
    pass


def episodes_for_title(title_id: int) -> list:
    """All parsed, non-missing episode files of a title, display order."""
    return [dict(r) for r in db.q(
        """SELECT id, path, size_bytes, season, episode, watched_manual
           FROM files
           WHERE title_id=? AND missing=0 AND episode IS NOT NULL
           ORDER BY COALESCE(season,1), episode, path""", (title_id,))]


def _counts(title_id: int) -> tuple:
    """(ticked, total) over non-missing parsed episode files."""
    r = db.q1(
        """SELECT COUNT(*) total, COALESCE(SUM(watched_manual=1),0) done
           FROM files
           WHERE title_id=? AND missing=0 AND episode IS NOT NULL""",
        (title_id,))
    return r["done"], r["total"]


def set_episode_watched(title_id: int, file_id: int, watched: bool) -> dict:
    """Tick one episode checkbox; auto-promotes the series when this tick
    completes the set."""
    row = db.q1(
        "SELECT id FROM files WHERE id=? AND title_id=? AND episode IS NOT NULL",
        (file_id, title_id))
    if not row:
        raise EpisodeNotFound("episode file not found")
    done_before, total = _counts(title_id)
    with db.tx() as c:
        c.execute("UPDATE files SET watched_manual=? WHERE id=?",
                  (1 if watched else 0, file_id))
    promoted = False
    if watched and total and done_before < total:
        promoted = _maybe_promote_series(title_id)
    return _result(title_id, file_id=file_id, watched=watched,
                   promoted=promoted)


def set_season_watched(title_id: int, season, watched: bool) -> dict:
    """Bulk-tick every episode of one season (season=None: all seasons)."""
    done_before, total = _counts(title_id)
    if season is None:
        with db.tx() as c:
            c.execute(
                "UPDATE files SET watched_manual=? WHERE title_id=? "
                "AND missing=0 AND episode IS NOT NULL",
                (1 if watched else 0, title_id))
    else:
        with db.tx() as c:
            c.execute(
                "UPDATE files SET watched_manual=? WHERE title_id=? "
                "AND missing=0 AND episode IS NOT NULL AND COALESCE(season,1)=?",
                (1 if watched else 0, title_id, season))
    promoted = False
    if watched and total and done_before < total:
        promoted = _maybe_promote_series(title_id)
    return _result(title_id, season=season, watched=watched, promoted=promoted)


# ---- internals ---------------------------------------------------------------

def _maybe_promote_series(title_id: int) -> bool:
    """All episodes ticked -> the series itself becomes watched (same side
    effects as the manual watch button). True when a promotion happened."""
    r = db.q1("SELECT kind, watched_manual FROM titles WHERE id=?", (title_id,))
    if not r or r["kind"] != "series" or r["watched_manual"]:
        return False
    done, total = _counts(title_id)
    if not total or done < total:
        return False
    with db.tx() as c:
        c.execute("UPDATE titles SET watched_manual=1, watched_at=? WHERE id=?",
                  (now_iso(), title_id))
    # mirror the manual watch button's side effects (never fatal)
    try:
        notifications.notify_watched_backup(title_id)
    except Exception:
        pass
    try:
        seasons.on_watched(title_id)
    except Exception:
        pass
    return True


def _result(title_id: int, **extra) -> dict:
    done, total = _counts(title_id)
    out = {"ok": True, "promoted": False,
           "episodes_watched": done, "episodes_total": total}
    out.update(extra)
    return out

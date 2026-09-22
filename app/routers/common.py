"""Shared API helpers: catalog-row shaping used by several routers."""
import json

from fastapi import HTTPException

from .. import db


def _avg_rating(d: dict):
    """Mean of the available ratings, all normalized to a 0-10 scale
    (IMDb 0-10, TMDB 0-10, RT %/10, Metacritic /10). Zero ratings count as
    'no data' so the average only spans providers that actually scored it."""
    vals = [v for v in (
        d.get("rating_imdb"), d.get("rating_tmdb"),
        d["rating_rt"] / 10 if d.get("rating_rt") is not None else None,
        d["rating_mc"] / 10 if d.get("rating_mc") is not None else None,
    ) if v is not None and v > 0]
    return round(sum(vals) / len(vals), 1) if vals else None


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
    d["rating_mc"] = d.get("rating_mc")
    d["avg_rating"] = _avg_rating(d)
    return d


def _get_title_or_404(tid: int):
    row = db.q1("SELECT * FROM titles WHERE id=?", (tid,))
    if not row:
        raise HTTPException(404, "title not found")
    return row


def _secrets_masked(all_settings: dict) -> dict:
    out = {}
    from .. import config
    for k, v in all_settings.items():
        if k in config.SECRET_KEYS and v:
            out[k] = ("*" * 6) + v[-4:] if len(v) > 4 else "******"
            out[k + "_set"] = True
        else:
            out[k] = v
            out[k + "_set"] = bool(v)
    return out


# SQL twin of _avg_rating(): the same provider-mean on a 0-10 scale (RT and
# MC are 0-100 and MUST be divided by 10 here — mixing raw scales once made
# an RT-only 83% outrank a 9.1 IMDb), built for ORDER BY so sorted and
# displayed averages agree exactly. All-four-missing -> 0/0 -> NULL.
_AVG_SCORE_SQL = """(
    (COALESCE(CASE WHEN t.rating_imdb > 0 THEN t.rating_imdb END, 0)
   + COALESCE(CASE WHEN t.rating_tmdb > 0 THEN t.rating_tmdb END, 0)
   + COALESCE(CASE WHEN t.rating_rt   > 0 THEN t.rating_rt / 10.0 END, 0)
   + COALESCE(CASE WHEN t.rating_mc   > 0 THEN t.rating_mc / 10.0 END, 0))
   /(CASE WHEN t.rating_imdb > 0 THEN 1 ELSE 0 END
    + CASE WHEN t.rating_tmdb > 0 THEN 1 ELSE 0 END
    + CASE WHEN t.rating_rt   > 0 THEN 1 ELSE 0 END
    + CASE WHEN t.rating_mc   > 0 THEN 1 ELSE 0 END))"""


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

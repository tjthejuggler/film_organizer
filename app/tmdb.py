"""TMDB provider: search + details for movies and TV.

Uses the classic v3 API with an api_key (user supplies it in Settings).
Scoring combines normalised-string similarity and token overlap, with a
year-proximity bonus/penalty so remakes pick the right candidate.
"""
import difflib
import time

import httpx

from . import config, db, parser

BASE = "https://api.themoviedb.org/3"
_last_call = 0.0


def enabled() -> bool:
    return bool(db.settings_get("tmdb_api_key"))


def _get(path: str, **params) -> dict:
    global _last_call
    key = db.settings_get("tmdb_api_key")
    if not key:
        raise RuntimeError("TMDB api key not configured")
    # light rate limiting (~4 req/s)
    global_wait = 0.25 - (time.monotonic() - _last_call)
    if global_wait > 0:
        time.sleep(global_wait)
    _last_call = time.monotonic()
    params["api_key"] = key
    r = httpx.get(f"{BASE}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def probe():
    """Connectivity test for the Settings 'Test TMDB' button."""
    if not enabled():
        return False, "no TMDB API key configured"
    try:
        data = _get("/search/movie", query="heat", include_adult="false")
        return True, f"search works, {data.get('total_results', 0)} results"
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except Exception as e:
        return False, str(e)[:300]


def _year_of(date_str) -> int:
    try:
        return int((date_str or "")[:4])
    except (TypeError, ValueError):
        return 0


def score(name: str, year, cand_name: str, cand_date) -> float:
    a = parser.normalize_key(name)
    b = parser.normalize_key(cand_name)
    if not a or not b:
        return 0.0
    s = max(
        difflib.SequenceMatcher(None, a, b).ratio(),
        parser.jaccard(parser.tokens(name), parser.tokens(cand_name)),
    )
    cy = _year_of(cand_date)
    if year and cy:
        d = abs(int(year) - cy)
        if d == 0:
            s = min(1.0, s + 0.25)
        elif d == 1:
            s = min(1.0, s + 0.10)
        elif d > 3:
            s -= 0.25
    return round(s, 4)


def search_movie(name: str, year=None) -> list:
    data = _get("/search/movie", query=name, include_adult="false",
                **({"year": int(year)} if year else {}))
    return [
        {"id": r["id"], "name": r.get("title") or "", "date": r.get("release_date"),
         "score": score(name, year, r.get("title") or "", r.get("release_date")),
         "popularity": r.get("popularity") or 0,
         "poster": _img(r.get("poster_path"), "w185"),
         "overview": (r.get("overview") or "")[:200]}
        for r in data.get("results", [])
    ]


def search_tv(name: str, year=None) -> list:
    data = _get("/search/tv", query=name, include_adult="false",
                **({"first_air_date_year": int(year)} if year else {}))
    return [
        {"id": r["id"], "name": r.get("name") or "", "date": r.get("first_air_date"),
         "score": score(name, year, r.get("name") or "", r.get("first_air_date")),
         "popularity": r.get("popularity") or 0,
         "poster": _img(r.get("poster_path"), "w185"),
         "overview": (r.get("overview") or "")[:200]}
        for r in data.get("results", [])
    ]


def best_candidate(cands: list, threshold=0.55):
    if not cands:
        return None
    cands.sort(key=lambda c: (c["score"], c["popularity"]), reverse=True)
    top = cands[0]
    if top["score"] < threshold:
        return None
    return top


def movie_detail(tmdb_id: int) -> dict:
    d = _get(f"/movie/{tmdb_id}", append_to_response="credits,external_ids")
    cast = [m.get("name") for m in (d.get("credits") or {}).get("cast", [])[:6]]
    directors = [c.get("name") for c in (d.get("credits") or {}).get("crew", [])
                 if c.get("job") == "Director"]
    return {
        "tmdb_id": d.get("id"),
        "imdb_id": (d.get("external_ids") or {}).get("imdb_id"),
        "title": d.get("title"),
        "original_title": d.get("original_title"),
        "year": _year_of(d.get("release_date")) or None,
        "overview": d.get("overview"),
        "tagline": d.get("tagline"),
        "genres": [g.get("name") for g in d.get("genres", [])],
        "stars": cast,
        "director": ", ".join(directors) or None,
        "runtime": d.get("runtime"),
        "status": d.get("status"),
        "rating_tmdb": d.get("vote_average"),
        "poster": _img(d.get("poster_path"), "w500"),
        "backdrop": _img(d.get("backdrop_path"), "w780"),
    }


def tv_detail(tmdb_id: int) -> dict:
    d = _get(f"/tv/{tmdb_id}", append_to_response="credits,external_ids")
    cast = [m.get("name") for m in (d.get("credits") or {}).get("cast", [])[:6]]
    creators = [c.get("name") for c in d.get("created_by", [])]
    networks = [n.get("name") for n in d.get("networks", [])]
    rt = d.get("episode_run_time") or []
    return {
        "tmdb_id": d.get("id"),
        "tv_type": d.get("type"),  # 'Miniseries' | 'Scripted' | ...
        "imdb_id": (d.get("external_ids") or {}).get("imdb_id"),
        "title": d.get("name"),
        "original_title": d.get("original_name"),
        "year": _year_of(d.get("first_air_date")) or None,
        "overview": d.get("overview"),
        "tagline": d.get("tagline"),
        "genres": [g.get("name") for g in d.get("genres", [])],
        "stars": cast,
        "creator": ", ".join(creators) or None,
        "network": ", ".join(networks) or None,
        "seasons": d.get("number_of_seasons"),
        "episodes": d.get("number_of_episodes"),
        "runtime": rt[0] if rt else None,
        "status": d.get("status"),
        "rating_tmdb": d.get("vote_average"),
        "poster": _img(d.get("poster_path"), "w500"),
        "backdrop": _img(d.get("backdrop_path"), "w780"),
    }


def tv_calendar(tmdb_id: int) -> dict:
    """Season-level airing info for the season-calendar checker: status,
    next/last episode air dates and the per-season episode counts."""
    d = _get(f"/tv/{tmdb_id}")
    return {
        "status": d.get("status"),
        "type": d.get("type"),
        "network": ", ".join(n.get("name") for n in d.get("networks", [])) or None,
        "next_episode": d.get("next_episode_to_air") or None,
        "last_episode": d.get("last_episode_to_air") or None,
        "seasons": [
            {"season": s.get("season_number"),
             "episodes": s.get("episode_count"),
             "air_date": s.get("air_date")}
            for s in d.get("seasons", []) if (s.get("season_number") or 0) > 0
        ],
    }


def tv_type(tmdb_id: int):
    """TV 'type' string, e.g. 'Miniseries' | 'Scripted' | 'Talk Show'.
    Drives the miniseries tag (a limited/one-season series)."""
    try:
        d = _get(f"/tv/{tmdb_id}")
        return d.get("type") or None
    except Exception:
        return None


def cert(tmdb_id: int, kind: str):
    """US content certification: PG-13 / R for movies, TV-MA / TV-PG for TV."""
    try:
        if kind == "series":
            d = _get(f"/tv/{tmdb_id}/content_ratings")
            for r in d.get("results", []):
                if r.get("iso_3166_1") == "US" and r.get("rating"):
                    return r["rating"]
        else:
            d = _get(f"/movie/{tmdb_id}/release_dates")
            for r in d.get("results", []):
                if r.get("iso_3166_1") == "US":
                    for rd in r.get("release_dates", []):
                        c = (rd.get("certification") or "").strip()
                        if c:
                            return c
    except Exception:
        return None
    return None


def _img(path, size):
    return f"{config.TMDB_IMG_BASE}/{size}{path}" if path else None

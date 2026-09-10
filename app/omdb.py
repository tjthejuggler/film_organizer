"""OMDb provider (optional): fills IMDb rating/votes, seasons, extra cast."""
import httpx

from . import db


def enabled() -> bool:
    return bool(db.settings_get("omdb_api_key"))


def fetch(imdb_id: str):
    key = db.settings_get("omdb_api_key")
    if not key or not imdb_id:
        return None
    r = httpx.get(
        "https://www.omdbapi.com/",
        params={"i": imdb_id, "apikey": key, "plot": "short"},
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("Response") == "False":
        return None
    votes = None
    try:
        votes = int((d.get("imdbVotes") or "").replace(",", ""))
    except ValueError:
        pass
    seasons = None
    try:
        seasons = int(d.get("totalSeasons"))
    except (TypeError, ValueError):
        pass
    rating_rt = None
    for row in d.get("Ratings") or []:
        if row.get("Source") == "Rotten Tomatoes":
            try:
                rating_rt = int(row["Value"].rstrip("%"))
            except (ValueError, KeyError):
                pass
    return {
        "rating_imdb": float(d["imdbRating"]) if d.get("imdbRating") not in (None, "N/A") else None,
        "votes_imdb": votes,
        "rated": d.get("Rated") if d.get("Rated") != "N/A" else None,
        "rating_rt": rating_rt,
        "metacritic": d.get("Metascore") if d.get("Metascore") not in (None, "N/A") else None,
        "country": d.get("Country") if d.get("Country") != "N/A" else None,
        "seasons_omdb": seasons,
        "poster_omdb": d.get("Poster") if d.get("Poster") != "N/A" else None,
        "actors_omdb": [a for a in (d.get("Actors") or "").split(", ") if a and a != "N/A"],
        "director_omdb": d.get("Director") if d.get("Director") != "N/A" else None,
        "writer_omdb": d.get("Writer") if d.get("Writer") != "N/A" else None,
    }

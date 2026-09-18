"""Local poster/backdrop cache: TMDB images are downloaded once, then served
from disk via GET /img?u=<tmdb-image-url>.

The catalog stores remote image.tmdb.org URLs; before this cache every page
load made the browser fetch dozens of posters over the WAN (slow on a weak
link, useless offline). With the cache the browser only ever talks to this
server: the first request for an image downloads it into data/imgcache/,
every later request is a plain file serve marked immutable (browsers keep it
for a year). A startup warmer pre-downloads the whole catalog so even the
FIRST view after enrichment is instant.

Filenames are content-addressed (md5 of the remote URL): a re-enriched title
that points at a new image URL simply gets a new cache file; stale files are
harmless and tiny (a w500 poster is ~30-60 KB).
"""
import hashlib
import os
import threading
import time
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse

from . import config, db

IMG_DIR = os.path.join(config.DATA_DIR, "imgcache")

_MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".svg": "image/svg+xml",
}

# /img must never become an open proxy: only TMDB's image host is allowed.
_ALLOWED_HOST = urlparse(config.TMDB_IMG_BASE).netloc

_client = httpx.Client(timeout=20, follow_redirects=True,
                       headers={"User-Agent": "film_organizer/1.0"})

_locks: dict = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _hash_path(remote: str):
    """Cache file path for a remote URL: data/imgcache/<md5><ext>."""
    h = hashlib.md5(remote.encode()).hexdigest()
    ext = os.path.splitext(urlparse(remote).path)[1].lower() or ".jpg"
    return os.path.join(IMG_DIR, h + ext), ext


def _download(remote: str, path: str) -> bool:
    """Fetch one image to path (atomically). False on any failure."""
    tmp = path + ".part"
    try:
        r = _client.get(remote)
        r.raise_for_status()
        os.makedirs(IMG_DIR, exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def serve(u: str):
    """FastAPI handler body: serve a cached image, downloading on first hit."""
    if not u or urlparse(u).netloc != _ALLOWED_HOST:
        raise HTTPException(404, "unsupported image host")
    path, ext = _hash_path(u)
    if not os.path.exists(path):
        with _lock_for(path):        # collapse concurrent misses into one fetch
            if not os.path.exists(path):
                _download(u, path)
    if not os.path.exists(path):
        raise HTTPException(404, "image unavailable")
    # immutable: the URL (and thus its hash) pins the exact image forever
    return FileResponse(path, media_type=_MEDIA_TYPES.get(ext, "image/jpeg"),
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


def warm_async():
    """Pre-download every catalog + recommendation image in the background."""
    threading.Thread(target=_warm, daemon=True).start()


def _catalog_urls() -> list:
    urls = []
    for table in ("titles", "recommendations"):
        try:
            for row in db.q(f"SELECT poster, backdrop FROM {table}"):
                urls.append(row["poster"])
                urls.append(row["backdrop"])
        except Exception:
            pass  # a missing table must not break the warmer
    host_ok = (u for u in urls if u and urlparse(u).netloc == _ALLOWED_HOST)
    return sorted({u for u in host_ok if not os.path.exists(_hash_path(u)[0])})


def _warm():
    misses = 0
    for u in _catalog_urls():
        path, _ = _hash_path(u)
        if _download(u, path):
            misses = 0
        else:
            misses += 1
            if misses >= 3:
                return  # TMDB unreachable — don't grind through the rest
        time.sleep(0.25)  # stay gentle with the image CDN

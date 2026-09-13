"""Central configuration constants."""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "film_organizer.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

HOST = "127.0.0.1"
PORT = 8765

# Initial library roots (first boot seeds these when the table is empty).
DEFAULT_ROOTS = [
    os.path.expanduser("~/Videos"),
    os.path.expanduser("~/Downloads"),
]

# Watch Next staging folders: a COPY of the pinned title lives here
# (inside internal_root); setting a new pin wipes the previous one.
WATCH_NEXT_DIRS = {
    "movie": "aaNext_Movie",
    "series": "aaNext_Series",
}

# Backup drive (external_root) layout: moved titles land in the subfolder
# matching their kind, so the drive stays organized (internal storage keeps
# its existing layout).
EXTERNAL_SUBDIRS = {
    "movie": "Movies",
    "series": "Series",
}

# z.ai (Zhipu) OpenAI-compatible endpoint + GLM flash model
DEFAULT_SETTINGS = {
    "llm_base_url": "https://api.z.ai/api/paas/v4",
    "llm_model": "glm-5.3-flash",
    # storage targets for the Move feature (drawer buttons)
    "internal_root": os.path.expanduser("~/Videos"),
    "external_root": "",
    # route moves through KIO so the desktop shows its native move dialog
    # ("1" on / "0" off; falls back silently when no desktop is available)
    "move_native_dialog": "1",
}

# settings that must be migrated away from when they still hold these
# untouched legacy defaults (user never customized them)
LEGACY_DEFAULTS = {
    "llm_base_url": "https://api.openai.com/v1",
    "llm_model": "gpt-4o-mini",
}

# Settings keys whose values must never be returned verbatim by the API.
SECRET_KEYS = {"llm_api_key", "tmdb_api_key", "omdb_api_key"}

TMDB_IMG_BASE = "https://image.tmdb.org/t/p"

# Files smaller than this are considered samples/trailers, not real media.
MIN_VIDEO_BYTES = 20 * 1024 * 1024

"""Central configuration constants."""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "film_organizer.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 0.0.0.0 = listen on all interfaces so paired LAN devices can connect;
# unpaired remote requests are rejected by the device gate (app/pairing.py).
# Loopback stays trusted automatically.  pairing.lan_urls() detects the
# real LAN address for the QR code when HOST is the wildcard.
HOST = "0.0.0.0"
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

# Backup destinations. Since the 4-way split there are FOUR user-chosen
# folders (settings): backup_movies_root / backup_series_root — where EVERY
# movie / series gets backed up — plus liked_movies_root / liked_series_root,
# additional drives that favorites ALSO get copied to (a liked title ends up
# in 2 places: its regular backup + its liked drive).
#
# external_root is the LEGACY single-drive setting: when both per-kind keys
# are still empty, the old drive keeps working with the subfolder layout
# below (Movies/ + Series/ side by side).
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
    # the four backup destinations (see the comment above EXTERNAL_SUBDIRS);
    # per-kind regular backups + additional liked-only drives
    "backup_movies_root": "",
    "backup_series_root": "",
    "liked_movies_root": "",
    "liked_series_root": "",
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

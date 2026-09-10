"""SQLite access layer: schema, connection helper, settings store."""
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_local = threading.local()
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS roots(
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT UNIQUE NOT NULL,
    label    TEXT,
    enabled  INTEGER DEFAULT 1,
    added_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS titles(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key     TEXT UNIQUE NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'movie',
    title          TEXT NOT NULL,
    title_locked   INTEGER DEFAULT 0,
    kind_locked    INTEGER DEFAULT 0,
    year           INTEGER,
    original_title TEXT,
    tmdb_id        INTEGER,
    imdb_id        TEXT,
    overview       TEXT,
    tagline        TEXT,
    genres         TEXT DEFAULT '[]',
    stars          TEXT DEFAULT '[]',
    director       TEXT,
    creator        TEXT,
    network        TEXT,
    seasons        INTEGER,
    episodes       INTEGER,
    episode_count  INTEGER DEFAULT 0,
    runtime        INTEGER,
    status         TEXT,
    rating_imdb    REAL,
    votes_imdb     INTEGER,
    rating_tmdb    REAL,
    poster         TEXT,
    backdrop       TEXT,
    data_source    TEXT,
    match_status   TEXT DEFAULT 'unmatched',
    match_error    TEXT,
    llm_attempts   INTEGER DEFAULT 0,
    cert           TEXT,
    rating_rt      INTEGER,
    enriched_at    TEXT,
    cataloged_at   TEXT,
    watched_folder INTEGER DEFAULT 0,
    watched_manual INTEGER,
    watched_at     TEXT,
    last_seen      TEXT,
    size_bytes     INTEGER DEFAULT 0,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_titles_kind  ON titles(kind);
CREATE INDEX IF NOT EXISTS idx_titles_match ON titles(match_status);
CREATE INDEX IF NOT EXISTS idx_titles_year  ON titles(year);
CREATE TABLE IF NOT EXISTS files(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id       INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    path           TEXT UNIQUE NOT NULL,
    is_dir         INTEGER DEFAULT 0,
    size_bytes     INTEGER DEFAULT 0,
    mtime          REAL,
    season         INTEGER,
    episode        INTEGER,
    watched_folder INTEGER DEFAULT 0,
    missing        INTEGER DEFAULT 0,
    last_seen      TEXT,
    created        REAL
);
CREATE INDEX IF NOT EXISTS idx_files_title ON files(title_id);
CREATE TABLE IF NOT EXISTS jobs(
    id          TEXT PRIMARY KEY,
    type        TEXT,
    status      TEXT DEFAULT 'running',
    progress    INTEGER DEFAULT 0,
    total       INTEGER DEFAULT 0,
    message     TEXT,
    log         TEXT,
    created_at  TEXT,
    finished_at TEXT,
    error       TEXT
);
"""


def ensure():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    with conn() as con:
        con.executescript(SCHEMA)
        # lightweight migrations for databases created before a column existed
        cols = {r[1] for r in con.execute("PRAGMA table_info(titles)")}
        if "llm_attempts" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN llm_attempts INTEGER DEFAULT 0")
        if "cert" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN cert TEXT")
        if "rating_rt" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN rating_rt INTEGER")
        if "created_at" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN created_at TEXT")
        fcols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
        if "created" not in fcols:
            con.execute("ALTER TABLE files ADD COLUMN created REAL")


def init():
    """Full first-boot initialization: schema + default roots/settings."""
    ensure()
    if not q1("SELECT 1 FROM roots LIMIT 1"):
        for p in config.DEFAULT_ROOTS:
            with tx() as c:
                c.execute("INSERT OR IGNORE INTO roots(path) VALUES(?)", (p,))
    for k, v in config.DEFAULT_SETTINGS.items():
        if settings_get(k) is None:
            settings_set(k, v)
    # migrate legacy untouched defaults (e.g. old OpenAI base URL) to current
    for k, old in config.LEGACY_DEFAULTS.items():
        if settings_get(k) == old:
            settings_set(k, config.DEFAULT_SETTINGS[k])


def conn() -> sqlite3.Connection:
    c = getattr(_local, "con", None)
    if c is None:
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.con = c
    return c


@contextmanager
def tx():
    """Serialized write transaction."""
    with _write_lock:
        c = conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def q(sql: str, params=()) -> list:
    return conn().execute(sql, params).fetchall()


def q1(sql: str, params=()):
    return conn().execute(sql, params).fetchone()


# ---- settings -------------------------------------------------------------

def settings_all() -> dict:
    return {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}


def settings_get(key: str, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def settings_set(key: str, value: str):
    with tx() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

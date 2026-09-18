"""SQLite access layer: schema, connection helper, settings store."""
import os
import sqlite3
import threading
from contextlib import contextmanager

from . import config

_local = threading.local()
# RLock (not Lock): code paths legitimately nest tx() — e.g. scan_roots
# opens one big transaction and then calls jobs.log() inside it, which
# opens its own tx() on the same thread. A plain Lock self-deadlocks there
# (this is what froze scans at 'writing database...').
_write_lock = threading.RLock()

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
    enrich_attempts INTEGER DEFAULT 0,  -- failed full-enrich passes; capped (see migrations)
    backfill_miss  INTEGER DEFAULT 0,   -- backfill passes that found nothing; capped
    cert           TEXT,
    rating_rt      INTEGER,
    wanted         INTEGER DEFAULT 0,
    wanted_note    TEXT,
    wanted_by      TEXT,
    favorite       INTEGER DEFAULT 0,
    manual_edits   TEXT DEFAULT '[]',
    enriched_at    TEXT,
    cataloged_at   TEXT,
    watched_folder INTEGER DEFAULT 0,
    watched_manual INTEGER,
    watched_at     TEXT,
    history        INTEGER DEFAULT 0,
    hidden         INTEGER DEFAULT 0,  -- tucked away: excluded from the default list, never deleted
    last_seen      TEXT,
    size_bytes     INTEGER DEFAULT 0,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_titles_kind    ON titles(kind);
CREATE INDEX IF NOT EXISTS idx_titles_match   ON titles(match_status);
CREATE INDEX IF NOT EXISTS idx_titles_year    ON titles(year);
-- idx_titles_wanted / idx_titles_fav are created in ensure() AFTER the
-- wanted/favorite column migrations, so pre-wishlist databases migrate fine
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
    watched_manual INTEGER,  -- user-ticked episode watch flag (NULL = never touched)
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
CREATE TABLE IF NOT EXISTS recommendations(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key     TEXT UNIQUE NOT NULL,
    kind           TEXT NOT NULL DEFAULT 'movie',  -- movie | series (miniseries stays series + flag)
    is_miniseries  INTEGER DEFAULT 0,
    title          TEXT NOT NULL,
    year           INTEGER,
    overview       TEXT,
    why            TEXT,          -- LLM: why THIS user might like it
    genres         TEXT DEFAULT '[]',
    stars          TEXT DEFAULT '[]',
    director       TEXT,
    creator        TEXT,
    runtime        INTEGER,
    seasons        INTEGER,
    episodes       INTEGER,
    cert           TEXT,
    rating_imdb    REAL,
    rating_tmdb    REAL,
    rating_rt      INTEGER,
    poster         TEXT,
    backdrop       TEXT,
    tmdb_id        INTEGER,
    imdb_id        TEXT,
    where_watch    TEXT,          -- LLM: where it currently streams / how to watch
    match_status   TEXT DEFAULT 'unmatched',
    status         TEXT DEFAULT 'pending',  -- pending | accepted | rejected
    feedback_note  TEXT,
    liked          INTEGER,       -- user: did they like it? (after watching/deciding)
    seen           INTEGER DEFAULT 0,  -- user says they already saw it
    batch_id       TEXT,
    decided_at     TEXT,
    title_id       INTEGER,       -- titles row created on accept / seen-reject
    created_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_recs_status ON recommendations(status);
CREATE TABLE IF NOT EXISTS notifications(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id   INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    type       TEXT NOT NULL DEFAULT 'watched_backup',
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | done | rejected | failed | queued
    message    TEXT,
    created_at TEXT,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
CREATE TABLE IF NOT EXISTS drive_queue(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,   -- move | delete | delete_copy
    -- deliberately NO foreign key: the finalize step of a queued delete
    -- removes the title row itself and must not cascade-erase this entry
    title_id    INTEGER,
    payload     TEXT,            -- JSON: target/root/keep_record/drives/notification_id
    drive       TEXT NOT NULL,   -- primary drive the entry waits for
    description TEXT,            -- human-readable, shown in Settings
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error|cancelled
    last_error  TEXT,
    attempts    INTEGER DEFAULT 0,  -- failed-run counter (retry cap, see drivequeue)
    job_id      TEXT,            -- jobs.id of the auto-started run
    created_at  TEXT DEFAULT (datetime('now')),
    ran_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_drive_queue_status ON drive_queue(status);
CREATE INDEX IF NOT EXISTS idx_drive_queue_drive  ON drive_queue(drive);
CREATE TABLE IF NOT EXISTS season_watch(
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title_id       INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
    season         INTEGER NOT NULL,
    -- announced: exact date(s) known | vague: rough window only
    status         TEXT NOT NULL DEFAULT 'pending',
    release_kind   TEXT,            -- all_at_once | weekly | daily | unknown
    release_start  TEXT,            -- ISO date of first episode
    release_end    TEXT,            -- ISO date of last episode (weekly range / finale)
    window_hint    TEXT,            -- vague period: '2027' | 'Spring 2027' | 'TBA'
    finished       INTEGER DEFAULT 0,  -- series finale has aired
    seen           INTEGER DEFAULT 0,  -- user watched THIS season (catch-up list)
    source         TEXT,            -- where the info came from (tmdb/web/manual)
    note           TEXT,
    next_check_at  TEXT,            -- when to re-query for a real date (vague rows)
    checked_at     TEXT,            -- last lookup of ANY kind
    changed_at     TEXT,            -- info last changed -> calendar NEW badge/highlight
    created_at     TEXT,
    UNIQUE(title_id, season)
);
CREATE INDEX IF NOT EXISTS idx_season_watch_check ON season_watch(status, next_check_at);
CREATE INDEX IF NOT EXISTS idx_season_watch_title ON season_watch(title_id);
-- idx_season_watch_seen is created in ensure() AFTER the seen-column
-- migration (same pattern as the migrated titles indexes above)
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
        # wanted-list + favorites (user requests from external programs)
        if "wanted" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN wanted INTEGER DEFAULT 0")
        if "wanted_note" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN wanted_note TEXT")
        if "wanted_by" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN wanted_by TEXT")
        if "favorite" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN favorite INTEGER DEFAULT 0")
        # per-field user-edit locks: enrich skips fields listed here
        if "manual_edits" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN manual_edits TEXT DEFAULT '[]'")
        # watch-next pin: NULL | 'movie' | 'series' — sticks the title to
        # the top of the list and marks it as the one queued to watch
        if "watch_next" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN watch_next TEXT")
        # seen-history: rows the user recorded as watched but owns no file
        # of; the scanner must never prune them (see scan_roots cleanup)
        if "history" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN history INTEGER DEFAULT 0")
        # miniseries: still a series (kind='series') but shown/tagged as
        # miniseries and filterable via the genre dropdown
        if "is_miniseries" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN is_miniseries INTEGER DEFAULT 0")
        # failed-enrich attempt counter: once a row failed ENOUGH times
        # (no TMDB match / provider errors), the default Enrich run stops
        # retrying it — re-running Enrich must not redo the same old work
        if "enrich_attempts" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN enrich_attempts INTEGER DEFAULT 0")
        # backfill "found nothing" counter: after a couple of passes where
        # a row's missing cert/RT/miniseries could NOT be filled (the data
        # simply doesn't exist at the providers), the backfill stops
        # re-fetching it on every run; a successful write resets it to 0
        if "backfill_miss" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN backfill_miss INTEGER DEFAULT 0")
        # hidden: tucked-away rows — excluded from the default list, never
        # deleted; reachable again via the Hidden filter
        if "hidden" not in cols:
            con.execute("ALTER TABLE titles ADD COLUMN hidden INTEGER DEFAULT 0")
        # season_watch.seen / changed_at: catch-up flag + info-change stamp
        # (drives the calendar NEW badge/highlight); fresh DBs get both
        # from SCHEMA, old ones migrate here
        sw_cols = {r[1] for r in con.execute("PRAGMA table_info(season_watch)")}
        if "seen" not in sw_cols:
            con.execute("ALTER TABLE season_watch ADD COLUMN seen INTEGER DEFAULT 0")
        if "changed_at" not in sw_cols:
            con.execute("ALTER TABLE season_watch ADD COLUMN changed_at TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_season_watch_seen "
                    "ON season_watch(seen)")
        fcols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
        if "created" not in fcols:
            con.execute("ALTER TABLE files ADD COLUMN created REAL")
        # per-episode user watch flag (drawer checklist); NULL = never touched
        if "watched_manual" not in fcols:
            con.execute("ALTER TABLE files ADD COLUMN watched_manual INTEGER")
        # drive_queue built before the FK removal would cascade-erase queue
        # history whenever a queued delete removed its title row: rebuild
        if con.execute("PRAGMA foreign_key_list(drive_queue)").fetchone():
            con.executescript(
                "ALTER TABLE drive_queue RENAME TO drive_queue_legacy;"
                "CREATE TABLE drive_queue("
                "    id          INTEGER PRIMARY KEY AUTOINCREMENT,"
                "    kind        TEXT NOT NULL,"
                "    title_id    INTEGER,"
                "    payload     TEXT,"
                "    drive       TEXT NOT NULL,"
                "    description TEXT,"
                "    status      TEXT NOT NULL DEFAULT 'pending',"
                "    last_error  TEXT,"
                "    job_id      TEXT,"
                "    created_at  TEXT DEFAULT (datetime('now')),"
                "    ran_at      TEXT);"
                "INSERT INTO drive_queue SELECT * FROM drive_queue_legacy;"
                "DROP TABLE drive_queue_legacy;"
                "CREATE INDEX IF NOT EXISTS idx_drive_queue_status ON drive_queue(status);"
                "CREATE INDEX IF NOT EXISTS idx_drive_queue_drive  ON drive_queue(drive);")
        # drive_queue.attempts: failed-run counter for the retry cap
        dq_cols = {r[1] for r in con.execute("PRAGMA table_info(drive_queue)")}
        if "attempts" not in dq_cols:
            con.execute("ALTER TABLE drive_queue ADD COLUMN attempts INTEGER DEFAULT 0")
        # indexes on migrated columns must come after the ALTERs above
        con.execute("CREATE INDEX IF NOT EXISTS idx_titles_wanted ON titles(wanted)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_titles_fav ON titles(favorite)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_titles_miniseries ON titles(is_miniseries)")


def init():
    """Full first-boot initialization: schema + default roots/settings."""
    ensure()
    # jobs still marked 'running' belong to a previous process that died:
    # no thread carries them anymore, so close them out — otherwise the UI
    # would poll them forever and their rows would block honest status
    with tx() as c:
        c.execute(
            "UPDATE jobs SET status='cancelled', "
            "error='interrupted: app was stopped or restarted', "
            "finished_at=COALESCE(finished_at, datetime('now')) "
            "WHERE status='running'"
        )
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
        c.execute("PRAGMA busy_timeout=30000")
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

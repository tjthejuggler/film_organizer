"""Filesystem scanner: walks library roots, groups video files into titles.

Grouping rules
--------------
* A video file whose own name carries SxxEyy belongs to a SERIES whose name
  comes from the nearest non-container ancestor directory (skipping pure
  "Season X" dirs).
* A plain video file (or a directory that looks like a release, i.e. has a
  year / quality tags / is a course) becomes a MOVIE (or course-series)
  anchored on that directory name; otherwise the file name itself is used.
* Watched markers: the nearest ancestor directory named (case/punct
  insensitive) watched / aWatched / aaWatched / aaWatchedd => watched.
  unwatched / aUnwatched / aaUnwatched => explicitly not watched.
  Nearest ancestor wins; these never cancel each other by substring.
"""
import os
import re
from datetime import datetime, timezone

from . import config, consolidate, db, notifications, parser
from .db import q, q1, tx

CONTAINER_NAMES = {
    "movies", "movie", "series", "tv", "tvshows", "tvseries", "shows",
    "films", "film", "videos", "video", "downloads", "download", "new",
    "aanew", "unsorted", "miniseries", "aaminiseries", "aaseries",
    "documentaries", "docs", "anime", "courses", "music", "other", "root",
    # Watch Next staging slots: never title anchors (same aa* convention
    # as aanew / aaseries)
    "aanextmovie", "aanextseries",
}

# multi-film pack folders: the folder names a COLLECTION, the files inside
# name the actual movies ("Austin Powers Trilogy 1997,1999,2002" holding
# "Austin Powers The Spy Who Shagged Me 1999 ..."). Never anchor a movie on
# the collection name when the file itself carries its own title+year.
COLLECTION_DIR_RE = re.compile(
    r"trilogy|quadrilogy|collection|anthology|boxset|box[\s._-]set|"
    r"movie[\s._-]pack|films?[\s._-]pack", re.I)

LEADING_EP_RE = re.compile(r"^\D{0,4}?(\d{1,3})\b")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_container(name: str) -> bool:
    return parser.norm_component(name) in CONTAINER_NAMES


def dedupe_key(kind: str, title: str, year) -> str:
    k = parser.normalize_key(title)
    return f"m:{k}:{year or 0}" if kind == "movie" else f"s:{k}"


def _anchor_for(path: str, root: str, pf: dict):
    """Return (anchor_dir|None, title, year, kind)."""
    d = os.path.dirname(path)
    root = os.path.abspath(root)
    while d == root or d.startswith(root + os.sep):
        name = os.path.basename(d)
        n = parser.norm_component(name)
        if d != root and name and not name.startswith("."):
            if n in parser.WATCHED_MARKERS or n in parser.UNWATCHED_MARKERS:
                pass  # marker dir: never an anchor, keep walking up
            elif not _is_container(name):
                pd = parser.parse_name(name)
                if pd["title"]:
                    if parser.is_pure_season_dir(name) is not None:
                        pass  # "Season 1" etc. - title lives above
                    elif pf["is_series"]:
                        return d, pd["title"], pd["year"], "series"
                    elif COLLECTION_DIR_RE.search(name) and pf["title"] and \
                            (pf["year"] or pf["has_meta"]):
                        break  # multi-film pack: the FILE names the movie
                    elif (pd["has_meta"] or pd["year"]
                          or parser.MASTERCLASS_RE.search(name)
                          or len(pd["title"].split()) >= 3):
                        kind = "series" if parser.MASTERCLASS_RE.search(name) else "movie"
                        return d, pd["title"], pd["year"], kind
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    kind = "series" if pf["is_series"] else "movie"
    return None, pf["title"], pf["year"], kind


def _created_ts(st: os.stat_result) -> float:
    """Best available creation timestamp. True birth time when the OS/FS
    provides it (st_birthtime), otherwise ctime (inode creation proxy)."""
    bt = getattr(st, "st_birthtime", None)
    return bt if bt else st.st_ctime


def _iter_videos(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in parser.VIDEO_EXTS:
                continue
            if parser.SAMPLE_RE.search(fn):
                continue
            # camera/phone default clip names ("20250113_150345", "IMG_1234")
            # are personal recordings, never catalogue titles — skipping them
            # here keeps backup-drive mishmash out of the library entirely
            if parser.is_camera_name(os.path.splitext(fn)[0]):
                continue
            yield os.path.join(dirpath, fn)


def _count_videos(root: str) -> int:
    return sum(1 for _ in _iter_videos(root))


def scan_roots(job_id: str, roots: list):
    """Scan the given root paths (full pass) and sync the database."""
    from .jobs import is_cancelled, log, update

    scan_started = _now()
    groups = {}  # dedupe_key -> group dict

    # user-corrected identities win over re-parsing: if a title row was
    # manually fixed (title_locked/kind_locked), files whose parsed title
    # matches it are attached to THAT row's key instead of a fresh one.
    # Without this, a rescan after a kind flip (e.g. Band of Brothers
    # movie->series) creates a duplicate row and orphans the enriched one.
    locked = {}
    for r in q("""SELECT dedupe_key, title, kind, year FROM titles
                  WHERE title_locked=1 OR kind_locked=1"""):
        locked[parser.normalize_key(r["title"])] = dict(r)
    if locked:
        from .jobs import log
        log(job_id, f"Protecting {len(locked)} user-locked title identit(y/ies)")

    # series alias map: every existing series row claims its LOOSE title
    # identity (leading article / trailing US-UK token / '&' spelling
    # folded away), so variant parses ("righteous.gemstones.s04" vs
    # "The Righteous Gemstones", "Euphoria US" vs "Euphoria") attach to
    # the SAME row instead of re-creating splits consolidation healed.
    # Matched rows win the alias; oldest row breaks ties.
    aliases = {}
    for r in q("""SELECT dedupe_key, title FROM titles WHERE kind='series'
                  ORDER BY CASE WHEN match_status='matched' THEN 0 ELSE 1 END, id"""):
        aliases.setdefault(parser.series_title_key(r["title"]), r["dedupe_key"])

    planned = []
    for root in roots:
        root = os.path.abspath(root)
        if not os.path.isdir(root):
            log(job_id, f"Root not accessible (unplugged drive?): {root}")
            continue
        planned.append(root)

    total = sum(_count_videos(r) for r in planned)
    update(job_id, total=total, message=f"Scanning {len(planned)} root(s)")
    log(job_id, f"Found {total} video file(s) under {len(planned)} root(s)")

    done = 0
    for root in planned:
        for path in _iter_videos(root):
            if is_cancelled(job_id):
                log(job_id, f"Scan cancelled after {done}/{total} file(s)")
                update(job_id, message=f"Scan cancelled after {done}/{total}")
                return len(groups), done
            done += 1
            if done % 25 == 0 or done == total:
                update(job_id, progress=done, message=f"Scanned {done}/{total}")

            try:
                st = os.stat(path)
                size, mtime = st.st_size, st.st_mtime
                created = _created_ts(st)
            except OSError:
                continue
            if size < config.MIN_VIDEO_BYTES:
                continue

            rel_root = os.path.dirname(path)[len(root):].lstrip(os.sep)
            stem = os.path.splitext(os.path.basename(path))[0]
            pf = parser.parse_name(stem)

            anchor_dir, title, year, kind = _anchor_for(path, root, pf)
            if not title:
                title = stem
            # a 'Miniseries' folder marks the series as a miniseries
            mini = 1 if (kind == "series" and parser.in_miniseries_folder(path)) else 0

            lk = locked.get(parser.normalize_key(title))
            if lk:
                gk = lk["dedupe_key"]
                kind = lk["kind"]
                year = lk["year"]
                title = lk["title"]
            else:
                gk = None  # computed after episode/season tweaks below

            season, episode = pf["season"], pf["episode"]
            if kind in ("series",) and episode is None:
                m = LEADING_EP_RE.match(stem)
                if m:
                    episode = int(m.group(1))
            if season is None and pf["is_series"]:
                season = 1

            # WATCHED LATCH: seeing a file in a watched-marker folder turns
            # the flag ON permanently. Seeing it elsewhere NEVER turns it
            # off — watched state is system-owned after the folder
            # "jumpstart" (manual toggle can still override via
            # watched_manual).
            watched = parser.watched_marker(path) is True

            if gk is None and kind == "series":
                gk = aliases.get(parser.series_title_key(title))
            if gk is None:
                gk = dedupe_key(kind, title, year)
            g = groups.get(gk)
            if g is None:
                g = groups[gk] = {
                    "key": gk, "title": title, "year": year, "kind": kind,
                    "watched": watched, "size": 0, "miniseries": mini,
                    "seasons": set(), "eps": set(), "files": [],
                }
            g["watched"] = g["watched"] or watched
            g["miniseries"] = g.get("miniseries", 0) or mini
            g["size"] += size
            if season is not None:
                g["seasons"].add(season)
            if episode is not None:
                g["eps"].add((season or 1, episode))
            # the containing release folder's creation time, when the file
            # actually lives inside a title-specific directory
            folder_created = None
            parent = os.path.dirname(path)
            if os.path.basename(parent) != os.path.basename(root):
                try:
                    folder_created = _created_ts(os.stat(parent))
                except OSError:
                    pass

            g["files"].append({
                "path": path, "root": root, "rel_root": rel_root,
                "size": size, "mtime": mtime, "created": created,
                "folder_created": folder_created,
                "season": season, "episode": episode, "watched": watched,
            })

    log(job_id, f"Grouped into {len(groups)} title(s); writing database...")

    with tx() as c:
        for g in groups.values():
            row = q1("SELECT id, watched_folder, watched_at, wanted, history FROM titles WHERE dedupe_key=?", (g["key"],))
            if row is None:
                c.execute(
                    """INSERT INTO titles(dedupe_key, kind, title, year, cataloged_at,
                       watched_folder, watched_at, last_seen, size_bytes, seasons,
                       episode_count, is_miniseries)
                       VALUES(?,?,?,?,?,0,NULL,?,0,NULL,0,?)""",
                    (g["key"], g["kind"], g["title"], g["year"], scan_started,
                     scan_started, g.get("miniseries", 0)),
                )
                tid = q1("SELECT id FROM titles WHERE dedupe_key=?", (g["key"],))["id"]
                was = wat = None
                was_wanted = 0
                was_history = 0
                had_files = False
            else:
                tid = row["id"]
                was, wat = row["watched_folder"], row["watched_at"]
                was_wanted = row["wanted"] or 0
                was_history = row["history"] or 0
                had_files = bool(q1(
                    "SELECT 1 FROM files WHERE title_id=? AND missing=0 LIMIT 1", (tid,)))

            for f in g["files"]:
                # EARLIEST of file/folder creation wins: a folder created
                # during a later copy (like The Last Frontier's folder being
                # stamped today) must never override the file's true
                # creation date; but an OLD folder still counts. mtime is a
                # last-resort candidate because copy tools that preserve it
                # (Windows Explorer, rsync -a) reveal the original date when
                # creation time was reset by the copy.
                fc, cc = f.get("folder_created"), f.get("created")
                candidates = [x for x in (fc, cc, f.get("mtime")) if x]
                eff_created = min(candidates) if candidates else None
                c.execute(
                    """INSERT INTO files(title_id, path, is_dir, size_bytes, mtime,
                       season, episode, watched_folder, missing, last_seen, created)
                       VALUES(?,?,0,?,?,?,?,?,0,?,?)
                       ON CONFLICT(path) DO UPDATE SET
                         title_id=excluded.title_id, size_bytes=excluded.size_bytes,
                         mtime=excluded.mtime, season=excluded.season,
                         episode=excluded.episode,
                         watched_folder=MAX(files.watched_folder, excluded.watched_folder),
                         missing=0, last_seen=excluded.last_seen,
                         created=excluded.created""",
                    (tid, f["path"], f["size"], f["mtime"], f["season"],
                     f["episode"], 1 if f["watched"] else 0, scan_started,
                     eff_created),
                )

            # Aggregate over ALL non-missing file rows — not just files seen
            # in this pass. Copies on unplugged drives keep missing=0, so a
            # title's size / episode count / watched flag never shrink just
            # because one of its drives is asleep.
            size, n_eps, n_seasons, fwatched, created = c.execute(
                """SELECT COALESCE(SUM(size_bytes),0),
                          COUNT(DISTINCT CASE WHEN episode IS NOT NULL
                                THEN COALESCE(season,1) || '-' || episode END),
                          COUNT(DISTINCT season),
                          COALESCE(MAX(watched_folder),0),
                          MIN(created)
                   FROM files WHERE title_id=? AND missing=0""",
                (tid,)).fetchone()
            # created_at = the oldest file/folder creation date of this
            # title on disk. Stays NULL while unknown (e.g. all files on an
            # unplugged drive); the UI falls back to the catalog date then.
            if created:
                c.execute(
                    "UPDATE titles SET created_at=datetime(?, 'unixepoch') WHERE id=?",
                    (created, tid))

            # title-level latch: MAX over files can only grow; a title that
            # was ever watched stays watched unless the user manually
            # overrides via watched_manual
            nw = 1 if ((was or 0) or (g["watched"] or 0) or fwatched) else 0
            new_wat = wat
            if nw and not was:
                new_wat = scan_started
                # watched via folder jumpstart during this scan -> queue the
                # "move to backup?" decision (no user is present at scan time)
                try:
                    notifications.notify_watched_backup(tid)
                except Exception:
                    pass  # never let a notification break a scan
            # A wishlist row that just gained its first files is ADOPTED:
            # the wanted flag flips off. A manually-wanted owned title keeps
            # its flag (had_files was already true before this scan).
            adopt = 1 if (was_wanted and not had_files and size > 0) else 0
            # A seen-history row that gained files is owned again: the
            # history flag clears but the watched state is kept (nw latch).
            owned_again = 1 if (was_history and size > 0) else 0
            c.execute(
                """UPDATE titles SET last_seen=?, size_bytes=?, seasons=?,
                   episode_count=?, watched_folder=?, watched_at=?,
                   is_miniseries=MAX(titles.is_miniseries, ?),
                   wanted=CASE WHEN ? THEN 0 ELSE wanted END,
                   history=CASE WHEN ? THEN 0 ELSE history END WHERE id=?""",
                (scan_started, size, n_seasons or None, n_eps, nw, new_wat,
                 g.get("miniseries", 0), adopt, owned_again, tid),
            )
            if adopt:
                from .jobs import log
                log(job_id, f"Wanted title now on disk: {g['title']} (wanted flag cleared)")

        # flag vanished files, but only under roots we actually scanned
        for root in planned:
            c.execute(
                "UPDATE files SET missing=1 WHERE missing=0 AND last_seen < ? AND (path = ? OR path LIKE ?)",
                (scan_started, root, root + os.sep + "%"),
            )
        # drop titles that no longer have any files — but keep WISHLIST rows:
        # a wanted title has no files by design until it is acquired.
        # history=1 rows are the seen log (watched titles we no longer own
        # a file of) — pure records, they must survive the cleanup too
        c.execute("DELETE FROM titles WHERE wanted=0 AND history=0 AND id NOT IN (SELECT DISTINCT title_id FROM files)")

    update(job_id, progress=total, message=f"Done: {len(groups)} titles, {done} files")
    log(job_id, "Scan complete.")
    # heal any splits the scan surfaced (tmdb-same / folded-key series
    # duplicates): runs AFTER the sync transaction so it sees final rows
    try:
        n = consolidate.run()
        if n:
            log(job_id, f"Consolidated {n} duplicate series row(s) into their show")
    except Exception:
        pass  # consolidation must never fail a scan
    return len(groups), done


def root_dirs_for_scan(root_ids=None):
    if root_ids:
        rows = q(f"SELECT path FROM roots WHERE enabled=1 AND id IN ({','.join('?' * len(root_ids))})", root_ids)
    else:
        rows = q("SELECT path FROM roots WHERE enabled=1")
    return [r["path"] for r in rows]

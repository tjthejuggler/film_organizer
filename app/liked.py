"""Liked-title secondary backups.

A favorite (liked) title lives in TWO backup places: its regular per-kind
destination (backup_movies_root / backup_series_root) plus an ADDITIONAL
liked-only drive (liked_movies_root / liked_series_root). This module keeps
that second copy in step:

* sync_liked_copy  — copy whatever is missing onto the liked drive
  (folder-aware: a release folder travels whole so Subs/, artwork and .nfo
  ride along; loose files take their same-stem sidecars with them)
* remove_liked_copy — drop the secondary copy again when the title is
  un-liked (only bytes under the liked root are touched; the regular backup
  and the title row survive)

Copies are cataloged like any other file row (unique path), and the copied
release folder becomes a managed root so later scans keep it fresh — the
same pattern the mover uses for its landing folders.
"""
import os
import shutil
from datetime import datetime, timezone

from . import db, fileops
from .db import q, q1, tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _roots_overlap(a: str, b: str) -> bool:
    """True when either path sits inside the other (same drive/folder or
    nested) — copying between such roots would duplicate bytes in place or
    let a 'removal' eat the regular backup."""
    a = os.path.abspath(a).rstrip(os.sep) + os.sep
    b = os.path.abspath(b).rstrip(os.sep) + os.sep
    return a.startswith(b) or b.startswith(a)


def liked_dest(title) -> str:
    """The liked root for this title's kind, or '' when unset / unsafe.

    Unsafe = overlapping the regular per-kind backup root (or the legacy
    drive): the second copy must be a genuinely different place, otherwise
    sync would be a no-op and removal could delete the only copy."""
    kind = title["kind"] if "kind" in title.keys() else \
        (q1("SELECT kind FROM titles WHERE id=?", (title["id"],)) or
         {"kind": "movie"})["kind"]
    dest = fileops.liked_root(kind)
    if not dest:
        return ""
    regular = fileops.regular_root(kind)
    if regular and _roots_overlap(dest, regular):
        return ""
    internal = db.settings_get("internal_root")
    if internal and _roots_overlap(dest, os.path.abspath(internal)):
        return ""  # the liked drive must not be the live library itself
    return dest


def _under(p: str, root: str) -> bool:
    return (os.path.abspath(p) + os.sep).startswith(
        os.path.abspath(root).rstrip(os.sep) + os.sep)


def sync_liked_copy(job_id: str = None, title_id: int = None, log=None):
    """Ensure the favorite's files exist under its liked root (idempotent).

    Copies every cataloged file that is missing there, whole release
    folders first. Registers each copied folder as a managed root and
    upserts file rows so the catalog knows the copies immediately.
    Returns the number of files copied (0 = nothing to do / already there).
    """
    from . import mover  # src_base: longest known root containing a path

    log = log or (lambda *_a, **_k: None)
    t = q1("SELECT * FROM titles WHERE id=?", (title_id,))
    if not t or not t["favorite"]:
        return 0
    dest_root = liked_dest(t)
    if not dest_root:
        return 0
    if not os.path.isdir(dest_root):
        raise ValueError(f"liked folder not accessible: {dest_root} — "
                         "is the drive plugged in?")

    files = q("SELECT * FROM files WHERE title_id=? AND missing=0", (title_id,))
    if not files:
        return 0

    # plan: dedicated release folders as units, leftovers file-by-file
    folders: dict = {}  # src folder -> [file rows]
    loose = []
    for f in files:
        src = os.path.abspath(f["path"])
        if _under(src, dest_root):
            continue  # already backed up on the liked drive
        base = mover.src_base(src)
        folder = fileops.dedicated_folder(src, base, title_id) if base else None
        if folder and os.path.isdir(folder):
            folders.setdefault(folder, []).append(f)
        else:
            loose.append(f)

    copied = 0

    def _upsert(f, new_path):
        with tx() as c:
            c.execute(
                """INSERT INTO files(title_id, path, is_dir, size_bytes, mtime,
                   season, episode, watched_folder, missing, last_seen, created)
                   VALUES(?,?,0,?,?,?,?,0,0,?,?)
                   ON CONFLICT(path) DO UPDATE SET
                     title_id=excluded.title_id, size_bytes=excluded.size_bytes,
                     mtime=excluded.mtime, season=excluded.season,
                     episode=excluded.episode, missing=0,
                     last_seen=excluded.last_seen""",
                (title_id, new_path, f["size_bytes"], f["mtime"], f["season"],
                 f["episode"], _now(), f["created"]))

    def _copy_sidecars(src, dest_dir):
        for sc in fileops.sidecar_paths(src):
            try:
                shutil.copy2(sc, os.path.join(dest_dir, os.path.basename(sc)))
            except OSError:
                pass  # losing a sidecar must never fail the backup

    for folder, fs in folders.items():
        base = mover.src_base(folder)
        rel = os.path.relpath(folder, base) if base else os.path.basename(folder)
        dest = os.path.join(dest_root, rel)
        if os.path.abspath(folder) == os.path.abspath(dest):
            continue
        if not os.path.exists(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copytree(folder, dest)
            log(f"Liked copy: folder {os.path.basename(folder)} -> {dest}")
            for f in fs:
                # keep the file's sub-path inside the release folder
                # (episodes often sit in Season N/ subfolders)
                _upsert(f, os.path.join(
                    dest, os.path.relpath(os.path.abspath(f["path"]), folder)))
            _register_root(dest)
            copied += len(fs)
        else:
            # folder exists: top up file-by-file (only what is missing)
            os.makedirs(dest, exist_ok=True)
            fresh = 0
            for f in fs:
                new_path = os.path.join(
                    dest, os.path.relpath(os.path.abspath(f["path"]), folder))
                if not os.path.exists(new_path):
                    os.makedirs(os.path.dirname(new_path), exist_ok=True)
                    shutil.copy2(f["path"], new_path)
                    _copy_sidecars(f["path"], os.path.dirname(new_path))
                    fresh += 1
                _upsert(f, new_path)
            if fresh:
                _register_root(dest)
            copied += fresh

    for f in loose:
        src = os.path.abspath(f["path"])
        base = mover.src_base(src)
        rel = os.path.relpath(src, base) if base else os.path.basename(src)
        dest = os.path.join(dest_root, rel)
        if os.path.abspath(src) == os.path.abspath(dest) or os.path.exists(dest):
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        _copy_sidecars(src, os.path.dirname(dest))
        _upsert(f, dest)
        copied += 1
    if copied:
        _register_root(dest_root)
        log(f"Liked backup complete: {copied} file(s) on {dest_root}")
    return copied


def _register_root(path: str):
    """Catalog the copied folder so scans cover the liked drive — one
    folder at a time, never the whole device (camera clips / photos on a
    backup drive must not flood the catalog)."""
    with tx() as c:
        c.execute("INSERT OR IGNORE INTO roots(path) VALUES(?)", (path,))


def remove_liked_copy(title_id: int):
    """Un-like cleanup: delete this title's files under its liked root.

    Folder-aware (Subs/, artwork, .nfo go with the release folder), row
    rows removed, title row kept. Safe when the liked root is unset or
    overlaps the regular backup (refuses rather than nuke real backups).
    Returns the number of file rows removed."""
    t = q1("SELECT * FROM titles WHERE id=?", (title_id,))
    if not t:
        return 0
    dest_root = liked_dest(t)
    if not dest_root:
        return 0
    doomed = [dict(f) for f in q(
        "SELECT * FROM files WHERE title_id=? AND missing=0", (title_id,))
        if _under(f["path"], dest_root) and os.path.exists(f["path"])]
    if not doomed:
        # also prune stale rows (drive was swapped / folder removed by hand)
        with tx() as c:
            c.execute(
                "DELETE FROM files WHERE title_id=? AND (path = ? OR path LIKE ?)",
                (title_id, dest_root, dest_root.rstrip(os.sep) + os.sep + "%"))
        return 0
    # folder-aware removal SCOPED to the liked root: whole release folders
    # go (Subs/, artwork, .nfo sidecars ride along), files shared with
    # other titles' folders go individually; emptied dirs are pruned up to
    # the liked root only — the regular backup is a different place and
    # can never be touched here
    folders: set = set()
    loose = []
    for f in doomed:
        folder = fileops.dedicated_folder(f["path"], dest_root, title_id)
        if folder:
            folders.add(folder)
        else:
            loose.append(f)
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)
        fileops.prune_dirs(os.path.dirname(folder), dest_root)
    for f in loose:
        if os.path.isdir(f["path"]):
            shutil.rmtree(f["path"], ignore_errors=True)
        else:
            try:
                os.remove(f["path"])
            except OSError:
                pass
        fileops.prune_dirs(os.path.dirname(f["path"]), dest_root)
    with tx() as c:
        for f in doomed:
            c.execute("DELETE FROM files WHERE id=?", (f["id"],))
        c.execute(
            "UPDATE titles SET size_bytes=(SELECT COALESCE(SUM(size_bytes),0) "
            "FROM files WHERE title_id=? AND missing=0) WHERE id=?",
            (title_id, title_id))
    return len(doomed)

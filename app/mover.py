"""Move a title's files between the configured internal/external folders.

Preserves each file's path relative to its current base, updates the DB in
place (so locations are correct immediately), prunes emptied source
directories, and recomputes watched-folder flags for the new paths.

Folder-aware: when a title's files sit inside a folder that (per the
catalog) belongs exclusively to that title — a release folder that also
holds Subs/, artwork, .nfo … — the WHOLE folder is moved so sidecar files
are never left behind. Files directly in a shared root move individually,
taking matching same-stem sidecar files (.srt, .nfo, …) along.
"""
import os
import shutil
from datetime import datetime, timezone

from . import db, fileops, parser
from .db import q, q1, tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_bases() -> list:
    bases = [
        db.settings_get("internal_root"),
        db.settings_get("external_root"),
    ]
    bases += [r["path"] for r in q("SELECT path FROM roots WHERE enabled=1")]
    return [os.path.abspath(b) for b in bases if b]


def src_base(path: str):
    """Longest configured base (root setting or library root) containing path."""
    path = os.path.abspath(path)
    cands = [b for b in _candidate_bases()
             if path == b or path.startswith(b.rstrip(os.sep) + os.sep)]
    return max(cands, key=len) if cands else None


def move_title(job_id: str, title_id: int, target: str):
    from .jobs import log, update

    title = q1("SELECT * FROM titles WHERE id=?", (title_id,))
    if not title:
        raise ValueError("title not found")
    dest_base = db.settings_get(f"{target}_root")
    if not dest_base:
        raise ValueError(f"No {target} folder configured — set it in Settings first")
    dest_base = os.path.abspath(dest_base)
    if not os.path.isdir(dest_base):
        raise ValueError(
            f"{target} folder not accessible: {dest_base} — is the drive plugged in?"
        )

    files = q("SELECT * FROM files WHERE title_id=? AND missing=0", (title_id,))
    if not files:
        raise ValueError("No accessible files to move for this title")

    # --- plan: dedicated release folders as units, leftovers file-by-file ----
    folders: dict = {}  # (folder, base) -> [file rows]
    loose = []          # rows without a dedicated folder (or on dead roots)
    for f in files:
        src = os.path.abspath(f["path"])
        base = src_base(src)
        folder = fileops.dedicated_folder(src, base, title_id)
        if folder and os.path.isdir(folder):
            folders.setdefault((folder, base), []).append(f)
        else:
            loose.append(f)

    total = sum(len(fs) for fs in folders.values()) + len(loose)
    update(job_id, total=total, message=f"Moving {total} file(s) to {target}")
    log(job_id, f"Moving '{title['title']}' -> {dest_base}")

    moved = skipped = 0
    done = 0

    def _tick(n=1):
        nonlocal done
        done += n
        update(job_id, progress=done, message=f"Moving {done}/{total}")

    def _relocate(fs, new_path_of):
        """Point the catalog rows in `fs` at their new paths (keeps created)."""
        with tx() as c:
            for f in fs:
                c.execute(
                    "UPDATE files SET path=?, created=COALESCE(created, ?) WHERE id=?",
                    (new_path_of(f), f["created"], f["id"]))

    def _move_one(f) -> None:
        """Move a single video file plus its same-stem sidecar files."""
        nonlocal moved, skipped
        src = os.path.abspath(f["path"])
        base = src_base(src)
        if not base:
            log(job_id, f"SKIP (outside all known roots): {src}")
            skipped += 1
            return
        rel = os.path.relpath(src, base)
        dest = os.path.join(dest_base, rel)
        if dest == src:
            skipped += 1
            return
        if os.path.exists(dest):
            log(job_id, f"SKIP (destination exists): {dest}")
            skipped += 1
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        for sc in fileops.sidecar_paths(src):
            try:
                shutil.move(sc, os.path.join(os.path.dirname(dest),
                                             os.path.basename(sc)))
            except OSError:
                pass  # losing a sidecar must never fail the move
        _relocate([f], lambda _f: dest)
        fileops.prune_dirs(os.path.dirname(src), base)
        moved += 1

    for (folder, base), fs in folders.items():
        rel = os.path.relpath(folder, base)
        dest = os.path.join(dest_base, rel)
        if dest == folder:
            skipped += len(fs)  # already lives at the destination
        elif os.path.exists(dest):
            # destination folder already exists: move files one by one
            # (skipping duplicates) instead of blindly merging the folders
            log(job_id, f"Destination exists — file-by-file fallback: {dest}")
            for f in fs:
                _move_one(f)
                _tick()
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(folder, dest)
            log(job_id, f"Moved folder (Subs etc. ride along): "
                        f"{os.path.basename(folder)} -> {dest}")
            _relocate(fs, lambda f: os.path.join(
                dest, os.path.relpath(os.path.abspath(f["path"]), folder)))
            fileops.prune_dirs(os.path.dirname(folder), base)
            moved += len(fs)
            _tick(len(fs))

    for f in loose:
        _move_one(f)
        _tick()

    # watched LATCH: moving INTO a watched-marker folder turns the flag on;
    # moving out never turns it off (watched state is system-owned after
    # the folder jumpstart)
    rows = q("SELECT id, path FROM files WHERE title_id=?", (title_id,))
    any_watched = False
    with tx() as c:
        for r in rows:
            w = parser.watched_marker(r["path"]) is True
            any_watched = any_watched or w
            if w:
                c.execute("UPDATE files SET watched_folder=1 WHERE id=?", (r["id"],))
        if any_watched:
            c.execute(
                "UPDATE titles SET watched_folder=1, "
                "watched_at=COALESCE(watched_at, ?) WHERE id=?",
                (_now(), title_id))

    # the destination becomes a managed root so scans cover it and
    # locations stay accurate (idempotent)
    if moved:
        with tx() as c:
            c.execute("INSERT OR IGNORE INTO roots(path) VALUES(?)", (dest_base,))

    log(job_id, f"Move complete: {moved} moved, {skipped} skipped")
    update(job_id, message=f"Moved {moved} file(s), {skipped} skipped")

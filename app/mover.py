"""Move a title's files between the configured internal/external folders.

Preserves each file's path relative to its current base, updates the DB in
place (so locations are correct immediately), prunes emptied source
directories, and recomputes watched-folder flags for the new paths.
"""
import os
import shutil
from datetime import datetime, timezone

from . import db, parser
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

    update(job_id, total=len(files), message=f"Moving {len(files)} file(s) to {target}")
    log(job_id, f"Moving '{title['title']}' -> {dest_base}")

    moved = skipped = 0
    for i, f in enumerate(files, 1):
        src = os.path.abspath(f["path"])
        base = src_base(src)
        if not base:
            log(job_id, f"SKIP (outside all known roots): {src}")
            skipped += 1
            continue
        rel = os.path.relpath(src, base)
        dest = os.path.join(dest_base, rel)
        if dest == src:
            skipped += 1
            continue
        if os.path.exists(dest):
            log(job_id, f"SKIP (destination exists): {dest}")
            skipped += 1
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        moved += 1
        with tx() as c:
            # keep the file's recorded creation time across the move
            c.execute("UPDATE files SET path=?, created=COALESCE(created, ?) WHERE id=?",
                      (dest, f.get("created"), f["id"]))
        # prune now-empty source directories up to the base
        d = os.path.dirname(src)
        while os.path.abspath(d) != os.path.abspath(base):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)
        update(job_id, progress=i, message=f"Moved {i}/{len(files)}")

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

"""Watch Next: pin a movie / series as the one queued up to watch.

Setting a pin COPIES the title's accessible files into

    <internal_root>/aaNext_Movie    (movies)
    <internal_root>/aaNext_Series   (series)

with each file's path (relative to its source base) preserved. Whatever
previously occupied the slot is wiped — files on disk AND their catalog
rows. Pinned titles stick to the top of the catalog list regardless of
the active sort (see main.list_titles).
"""
import os
import shutil
from datetime import datetime, timezone

from . import config, db, mover, parser
from .db import q, q1, tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slot_dir(kind: str) -> str:
    """Dedicated Watch Next folder for `kind` inside the internal root.
    Raises ValueError when the internal folder is unset or unreachable."""
    internal = db.settings_get("internal_root")
    if not internal:
        raise ValueError("No internal folder configured — set it in Settings first")
    internal = os.path.abspath(internal)
    if not os.path.isdir(internal):
        raise ValueError(
            f"Internal folder not accessible: {internal} — is the drive plugged in?")
    return os.path.join(internal, config.WATCH_NEXT_DIRS[kind])


def pinned_id(kind: str):
    r = q1("SELECT id FROM titles WHERE watch_next=?", (kind,))
    return r["id"] if r else None


def offline_roots(title_id: int) -> list:
    """Configured bases of the title's live files that are not mounted."""
    offline, seen = [], set()
    for f in q("SELECT path FROM files WHERE title_id=? AND missing=0",
               (title_id,)):
        base = mover.src_base(f["path"])
        if base and base not in seen:
            seen.add(base)
            if not os.path.isdir(base):
                offline.append(base)
    return offline


def set_next(job_id: str, title_id: int):
    """Copy `title_id` into its Watch Next slot, replacing any old pin."""
    from .jobs import log, update

    title = q1("SELECT * FROM titles WHERE id=?", (title_id,))
    if not title:
        raise ValueError("title not found")
    kind = title["kind"]
    if kind not in config.WATCH_NEXT_DIRS:
        raise ValueError("only movies and series can be set as Watch Next")

    slot = slot_dir(kind)
    files = q("SELECT * FROM files WHERE title_id=? AND missing=0", (title_id,))
    if not files:
        raise ValueError("No accessible files for this title")

    # copy plan: every live file not already inside the slot
    plan = []
    for f in files:
        src = os.path.abspath(f["path"])
        if src.startswith(slot.rstrip(os.sep) + os.sep):
            continue  # already the Watch Next copy
        base = mover.src_base(src)
        if not base:
            continue  # outside every known root — nothing to copy from
        plan.append((f, src, os.path.relpath(src, base)))

    update(job_id, total=len(plan),
           message=f"Preparing Watch Next {kind}")
    log(job_id, f"Watch Next '{title['title']}' -> {slot}")

    if plan:
        # wipe whatever the previous pin left in the slot: catalog rows
        # first, then the bytes (slot is dedicated to this purpose)
        old_rows = q("SELECT id FROM files WHERE path LIKE ?",
                     (slot + os.sep + "%",))
        if old_rows:
            with tx() as c:
                c.executemany("DELETE FROM files WHERE id=?",
                              [(r["id"],) for r in old_rows])
        shutil.rmtree(slot, ignore_errors=True)
        os.makedirs(slot, exist_ok=True)

        copied = 0
        for i, (f, src, rel) in enumerate(plan, 1):
            dest = os.path.join(slot, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
            with tx() as c:
                c.execute(
                    """INSERT OR IGNORE INTO files(
                           title_id, path, is_dir, size_bytes, mtime,
                           season, episode, watched_folder, missing,
                           last_seen, created)
                       VALUES(?,?,0,?,?,?,?,?,?,0,?)""",
                    (title_id, dest, f["size_bytes"], f["mtime"],
                     f["season"], f["episode"],
                     1 if parser.watched_marker(dest) is True else 0,
                     _now(), f["created"]))
            copied += 1
            update(job_id, progress=i, message=f"Copied {i}/{len(plan)}")
        log(job_id, f"Copied {copied} file(s) into the slot")
    else:
        log(job_id, "Files already live in the slot — nothing to copy")

    # one pinned movie + one pinned series at a time
    with tx() as c:
        c.execute("UPDATE titles SET watch_next=NULL WHERE watch_next=? AND id!=?",
                  (kind, title_id))
        c.execute("UPDATE titles SET watch_next=? WHERE id=?", (kind, title_id))
        c.execute(
            """UPDATE titles SET size_bytes=(
                   SELECT COALESCE(SUM(size_bytes),0) FROM files
                   WHERE title_id=titles.id AND missing=0)
               WHERE id=?""",
            (title_id,))

    log(job_id, f"'{title['title']}' is now the Watch Next {kind}")
    update(job_id, message="Watch Next set")


def clear_next(title_id: int):
    """Unpin a title; the copy already in the slot is left on disk
    (it stays catalogued and can be removed via Duplicates)."""
    row = q1("SELECT id, watch_next FROM titles WHERE id=?", (title_id,))
    if not row or not row["watch_next"]:
        return
    with tx() as c:
        c.execute("UPDATE titles SET watch_next=NULL WHERE id=?", (title_id,))

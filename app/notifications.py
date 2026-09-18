"""Server-persisted notifications: things the user should decide later.

The watched→backup flow: when a title that has files on THIS machine gets
marked watched (manually, by an external program via /api/watched, or by
the watched-folder scanner latch), a 'pending' notification row is created.
The web app shows it as an actionable bell entry whenever the user is
back — accept (move to backup drive) or reject (leave it where it is).

Notification creation is deduplicated: only ONE pending notification per
(title, type) can exist. A decided notification is never resurrected —
the user already answered; if the title's watched state changes again in
the future, only the user's manual action can create a new one.
"""
import os
import threading
from datetime import datetime, timezone

from . import db, fileops, jobs, mover
from .db import q, q1, tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lives_only_on_backup(title_id: int) -> bool:
    """True when every non-missing file already sits under one of the
    title's backup destinations (regular per-kind root or any liked root)
    — nothing to back up, so no notification."""
    t = q1("SELECT kind FROM titles WHERE id=?", (title_id,))
    if not t:
        return True
    roots = [r for r in (fileops.regular_root(t["kind"]),
                         fileops.liked_root(t["kind"]),
                         fileops.backup_root()) if r]
    if not roots:
        return False
    prefixed = [os.path.abspath(r).rstrip(os.sep) + os.sep for r in roots]
    for f in q("SELECT path FROM files WHERE title_id=? AND missing=0",
               (title_id,)):
        p = os.path.abspath(f["path"]) + os.sep
        if not any(p.startswith(x) for x in prefixed):
            return False
    return True


def notify_watched_backup(title_id: int):
    """Create a 'move to backup?' notification for a watched title that has
    accessible files locally. Deduplicated per (title, type); a previously
    decided notification for the same pair is never resurrected."""
    title = q1("SELECT id, title, kind FROM titles WHERE id=?", (title_id,))
    if not title:
        return None
    # only titles with files actually present on THIS machine are candidates
    if not q1("SELECT 1 FROM files WHERE title_id=? AND missing=0 LIMIT 1",
              (title_id,)):
        return None
    # already fully on the backup drive -> nothing to move
    if _lives_only_on_backup(title_id):
        return None
    with tx() as c:
        c.execute(
            """INSERT INTO notifications(title_id, type, status, message, created_at)
               SELECT ?, 'watched_backup', 'pending',
                      'Watched — move to backup?',
                      datetime('now')
               WHERE NOT EXISTS (
                   SELECT 1 FROM notifications
                   WHERE title_id=? AND type='watched_backup'
                     AND status IN ('pending','queued','done','rejected'))""",
            (title_id, title_id),
        )
    return q1("""SELECT id FROM notifications WHERE title_id=? AND type='watched_backup'
                 AND status='pending' ORDER BY id DESC LIMIT 1""", (title_id,))


def payload(n) -> dict:
    """API shape for one notification row, joined with its title."""
    t = q1("SELECT id, title, kind, year, poster FROM titles WHERE id=?",
           (n["title_id"],))
    return {
        "id": n["id"],
        "title_id": n["title_id"],
        "type": n["type"],
        "status": n["status"],
        "message": n["message"],
        "created_at": n["created_at"],
        "decided_at": n["decided_at"],
        "title": dict(t) if t else None,
    }


def list_notifications(status: str = "pending", limit: int = 50) -> list:
    if status == "all":
        rows = q("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))
    else:
        rows = q("SELECT * FROM notifications WHERE status=? ORDER BY id DESC LIMIT ?",
                 (status, limit))
    return [payload(r) for r in rows]


def pending_count() -> int:
    return q1("SELECT COUNT(*) n FROM notifications WHERE status='pending'")["n"]


def _offline_backup_roots(title_id: int) -> set:
    """Which of this title's backup destinations are currently not mounted.
    The move target is the title's regular per-kind backup root; a liked
    title also needs its liked drive. Empty set = everything accessible."""
    t = q1("SELECT kind FROM titles WHERE id=?", (title_id,))
    if not t:
        return set()
    roots = {r for r in (fileops.regular_root(t["kind"]),
                         fileops.liked_root(t["kind"])) if r}
    # dedupe nested roots (keep the longest): a liked folder inside the
    # regular drive would otherwise name the same device twice
    roots = {r for r in roots
             if not any(r != o and r.startswith(o.rstrip(os.sep) + os.sep)
                        for o in roots)}
    return {r for r in roots if not os.path.isdir(r)}


def decide(notification_id: int, decision: str, queue_when_offline: bool = True):
    """Apply the user's verdict: 'accept' (move to backup) or 'reject'.

    Accepting starts the move job synchronously via the mover; when the
    backup drive is not plugged in the move is put into the persistent
    drive queue and runs automatically the moment the drive is connected
    (queue_when_offline=False restores the legacy 'stay pending' reply)."""
    n = q1("SELECT * FROM notifications WHERE id=?", (notification_id,))
    if not n:
        raise ValueError("notification not found")
    if n["status"] != "pending":
        raise ValueError(f"notification already decided ({n['status']})")

    if decision == "reject":
        with tx() as c:
            c.execute(
                "UPDATE notifications SET status='rejected', decided_at=? WHERE id=?",
                (_now(), notification_id))
        return {"ok": True, "status": "rejected"}

    if decision != "accept":
        raise ValueError("decision must be 'accept' or 'reject'")

    # drive check BEFORE any bytes move: tell the user which drive to plug
    # in (a liked title needs BOTH its regular backup drive and its liked
    # drive to complete the two-place backup)
    offline = _offline_backup_roots(n["title_id"])
    if offline:
        from . import drivequeue
        if queue_when_offline:
            trow = q1("SELECT title FROM titles WHERE id=?", (n["title_id"],))
            tname = trow["title"] if trow else f"#{n['title_id']}"
            drives = sorted(offline)
            qid, created = drivequeue.enqueue(
                "move", n["title_id"], drives[0],
                {"target": "external", "notification_id": notification_id,
                 "drives": drives},
                f"Move '{tname}' to the backup drive")
            if created:
                with tx() as c:
                    c.execute(
                        "UPDATE notifications SET status='queued', decided_at=? "
                        "WHERE id=?", (_now(), notification_id))
            return {"ok": True, "status": "queued",
                    "queued": created,
                    "queue_id": qid,
                    "missing_drives": drives}
        return {
            "ok": False,
            "status": "pending",
            "error": ("Backup drive not plugged in — connect: "
                      + ", ".join(sorted(offline))),
            "missing_drives": sorted(offline),
        }

    # Run the move in a BACKGROUND job: the old synchronous version held the
    # HTTP request open for minutes with no UI feedback, so the click looked
    # dead (and impatient re-clicks started several racing move jobs).
    jid = jobs.create("move", total=0)
    jobs.log(jid, f"Move to backup (notification #{notification_id})")
    with tx() as c:
        c.execute(
            "UPDATE notifications SET status='queued', decided_at=? WHERE id=?",
            (_now(), notification_id))

    def _run(job: str):
        error = None
        try:
            mover.move_title(job, n["title_id"], "external")
            # favorite = two backup places: after the regular move lands,
            # top up the liked-drive copy (skipped quietly when no liked
            # folder is configured for this kind)
            from . import liked
            liked.sync_liked_copy(title_id=n["title_id"],
                                  log=lambda msg: jobs.log(job, msg))
        except Exception as e:
            jobs.log(job, f"FAILED: {e}")
            error = str(e)
        jobs.finish(job, error=error)
        with tx() as c:
            if error:
                # back to pending so the bell offers the retry
                c.execute(
                    "UPDATE notifications SET status='pending' WHERE id=? "
                    "AND status='queued'", (notification_id,))
            else:
                c.execute(
                    "UPDATE notifications SET status='done', "
                    "decided_at=COALESCE(decided_at, ?) WHERE id=? "
                    "AND status='queued'", (_now(), notification_id))

    threading.Thread(target=_run, args=(jid,), daemon=True).start()
    return {"ok": True, "status": "started", "job_id": jid}

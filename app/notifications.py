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
from datetime import datetime, timezone

from . import db, mover
from .db import q, q1, tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lives_only_on_backup(title_id: int) -> bool:
    """True when every non-missing file already sits under external_root —
    nothing to back up, so no notification."""
    ext = db.settings_get("external_root")
    if not ext:
        return False
    ext = os.path.abspath(ext).rstrip(os.sep) + os.sep
    for f in q("SELECT path FROM files WHERE title_id=? AND missing=0",
               (title_id,)):
        p = os.path.abspath(f["path"])
        if not (p + os.sep).startswith(ext):
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
                     AND status IN ('pending','done','rejected'))""",
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


def _offline_backup_roots() -> set:
    """Which roots the backup would need that are currently not mounted.
    The move target is the configured external_root; also treat every
    enabled library root that is not accessible as unavailable."""
    ext = db.settings_get("external_root")
    if not ext:
        return set()
    ext = os.path.abspath(ext)
    return set() if os.path.isdir(ext) else {ext}


def decide(notification_id: int, decision: str):
    """Apply the user's verdict: 'accept' (move to backup) or 'reject'.

    Accepting starts the move job synchronously via the mover; when the
    backup drive is not plugged in the notification STAYS pending and the
    error names the missing drive path so the UI can tell the user exactly
    what to plug in."""
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

    # drive check BEFORE any bytes move: tell the user which drive to plug in
    offline = _offline_backup_roots()
    if offline:
        return {
            "ok": False,
            "status": "pending",
            "error": ("Backup drive not plugged in — connect: "
                      + ", ".join(sorted(offline))),
            "missing_drives": sorted(offline),
        }

    ext = db.settings_get("external_root")
    try:
        from . import jobs
        jid = jobs.create("move", total=0)
        mover.move_title(jid, n["title_id"], "external")
        jobs.finish(jid)
    except Exception as e:
        # keep the notification pending: the user can retry after fixing
        return {"ok": False, "status": "pending", "error": str(e)}

    with tx() as c:
        c.execute(
            "UPDATE notifications SET status='done', decided_at=? WHERE id=?",
            (_now(), notification_id))
    return {"ok": True, "status": "done"}

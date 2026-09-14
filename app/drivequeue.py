"""Persistent per-drive operation queue.

Whenever an operation needs a drive that is not connected right now (moving
to the external folder, deleting files, deleting a duplicate copy, accepting
a watched→backup notification), the operation is stored in the drive_queue
table instead of failing. A background worker polls every few seconds and
runs each pending entry automatically as soon as its drive is mounted —
no user action required beyond plugging the drive in.

Entry kinds and their payloads (JSON):
  move        {"target": "internal"|"external", "notification_id"?: int}
  delete      {"keep_record": bool, "drives": [root, ...]}
  delete_copy {"root": str, "drives": [root, ...]}

`drive` is the PRIMARY drive the entry waits for (used for grouping in the
Settings UI); `payload.drives`, when present, lists ALL drives that must be
connected before the entry may run.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from . import db, jobs
from .db import q, q1, tx

# Only one queue runner at a time; the SSE hook and the poller may race.
_run_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---- enqueue ---------------------------------------------------------------

def enqueue(kind: str, title_id: int, drive: str, payload: dict,
            description: str) -> tuple:
    """Store a pending operation. Returns (queue_id, created). Deduplicated:
    an identical pending entry (same kind/title/drive) is never doubled."""
    with tx() as c:
        dup = c.execute(
            "SELECT id FROM drive_queue "
            "WHERE status='pending' AND kind=? AND title_id=? AND drive=?",
            (kind, title_id, drive)).fetchone()
        if dup:
            return dup["id"], False
        cur = c.execute(
            "INSERT INTO drive_queue(kind,title_id,payload,drive,description) "
            "VALUES(?,?,?,?,?)",
            (kind, title_id, json.dumps(payload or {}), drive, description))
        qid = cur.lastrowid
    return qid, True


def cancel(qid: int):
    """Remove a pending entry before its drive gets connected."""
    row = q1("SELECT status FROM drive_queue WHERE id=?", (qid,))
    if not row:
        raise ValueError("queue entry not found")
    if row["status"] != "pending":
        raise ValueError(f"cannot cancel — entry is {row['status']}")
    with tx() as c:
        c.execute("UPDATE drive_queue SET status='cancelled', ran_at=? WHERE id=?",
                  (_now(), qid))


# ---- listing ---------------------------------------------------------------

def list_pending() -> list:
    """Pending entries grouped by drive, for the Settings UI."""
    rows = q(
        """SELECT d.id, d.kind, d.title_id, d.drive, d.description, d.created_at,
                  t.title AS title_name
           FROM drive_queue d LEFT JOIN titles t ON t.id = d.title_id
           WHERE d.status='pending' ORDER BY d.drive, d.id""")
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(r["drive"], {
            "drive": r["drive"], "mounted": os.path.isdir(r["drive"]),
            "items": []})
        g["items"].append({
            "id": r["id"], "kind": r["kind"], "title_id": r["title_id"],
            "title": r["title_name"], "description": r["description"],
            "created_at": r["created_at"]})
    return [groups[k] for k in sorted(groups)]


def pending_count() -> int:
    return q1("SELECT COUNT(*) n FROM drive_queue WHERE status='pending'")["n"]


# ---- execution ---------------------------------------------------------------

def _entry_drives(entry, payload: dict) -> list:
    return payload.get("drives") or [entry["drive"]]


def _ready(entry, payload: dict) -> bool:
    return all(os.path.isdir(d) for d in _entry_drives(entry, payload))


def run_due() -> list:
    """Run every pending entry whose drive(s) are mounted. Sequential and
    guarded: overlapping callers (poller thread + SSE kick) simply skip."""
    if not _run_lock.acquire(blocking=False):
        return []
    started = []
    try:
        for entry in q("SELECT * FROM drive_queue WHERE status='pending' ORDER BY id"):
            payload = json.loads(entry["payload"] or "{}")
            if not _ready(entry, payload):
                continue
            _execute(entry, payload)
            started.append(entry["id"])
    finally:
        _run_lock.release()
    return started


def worker_loop(interval: float = 3.0):
    """Background poller: drains the queue forever, so queued jobs run even
    with no browser open. Started as a daemon thread on app startup."""
    while True:
        try:
            run_due()
        except Exception:
            pass  # the queue must never take the app down
        time.sleep(interval)


def _execute(entry, payload: dict):
    from . import lut_sync
    with tx() as c:
        c.execute("UPDATE drive_queue SET status='running' WHERE id=?", (entry["id"],))
    jid = jobs.create(f"queue_{entry['kind']}", total=0)
    jobs.log(jid, f"Queued job #{entry['id']} auto-started (drive connected): "
                  f"{entry['description']}")
    error = None
    try:
        if entry["kind"] == "move":
            from . import mover
            mover.move_title(jid, entry["title_id"],
                             payload.get("target", "external"),
                             purge_others=bool(payload.get("purge_others")),
                             seasons=payload.get("seasons") or None)
        elif entry["kind"] == "delete":
            _run_delete(entry, payload, jid)
        elif entry["kind"] == "delete_copy":
            from . import duplicates
            duplicates.delete_copy(entry["title_id"], payload.get("root"))
        else:
            raise ValueError(f"unknown queue kind: {entry['kind']}")
    except Exception as e:
        error = str(e)
        jobs.log(jid, f"FAILED: {error}")
        jobs.finish(jid, error=error)
        with tx() as c:
            c.execute(
                "UPDATE drive_queue SET status='error', ran_at=?, last_error=?, "
                "job_id=? WHERE id=?", (_now(), error, jid, entry["id"]))
        return
    jobs.finish(jid)
    with tx() as c:
        c.execute("UPDATE drive_queue SET status='done', ran_at=?, job_id=? WHERE id=?",
                  (_now(), jid, entry["id"]))
    lut_sync.request_sync(f"queue_{entry['kind']}")  # drive plugged -> reachability changed
    # a queued backup-move completes its originating notification
    nid: Optional[int] = payload.get("notification_id")
    if nid:
        with tx() as c:
            c.execute("UPDATE notifications SET status='done', decided_at=? "
                      "WHERE id=? AND status='queued'", (_now(), nid))


def _run_delete(entry, payload: dict, jid: str):
    """Scoped variant of main.delete_title_files: deletes only the files
    under this entry's drive(s), then finalizes the title exactly like the
    synchronous path once nothing is left (also cleans missing-ghost rows)."""
    from . import duplicates

    tid = entry["title_id"]
    keep_record = bool(payload.get("keep_record"))
    drives = _entry_drives(entry, payload)
    for d in drives:
        if not os.path.isdir(d):
            raise ValueError(f"drive still not connected: {d}")

    roots = [r["path"] for r in q("SELECT path FROM roots ORDER BY length(path) DESC")]
    files = [dict(f) for f in q(
        "SELECT id, path, missing FROM files WHERE title_id=?", (tid,))]

    def root_of(p):
        for r in roots:
            if p == r or p.startswith(r.rstrip("/") + "/"):
                return r
        return None

    scope = [f for f in files if root_of(f["path"]) in drives]
    jobs.log(jid, f"Deleting {len(scope)} file(s) under {', '.join(drives)}")
    if scope:
        duplicates._delete_title_files(scope, tid, delete_row=False)

    # finalize only when every other offline-drive entry for this title is
    # done too — otherwise the last one to run wraps things up
    pending_same = q1(
        "SELECT COUNT(*) n FROM drive_queue "
        "WHERE status IN ('pending','running') AND kind='delete' "
        "AND title_id=? AND id != ?", (tid, entry["id"]))["n"]
    left = q1("SELECT COUNT(*) n FROM files WHERE title_id=?", (tid,))["n"]
    if left and pending_same:
        return  # files on other drives still queued — they will finish this

    with tx() as c:
        c.execute("DELETE FROM files WHERE title_id=?", (tid,))
        if keep_record:
            c.execute(
                "UPDATE titles SET history=1, size_bytes=0, episode_count=0, "
                "seasons=NULL, last_seen=? WHERE id=?", (_now(), tid))
        else:
            c.execute("DELETE FROM titles WHERE id=?", (tid,))

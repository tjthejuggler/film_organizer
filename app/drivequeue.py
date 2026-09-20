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

# Retry policy for failed entries: the worker re-runs them after a backoff
# so a freshly mounted drive can settle; past the cap the entry stays
# 'error' and is surfaced in the queue UI (with a cancel button).
MAX_ATTEMPTS = 5
RETRY_BACKOFF_S = 30


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
    if row["status"] not in ("pending", "error"):
        raise ValueError(f"cannot cancel — entry is {row['status']}")
    with tx() as c:
        c.execute("UPDATE drive_queue SET status='cancelled', ran_at=? WHERE id=?",
                  (_now(), qid))


# ---- listing ---------------------------------------------------------------

def list_pending() -> list:
    """Pending entries grouped by (primary) drive, for the Settings UI.

    Each item carries the FULL list of drives it waits for plus which of
    them are missing: an entry is gated on all of payload.drives, not just
    the group's primary drive — showing only 'connected' for the primary
    drive made the queue claim 'will run now' while it silently waited for
    a different, unplugged drive."""
    rows = q(
        """SELECT d.id, d.kind, d.title_id, d.drive, d.description, d.created_at,
                  d.status, d.last_error, d.payload, d.attempts,
                  t.title AS title_name
           FROM drive_queue d LEFT JOIN titles t ON t.id = d.title_id
           WHERE d.status IN ('pending','error') ORDER BY d.drive, d.id""")
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(r["drive"], {
            "drive": r["drive"], "mounted": os.path.isdir(r["drive"]),
            "items": []})
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = {}
        drives = payload.get("drives") or [r["drive"]]
        missing = [d for d in drives if not os.path.isdir(d)]
        # queued moves additionally need every drive their FILES sit on
        # (plus the liked-copy drive when backing up) — mirror the gate the
        # runner applies so the UI shows exactly what to plug in instead of
        # claiming "will run now" over a move that would instantly fail
        if r["kind"] == "move":
            missing += [b for b in _move_blockers(dict(r), payload)
                        if b not in missing]
        attempts = r["attempts"] or 0
        g["items"].append({
            "id": r["id"], "kind": r["kind"], "title_id": r["title_id"],
            "title": r["title_name"], "description": r["description"],
            "created_at": r["created_at"], "status": r["status"],
            "last_error": r["last_error"], "attempts": attempts,
            "exhausted": r["status"] == "error" and attempts >= MAX_ATTEMPTS,
            "drives": drives, "missing": missing, "ready": not missing})
    return [groups[k] for k in sorted(groups)]


def pending_count() -> int:
    """Pending + retryable-error entries (both show up in the queue UI)."""
    return q1("SELECT COUNT(*) n FROM drive_queue "
              "WHERE status IN ('pending','error')")["n"]


# ---- execution ---------------------------------------------------------------

def _entry_drives(entry, payload: dict) -> list:
    return payload.get("drives") or [entry["drive"]]


def _ready(entry, payload: dict) -> bool:
    return all(os.path.isdir(d) for d in _entry_drives(entry, payload))


def _drive_root_of(path: str):
    """Plug-able drive root containing path, or None for internal disk.

    Removable media on this system mount under /run/media/<user>/<LABEL>
    (udisks2), so a file there belongs to the 4th path component; that
    prefix answers 'is the drive plugged in?' even while unplugged (the
    mount point itself does not exist then). Paths anywhere else fall
    back to the shallowest existing mountpoint ancestor — '/' for the
    internal disk, which is always present and can never be 'plugged in'.
    """
    p = os.path.abspath(path)
    parts = p.split(os.sep)  # ['', 'run', 'media', '<user>', '<label>', ...]
    if len(parts) >= 5 and parts[1:3] == ["run", "media"]:
        return os.sep.join([""] + parts[1:5])
    probe = p
    while probe != os.path.dirname(probe):
        if os.path.ismount(probe):
            return probe
        probe = os.path.dirname(probe)
    return None


def _move_blockers(entry, payload: dict) -> list:
    """Extra readiness checks for queued MOVES beyond the destination
    gate (`_ready`). Returns human-readable blockers; empty = good to run.

    payload.drives lists only DESTINATION drives, so a move whose files
    sit on an unplugged drive used to fire the moment the destination
    connected — and burn its whole retry budget (5 fails in ~4 minutes)
    on 'system move failed or was cancelled'. Two holes, both closed:
      - source drives: every drive root holding the title's cataloged
        files must be mounted before the move may run;
      - the liked-copy drive: target=external tops the fresh backup up
        on the title's liked drive afterwards (liked.sync_liked_copy),
        so that drive must be mounted too or the move fails post-copy.
    """
    if entry["kind"] != "move":
        return []
    blocked: list = []
    for r in q("SELECT path FROM files WHERE title_id=?", (entry["title_id"],)):
        drv = _drive_root_of(r["path"])
        if drv and drv not in blocked and not os.path.isdir(drv):
            blocked.append(drv)
    # a dest_root override that is NOT under any backup root lands on
    # internal storage — its liked top-up step never runs
    from . import fileops
    dr = payload.get("dest_root")
    on_backup = False
    if dr:
        dr = os.path.abspath(dr)
        on_backup = any((dr + os.sep).startswith(r.rstrip(os.sep) + os.sep)
                        for r in fileops.all_backup_roots())
    t = q1("SELECT kind FROM titles WHERE id=?", (entry["title_id"],))
    if t and (on_backup or (not dr and
                            (payload.get("target") or "external") == "external")):
        lr = fileops.liked_root(t["kind"])
        if lr:
            lr = os.path.abspath(lr)
            if not os.path.isdir(lr) and lr not in blocked:
                blocked.append(lr + " (liked copy)")
    return blocked


def _hold_for_blockers(entry, blockers: list):
    """Park a move whose drive(s) are unplugged: status stays 'pending',
    NO attempt is burned, and last_error records what to plug in so the
    Settings queue says 'waiting for …' instead of a failure."""
    hint = "waiting for: " + ", ".join(blockers)
    if entry["last_error"] != hint:  # write once, not every poll
        with tx() as c:
            c.execute("UPDATE drive_queue SET last_error=? WHERE id=?",
                      (hint, entry["id"]))


def _retryable(entry) -> bool:
    """Failed entries get another chance after a short backoff, capped at
    MAX_ATTEMPTS — a just-plugged drive often needs a few seconds to
    settle, and one flaky poll must not dead-end the operation forever."""
    if entry["status"] != "error":
        return True
    if (entry["attempts"] or 0) >= MAX_ATTEMPTS:
        return False
    ran_at = entry["ran_at"]
    if not ran_at:
        return True
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(ran_at)
        return age.total_seconds() >= RETRY_BACKOFF_S
    except ValueError:
        return True


def run_due() -> list:
    """Run every pending (or retryable failed) entry whose drive(s) are
    mounted. Sequential and guarded: overlapping callers (poller thread +
    SSE kick) simply skip. Statuses are re-checked per entry because a
    previous entry's execution may have cancelled or completed this one."""
    if not _run_lock.acquire(blocking=False):
        return []
    started = []
    try:
        entries = [dict(e) for e in q(
            "SELECT * FROM drive_queue WHERE status IN ('pending','error') "
            "ORDER BY id")]
        for entry in entries:
            if not _retryable(entry):
                continue
            cur = q1("SELECT status FROM drive_queue WHERE id=?", (entry["id"],))
            if not cur or cur["status"] not in ("pending", "error"):
                continue  # finished/cancelled by an earlier entry's run
            payload = json.loads(entry["payload"] or "{}")
            if not _ready(entry, payload):
                continue
            blockers = _move_blockers(entry, payload)
            if blockers:
                _hold_for_blockers(entry, blockers)
                continue  # a needed drive is unplugged — do NOT burn attempts
            _execute(dict(entry), payload)
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
            from . import mover, fileops
            dr = payload.get("dest_root")
            target = payload.get("target", "external")
            mover.move_title(jid, entry["title_id"], target,
                             purge_others=bool(payload.get("purge_others")),
                             seasons=payload.get("seasons") or None,
                             dest_root=dr)
            # favorites keep a second copy on their liked drive: top it up
            # after the regular backup lands (no-op for non-liked titles,
            # kinds without a liked folder configured, or dest_root moves
            # landing on internal storage)
            tops_backup = target == "external"
            if dr:
                dr = os.path.abspath(dr)
                tops_backup = any(
                    (dr + os.sep).startswith(r.rstrip(os.sep) + os.sep)
                    for r in fileops.all_backup_roots())
            if tops_backup:
                from . import liked
                liked.sync_liked_copy(
                    title_id=entry["title_id"],
                    log=lambda m: jobs.log(jid, m))
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
        attempts = (entry["attempts"] or 0) + 1
        exhausted = attempts >= MAX_ATTEMPTS
        with tx() as c:
            c.execute(
                "UPDATE drive_queue SET status='error', ran_at=?, last_error=?, "
                "job_id=?, attempts=? WHERE id=?",
                (_now(), error, jid, attempts, entry["id"]))
        if not exhausted:
            jobs.log(jid, f"queued job #{entry['id']} failed — will retry "
                          f"(attempt {attempts}/{MAX_ATTEMPTS})")
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
    # full rows needed: _delete_title_files -> _folder_plan reads size_bytes
    # (folder-vs-catalog byte comparison); a starved SELECT here caused
    # KeyError 'size_bytes' and silently dead-ended every queued delete
    files = [dict(f) for f in q(
        "SELECT * FROM files WHERE title_id=?", (tid,))]

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
    # 'error' counts too: a retrying sibling's drive may not be done yet,
    # so this entry must not finalize (and erase the file rows) early
    pending_same = q1(
        "SELECT COUNT(*) n FROM drive_queue "
        "WHERE status IN ('pending','running','error') AND kind='delete' "
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


def repost_errors() -> int:
    """Boot-time data repair: entries stuck in 'error' go back to pending
    with a FULL attempt budget so the worker drains them. Reposting once per
    boot is deliberately unconditional: historical poisonings (moves that
    burned all 5 tries against an unplugged SOURCE drive before the
    source-aware gate existed) are indistinguishable from fresh failures,
    and the gate now keeps drive-blocked entries parked, so a fresh boot is
    the right moment for one more honest chance. Mid-session failures still
    park after MAX_ATTEMPTS — the worker never reposts while running."""
    with tx() as c:
        cur = c.execute(
            "UPDATE drive_queue SET status='pending', ran_at=NULL, attempts=0 "
            "WHERE status='error'")
        return cur.rowcount

"""Background job tracking (scan / enrich), with live log lines."""
import threading
import traceback
import uuid
from datetime import datetime, timezone

from . import db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(jtype: str, total: int = 0) -> str:
    jid = uuid.uuid4().hex[:12]
    with db.tx() as c:
        c.execute(
            "INSERT INTO jobs(id,type,status,total,created_at) VALUES(?,?,?,?,datetime('now'))",
            (jid, jtype, "running", total),
        )
    return jid


def update(jid: str, **fields):
    sets, vals = [], []
    for k, v in fields.items():
        if k in {"progress", "total", "message", "status", "error", "finished_at"}:
            sets.append(f"{k}=?")
            vals.append(v)
    if sets:
        vals.append(jid)
        with db.tx() as c:
            c.execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", vals)


def log(jid: str, msg: str):
    line = f"[{now_iso()}] {msg}"
    with db.tx() as c:
        c.execute(
            "UPDATE jobs SET log = COALESCE(log,'') || ? WHERE id=?", (line + "\n", jid)
        )


def finish(jid: str, error: str = None):
    status = "error" if error else "done"
    with db.tx() as c:
        c.execute(
            "UPDATE jobs SET status=?, error=?, finished_at=datetime('now') WHERE id=?",
            (status, error, jid),
        )


def run_background(jid: str, fn):
    def _wrap():
        try:
            fn(jid)
            finish(jid)
        except Exception as e:  # pragma: no cover
            log(jid, f"FATAL: {e}\n{traceback.format_exc()}")
            finish(jid, error=str(e))

    threading.Thread(target=_wrap, daemon=True).start()

"""Settings (masked secrets) and library roots management."""
import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, db, llm, tmdb
from .common import _secrets_masked

router = APIRouter()


class RootIn(BaseModel):
    path: str
    label: Optional[str] = None


class SettingsIn(BaseModel):
    values: dict


@router.get("/api/roots")
def list_roots():
    rows = db.q("SELECT * FROM roots ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        d["exists"] = os.path.isdir(r["path"])
        d["stats"] = dict(db.q1(
            """SELECT COUNT(DISTINCT t.id) titles,
                      SUM(t.size_bytes) bytes
               FROM titles t JOIN files f ON f.title_id=t.id
               WHERE f.path LIKE ? || '%'""", (r["path"].rstrip("/") + "/",)) or {})
        out.append(d)
    return {"roots": out}


@router.post("/api/roots")
def add_root(body: RootIn):
    p = os.path.abspath(os.path.expanduser(body.path))
    if not os.path.isdir(p):
        raise HTTPException(400, f"not a directory: {p}")
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO roots(path, label) VALUES(?,?)", (p, body.label))
    return {"ok": True, "path": p}


@router.delete("/api/roots/{rid}")
def delete_root(rid: int):
    with db.tx() as c:
        c.execute("DELETE FROM roots WHERE id=?", (rid,))
    return {"ok": True}


@router.post("/api/roots/{rid}/toggle")
def toggle_root(rid: int):
    with db.tx() as c:
        c.execute("UPDATE roots SET enabled = 1 - enabled WHERE id=?", (rid,))
    return {"ok": True}


# ---- settings -------------------------------------------------------------
@router.get("/api/settings")
def get_settings():
    return _secrets_masked(db.settings_all())


@router.post("/api/settings")
def save_settings(body: SettingsIn):
    for k, v in body.values.items():
        if k in config.SECRET_KEYS:
            if v is None or ("*" in str(v)):
                continue  # masked placeholder sent back: keep existing
        db.settings_set(k, str(v) if v is not None else "")
    return get_settings()


@router.post("/api/test/llm")
def test_llm():
    """Saves nothing; probes the STORED config. UI saves fields first."""
    ok, detail = llm.probe()
    return {"ok": ok, "detail": detail}


@router.post("/api/test/tmdb")
def test_tmdb():
    ok, detail = tmdb.probe()
    return {"ok": ok, "detail": detail}

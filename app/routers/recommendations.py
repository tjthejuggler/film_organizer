"""LLM film recommender API: queue status / next pick / decisions."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import db, llm, recommender

router = APIRouter()


class RecDecideIn(BaseModel):
    """Verdict on a recommendation: accept (-> wanted) or reject.
    note: free-text why they liked/disliked it (feeds future research).
    liked/seen: optional flags recorded with the decision."""
    decision: str            # "accepted" | "rejected"
    note: Optional[str] = None
    liked: Optional[bool] = None
    seen: bool = False


class RecPeekIn(BaseModel):
    count: int = 10


@router.get("/api/recommendations/status")
def rec_status():
    """Queue health for the header badge; also opportunistically triggers
    the background refill when the queue ran low."""
    pending = recommender.pending_count()
    refill_job = recommender.maybe_refill(f"served status (pending={pending})")
    running = db.q1(
        """SELECT id, status, message FROM jobs WHERE type='recommend'
           AND status='running' ORDER BY created_at DESC LIMIT 1""")
    return {
        "pending": pending,
        "enabled": recommender.enabled(),
        "low": pending <= recommender.QUEUE_LOW,
        "refill_job_id": refill_job,
        "running_job": dict(running) if running else None,
    }


@router.get("/api/recommendations/next")
def rec_next():
    """Serve the oldest pending recommendation (FIFO) — the popup card.
    Final catalog guard: any pending row that collides with a title the
    user already has (name / tmdb / imdb match) is purged on the way out,
    so a served card is always something NEW to them."""
    # lazy purge of pending rows that became 'known' after they were queued
    known = recommender._known_title_keys()
    for r in db.q("SELECT id, title, tmdb_id, imdb_id FROM recommendations "
                  "WHERE status='pending'"):
        if recommender._is_known(r["title"], tmdb_id=r["tmdb_id"],
                                 imdb_id=r["imdb_id"], known=known):
            with db.tx() as c:
                c.execute("DELETE FROM recommendations WHERE id=?", (r["id"],))
    row = db.q1(
        """SELECT * FROM recommendations WHERE status='pending'
           ORDER BY id LIMIT 1""")
    if not row:
        recommender.maybe_refill("queue empty")
        return {"recommendation": None, "pending": recommender.pending_count()}
    d = recommender.rec_payload(row)
    behind = db.q1(
        "SELECT COUNT(*) n FROM recommendations WHERE status='pending' AND id>?",
        (row["id"],))["n"]
    return {"recommendation": d, "pending": recommender.pending_count(),
            "remaining_behind": behind}


@router.post("/api/recommendations/refill")
def rec_refill():
    """Manual 'research now' trigger (also used automatically)."""
    if not recommender.enabled():
        raise HTTPException(400, "no LLM API key configured (Settings)")
    pending = recommender.pending_count()
    jid = recommender.maybe_refill(f"manual (pending={pending})")
    if jid:
        return {"ok": True, "job_id": jid, "pending": pending}
    if pending > recommender.QUEUE_LOW:
        return {"ok": True, "job_id": None,
                "detail": f"queue healthy ({pending} pending — no refill needed)"}
    raise HTTPException(409, "a research job is already running")


@router.get("/api/recommendations")
def rec_list(status: str = "pending", limit: int = 50):
    """Queue / decision history listing."""
    if status not in ("pending", "accepted", "rejected", "all"):
        raise HTTPException(400, "status must be pending|accepted|rejected|all")
    wsql = "" if status == "all" else " WHERE status=?"
    rows = db.q(f"SELECT * FROM recommendations{wsql} "
                "ORDER BY id DESC LIMIT ?", ((status,) if status != "all" else ()) + (limit,))
    return {"recommendations": [recommender.rec_payload(r) for r in rows]}


@router.post("/api/recommendations/{rid}/decide")
def rec_decide(rid: int, body: RecDecideIn):
    """Accept (-> wanted list) or reject. note/liked/seen are stored as
    feedback for future research batches. Reject + seen creates a seen-log
    row ('watched it, own no file'); accept + seen also marks it watched."""
    try:
        rec, title_id, created = recommender.decide(
            rid, body.decision, note=body.note, liked=body.liked, seen=body.seen)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # decisions shrink the queue — kick a refill when we crossed the line
    recommender.maybe_refill("after decision")
    return {"ok": True, "recommendation": recommender.rec_payload(rec),
            "title_id": title_id, "created": created}


@router.post("/api/test/recommender")
def test_recommender():
    """Dry probe: checks LLM key + that the MCP servers are attached. Does
    NOT run a research batch."""
    if not recommender.enabled():
        return {"ok": False, "detail": "no LLM API key configured"}
    ok, detail = llm.probe()
    return {"ok": ok, "detail": detail + " (researcher uses the same key for web-search-prime + web-reader MCP)"}

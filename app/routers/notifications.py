"""Notifications: watched -> move-to-backup decisions."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import notifications

router = APIRouter()


class NotificationDecisionIn(BaseModel):
    decision: str  # 'accept' | 'reject'


@router.get("/api/notifications")
def list_notifications(status: str = "pending", limit: int = 50):
    if status not in ("pending", "queued", "done", "rejected", "failed", "all"):
        raise HTTPException(
            400, "status must be pending|queued|done|rejected|failed|all")
    return {"notifications": notifications.list_notifications(status, limit),
            "pending": notifications.pending_count()}


@router.get("/api/notifications/count")
def notification_count():
    return {"pending": notifications.pending_count()}


@router.post("/api/notifications/{nid}/decide")
def decide_notification(nid: int, body: NotificationDecisionIn):
    """Accept = start a background job that moves the title's files to the
    backup drive (response carries the job_id); reject = leave them where
    they are. When the backup drive is offline the move goes into the
    persistent drive queue and runs the moment it is connected."""
    try:
        return notifications.decide(nid, body.decision)
    except ValueError as e:
        raise HTTPException(400, str(e))

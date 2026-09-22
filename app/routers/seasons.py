"""Season calendar API: upcoming seasons of watched series."""
from fastapi import APIRouter
from pydantic import BaseModel

from .. import jobs, seasons

router = APIRouter()


class SeasonSeenIn(BaseModel):
    seen: bool = True


@router.get("/api/seasons/calendar")
def seasons_calendar():
    """Everything the calendar popup shows: announced dates (with release
    pattern all-at-once vs weekly + the full date range), vague windows
    waiting for an exact date, and finished series. Entries changed since
    the last calendar open carry is_new=true."""
    return {"entries": seasons.calendar_entries()}


@router.post("/api/seasons/calendar/opened")
def seasons_calendar_opened():
    """Baseline for 'what is new': everything the calendar shows right now
    counts as looked-at; only later changes light the badge again."""
    return seasons.mark_calendar_opened()


@router.get("/api/seasons/recent")
def seasons_recent():
    """Catch-up list: seasons that started airing in the last weeks for
    series the user watches (or partly watched), latest season per show,
    until the season is marked seen."""
    return {"entries": seasons.recent_entries()}


@router.post("/api/seasons/{entry_id}/seen")
def seasons_seen(entry_id: int, body: SeasonSeenIn):
    """'I watched this season' — removes it from the recently-released
    list (seen=false puts it back)."""
    return seasons.mark_season_seen(entry_id, body.seen)


@router.post("/api/seasons/poll")
def seasons_poll():
    """Manual 'check now' — the full sweep (every watched series), run now."""
    jid = jobs.create("season_poll", total=0)
    jobs.log(jid, "manual season sweep requested")
    jobs.run_background(jid, seasons.sweep)
    return {"ok": True, "job_id": jid}

"""FastAPI application: composition root.

Route logic lives in `app/routers/` (one module per feature domain); this
file only creates the app, wires middleware + startup hooks, mounts the
routers and serves the static frontend.
"""
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, consolidate, db, drivequeue, imgcache, jobs, \
    pairing, recommender, seasons
from .routers import (duplicates as dup_routes, external, history,
                      jobs as job_routes, notifications,
                      pairing as pair_routes, recommendations,
                      seasons as season_routes, settings as settings_routes,
                      stats as stats_routes, storage, system, titles)

db.init()
pairing.ensure_schema()

app = FastAPI(title="Film Organizer", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.on_event("startup")
def _startup_refill():
    """Automatically top the recommendation queue back up when the app boots
    (silently skipped when no LLM key is configured or a job already runs)."""
    try:
        recommender.maybe_refill("startup")
    except Exception:
        pass  # never block boot over recommendations
    # series consolidation: fold duplicate rows of the SAME show (left over
    # from old parser bugs) before anything reads the catalog; cheap gate
    # makes this a no-op on a healthy library
    try:
        consolidate.run()
    except Exception:
        pass  # consolidation must never block boot
    # season calendar: seed once (web-researched facts), then poll weekly
    try:
        if not db.q1("SELECT 1 FROM season_watch LIMIT 1"):
            jid = jobs.create("season_poll", total=0)
            jobs.log(jid, "first boot: loading web-researched season seeds")
            threading.Thread(target=seasons.seed, args=(jid,), daemon=True).start()
        seasons.maybe_poll_weekly()
    except Exception:
        pass  # the season calendar must never block boot
    # drive-queue worker: runs queued moves/deletes whenever their drive
    # gets connected — also drains anything queued while the app was off
    try:
        drivequeue.repost_errors()  # requeue entries stranded by the old
        # no-retry behaviour so the fixed worker drains them
    except Exception:
        pass
    threading.Thread(target=drivequeue.worker_loop, daemon=True).start()
    # pre-download every catalog poster/backdrop into data/imgcache/ so the
    # UI never has to reach image.tmdb.org over the WAN
    try:
        imgcache.warm_async()
    except Exception:
        pass  # images must never block boot


@app.middleware("http")
async def no_stale_static(request, call_next):
    """Never let the browser run old frontend code: stale cached JS caused
    'button clicked but nothing happened' reports. API responses must never
    be cached either, so lists reflect deletes instantly."""
    resp = await call_next(request)
    if request.url.path.startswith("/api"):
        resp.headers["Cache-Control"] = "no-store"
    elif "cache-control" not in resp.headers:
        # routes may set a stronger policy themselves (favicon: no-store)
        resp.headers["Cache-Control"] = "no-cache"
    return resp


# ---- LAN access gate --------------------------------------------------------
# Remote (non-loopback) devices may only reach the pairing page/endpoints
# until they present a `fo_device` cookie granted by redeeming the 6-digit
# QR pairing code.  Loopback is always trusted: the machine running the
# server is the owner (same machine the QR shows on).
_PUBLIC_PATHS = {"/pair", "/api/pair", "/api/pair/status", "/favicon.ico"}


@app.middleware("http")
async def device_gate(request: Request, call_next):
    from . import pairing as pairing_mod
    client_ip = request.client.host if request.client else ""
    if pairing_mod.is_loopback(client_ip):
        return await call_next(request)
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    if pairing_mod.check_token(request.cookies.get(pairing_mod.COOKIE_NAME, "")):
        return await call_next(request)
    if request.url.path.startswith("/api"):
        return JSONResponse(
            {"detail": "device not paired — scan the QR code shown in "
                       "Settings on the host machine"},
            status_code=403)
    from .routers.pairing import PAIR_PAGE
    return Response(PAIR_PAGE, media_type="text/html", status_code=403)


# ---- routers ----------------------------------------------------------------
for _router in (
    pair_routes.router,
    system.router,
    titles.router,
    history.router,
    external.router,
    notifications.router,
    season_routes.router,
    settings_routes.router,
    job_routes.router,
    storage.router,
    dup_routes.router,
    recommendations.router,
    stats_routes.router,
):
    app.include_router(_router)


# ---- static frontend assets — MUST stay last so /api/* routes win ---------
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")

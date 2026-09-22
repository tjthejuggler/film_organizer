"""System surface: static index, image cache, favicon, folder browser,
live drive-connect SSE events."""
import asyncio
import hashlib
import json
import os
import subprocess
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .. import config, db, drivequeue, imgcache, lut_sync

router = APIRouter()


@router.get("/")
def index():
    return FileResponse(os.path.join(config.STATIC_DIR, "index.html"))


@router.get("/img")
def img(u: str):
    """Serve a cached TMDB poster/backdrop from disk (downloads once on
    first request; the frontend rewrites all image URLs through this)."""
    return imgcache.serve(u)


@router.get("/favicon.ico")
def favicon():
    # no-store: browsers otherwise pin the tab icon per origin for weeks,
    # surviving tab closes and server restarts
    path = os.path.join(config.STATIC_DIR, "favicon.svg")
    return FileResponse(path, media_type="image/svg+xml",
                        headers={"Cache-Control": "no-store"}) \
        if os.path.exists(path) else JSONResponse({}, status_code=204)


# ---- live drive connect / disconnect events --------------------------------
def _drives_signature() -> str:
    """Fingerprint of what is mounted + which library roots are reachable.
    /proc/mounts changes whenever a drive is plugged or unplugged; the
    per-root liveness bit catches mount points that silently disappear."""
    try:
        with open("/proc/mounts") as f:
            mounts = f.read()
    except OSError:
        mounts = ""
    roots = ";".join(
        f"{r['path']}={1 if os.path.isdir(r['path']) else 0}"
        for r in db.q("SELECT path FROM roots ORDER BY id"))
    return hashlib.sha1((mounts + "|" + roots).encode()).hexdigest()


def _safe_queue_drain():
    try:
        drivequeue.run_due()
        # a drive just plugged in / out: reachability changed -> regenerate
        # the voice fast-path (parked series return, new movies go live)
        lut_sync.request_sync("drive_change")
    except Exception:
        pass  # the poller thread will retry


@router.get("/api/events/drives")
async def drive_events():
    """Server-sent events fired whenever a drive connects or disconnects.
    The frontend reloads the table so rows show connected/disconnected
    locations immediately — no manual page refresh needed."""
    async def gen():
        last = _drives_signature()
        yield f"data: {json.dumps({'signature': last})}\n\n"
        while True:
            await asyncio.sleep(2)
            cur = _drives_signature()
            if cur != last:
                last = cur
                # a drive may have just connected: kick the queue in the
                # background (a move can take minutes — never block SSE)
                threading.Thread(target=_safe_queue_drain, daemon=True).start()
                yield f"data: {json.dumps({'signature': cur})}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


def _unquote_mnt(p: str) -> str:
    """Decode /proc/mounts octal escapes: \\040 space, \\011 tab,
    \\012 newline, \\134 backslash."""
    for esc, ch in (("\\134", "\\"), ("\\040", " "), ("\\011", "\t"), ("\\012", "\n")):
        p = p.replace(esc, ch)
    return p


def _mounted_drives() -> list:
    """Real disk mounts (external USB drives etc.) for the folder picker.
    Reads /proc/mounts; excludes loop/zram/system mounts. Needed because the
    udisks parent dirs (/run/media/<user>) are root-owned and not listable
    by the app user - we shortcut straight to each mounted drive instead."""
    good_fs = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat",
               "ntfs", "ntfs3", "ntfs-3g", "fuseblk", "f2fs"}
    drives = []
    seen = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                dev, mnt, fstype = parts[0], parts[1], parts[2]
                if not dev.startswith("/dev/"):
                    continue
                if dev.startswith("/dev/loop") or dev.startswith("/dev/zram"):
                    continue
                if fstype not in good_fs:
                    continue
                if mnt == "/" or mnt.startswith("/boot"):
                    continue
                if mnt in seen:
                    continue
                seen.add(mnt)
                mnt = _unquote_mnt(mnt)
                drives.append({"label": f"💾 {os.path.basename(mnt)}", "path": mnt})
    except OSError:
        pass
    return drives


@router.get("/api/browse")
def browse(path: str = None):
    """List directories at `path` for the folder-picker dialog.
    Local single-user app: full filesystem visibility is intentional."""
    home = os.path.expanduser("~")
    target = os.path.abspath(os.path.expanduser(path)) if path else home
    if not os.path.isdir(target):
        raise HTTPException(400, f"not a directory: {target}")

    dirs = []
    denied = False
    try:
        entries = os.listdir(target)
    except PermissionError:
        denied = True
        entries = []

    for name in sorted(entries, key=str.lower):
        if name.startswith("."):
            continue
        full = os.path.join(target, name)
        if os.path.isdir(full):
            try:
                os.listdir(full)  # readable?
                dirs.append({"name": name, "path": full})
            except PermissionError:
                dirs.append({"name": name + " (locked)", "path": full, "locked": True})

    shortcuts = [{"label": "🏠 Home", "path": home}]
    shortcuts += _mounted_drives()
    shortcuts.append({"label": "🖥  / (root)", "path": "/"})
    parent = os.path.dirname(target) if target != "/" else None
    return {"path": target, "parent": parent, "dirs": dirs,
            "shortcuts": shortcuts, "denied": denied}

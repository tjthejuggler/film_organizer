"""Move a title's files between the configured internal/external folders.

Preserves each file's path relative to its current base, updates the DB in
place (so locations are correct immediately), prunes emptied source
directories, and recomputes watched-folder flags for the new paths.

Folder-aware: when a title's files sit inside a folder that (per the
catalog) belongs exclusively to that title — a release folder that also
holds Subs/, artwork, .nfo … — the WHOLE folder is moved so sidecar files
are never left behind. Files directly in a shared root move individually,
taking matching same-stem sidecar files (.srt, .nfo, …) along.
"""
import os
import shutil
from datetime import datetime, timezone

from . import config, db, fileops, kio, parser
from .db import q, q1, tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_bases() -> list:
    bases = [
        db.settings_get("internal_root"),
        db.settings_get("external_root"),
        fileops.backup_root(),  # normalized drive root (Movies//Series/ parent)
    ]
    bases += [r["path"] for r in q("SELECT path FROM roots WHERE enabled=1")]
    return [os.path.abspath(b) for b in bases if b]


def src_base(path: str):
    """Longest configured base (root setting or library root) containing path."""
    path = os.path.abspath(path)
    cands = [b for b in _candidate_bases()
             if path == b or path.startswith(b.rstrip(os.sep) + os.sep)]
    return max(cands, key=len) if cands else None


def move_title(job_id: str, title_id: int, target: str,
               purge_others: bool = False):
    """Move a title to the 'internal' or 'external' side.

    purge_others=True upgrades the move to a CONSOLIDATION (the drawer's
    move buttons): after relocating, leftover copies of the title that
    still sit on the other side are deleted, so it ends up in exactly one
    place. Backup-style callers (notifications, queued moves without the
    flag) keep the copy-preserving semantics.
    """
    from .jobs import log, update

    title = q1("SELECT * FROM titles WHERE id=?", (title_id,))
    if not title:
        raise ValueError("title not found")
    dest_base = db.settings_get(f"{target}_root")
    if not dest_base:
        raise ValueError(f"No {target} folder configured — set it in Settings first")
    # external = the backup drive; normalize so a setting pointing INTO the
    # Movies folder (legacy layout) still resolves to the drive root
    drive_root = fileops.backup_root() if target == "external" \
        else os.path.abspath(dest_base)
    if not os.path.isdir(drive_root):
        raise ValueError(
            f"{target} folder not accessible: {drive_root} — is the drive plugged in?"
        )
    # backup-drive layout: titles land in Movies/ or Series/ per their kind
    # (internal storage keeps its existing layout — no subfolder hop)
    dest_base = drive_root
    if target == "external":
        sub = config.EXTERNAL_SUBDIRS.get(title["kind"])
        if sub:
            dest_base = os.path.join(drive_root, sub)

    def _rel(src: str, base: str) -> str:
        """Path relative to `base`. On the way back from the backup drive,
        the Movies//Series/ kind hop is stripped so titles restore their
        original internal-storage layout."""
        if target == "internal":
            bk_root = fileops.backup_root()
            for sub in config.EXTERNAL_SUBDIRS.values():
                hop = os.path.join(bk_root, sub) + os.sep
                if bk_root and src.startswith(hop):
                    return os.path.relpath(src, hop)
        return os.path.relpath(src, base)

    files = q("SELECT * FROM files WHERE title_id=? AND missing=0", (title_id,))
    if not files:
        raise ValueError("No accessible files to move for this title")

    # native system move dialog: a helper process owns a Plasma JobView
    # (the same progress dialog Dolphin's moves use) and does the transfer
    # with real progress + pause/cancel. Settings off switch; quiet
    # fallback when the desktop session is missing (headless, ssh, …).
    native_wanted = db.settings_get("move_native_dialog", "1") != "0"
    native = native_wanted and kio.available()
    if native_wanted and not native:
        log(job_id, "System move dialog unavailable (no desktop session) "
                    "— moving quietly instead")

    def _desktop_move(src: str, dest: str):
        """src -> dest via the system file engine; shutil fallback."""
        if not native:
            shutil.move(src, dest)
            return
        ok, detail = kio.move(src, dest, title["title"])
        if not ok:
            raise RuntimeError(
                f"system move failed or was cancelled: {detail or 'no detail'}")

    # --- plan: dedicated release folders as units, leftovers file-by-file ----
    folders: dict = {}  # (folder, base) -> [file rows]
    loose = []          # rows without a dedicated folder (or on dead roots)
    for f in files:
        src = os.path.abspath(f["path"])
        base = src_base(src)
        folder = fileops.dedicated_folder(src, base, title_id)
        if folder and os.path.isdir(folder):
            folders.setdefault((folder, base), []).append(f)
        else:
            loose.append(f)

    total = sum(len(fs) for fs in folders.values()) + len(loose)
    update(job_id, total=total, message=f"Moving {total} file(s) to {target}")
    log(job_id, f"Moving '{title['title']}' -> {dest_base}")

    moved = skipped = 0
    done = 0

    def _tick(n=1):
        nonlocal done
        done += n
        update(job_id, progress=done, message=f"Moving {done}/{total}")

    def _relocate(fs, new_path_of):
        """Point the catalog rows in `fs` at their new paths (keeps created)."""
        with tx() as c:
            for f in fs:
                c.execute(
                    "UPDATE files SET path=?, created=COALESCE(created, ?) WHERE id=?",
                    (new_path_of(f), f["created"], f["id"]))

    def _on_target_drive(p: str) -> bool:
        """Already physically on the destination drive (any subfolder —
        canonical kind hop not required)."""
        return p.startswith(drive_root.rstrip(os.sep) + os.sep)

    def _dedupe_key(f) -> tuple:
        """Identity of a file for twin-matching: (season, episode) for
        cataloged episodes, else the lowercase filename stem."""
        if f["season"] is not None and f["episode"] is not None:
            return ("ep", f["season"] or 1, f["episode"])
        return ("stem", os.path.splitext(os.path.basename(f["path"]))[0].lower())

    # consolidation: identities already present on the destination drive —
    # matching files on the OTHER side are duplicates and get purged rather
    # than re-homed (real drives may hold copies outside the canonical kind
    # folder, e.g. aaSeries/, and the twin is already where the user wants)
    target_keys = set()
    if purge_others:
        for f in files:
            p = os.path.abspath(f["path"])
            if _on_target_drive(p) and os.path.exists(p):
                target_keys.add(_dedupe_key(f))

    def _move_one(f) -> None:
        """Move a single video file plus its same-stem sidecar files."""
        nonlocal moved, skipped
        src = os.path.abspath(f["path"])
        # consolidation: a copy already on the destination DRIVE is where it
        # belongs — leave it exactly where it is, do not re-home it into the
        # canonical kind folder (its real layout may differ, e.g. aaSeries/)
        if purge_others and _on_target_drive(src):
            skipped += 1
            return
        # twin of a file already on the target drive -> not moved; the
        # post-move purge removes this stale duplicate
        if purge_others and _dedupe_key(f) in target_keys:
            skipped += 1
            return
        base = src_base(src)
        if not base:
            log(job_id, f"SKIP (outside all known roots): {src}")
            skipped += 1
            return
        if src.startswith(dest_base.rstrip(os.sep) + os.sep):
            skipped += 1  # already lives under the destination (sub)tree
            return
        rel = _rel(src, base)
        dest = os.path.join(dest_base, rel)
        if dest == src:
            skipped += 1
            return
        if os.path.exists(dest):
            log(job_id, f"SKIP (destination exists): {dest}")
            skipped += 1
            return
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        _desktop_move(src, dest)
        for sc in fileops.sidecar_paths(src):
            try:
                _desktop_move(sc, os.path.join(os.path.dirname(dest),
                                               os.path.basename(sc)))
            except Exception:
                pass  # losing a sidecar must never fail the move
                # (OSError from shutil, RuntimeError from a failed/cancelled
                # native KIO move)
        _relocate([f], lambda _f: dest)
        fileops.prune_dirs(os.path.dirname(src), base)
        moved += 1

    for (folder, base), fs in folders.items():
        if purge_others and _on_target_drive(folder):
            skipped += len(fs)  # already on the target drive — leave in place
            continue
        if purge_others and any(_dedupe_key(f) in target_keys for f in fs):
            # folder mixes twins and unique files: handle one by one so
            # twins stay behind (for the purge) and uniques get moved
            for f in fs:
                _move_one(f)
                _tick()
            continue
        rel = _rel(folder, base)
        dest = os.path.join(dest_base, rel)
        if (dest == folder
                or folder.rstrip(os.sep) == dest_base.rstrip(os.sep)
                or folder.startswith(dest_base.rstrip(os.sep) + os.sep)):
            skipped += len(fs)  # already lives at the destination
        elif os.path.exists(dest):
            # destination folder already exists: move files one by one
            # (skipping duplicates) instead of blindly merging the folders
            log(job_id, f"Destination exists — file-by-file fallback: {dest}")
            for f in fs:
                _move_one(f)
                _tick()
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            _desktop_move(folder, dest)
            log(job_id, f"Moved folder (Subs etc. ride along): "
                        f"{os.path.basename(folder)} -> {dest}")
            _relocate(fs, lambda f: os.path.join(
                dest, os.path.relpath(os.path.abspath(f["path"]), folder)))
            fileops.prune_dirs(os.path.dirname(folder), base)
            moved += len(fs)
            _tick(len(fs))

    for f in loose:
        _move_one(f)
        _tick()

    # watched LATCH: moving INTO a watched-marker folder turns the flag on;
    # moving out never turns it off (watched state is system-owned after
    # the folder jumpstart)
    rows = q("SELECT id, path FROM files WHERE title_id=?", (title_id,))
    any_watched = False
    with tx() as c:
        for r in rows:
            w = parser.watched_marker(r["path"]) is True
            any_watched = any_watched or w
            if w:
                c.execute("UPDATE files SET watched_folder=1 WHERE id=?", (r["id"],))
        if any_watched:
            c.execute(
                "UPDATE titles SET watched_folder=1, "
                "watched_at=COALESCE(watched_at, ?) WHERE id=?",
                (_now(), title_id))

    # the destination drive becomes a managed root so scans cover it and
    # locations stay accurate (idempotent)
    if moved:
        with tx() as c:
            c.execute("INSERT OR IGNORE INTO roots(path) VALUES(?)", (drive_root,))

    # consolidation: files whose destination already existed were SKIPPED by
    # the move loop (never deleted) — a duplicated title would otherwise end
    # up with two copies on the target and the old one on the source drive
    purged = _purge_leftovers(job_id, title_id, target) if purge_others else 0

    tail = f", {purged} leftover(s) removed" if purged else ""
    log(job_id, f"Move complete: {moved} moved, {skipped} skipped{tail}")
    update(job_id, message=f"Moved {moved} file(s), {skipped} skipped{tail}")


def _purge_leftovers(job_id: str, title_id: int, target: str) -> int:
    """Delete copies of the title that STILL sit outside the target side.

    Only physically present files on known, connected roots are touched —
    a copy on an unplugged drive is left for the next visit. Deletion is
    folder-aware (duplicates._delete_title_files) so Subs/, artwork and
    .nfo sidecars of each doomed release folder go with it; the stored
    title size is recomputed afterwards.
    """
    from . import duplicates
    from .jobs import log

    ext = fileops.backup_root()
    if not ext:
        return 0  # no external drive configured -> no 'other side' exists
    ext = os.path.abspath(ext).rstrip(os.sep) + os.sep

    def _side(p: str) -> str:
        return "external" if os.path.abspath(p).startswith(ext) else "internal"

    doomed = []
    for f in q("SELECT * FROM files WHERE title_id=? AND missing=0", (title_id,)):
        if _side(f["path"]) == target:
            continue  # this copy already lives where the user asked
        base = src_base(f["path"])
        if not base or not os.path.isdir(base):
            continue  # unknown root or drive gone — never touch it here
        if os.path.exists(f["path"]):
            doomed.append(dict(f))
    if not doomed:
        return 0

    log(job_id, f"Consolidating: removing {len(doomed)} leftover copy "
                f"file(s) from the "
                f"{'internal' if target == 'external' else 'external'} side")
    duplicates._delete_title_files(doomed, title_id, delete_row=False)
    with tx() as c:
        c.execute(
            "UPDATE titles SET size_bytes=(SELECT COALESCE(SUM(size_bytes),0) "
            "FROM files WHERE title_id=? AND missing=0) WHERE id=?",
            (title_id, title_id))
    return len(doomed)

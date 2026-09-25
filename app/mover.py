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
from typing import List, Optional

from . import config, db, fileops, kio, parser
from .db import q, q1, tx


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_bases() -> list:
    bases = [
        db.settings_get("internal_root"),
        db.settings_get("external_root"),
        fileops.backup_root(),  # normalized legacy drive root
    ]
    # the four backup destinations (per-kind regular + liked drives)
    bases += [fileops.regular_root(k) for k in ("movie", "series")]
    bases += [fileops.liked_root(k) for k in ("movie", "series")]
    bases += [r["path"] for r in q("SELECT path FROM roots WHERE enabled=1")]
    return [os.path.abspath(b) for b in bases if b]


def src_base(path: str):
    """Longest configured base (root setting or library root) containing path."""
    path = os.path.abspath(path)
    cands = [b for b in _candidate_bases()
             if path == b or path.startswith(b.rstrip(os.sep) + os.sep)]
    return max(cands, key=len) if cands else None


def move_title(job_id: str, title_id: int, target: str,
               purge_others: bool = False,
               seasons: Optional[List[int]] = None,
               dest_root: Optional[str] = None,
               purge_roots: Optional[List[str]] = None):
    """Move a title to the 'internal' or 'external' side.

    purge_others=True upgrades the move to a CONSOLIDATION (the drawer's
    move buttons): after relocating, leftover copies of the title that
    still sit on the other side are deleted, so it ends up in exactly one
    place. Backup-style callers (notifications, queued moves without the
    flag) keep the copy-preserving semantics.

    purge_roots: EXPLICIT list of source places to clean after the move
    (drive/library roots, from the move dialog's 'remove from' checklist).
    When given it REPLACES the automatic other-side derivation: only
    copies under these roots are purged (liked drives stay protected),
    and purge_others is implied. None keeps the classic behaviour: purge
    the whole opposite side when purge_others is set.

    seasons (series): move only these seasons; None moves everything.
    A partial-season move is always FILE-BY-FILE — a release folder that
    also holds seasons left behind is never relocated whole.

    dest_root: explicit destination folder (any configured backup/liked/
    internal root, from the drawer's 'Move to' list). Overrides the plain
    internal/external target derivation; its physical drive then decides
    the purge side.
    """
    from .jobs import log, update

    title = q1("SELECT * FROM titles WHERE id=?", (title_id,))
    if not title:
        raise ValueError("title not found")
    if dest_root:
        dest_base = os.path.abspath(dest_root)
        if not os.path.isdir(dest_base):
            raise ValueError(
                f"destination folder not accessible: {dest_base} — "
                "is the drive plugged in?")
        drive_root = dest_base
        # physical drive = shortest configured backup root containing the
        # destination (or the destination itself when it IS a drive root)
        for r in fileops.all_backup_roots():
            if (dest_base + os.sep).startswith(r.rstrip(os.sep) + os.sep) \
                    and len(r) < len(drive_root):
                drive_root = r
        # side for the purge step: a destination under any backup root is
        # 'external'; anything else (internal storage) is 'internal'
        target = "external" if drive_root != dest_base or any(
            (dest_base + os.sep).startswith(r.rstrip(os.sep) + os.sep)
            for r in fileops.all_backup_roots()) else "internal"
    elif target == "external":
        # the title's kind decides the destination: every movie backs up to
        # backup_movies_root, every series to backup_series_root (the
        # legacy external_root drive still works when the per-kind keys
        # are empty — titles land in its Movies/ / Series/ subfolder)
        dest_base = fileops.regular_root(title["kind"])
        if not dest_base:
            raise ValueError(
                f"No backup folder configured for "
                f"{'movies' if title['kind'] == 'movie' else 'series'} "
                "— set it in Settings first")
        # physical drive = the shortest configured backup root containing
        # the destination (the legacy drive root, or the per-kind folder
        # itself when that IS the drive root)
        drive_root = dest_base
        for r in fileops.all_backup_roots():
            if (dest_base + os.sep).startswith(r.rstrip(os.sep) + os.sep) \
                    and len(r) < len(drive_root):
                drive_root = r
    else:
        dest_base = db.settings_get("internal_root")
        if not dest_base:
            raise ValueError("No internal folder configured — set it in Settings first")
        drive_root = os.path.abspath(dest_base)
    if not os.path.isdir(drive_root):
        raise ValueError(
            f"{target} folder not accessible: {drive_root} — is the drive plugged in?"
        )
    dest_base = os.path.abspath(dest_base)

    def _rel(src: str, base: str) -> str:
        """Path relative to `base`. On the way back from ANY backup root,
        the destination folder itself is stripped so titles restore their
        original internal-storage layout (covers the legacy Movies//Series/
        kind hop and per-kind backup/liked roots alike)."""
        if target == "internal":
            for r in fileops.all_backup_roots():
                if (os.path.abspath(src) + os.sep).startswith(
                        r.rstrip(os.sep) + os.sep):
                    return os.path.relpath(src, r)
        return os.path.relpath(src, base)

    all_files = q("SELECT * FROM files WHERE title_id=? AND missing=0", (title_id,))
    if not all_files:
        raise ValueError("No accessible files to move for this title")
    files = all_files
    # liked-drive copies never travel: the secondary backup of a favorite
    # stays on its liked drive no matter where the working copy moves
    lk = fileops.liked_root(title["kind"])
    if lk:
        lk = os.path.abspath(lk).rstrip(os.sep) + os.sep
        files = [f for f in files
                 if not (os.path.abspath(f["path"]) + os.sep).startswith(lk)]
        if not files:
            raise ValueError("Only liked-drive copies exist — nothing to move")
    # The ACTIVE Watch Next pin's staging slot participates in moves like
    # any other copy (standard move semantics — the user decides with the
    # pin button, not by working around a protected folder): it can be
    # moved to the destination like everything else, and _purge_leftovers
    # clears it when the title already lives at the destination. The pin
    # itself is dropped once its slot copy is gone (see below).
    if seasons:
        # partial move: keep only episodes of the chosen seasons (a file
        # without a parsed season counts as season 1, like the drawer UI)
        want = set(seasons)
        files = [f for f in files
                 if (f["season"] if f["season"] is not None else 1) in want]
        if not files:
            raise ValueError("No files match the selected seasons")
        log(job_id, f"Season filter: moving only "
                    f"{', '.join('S' + str(s) for s in sorted(want))}")

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
    # a PARTIAL move (selected seasons only) always goes file-by-file — a
    # release folder that also holds seasons left behind must never travel
    # whole (same-stem sidecar files of each moved episode still ride along)
    folders: dict = {}  # (folder, base) -> [file rows]
    loose = []          # rows without a dedicated folder (or on dead roots)
    for f in files:
        src = os.path.abspath(f["path"])
        base = src_base(src)
        folder = fileops.dedicated_folder(src, base, title_id) \
            if seasons is None else None
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
            log(job_id, f"SKIP (already on the destination drive): {src}")
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
            log(job_id, f"SKIP (already on the destination drive): {folder}")
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

    # the LANDING FOLDER becomes a managed root so scans cover moved files
    # and locations stay accurate (idempotent). dest_base is always a KIND
    # folder (Movies//Series/ on the legacy drive, or the exact per-kind
    # backup folder the user picked) — never a whole device, so sweeping
    # camera clips / photos / misc backups into the catalog cannot happen.
    if moved:
        with tx() as c:
            c.execute("INSERT OR IGNORE INTO roots(path) VALUES(?)", (dest_base,))

    # consolidation: files whose destination already existed were SKIPPED by
    # the move loop (never deleted) — a duplicated title would otherwise end
    # up with two copies on the target and the old one on the source drive.
    # Liked-drive copies are always protected (keep_roots): consolidating
    # must never destroy a favorite's secondary backup place. Everything
    # else goes — INCLUDING the active Watch Next pin's staging copy
    # (standard move semantics; the pin is dropped below once its copy
    # is gone) — unless the user picked the exact places to clear via
    # purge_roots, in which case ONLY those places go.
    # ALSO runs when nothing was moved at all: a move request for files
    # that ALREADY live at the destination must still clear the requested
    # source places (the user asked 'move it here', not 'copy it here'),
    # otherwise the job ends '0 moved, N skipped' and the old copies
    # linger forever.
    keep_roots = [fileops.liked_root(title["kind"])] \
        if fileops.liked_root(title["kind"]) else []
    explicit = [os.path.abspath(r) for r in (purge_roots or [])]
    purged = _purge_leftovers(job_id, title_id, target,
                              seasons=seasons,
                              keep_roots=keep_roots,
                              only_roots=explicit or None) \
        if (purge_others or moved == 0 or explicit) else 0

    # a Watch Next pin whose staging copy was consumed by this move (moved
    # away or purged) points at an empty slot — drop it so the UI stops
    # advertising a title that is no longer staged
    if title["watch_next"] == title["kind"]:
        from . import watchnext
        try:
            slot = watchnext.slot_dir(title["kind"])
            left = q1("SELECT COUNT(*) n FROM files WHERE title_id=? AND "
                      "missing=0 AND (path = ? OR path LIKE ?)",
                      (title_id, slot, slot.rstrip(os.sep) + os.sep + "%"))["n"]
            if not left:
                watchnext.clear_next(title_id)
                log(job_id, "Watch Next pin dropped — its staged copy "
                            "moved with the title")
        except ValueError:
            pass

    tail = f", {purged} leftover(s) removed" if purged else ""
    log(job_id, f"Move complete: {moved} moved, {skipped} skipped{tail}")
    update(job_id, message=f"Moved {moved} file(s), {skipped} skipped{tail}")


def _purge_leftovers(job_id: str, title_id: int, target: str,
                     seasons: Optional[List[int]] = None,
                     keep_roots: Optional[List[str]] = None,
                     only_roots: Optional[List[str]] = None) -> int:
    """Delete copies of the title that STILL sit outside the target side.

    Only physically present files on known, connected roots are touched —
    a copy on an unplugged drive is left for the next visit. Deletion is
    folder-aware (duplicates._delete_title_files) so Subs/, artwork and
    .nfo sidecars of each doomed release folder go with it; the stored
    title size is recomputed afterwards. With `seasons`, only copies of
    the selected seasons are purged. Copies under any root in
    `keep_roots` (the liked drives) are NEVER purged: consolidating a
    favorite must not destroy its secondary backup.

    only_roots narrows the purge to copies under THESE roots (absolute
    paths, from the move dialog's 'remove from' checklist). When given,
    the classic other-side derivation is skipped entirely: only the
    places the user explicitly ticked go — an unticked place keeps its
    copy.
    """
    from . import duplicates
    from .jobs import log

    backup_roots = [os.path.abspath(r) for r in
                    (fileops.all_backup_roots() if target == "external" else
                     [fileops.regular_root(k) for k in ("movie", "series")])
                    if r]
    if target == "external" and not backup_roots and not only_roots:
        return 0  # no backup destination configured -> no 'other side'
    keep = [os.path.abspath(r).rstrip(os.sep) + os.sep
            for r in (keep_roots or [])]
    only = [os.path.abspath(r).rstrip(os.sep) + os.sep
            for r in (only_roots or [])]

    def _side(p: str) -> str:
        ap = os.path.abspath(p) + os.sep
        if target == "external":
            return "external" if any(ap.startswith(r.rstrip(os.sep) + os.sep)
                                     for r in backup_roots) else "internal"
        return "internal" if not any(ap.startswith(r.rstrip(os.sep) + os.sep)
                                     for r in backup_roots) else "external"

    def _in_only(p: str) -> bool:
        ap = os.path.abspath(p) + os.sep
        return any(ap.startswith(r) for r in only) if only else True

    doomed = []
    for f in q("SELECT * FROM files WHERE title_id=? AND missing=0", (title_id,)):
        if seasons is not None and \
                (f["season"] if f["season"] is not None else 1) not in set(seasons):
            continue  # partial move: other seasons' copies are not ours to purge
        ap = os.path.abspath(f["path"]) + os.sep
        if any(ap.startswith(r) for r in keep):
            continue  # a liked drive copy is protected — never consolidated away
        if not _in_only(f["path"]):
            continue  # user kept this place — its copy stays
        if only_roots is None and _side(f["path"]) == target:
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

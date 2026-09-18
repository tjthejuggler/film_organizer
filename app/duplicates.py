"""Duplicate detection.

The scanner merges same-title releases into ONE title row, so a movie that
exists on the laptop AND on an external drive shows up as a single title
with files under multiple roots. Detection therefore works at file level:

* movies: title has non-missing files under 2+ roots  -> duplicate copies
* series: the same (season, episode) exists under 2+ roots -> duplicate
  episode copies (series that merely SPLIT episodes across roots are normal
  and NOT reported)

Cross-title near-duplicates (same normalized title, different rows, e.g.
one row with a wrong year) are reported as 'possible' groups.
"""
import os
import shutil

from . import fileops, parser
from .db import q, q1, tx

# A 'dedicated' release folder may contain at most this much BEYOND the
# cataloged bytes of the title's own files (subs, artwork and .nfo files
# are tiny; a whole second movie is not). Beyond that the folder is
# treated as shared and only the cataloged files are deleted one by one.
FOLDER_SLACK_RATIO = 1.1
FOLDER_SLACK_BYTES = 250_000_000  # 250 MB


def _roots_ordered() -> list:
    return [r["path"] for r in q("SELECT path FROM roots ORDER BY length(path) DESC")]


def _label(root: str) -> str:
    return root.replace(os.path.expanduser("~"), "~") if root else "unknown"


def _root_of(path: str, roots: list):
    for root in roots:
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return root
    return None


def find_duplicates() -> list:
    roots = _roots_ordered()
    groups = []
    claimed_titles = set()  # normalized titles already reported (avoid doubles)

    # --- CERTAIN cross-row duplicates: two rows matched the same TMDB entry --
    rows = q("""SELECT id, title, year, kind, size_bytes, tmdb_id, match_status
                FROM titles WHERE match_status='matched' AND tmdb_id IS NOT NULL""")
    by_tmdb = {}
    for r in rows:
        by_tmdb.setdefault(r["tmdb_id"], []).append(dict(r))
    for tmdb_id, cands in by_tmdb.items():
        if len(cands) < 2:
            continue
        cands.sort(key=lambda c: c["size_bytes"] or 0, reverse=True)
        for c in cands:
            claimed_titles.add(parser.normalize_key(c["title"]))
        groups.append({
            "title": cands[0]["title"], "year": cands[0]["year"],
            "kind": cands[0]["kind"], "scope": "two_rows",
            "copies": [{
                "title_id": c["id"], "root": None,
                "label": (f"library row #{c['id']}: {c['title']} "
                          f"({c['year'] or '?'})"),
                "file_count": None, "size_bytes": c["size_bytes"], "files": [],
            } for c in cands],
            "size_delta": (cands[0]["size_bytes"] or 0) - (cands[-1]["size_bytes"] or 0),
        })

    # --- within-title duplication (same row, files in 2+ places) ------------
    liked_roots = {}
    for kind in ("movie", "series"):
        r = fileops.liked_root(kind)
        if r:
            liked_roots[kind] = os.path.abspath(r).rstrip(os.sep) + os.sep
    for t in q("SELECT * FROM titles"):
        if parser.normalize_key(t["title"]) in claimed_titles:
            continue  # already covered by a certain two-rows group
        # a liked title's SECOND copy (on its liked drive) is intentional —
        # filter it out so the two-place backup never reads as duplication
        lk = liked_roots.get(t["kind"])
        # catalog-based, NOT filesystem-based: copies on unplugged drives
        # (missing=0 in the DB) still participate, so duplicates are found
        # across offline drives and newly plugged ones
        files = [dict(f) for f in q(
            "SELECT * FROM files WHERE title_id=? AND missing=0", (t["id"],))]
        if lk:
            files = [f for f in files
                     if not (os.path.abspath(f["path"]) + os.sep).startswith(lk)]
            if not files:
                continue
        if len(files) < 2:
            continue
        by_root = {}
        for f in files:
            by_root.setdefault(_root_of(f["path"], roots), []).append(dict(f))
        by_root.pop(None, None)
        if len(by_root) < 2:
            continue

        if t["kind"] == "movie":
            copies = [{
                "title_id": t["id"],
                "root": r, "label": _label(r),
                "file_count": len(fs), "size_bytes": sum(f["size_bytes"] for f in fs),
                "files": fs,
            } for r, fs in by_root.items()]
            copies.sort(key=lambda c: c["size_bytes"], reverse=True)
            groups.append({
                "title": t["title"], "year": t["year"], "kind": "movie",
                "title_id": t["id"], "scope": "movie",
                "copies": copies,
                "size_delta": copies[0]["size_bytes"] - copies[-1]["size_bytes"],
            })
        else:
            # series: the same (season, episode) present under 2+ roots.
            # A series merely SPLIT across roots is normal, not a duplicate.
            ep_roots = {}
            for f in files:
                if f["episode"] is None:
                    continue
                ep_roots.setdefault((f["season"] or 1, f["episode"]), set()).add(
                    _root_of(f["path"], roots))
            dup_eps = {ep for ep, rs in ep_roots.items()
                       if len({r for r in rs if r}) > 1}
            if not dup_eps:
                continue
            copies = []
            for r, fs in by_root.items():
                dup_files = [f for f in fs
                             if (f["season"] or 1, f["episode"] or 0) in dup_eps]
                if dup_files:
                    copies.append({
                        "title_id": t["id"],
                        "root": r, "label": _label(r),
                        "file_count": len(dup_files), "size_bytes": sum(f["size_bytes"] for f in dup_files),
                        "files": dup_files,
                    })
            copies.sort(key=lambda c: c["size_bytes"], reverse=True)
            if len(copies) < 2:
                continue
            groups.append({
                "title": t["title"], "year": t["year"], "kind": "series",
                "title_id": t["id"], "scope": "episodes",
                "dup_episodes": len(dup_eps),
                "copies": copies,
                "size_delta": copies[0]["size_bytes"] - copies[-1]["size_bytes"],
            })

    # NOTE: title-only 'possible' groups were REMOVED on purpose: normalized
    # titles collide across remakes/different works (Day of the Jackal 1973
    # vs 2024, The Accountant vs The Accountant²), producing false alarms.
    # tmdb_id equality (above) is the reliable cross-row signal.

    groups.sort(key=lambda g: g["size_delta"], reverse=True)
    return groups


def _is_still_duplicated(title_id: int, root: str = None) -> bool:
    """Guard: deletion is only allowed while a duplicate still exists in the
    CATALOG. Deliberately NOT filesystem-based: an unplugged drive's copies
    are missing=0 in the DB and count fully — otherwise deleting a copy on
    a plugged-in drive would be refused just because the other drive is
    asleep (user workflow: swap drives regularly)."""
    t = q1("SELECT title FROM titles WHERE id=?", (title_id,))
    if not t:
        return False
    files = q("SELECT path FROM files WHERE title_id=? AND missing=0", (title_id,))
    roots_present = {_root_of(f["path"], _roots_ordered()) for f in files} - {None}
    if root is not None:
        return len(roots_present - {root}) >= 1  # another cataloged copy survives
    if len(roots_present) >= 2:
        return True
    # cross-row twin (same normalized title, also matched)
    twin = q1(
        "SELECT id FROM titles WHERE id != ? AND match_status='matched' AND title=?",
        (title_id, t["title"]))
    return twin is not None


def delete_copy(title_id: int, root: str = None):
    """Delete one copy of a duplicated title.

    root given  -> delete that title's files under this root (title survives
                   if it has files elsewhere).
    root is None-> delete the whole title row + its files (used for
                   'possible' cross-title groups).
    Either way the voice fast-path catalog is re-synced (reachability of
    the title may have changed).
    """
    from . import lut_sync
    try:
        result = _delete_copy_impl(title_id, root)
    finally:
        lut_sync.request_sync("delete_copy")
    return result


def _delete_copy_impl(title_id: int, root: str = None):
    """(Original delete_copy body — see wrapper above.)

    SAFETY: refuses unless the title is *currently* duplicated — i.e. after
    the deletion at least one other copy of the same title still exists.
    This makes 'delete the only copy' impossible through this endpoint.
    """
    title = q1_row(title_id)
    if not title:
        raise ValueError("copy not found")
    if not _is_still_duplicated(title_id, root):
        raise ValueError(
            "Safety refused: this is (no longer) a duplicate — the same title "
            "does not exist anywhere else. Deleting the only copy is disabled.")

    # offline-drive guard: never claim deletion while bytes sit on a
    # disconnected disk
    roots_all = _roots_ordered()
    files_pre = q("SELECT path, missing FROM files WHERE title_id=?", (title_id,))
    if root:
        files_pre = [f for f in files_pre
                     if _root_of(f["path"], roots_all) == root]
    offline = set()
    for f in files_pre:
        if f["missing"]:
            continue
        rt = _root_of(f["path"], roots_all)
        if rt and not os.path.isdir(rt):
            offline.add(rt)
    if offline:
        raise ValueError("Connect these drives first: " + ", ".join(sorted(offline)))

    if root:
        files = [f for f in q("SELECT * FROM files WHERE title_id=?", (title_id,))
                 if _root_of(f["path"], _roots_ordered()) == root]
        if not files:
            raise ValueError("no files under that location for this title")
        return {**_delete_title_files(files, title_id, delete_row=False), "title_deleted": False}

    files = q("SELECT * FROM files WHERE title_id=?", (title_id,))
    return {**_delete_title_files(files, title_id, delete_row=True), "title_deleted": True}


def q1_row(title_id: int):
    return q1("SELECT * FROM titles WHERE id=?", (title_id,))


def _folder_plan(files, title_id: int):
    """Split deletable rows into (whole-folder wipes, individual files).

    A folder qualifies when it is this title's dedicated folder (no other
    title's files under it per the catalog) AND its actual on-disk content
    is not way bigger than the title's cataloged bytes — the anti foot-gun
    that keeps a false 'dedicated' match from wiping a shared directory."""
    roots = _roots_ordered()
    candidates: dict = {}  # folder -> [file rows]
    fallback: list = []
    for f in files:
        if f["missing"]:
            continue
        p = os.path.abspath(f["path"])
        if not os.path.exists(p):
            continue
        base = _root_of(p, roots)
        folder = fileops.dedicated_folder(p, base, title_id) if base else None
        if folder:
            candidates.setdefault(folder, []).append(f)
        else:
            fallback.append(f)

    plan: dict = {}
    for folder, rows in candidates.items():
        catalog_bytes = sum(r["size_bytes"] or 0 for r in rows)
        on_disk = fileops.folder_bytes_on_disk(folder)
        if on_disk <= catalog_bytes * FOLDER_SLACK_RATIO + FOLDER_SLACK_BYTES:
            plan[folder] = rows
        else:
            fallback.extend(rows)
    return plan, fallback


def _delete_title_files(files, title_id: int, delete_row=True):
    removed, missing = 0, 0
    roots = _roots_ordered()

    folders, rest = _folder_plan(files, title_id)
    for folder, rows in folders.items():
        # whole release folder goes (Subs/, artwork, … ride along)
        shutil.rmtree(folder, ignore_errors=True)
        removed += len(rows)
    for folder in folders:
        base = _root_of(folder, roots)
        if base:
            fileops.prune_dirs(os.path.dirname(os.path.abspath(folder)), base)

    for f in rest:
        p = f["path"]
        if f["missing"] or not os.path.exists(p):
            missing += 1
            continue
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            os.remove(p)
        removed += 1
        base = _root_of(p, roots)
        if base:
            fileops.prune_dirs(os.path.dirname(os.path.abspath(p)), base)

    with tx() as c:
        if delete_row:
            c.execute("DELETE FROM files WHERE title_id=?", (title_id,))
            c.execute("DELETE FROM titles WHERE id=?", (title_id,))
        else:
            for f in files:
                c.execute("DELETE FROM files WHERE id=?", (f["id"],))
    return {"removed_files": removed, "already_missing": missing,
            "folders_removed": sorted(folders)}

"""Shared filesystem helpers for folder-aware moves and deletes.

A "dedicated folder" is a directory that (per the catalog) contains files
of exactly ONE title — a typical release folder holding episodes plus
Subs/, artwork and .nfo files. Moves and deletes operate on such folders
as a unit so sidecar files never get left behind, while a size guard for
deletes makes accidentally wiping a huge shared directory impossible.
"""
import os

from . import config, db
from .db import q1

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".sub", ".vtt", ".idx", ".sup"}
SIDECAR_EXTS = SUBTITLE_EXTS | {".nfo", ".txt", ".jpg", ".jpeg", ".png"}


def backup_root() -> str:
    """Normalized backup-drive root from the external_root setting.

    The backup layout keeps Movies/ and Series/ side by side on the drive;
    when the setting points INTO one of those kind folders (e.g.
    /media/X10 Pro/Movies), the actual drive root is one level up — so
    movies keep landing in the existing Movies/ and series get a sibling
    Series/. Empty string when no external root is configured."""
    ext = db.settings_get("external_root")
    if not ext:
        return ""
    root = os.path.abspath(ext)
    kind_dirs = {"movies", "series"}
    if os.path.basename(root).lower() in kind_dirs:
        root = os.path.dirname(root)
    return root


def like_under(folder: str) -> str:
    """SQL LIKE pattern matching every cataloged path strictly under folder."""
    return os.path.abspath(folder).rstrip(os.sep) + os.sep + "%"


def dedicated_folder(path: str, base: str, title_id: int):
    """Highest ancestor directory of `path` (still under `base`) that holds
    no cataloged files of any OTHER title — the title's own release folder.
    None when the file sits directly in a shared root (or outside base).

    The walk never claims a backup-drive kind hop (Movies/ or Series/):
    on a drive hosting a single title that folder looks 'dedicated' but is
    shared layout — moving/deleting it wholesale would destroy the layout
    (Movies -> Movies/Movies, or wiping every sibling release)."""
    if not base:
        return None
    base = os.path.abspath(base)
    hops = set()
    bk = backup_root()
    if bk:
        hops = {os.path.normpath(os.path.join(bk, s))
                for s in config.EXTERNAL_SUBDIRS.values()}
    folder = None
    d = os.path.dirname(os.path.abspath(path))
    while d.startswith(base + os.sep):
        if os.path.normpath(d) in hops:
            break
        foreign = q1("SELECT COUNT(*) n FROM files "
                     "WHERE path LIKE ? AND title_id != ? AND missing=0",
                     (like_under(d), title_id))["n"]
        if foreign:
            break
        folder = d
        d = os.path.dirname(d)
    return folder


def sidecar_paths(src: str) -> list:
    """Uncataloged sibling files belonging to `src`: same stem (movie.srt,
    movie.nfo) or language-tagged subtitles (movie.en.srt)."""
    d = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0]  # filename only!
    out = []
    try:
        entries = os.listdir(d)
    except OSError:
        return out
    for name in entries:
        p = os.path.join(d, name)
        if p == src or not os.path.isfile(p):
            continue
        root, ext = os.path.splitext(name)
        ext = ext.lower()
        if root == stem and ext in SIDECAR_EXTS:
            out.append(p)
        elif ext in SUBTITLE_EXTS and root.startswith(stem + "."):
            out.append(p)
    return out


def prune_dirs(start: str, base: str):
    """Remove now-empty directories from `start` upward until `base`."""
    d = start
    while d and os.path.abspath(d) != os.path.abspath(base):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def folder_bytes_on_disk(folder: str) -> int:
    total = 0
    for root, _dirs, names in os.walk(folder):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    return total


def gb(n) -> str:
    return f"{(n or 0) / 1e9:.1f} GB"

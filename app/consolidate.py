"""Title consolidation: fold duplicate title rows into ONE row per show.

Why this exists
---------------
Several independent bugs used to split a single show across multiple title
rows (Euphoria had EIGHT, The Righteous Gemstones and The Life & Times of
Tim two each):

* a release like "Euphoria.US.S02E01..." was parsed as site "euphoria.us" +
  no title -> one junk row PER FILE (fixed in parser: .us/.uk are no longer
  treated as site TLDs);
* title spelling variants ("The Righteous Gemstones" vs the release folder
  "righteous.gemstones.s04") produced different dedupe keys;
* "The Life & Times of Tim" vs "The Life and Times of Tim" — the "&" is
  gone from normalized keys, so the two spellings never met (fixed: "&" is
  now spelled out as "and").

This module heals the DATABASE after those fixes, in passes:

PASS 1 — TMDB merge (movies AND series): rows that are matched to the SAME
tmdb_id are the same title, period (this is what heals old CD-split rows
like Tampopo.cd2, whose enriched title no longer matches its stale key).
All but one are merged into the survivor (files, season calendar,
notifications, queue entries, recommendations move across).

PASS 2 — folded-key merge: remaining rows whose titles fold to the same
loose identity are merged ONLY when every matched row in the group agrees
on one tmdb_id — conflicting matches are left untouched. Series fold by
loose title (article dropped, trailing US/UK token folded:
parser.series_title_key; The Office UK vs US stay separate). Movies fold
by exact normalized title AND year (year is part of a movie's identity —
remakes are real) and merge only with agreeing matches.

User-locked rows (title_locked / kind_locked) are never victims; they may
absorb files as survivors. Movies are deliberately not merged here (year is
part of a movie's identity and remakes are real).

Safe to run any time; no network access; skips when there is nothing to do.
"""
from . import db, parser
from .db import q, q1, tx


def _canonical_key(kind: str, title: str, year) -> str:
    k = parser.normalize_key(title)
    return f"m:{k}:{year or 0}" if kind == "movie" else f"s:{k}"


def _pick_survivor(rows: list) -> dict:
    """Survivor choice: canonical-keyed row first, then the oldest
    matched row, then simply the oldest."""
    rows = list(rows)

    def rank(r):
        matched = 0 if r["match_status"] == "matched" else 1
        return (0 if r["is_canonical"] else 1, matched, r["id"])

    return sorted(rows, key=rank)[0]


def _display_title(survivor: dict, rows: list) -> str:
    """Prefer a title variant WITHOUT the trailing 'US'/'UK' country token
    ('Euphoria' over 'Euphoria US'); otherwise keep the survivor's."""
    t = (survivor["title"] or "").strip()
    if parser._TRAILING_COUNTRY_RE.search(t):
        for r in rows:
            rt = (r["title"] or "").strip()
            if rt and not parser._TRAILING_COUNTRY_RE.search(rt):
                return rt
    return t


def _merge_into(survivor_id: int, victim: dict, c) -> None:
    """Move every child reference from victim to survivor, fold scalar
    flags, then delete the victim row. Runs inside one tx() cursor."""
    vid = victim["id"]
    sid = survivor_id
    # children (files FK-cascades on delete — re-point them FIRST)
    c.execute("UPDATE files SET title_id=? WHERE title_id=?", (sid, vid))
    # season calendar: UNIQUE(title_id, season) — a season the survivor
    # already tracks must not block the update; OR IGNORE keeps the
    # survivor's row and just drops the duplicate
    # UNIQUE(title_id, season) conflicts are skipped by OR IGNORE; skipped
    # rows keep pointing at the victim and die with its cascade delete —
    # the survivor's own calendar row always wins
    c.execute("UPDATE OR IGNORE season_watch SET title_id=? WHERE title_id=?",
              (sid, vid))
    c.execute("UPDATE notifications SET title_id=? WHERE title_id=?", (sid, vid))
    c.execute("UPDATE recommendations SET title_id=? WHERE title_id=?", (sid, vid))
    # deliberately no FK on drive_queue.title_id: keep queue history intact
    c.execute("UPDATE drive_queue SET title_id=? WHERE title_id=?", (sid, vid))

    # scalar folds: MAX for sticky user/system flags, MIN for "earliest"
    c.execute(
        """UPDATE titles SET
             watched_folder=MAX(watched_folder, ?),
             watched_manual=MAX(COALESCE(watched_manual,0), COALESCE(?,0)),
             history=MAX(history, ?),
             favorite=MAX(favorite, ?),
             hidden=MIN(hidden, ?),
             watched_at=MIN(watched_at, ?),
             created_at=MIN(created_at, ?),
             wanted=MAX(wanted, ?),
             wanted_note=COALESCE(wanted_note, ?),
             wanted_by=COALESCE(wanted_by, ?),
             watch_next=COALESCE(watch_next, ?)
           WHERE id=?""",
        (victim["watched_folder"] or 0, victim["watched_manual"],
         victim["history"] or 0, victim["favorite"] or 0,
         1 if victim["hidden"] else 0, victim["watched_at"],
         victim["created_at"], victim["wanted"] or 0,
         victim["wanted_note"], victim["wanted_by"], victim["watch_next"],
         sid))
    c.execute("DELETE FROM titles WHERE id=?", (vid,))


def run() -> int:
    """Heal duplicated title rows. Returns the number of rows merged away."""
    rows = [dict(r) for r in q("""SELECT id, dedupe_key, kind, title, year, tmdb_id,
                                    match_status, title_locked, kind_locked,
                                    watched_folder, watched_manual, watched_at, history,
                                    favorite, hidden, wanted, wanted_note, wanted_by,
                                    watch_next, created_at
                                  FROM titles""")]
    if not rows:
        return 0

    # ---- build merge groups ------------------------------------------------
    groups = {}  # group_key -> [rows]
    for r in rows:
        r["is_canonical"] = (r["dedupe_key"] == _canonical_key(r["kind"], r["title"], r["year"]))
        if r["tmdb_id"]:
            groups[("t", r["tmdb_id"])] = groups.get(("t", r["tmdb_id"]), []) + [r]

    for g in [v for v in groups.values() if len(v) > 1]:
        for r in g:
            r["grouped"] = True

    # folded-key passes over rows not already in a tmdb group
    for r in rows:
        if r.get("grouped"):
            continue
        fk = parser.series_title_key(r["title"])
        if not fk:
            continue
        if r["kind"] == "series":
            groups[("f", fk)] = groups.get(("f", fk), []) + [r]
        elif r["kind"] == "movie" and r["year"]:
            # year is part of a movie's identity: only the same year folds
            groups[("fy", fk, r["year"])] = groups.get(("fy", fk, r["year"]), []) + [r]

    merged = 0
    for key, g in groups.items():
        if len(g) < 2:
            continue
        # TMDB groups: same title by provider id — merge unconditionally.
        # Folded groups: only when every MATCHED member agrees on one title;
        # conflicting matches (The Office UK vs US, real remakes) stay
        # separate — suspects() reports them for review instead.
        if key[0] in ("f", "fy"):
            tmdb_ids = {r["tmdb_id"] for r in g if r["match_status"] == "matched" and r["tmdb_id"]}
            if len(tmdb_ids) > 1:
                continue
        survivor = _pick_survivor(g)
        victims = [r for r in g
                   if r["id"] != survivor["id"]
                   and not r["title_locked"] and not r["kind_locked"]]
        if not victims:
            continue
        new_title = _display_title(survivor, g)
        new_key = _canonical_key(survivor["kind"], new_title, survivor["year"])
        with tx() as c:
            # re-key first; a key collision means the canonical row already
            # exists somewhere unexpected — keep the survivor's old key then
            try:
                c.execute("UPDATE titles SET title=?, dedupe_key=? WHERE id=?",
                          (new_title, new_key, survivor["id"]))
            except Exception:
                new_key = survivor["dedupe_key"]
            for v in victims:
                _merge_into(survivor["id"], v, c)
                merged += 1
            # aggregates over the now-merged file set
            size, n_eps, n_seasons = c.execute(
                """SELECT COALESCE(SUM(size_bytes),0),
                          COUNT(DISTINCT CASE WHEN episode IS NOT NULL
                                THEN COALESCE(season,1) || '-' || episode END),
                          COUNT(DISTINCT season)
                   FROM files WHERE title_id=? AND missing=0""",
                (survivor["id"],)).fetchone()
            c.execute("UPDATE titles SET size_bytes=?, seasons=?, episode_count=? WHERE id=?",
                      (size, n_seasons or None, n_eps, survivor["id"]))
    if merged:
        from . import lut_sync
        try:
            lut_sync.request_sync("consolidate")
        except Exception:
            pass  # catalog voice lookup is best-effort
    return merged


def suspects() -> list:
    """Programmatic duplicate check: title groups the AUTO-MERGE deliberately
    leaves alone, surfaced for human review instead of being merged blind.

    Reported groups (each entry: kind, rows with ids/titles/keys, why):
    * same tmdb_id but a member is user-locked (title_locked/kind_locked)
    * folded-key group with CONFLICTING tmdb matches (Office UK vs US,
      same-year remakes) — merging these needs a human yes/no
    * near-key movies: same normalized title, year differs by 0 (no year
      info on one side) — ambiguous, reported not merged
    """
    rows = [dict(r) for r in q("""SELECT id, dedupe_key, kind, title, year,
                                    tmdb_id, match_status, title_locked,
                                    kind_locked, episode_count
                                  FROM titles""")]
    out = []
    seen = set()

    def _pack(g, why):
        out.append({
            "kind": g[0]["kind"],
            "why": why,
            "rows": [{"id": r["id"], "title": r["title"], "year": r["year"],
                      "dedupe_key": r["dedupe_key"], "tmdb_id": r["tmdb_id"],
                      "match_status": r["match_status"],
                      "locked": bool(r["title_locked"] or r["kind_locked"])}
                     for r in sorted(g, key=lambda x: x["id"])],
        })

    by_tmdb = {}
    for r in rows:
        if r["tmdb_id"]:
            by_tmdb.setdefault(r["tmdb_id"], []).append(r)
    for tid, g in by_tmdb.items():
        if len(g) > 1 and any(r["title_locked"] or r["kind_locked"] for r in g):
            _pack(g, "same tmdb_id but a row is user-locked — unlock before merging")
            for r in g:
                seen.add(r["id"])

    folded = {}
    for r in rows:
        if r["id"] in seen:
            continue
        fk = parser.series_title_key(r["title"])
        if not fk:
            continue
        k = (r["kind"], fk, r["year"] if r["kind"] == "movie" else None)
        folded.setdefault(k, []).append(r)
    for k, g in folded.items():
        if len(g) < 2:
            continue
        matched = {r["tmdb_id"] for r in g if r["match_status"] == "matched" and r["tmdb_id"]}
        if len(matched) > 1:
            _pack(g, "same loose title but CONFLICTING tmdb matches — verify before merging")
        elif not matched and k[0] == "movie":
            _pack(g, "same title; no TMDB match on either side to confirm identity")
    return out


def has_duplicates() -> bool:
    """Cheap gate: any tmdb_id owned by 2+ rows of the same kind."""
    for r in q("""SELECT tmdb_id, COUNT(*) n FROM titles
                  WHERE tmdb_id IS NOT NULL
                  GROUP BY tmdb_id HAVING n > 1"""):
        return True
    return False

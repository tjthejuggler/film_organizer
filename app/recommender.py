"""Film recommender: an LLM "researcher" that suggests movies/series the
user is likely to enjoy, researched live on the web via z.ai MCP tool-passing.

How it works
------------
* One long `chat/completions` call carries BOTH MCP servers
  (web-search-prime + web-reader) in the `tools` array — the model calls
  the tools server-side, so this file needs no MCP client code.
* The researcher receives a taste profile built from the local catalog:
  favorites, watched, wanted (+notes), seen-log, and every past
  recommendation decision with the user's free-text feedback note.
* Results land in the `recommendations` table with status='pending'
  (the queue). The queue is refilled to 30 whenever it drops to 15.
* Accepting a recommendation creates/flags a wanted titles row; rejecting
  with "seen" creates a seen-log row (history=1). Every decision with its
  note feeds the next research batch.
"""
import json
import re
import threading
import time
import uuid

import httpx

from . import config, db, llm, parser, tmdb

# queue geometry
QUEUE_TARGET = 30      # keep this many pending recommendations around
QUEUE_LOW = 15         # ...refill whenever it dips to/below this
BATCH_SIZE = 8         # titles per research run — small batches finish fast;
                       # repeated refills accrue toward the target

# the gateway is SILENT while the model runs MCP tool calls server-side —
# the read timeout must span the whole research chain, not one HTTP exchange
RESEARCH_TIMEOUT = httpx.Timeout(connect=30.0, read=900.0, write=60.0, pool=30.0)

# cap each prompt section so one giant library can't blow the context
CAP_SECTIONS = 60


def _known_title_keys() -> set:
    """Normalized names of EVERY title in the catalog (owned, wanted or
    seen). The LLM exclusion list is best-effort; this is the hard filter
    used at store AND serve time so the user never sees a title they
    already have."""
    return {parser.normalize_key(r["title"])
            for r in db.q("SELECT title FROM titles")}


def _is_known(title: str, tmdb_id=None, imdb_id=None,
              known=None) -> bool:
    """True when this title is already in the catalog: normalized-name
    match, or an exact tmdb/imdb id owned by an existing row."""
    if known is not None and parser.normalize_key(title) in known:
        return True
    if tmdb_id and db.q1("SELECT 1 FROM titles WHERE tmdb_id=?", (tmdb_id,)):
        return True
    if imdb_id and db.q1("SELECT 1 FROM titles WHERE imdb_id=?", (imdb_id,)):
        return True
    return False
# a research job running longer than this is presumed hung (MCP keep-alives
# can defeat the httpx read timeout)
STALE_AFTER_MIN = 25

MCP_SEARCH = {
    "type": "mcp",
    "mcp": {
        "server_label": "web-search-prime",
        "server_url": "https://api.z.ai/api/mcp/web_search_prime/mcp",
        "transport_type": "streamable-http",
        "allowed_tools": ["web_search_prime"],
        "headers": {},  # filled per-call from stored settings
    },
}
MCP_READER = {
    "type": "mcp",
    "mcp": {
        "server_label": "web-reader",
        "server_url": "https://api.z.ai/api/mcp/web_reader/mcp",
        "transport_type": "streamable-http",
        "allowed_tools": ["webReader"],
        "headers": {},
    },
}


def enabled() -> bool:
    return llm.enabled()


# ---- taste profile ----------------------------------------------------------

def _json_list(v):
    try:
        return json.loads(v or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def _clip(s, n=200):
    s = (s or "").strip().replace("\n", " ")
    return s[:n] + ("…" if len(s) > n else "")


def _taste_profile() -> str:
    """Serialize the user's catalog + feedback history for the researcher."""
    favs, watched, wanted, seen = [], [], [], []
    for r in db.q("SELECT * FROM titles ORDER BY id"):
        watched_flag = bool(r["watched_manual"]) if r["watched_manual"] is not None \
            else bool(r["watched_folder"])
        label = f"{r['title']}" + (f" ({r['year']})" if r["year"] else "") \
            + f" [{r['kind']}]"
        g = ", ".join(_json_list(r["genres"])[:3])
        if g:
            label += f" — {g}"
        if r["favorite"]:
            favs.append(label)
        if watched_flag:
            watched.append(label)
        if r["wanted"]:
            wanted.append(label + (f" — note: {_clip(r['wanted_note'], 120)}"
                                   if r["wanted_note"] else ""))
        if r["history"] and not watched_flag:
            seen.append(label)

    decided = []
    for r in db.q(
        """SELECT * FROM recommendations WHERE status!='pending'
           ORDER BY decided_at DESC, id DESC LIMIT ?""", (CAP_SECTIONS,)):
        verdict = "ACCEPTED (added to wanted list)" if r["status"] == "accepted" \
            else "REJECTED"
        extra = []
        if r["seen"]:
            extra.append("already seen")
        if r["liked"] is not None:
            extra.append("liked" if r["liked"] else "disliked")
        if r["feedback_note"]:
            extra.append(f'because: "{_clip(r["feedback_note"], 150)}"')
        decided.append(f"{r['title']} ({r['year'] or '?'}) [{r['kind']}] {verdict}"
                       + (" — " + "; ".join(extra) if extra else ""))

    known = []
    for r in db.q(
        """SELECT title, year, kind FROM (
               SELECT title, year, kind FROM titles
               UNION ALL
               SELECT title, year, kind FROM recommendations
           ) GROUP BY LOWER(title), kind LIMIT ?""", (CAP_SECTIONS * 3,)):
        known.append(f"{r['title']} ({r['year'] or '?'}) [{r['kind']}]")

    def block(name, items, empty):
        head = f"### {name}\n"
        return head + ("\n".join(f"- {i}" for i in items[:CAP_SECTIONS])
                       if items else f"- {empty}") + "\n"

    out = ""
    out += block("Favorites (strongest signal)", favs,
                 "none marked yet")
    out += block("Watched (owns file or marked watched)", watched,
                 "nothing recorded yet")
    out += block("Seen log (watched, no file owned)", seen,
                 "empty")
    out += block("Wanted list (already queued to acquire — do NOT re-recommend)",
                 wanted, "empty")
    out += block("Past recommendations — user decisions & feedback "
                 "(LEARN from the reasons)", decided,
                 "no decisions yet — this is the first batch")
    out += block("Titles already known to the system "
                 "(exclude ALL of these from recommendations)", known,
                 "(none)")
    return out


# ---- the researcher ---------------------------------------------------------

SYSTEM_PROMPT = """You are a personal film & series researcher for one user. \
Your job: recommend movies and series (including miniseries) this specific \
user is likely to love, based on their taste profile.

You have live web tools (web_search_prime to search, webReader to read \
pages). USE THEM: verify titles exist, check years, check ratings, look up \
where something streams, and find newer/lesser-known gems — don't rely on \
memory alone. Be efficient: at most ~12 searches for the whole batch.

Hard rules:
1. NEVER recommend anything from the "already known" exclusion list, and \
never duplicate a title anywhere else in the profile.
2. Recommend the requested number of DIFFERENT titles. Mix movies and \
series; include at most 4 miniseries per batch. Spread across eras and \
countries; at least a third should not be mega-famous blockbusters.
3. Match the taste signals: favorites and feedback notes are the strongest \
signal; watched titles calibrate what they already got around to.
4. Answer with STRICT JSON only, no markdown fences:
{"recommendations": [{
  "title": "string (official English title)",
  "year": 2024,
  "kind": "movie" | "series" | "miniseries",
  "overview": "1-3 sentence spoiler-free synopsis",
  "why": "1-2 sentences: why THIS user specifically, referencing their taste",
  "where_watch": "where it streams / how to watch (from web research)",
  "genres": ["max 3"],
  "stars": ["up to 4 lead actors"],
  "director": "name or null (movies)",
  "creator": "name or null (series)",
  "runtime": 120,
  "seasons": 2,
  "episodes": 16,
  "cert": "PG-13 / TV-MA or null",
  "rating_imdb": 8.1,
  "rating_tmdb": 7.9,
  "poster_hint": "null"
}]}

Poster/backdrop images are NOT your job (the app fetches them from TMDB). \
Numbers must be numbers, not strings. Omit fields you don't know.

CRITICAL: your FINAL message must be ONLY the JSON object — no narration, \
no progress commentary, no markdown. When you are done researching, output \
the JSON and nothing else. Research first, answer once."""

RETRY_PROMPT = """Your last message was narration, not the JSON answer. \
Output ONLY the JSON object now: {"recommendations": [...]} with exactly \
{count} title objects. No other text."""


def _mcp_tool_header_auth() -> bool:
    """Fill the MCP Authorization headers from the stored z.ai key."""
    key = db.settings_get("llm_api_key")
    if not key:
        return False
    for t in (MCP_SEARCH, MCP_READER):
        t["mcp"]["headers"] = {"Authorization": f"Bearer {key}"}
    return True


def _research_once(profile: str, count: int) -> dict:
    """Single research chat call with both MCP servers attached."""
    base = (db.settings_get("llm_base_url") or "").rstrip("/")
    key = db.settings_get("llm_api_key")
    model = db.settings_get("llm_model") or "glm-5.3-flash"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"{profile}\n### Task\nResearch the web and recommend "
                f"exactly {count} new titles now."},
        ],
        "tools": [MCP_SEARCH, MCP_READER],
        "tool_choice": "auto",
        "temperature": 0.6,
        "max_tokens": 8000,
    }
    r = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
        timeout=RESEARCH_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return {}
        return {}


def run_research(job_id: str, count: int = BATCH_SIZE) -> int:
    """One research batch: profile -> LLM(+MCP) -> rows in the queue.
    Returns the number of recommendations actually stored."""
    from .jobs import log, update

    if not _mcp_tool_header_auth():
        log(job_id, "no LLM API key configured — cannot research")
        return 0

    profile = _taste_profile()
    log(job_id, f"Taste profile built ({len(profile)} chars); asking the "
                f"researcher for {count} recommendations…")
    update(job_id, total=1, message="Researching via web search…")

    data = _research_once(profile, count)
    msg = (data.get("choices") or [{}])[0].get("message", {})
    content = msg.get("content")
    if not content and msg.get("tool_calls"):
        # some gateways return tool call transcripts without a final answer
        raise RuntimeError("model stopped after tool calls without a final answer")
    parsed = _extract_json(content or "")
    items = parsed.get("recommendations") or []

    if not items:
        # the model narrated instead of answering — one strict retry with
        # its own transcript attached so the research isn't wasted
        log(job_id, "no JSON in first answer; retrying with strict instruction")
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"{profile}\n### Task\nResearch the web and recommend "
                f"exactly {count} new titles now."},
            {"role": "assistant", "content": content or "(stopped early)"},
            {"role": "user", "content": RETRY_PROMPT.replace("{count}", str(count))},
        ]
        payload = {
            "model": db.settings_get("llm_model") or "glm-5.3-flash",
            "messages": messages,
            "tools": [MCP_SEARCH, MCP_READER],
            "tool_choice": "auto",
            "temperature": 0.3,
            "max_tokens": 8000,
        }
        base = (db.settings_get("llm_base_url") or "").rstrip("/")
        key = db.settings_get("llm_api_key")
        r = httpx.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}"},
                       json=payload, timeout=RESEARCH_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        msg = (data.get("choices") or [{}])[0].get("message", {})
        content = msg.get("content")
        parsed = _extract_json(content or "")
        items = parsed.get("recommendations") or []

    if not items:
        raise RuntimeError(f"researcher returned no recommendations "
                           f"(content: {_clip(content, 200)!r})")

    known = _known_title_keys()
    stored, skipped = 0, 0
    for it in items:
        title = (it.get("title") or "").strip()
        if not title:
            continue
        # HARD catalog filter: the prompt already lists exclusions, but the
        # model sometimes ignores them (seen live with Severance). Anything
        # the user already owns / wants / has seen is silently dropped here.
        if _is_known(title, known=known):
            skipped += 1
            from .jobs import log
            log(job_id, f"dropped (already in catalog): {title}")
            continue
        kind_raw = (it.get("kind") or "movie").lower()
        is_mini = 1 if kind_raw in ("miniseries", "mini-series", "limited series") else 0
        kind = "series" if (kind_raw.startswith("serie") or is_mini
                            or kind_raw == "tv") else "movie"
        year = it.get("year")
        try:
            year = int(year) if year else None
        except (TypeError, ValueError):
            year = None
        key = f"{('s' if kind == 'series' else 'm')}:" \
              f"{parser.normalize_key(title)}" + \
              ("" if kind == "series" else f":{year or 0}")
        # unique key needs uniqueness even between movie/series collision
        if kind == "movie":
            key = f"m:{parser.normalize_key(title)}:{year or 0}"
        else:
            key = f"s:{parser.normalize_key(title)}"
        try:
            with db.tx() as c:
                c.execute(
                    """INSERT OR IGNORE INTO recommendations(
                       dedupe_key, kind, is_miniseries, title, year, overview,
                       why, where_watch, genres, stars, director, creator,
                       runtime, seasons, episodes, cert, rating_imdb,
                       rating_tmdb, batch_id, match_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'unmatched')""",
                    (key, kind, is_mini, title, year,
                     _clip(it.get("overview"), 1200) or None,
                     _clip(it.get("why"), 400) or None,
                     _clip(it.get("where_watch"), 200) or None,
                     json.dumps([str(g) for g in (it.get("genres") or [])[:3]]),
                     json.dumps([str(s) for s in (it.get("stars") or [])[:4]]),
                     _clip(it.get("director"), 120) or None,
                     _clip(it.get("creator"), 120) or None,
                     _int_or_none(it.get("runtime")),
                     _int_or_none(it.get("seasons")),
                     _int_or_none(it.get("episodes")),
                     _clip(it.get("cert"), 20) or None,
                     _num_or_none(it.get("rating_imdb")),
                     _num_or_none(it.get("rating_tmdb")),
                     job_id))
            if db.q1("SELECT 1 FROM recommendations WHERE dedupe_key=? AND batch_id=?",
                     (key, job_id)):
                stored += 1
                _enrich_rec(db.q1(
                    "SELECT id FROM recommendations WHERE dedupe_key=?", (key,))["id"])
            else:
                skipped += 1
        except Exception:
            skipped += 1

    log(job_id, f"Research batch done: {stored} new, {skipped} skipped "
                f"(duplicates/invalid)")
    update(job_id, progress=1, message=f"Batch done: +{stored} recommendations")
    return stored


def _int_or_none(v):
    try:
        return int(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _num_or_none(v):
    try:
        return float(v) if v not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


# ---- TMDB enrichment for a queued recommendation ----------------------------

def _enrich_rec(rid: int):
    """Best-effort TMDB match for one recommendation row: fills poster,
    ratings, cast, cert. LLM-provided fields are only overwritten with
    confirmed data (match_status flips to 'matched').
    Also the TMDB-level catalog check: when the matched tmdb/imdb id is
    already owned by a catalog row, the recommendation is dropped — the
    LLM may use a variant title ('Mindhunter' vs 'Mindhunters') that the
    name filter cannot see."""
    try:
        rec = db.q1("SELECT * FROM recommendations WHERE id=?", (rid,))
        if not rec or not tmdb.enabled():
            return
        title, year, kind = rec["title"], rec["year"], rec["kind"]
        cands = (tmdb.search_tv(title, year) if kind == "series"
                 else tmdb.search_movie(title, year))
        best = tmdb.best_candidate(cands, threshold=0.6)
        if best is None:
            return
        detail = (tmdb.tv_detail(best["id"]) if kind == "series"
                  else tmdb.movie_detail(best["id"]))
        if _is_known(rec["title"],
                     tmdb_id=detail.get("tmdb_id"),
                     imdb_id=detail.get("imdb_id")):
            with db.tx() as c:
                c.execute("DELETE FROM recommendations WHERE id=?", (rid,))
            return
        try:
            detail["cert"] = tmdb.cert(best["id"], kind) or rec["cert"]
        except Exception:
            detail["cert"] = rec["cert"]
        sets, vals = [], []
        colmap = {
            "tmdb_id": "tmdb_id", "imdb_id": "imdb_id", "title": "title",
            "year": "year", "overview": "overview", "genres": "genres",
            "stars": "stars", "director": "director", "creator": "creator",
            "runtime": "runtime", "seasons": "seasons", "episodes": "episodes",
            "cert": "cert", "rating_tmdb": "rating_tmdb", "poster": "poster",
            "backdrop": "backdrop",
        }
        for k, col in colmap.items():
            v = detail.get(k)
            if v is None:
                continue
            if k in ("genres", "stars"):
                v = json.dumps(v or [])
            sets.append(f"{col}=?")
            vals.append(v)
        sets.append("match_status='matched'")
        vals.append(rid)
        with db.tx() as c:
            c.execute(f"UPDATE recommendations SET {', '.join(sets)} WHERE id=?", vals)
    except Exception:
        pass  # enrichment is cosmetic; the LLM data still shows


# ---- queue helpers ----------------------------------------------------------

def pending_count() -> int:
    return db.q1("SELECT COUNT(*) n FROM recommendations WHERE status='pending'")["n"]


def refill_guard() -> bool:
    """Claim the right to run one refill. Returns False if one is already
    queued/running (row type='recommend' not finished). A running job older
    than STALE_AFTER_MIN is presumed hung (MCP chains can stall without
    triggering the read timeout): it is marked failed so a fresh refill
    can start."""
    running = db.q1(
        """SELECT id, created_at FROM jobs WHERE type='recommend'
           AND status='running' ORDER BY created_at DESC LIMIT 1""")
    if not running:
        return True
    started = running["created_at"] or ""
    try:
        from datetime import datetime, timezone
        t0 = datetime.fromisoformat(started)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)  # SQLite datetime('now') is naive UTC
        age_min = (datetime.now(timezone.utc) - t0).total_seconds() / 60
    except ValueError:
        return True
    if age_min > STALE_AFTER_MIN:
        from . import jobs
        jobs.log(running["id"], f" presumed hung after {age_min:.0f} min — marking failed")
        jobs.finish(running["id"], error="research job timed out (stale)")
        return True
    return False


def maybe_refill(reason: str = "auto") -> str | None:
    """Start a refill job when the queue is at/below QUEUE_LOW.
    Returns the job id or None."""
    if pending_count() > QUEUE_LOW:
        return None
    if not enabled():
        return None
    if not refill_guard():
        return None
    from . import jobs
    jid = jobs.create("recommend", total=1)
    jobs.log(jid, f"Refill triggered ({reason}): "
                  f"{pending_count()} pending -> target {QUEUE_TARGET}")
    need = max(BATCH_SIZE, QUEUE_TARGET - pending_count())
    jobs.run_background(jid, lambda j: run_research(j, count=min(need, BATCH_SIZE)))
    return jid


# ---- decide -----------------------------------------------------------------

def decide(rec_id: int, decision: str, note: str = None,
           liked: bool = None, seen: bool = False):
    """Apply the user's verdict on a recommendation.
    decision: 'accepted' | 'rejected'
    Returns (rec_row, title_id_or_None, created_bool)."""
    from .scanner import dedupe_key
    from .jobs import now_iso

    rec = db.q1("SELECT * FROM recommendations WHERE id=?", (rec_id,))
    if not rec:
        raise ValueError("recommendation not found")
    if decision not in ("accepted", "rejected"):
        raise ValueError("decision must be 'accepted' or 'rejected'")

    title_id, created = None, False
    needs_title = decision == "accepted" or (decision == "rejected" and seen)
    if needs_title:
        key = dedupe_key(rec["kind"], rec["title"], rec["year"])
        existing = db.q1("SELECT * FROM titles WHERE dedupe_key=?", (key,))
        if existing:
            title_id = existing["id"]
            sets, vals = [], []
            if decision == "accepted":
                sets.append("wanted=1")
                if rec["why"]:
                    sets.append("wanted_note=COALESCE(?, wanted_note)")
                    vals.append(f"Recommended: {rec['why']}")
            if seen:
                sets.append("history=1, watched_manual=1, "
                            "watched_at=COALESCE(watched_at, ?)")
                vals.append(now_iso())
            if sets:
                vals.append(title_id)
                with db.tx() as c:
                    c.execute(f"UPDATE titles SET {', '.join(sets)} WHERE id=?", vals)
        else:
            # create a fresh row carrying over the (possibly TMDB-matched)
            # recommendation data so the catalog looks complete instantly
            with db.tx() as c:
                c.execute(
                    """INSERT INTO titles(
                       dedupe_key, kind, title, year, match_status, tmdb_id,
                       imdb_id, overview, genres, stars, director, creator,
                       runtime, seasons, episodes, cert, rating_imdb,
                       rating_tmdb, rating_rt, poster, backdrop, data_source,
                       wanted, wanted_note, wanted_by, history, watched_manual,
                       watched_at, cataloged_at, is_miniseries)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, rec["kind"], rec["title"], rec["year"],
                     "matched" if rec["match_status"] == "matched" else "unmatched",
                     rec["tmdb_id"], rec["imdb_id"], rec["overview"],
                     rec["genres"], rec["stars"], rec["director"], rec["creator"],
                     rec["runtime"], rec["seasons"], rec["episodes"], rec["cert"],
                     rec["rating_imdb"], rec["rating_tmdb"], rec["rating_rt"],
                     rec["poster"], rec["backdrop"],
                     "recommender",
                     1 if decision == "accepted" else 0,
                     f"Recommended: {rec['why']}" if (decision == "accepted" and rec["why"]) else None,
                     "recommender",
                     1 if (decision == "rejected" and seen) else 0,
                     1 if seen else 0,
                     now_iso() if seen else None,
                     now_iso(),
                     rec["is_miniseries"]))
            title_id = db.q1("SELECT id FROM titles WHERE dedupe_key=?", (key,))["id"]
            created = True

    with db.tx() as c:
        c.execute(
            """UPDATE recommendations SET status=?, feedback_note=?,
               liked=?, seen=?, decided_at=?, title_id=? WHERE id=?""",
            (decision, note, None if liked is None else (1 if liked else 0),
             1 if seen else 0, now_iso(), title_id, rec_id))
    return db.q1("SELECT * FROM recommendations WHERE id=?", (rec_id,)), title_id, created


# ---- payloads ---------------------------------------------------------------

def rec_payload(row) -> dict:
    d = dict(row)
    for k in ("genres", "stars"):
        d[k] = _json_list(d.get(k))
    d["is_miniseries"] = bool(d.get("is_miniseries"))
    d["seen"] = bool(d.get("seen"))
    d["liked"] = (bool(d["liked"]) if d.get("liked") is not None else None)
    return d

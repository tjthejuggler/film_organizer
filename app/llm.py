"""Optional LLM helper (any OpenAI-compatible endpoint).

Used when a raw folder/file name is too messy for a confident TMDB match:
the LLM extracts a clean {title, year, kind} which we then re-search.
"""
import json
import re

import httpx

from . import db


def enabled() -> bool:
    return bool(db.settings_get("llm_api_key")) and bool(db.settings_get("llm_base_url"))


def _chat(messages: list, force_json=True):
    base = (db.settings_get("llm_base_url") or "").rstrip("/")
    key = db.settings_get("llm_api_key")
    model = db.settings_get("llm_model") or "glm-5.3-flash"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
    }
    if force_json:
        payload["response_format"] = {"type": "json_object"}

    def _post():
        return httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=30,
        )

    r = _post()
    if r.status_code >= 400 and force_json:
        # some providers/models reject response_format; retry plain and
        # rely on the robust JSON extraction in _parse_json()
        payload.pop("response_format", None)
        r = _post()
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def probe():
    """Connectivity test used by the Settings 'Test LLM' button.
    Returns (ok, detail). Unlike clean_title, errors are NOT swallowed."""
    if not enabled():
        return False, "no LLM API key configured (paste your z.ai key above)"
    try:
        content = _chat([
            {"role": "system", "content": 'Reply with JSON {"ok": true}'},
            {"role": "user", "content": "ping"},
        ])
        return True, f"model replied: {content[:100]}"
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code} from {db.settings_get('llm_base_url')}: {e.response.text[:200]}"
    except Exception as e:
        return False, str(e)[:300]


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}


def clean_title(raw_name: str, hint_kind: str = None):
    """Return {title, year, kind} or None."""
    if not enabled():
        return None
    sys = (
        "You clean up movie/TV release names. Given a messy release or folder "
        "name, return STRICT JSON: {\"title\": <clean movie or show name>, "
        "\"year\": <release year int or null>, \"kind\": \"movie\"|\"series\"}. "
        "Strip scene tags, quality, codecs, release groups, site names. Keep "
        "original-language titles as commonly listed on IMDb/TMDB. A course or "
        "masterclass with numbered lessons counts as \"series\"."
    )
    user = raw_name if not hint_kind else f"{raw_name} (probably a {hint_kind})"
    try:
        content = _chat([
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ])
        d = _parse_json(content)
        if not d.get("title"):
            return None
        year = d.get("year")
        kind = d.get("kind") if d.get("kind") in ("movie", "series") else (hint_kind or "movie")
        return {"title": str(d["title"]).strip(), "year": int(year) if year else None, "kind": kind}
    except Exception:
        return None

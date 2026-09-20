"""Parse release/folder names into (kind, title, year, season, episode).

Also implements the watched-folder marker rules:
  * any ancestor directory whose normalised name is in WATCHED_MARKERS
    ("watched", "awatched", "aawatched", "aawatchedd") means WATCHED
  * "unwatched", "aunwatched", "aaunwatched" explicitly mean NOT watched
  * the NEAREST ancestor marker wins; exact-name matching (never substring)
    so "aaUnwatched" can never be mistaken for a watched folder.
"""
import os
import re

VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".mpe", ".m4v",
    ".webm", ".ts", ".m2ts", ".m2v", ".flv", ".ogv", ".divx", ".3gp", ".asf",
    ".rm", ".rmvb", ".vob", ".iso", ".qt",
}

WATCHED_MARKERS = {"watched", "awatched", "aawatched", "aawatchedd"}
UNWATCHED_MARKERS = {"unwatched", "aunwatched", "aaunwatched"}
# folder spellings that mark a title as a miniseries (kind stays 'series')
MINISERIES_MARKERS = {"miniseries", "aminiseries", "aaminiseries",
                      "mini-series", "limitedseries", "limited-series"}

SAMPLE_RE = re.compile(r"(^|[^a-z])sample([^a-z]|$)", re.I)
SEASON_DIR_RE = re.compile(r"^(?:s|season|series|saison)\s*\.?\s*(\d{1,2})$|^specials?$|^extras?$", re.I)
MASTERCLASS_RE = re.compile(r"masterclass|master\s?class|course|tutorial|udemy|lessons?", re.I)
# camera/phone default clip names: "20210710 142223", "20250113_150345",
# "IMG_20210924_210805", "VID_20210924_210805", "DSC_0123", "PXL_20250113_150345".
# These are raw recordings, never catalogued movie/show titles. (Only
# unambiguous maker prefixes are listed — no 'MOV', which could collide
# with real titles.)
CAMERA_CLIP_RE = re.compile(
    r"(?:(?:IMG|VID|DSC|DJI|MVIMG|PXL)[\s_.\-][\d\s.\-_]*"
    r"|\d{8}[\s_.\-]?\d{6}(?:[\s_.\-]\d+)?)$", re.I)

VLC_RE = re.compile(r"^vlc[\-._ ]?record[\-._ ]?\d{4}[\-._ ]?\d{2}[\-._ ]?\d{2}[\-._ ]?\d{2}h\d{2}m\d{2}s[\-._ ]*", re.I)
SITE_RE = re.compile(
    # NOTE: .us/.uk are deliberately NOT in the TLD list — in release names
    # "Show.US.S02E01" / "Show.UK.S01E01" spell the COUNTRY, and no real
    # release site sits on .us/.uk; treating them as sites used to eat the
    # whole title ("Euphoria.US..." -> empty -> one junk row per file)
    r"^\s*(?:www\.)?[\w\-]{2,40}\.(?:org|com|net|io|tv|co|cc|me|xyz|to|st|biz|info|site|online|club|ru"
    r"|su|pw|top|vip|pro|icu|cyou|cfd|sbs|pics|cam|fun|link|live|one|now|page|app|dev|fyi|gg|fm)\b[\s._\-]*",
    re.I,
)
BRACKET_RE = re.compile(r"\[[^\[\]]*\]")
PAREN_RE = re.compile(r"\(([^()]*)\)")
YEAR_RE = re.compile(r"(?:18|19|20)\d{2}")

SXE_RE = re.compile(r"\bs(\d{1,2})[\s._\-]?e(\d{1,3})(?:[\s._\-]?e(\d{1,3}))?\b", re.I)
ALT_SXE_RE = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I)
SEASON_TAG_RE = re.compile(r"\bs(\d{1,2})\b", re.I)
SEASON_WORD_RE = re.compile(r"\bseason[\s._]?(\d{1,2})\b", re.I)
EPISODE_WORD_RE = re.compile(r"\bepisode[\s._]?(\d{1,3})\b", re.I)
EP_TAG_RE = re.compile(r"\be(?:p)?[\s._]?(\d{1,3})\b", re.I)
# documentary packs: "Series 2 03of10 ..." or bare "04of12"
SERIES_WORD_RE = re.compile(r"\bseries[\s._]?(\d{1,2})\b", re.I)
OF_RE = re.compile(r"\b(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b", re.I)
# "Title - 101 - Episode Name" pod numbering: leading digit(s) season, last
# two episode ("101" -> S01E01). Year-like pods (1917/2019) are excluded —
# those dashes separate a title from a year, not an episode tag.
DASHED_EP_RE = re.compile(
    r"^(?P<title>.+?)\s*[-\u2013\u2014]\s*"
    r"(?P<pod>(?!(?:18|19|20)\d{2}\b)\d{3,4})\s*[-\u2013\u2014]\s+\S")

# --- junk-token machinery -------------------------------------------------
_MISC_WORDS = {
    "complete", "internal", "limited", "unrated", "uncut", "extended",
    "remastered", "repack", "proper", "dual", "dualaudio", "multiaudio",
    "nordic", "norsub", "norwegian", "swedish", "danish", "finnish",
    "icelandic", "french", "german", "italian", "spanish", "russian",
    "hindi", "tamil", "telugu", "korean", "japanese", "chinese", "mandarin",
    "cantonese", "subbed", "subs", "sub", "dubbed", "dub", "eng", "english",
    "criterion", "imax", "uhd", "sdr", "proof", "obs", "www", "season",
    "episode", "specials", "extras", "sample", "trailer", "vol", "part",
}
_GROUP_WORDS = {
    "rarbg", "eztv", "eztvre", "tgx", "psa", "yify", "etrg", "rmteam",
    "galaxyrg", "galaxytv", "galaxyrg265", "publichd", "1337x", "hdb",
    "jyk", "cadaver", "mhq", "ccow", "mqnecadd", "cbrg", "shaanig", "fgt",
}
_RE_CODEC = re.compile(r"^(?:[xh]?26[45]|x264|x265|hevc|av1|xvid|divx)$", re.I)
_RE_RES = re.compile(r"^\d{3,4}[pi]$", re.I)
_RE_AUDIO = re.compile(
    r"^(?:aac|ac3|eac3|ddp|dd|ddplus|dts|dtshd|dtsma|truehd|truehdd|atmos|"
    r"flac|opus|mp3|lpcm|pcm)\d*(?:ch)?$", re.I)
_RE_CHAN = re.compile(r"^\d+ch$|^[2568][01]$", re.I)
_RE_BIT = re.compile(r"^\d{1,2}bit$", re.I)
_RE_SOURCE = re.compile(
    r"^(?:webdl|webrip|web|bluray|blu|bdrip|bdremux|brrip|remux|dvdrip|dvd|"
    r"hddvd|hdtv|pdtv|hdrip|dsnp|nf|amzn|atvp|hmax|pmtp|itv|vhs|hc|cam|"
    r"screener|dubbed)$", re.I)
_RE_RANGE = re.compile(r"^(?:hdr10plus|hdr10|hdr|dv|dovi|dolbyvision)$", re.I)
_RE_SE_TAG = re.compile(r"^(?:s\d{1,3}|e(?:p)?\d{1,4})$", re.I)
# multi-file discs: "Tampopo.cd1"/"Movie disc2"/"...pt3" — the disc tag is
# junk, NOT part of the title (it used to key "Tampopo cd2" as its own movie)
_RE_DISC = re.compile(r"^(?:cd|disc|dvd|part|pt)\d{1,2}$", re.I)


def _part_is_junk(part: str) -> bool:
    c = re.sub(r"[^a-z0-9+]", "", part.lower())
    if not c:
        return True
    if YEAR_RE.fullmatch(c):
        return True
    if _RE_RES.match(c):
        return True
    if _RE_CODEC.match(c):
        return True
    if _RE_AUDIO.match(c):
        return True
    if _RE_CHAN.match(c):
        return True
    if _RE_BIT.match(c):
        return True
    if _RE_SOURCE.match(c):
        return True
    if _RE_RANGE.match(c):
        return True
    if _RE_SE_TAG.match(c):
        return True
    if _RE_DISC.match(c):
        return True
    return c in _MISC_WORDS or c in _GROUP_WORDS


def _token_is_junk(tok: str) -> bool:
    """A token is junk if any of its separator-split parts is junk.

    Splitting on [._-] lets "WEB-DL", "x264-HDB", "AAC2.0" all match while
    keeping real titles like "Spider-Man" or "Ready or Not 2" intact.
    """
    parts = [p for p in re.split(r"[._\-]+", tok) if p]
    if not parts:
        return True
    return any(_part_is_junk(p) for p in parts)


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" .,_-:;—–")
    s = re.sub(r"\s*[-–—]\s*$", "", s)
    return s.strip()


def normalize_key(title: str) -> str:
    # "&" is spelled out so "Life & Times" / "Life and Times" fold to one key
    return re.sub(r"[^a-z0-9]", "", re.sub(r"&", " and ", (title or "").lower()))


def _strip_wrappers(raw: str) -> str:
    s = VLC_RE.sub("", raw)
    s = SITE_RE.sub("", s)
    s = BRACKET_RE.sub(" ", s)

    def _paren(m: re.Match) -> str:
        inner = m.group(1).strip()
        if YEAR_RE.fullmatch(inner):
            return f" {inner} "
        return " "

    s = PAREN_RE.sub(_paren, s)
    return s.replace(".", " ").replace("_", " ")


def parse_name(raw: str) -> dict:
    """Parse a file name (extension already stripped) or directory name.

    Strategy: find structural tags (SxxEyy / season / episode) first - the
    title region is everything before the earliest tag. Inside that region
    walk tokens left-to-right: standalone years are remembered (last wins,
    and the title ends before the first year), hard junk (quality / codec /
    group / source) ends the title, pure punctuation tokens are skipped.
    """
    s = _strip_wrappers(raw)
    s = re.sub(r"\s+", " ", s).strip()

    season = episode = end_episode = year = None
    tag_starts = []

    # "Title - 101 - Episode Name" pod numbering (S01E01 in disguise):
    # season+episode come from the pod, the title is what precedes it
    md = DASHED_EP_RE.match(s)
    if md and not (SXE_RE.search(s) or ALT_SXE_RE.search(s)):
        season = int(md.group("pod")[:-2]) or 1
        episode = int(md.group("pod")[-2:])
        tag_starts.append(md.start("pod"))

    m = SXE_RE.search(s) or ALT_SXE_RE.search(s)
    if m:
        season = int(m.group(1))
        episode = int(m.group(2))
        if m.lastindex and m.lastindex >= 3 and m.group(3):
            end_episode = int(m.group(3))
        tag_starts.append(m.start())
    else:
        sw = SEASON_WORD_RE.search(s)
        st = None if sw else SEASON_TAG_RE.search(s)
        if sw:
            season = int(sw.group(1))
            tag_starts.append(sw.start())
        elif st:
            season = int(st.group(1))
            tag_starts.append(st.start())
        ew = EPISODE_WORD_RE.search(s)
        if not ew and season is not None:
            ew = EP_TAG_RE.search(s)
        if ew:
            episode = int(ew.group(1))
            tag_starts.append(ew.start())
        # "Series 2 03of10" / bare "03of10" packs: "series" only counts as a
        # season word when an NNofMM tag backs it up ("Series 7" the film
        # must stay a movie)
        if episode is None:
            mof = OF_RE.search(s)
            if mof:
                episode = int(mof.group(1))
                tag_starts.append(mof.start())
                if season is None:
                    msw = SERIES_WORD_RE.search(s)
                    if msw:
                        season = int(msw.group(1))
                        tag_starts.append(msw.start())

    region = s[: min(tag_starts)] if tag_starts else s

    year = None
    year_start = None
    cut = len(region)
    first_alnum = True
    for mt in re.finditer(r"\S+", region):
        tok = mt.group(0)
        if not any(ch.isalnum() for ch in tok):
            continue  # standalone punctuation ("-", "|"): keep, tidy() strips it
        core = tok.strip(".-_")
        if YEAR_RE.fullmatch(core):
            # the LAST standalone year is the release year; the title is
            # everything before it (handles "1917.2019.1080p" -> title 1917)
            year = int(core)
            year_start = mt.start()
            continue
        if first_alnum:
            # the first real token is never junk-cut: release grammars put
            # the title first, so a codec/channel-looking word there IS the
            # title ("Opus" the film vs the opus codec, "20" of "20 Days in
            # Mariupol" vs the 2.0 channel tag) — previously both titles
            # were eaten whole and the raw filename became the row title
            first_alnum = False
        elif _token_is_junk(tok):
            cut = mt.start()
            break

    if year_start is not None:
        pre = region[:year_start]
    else:
        pre = region[:cut]

    title = _tidy(pre)
    if not title and year:
        title = str(year)

    return {
        "raw": raw,
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "end_episode": end_episode,
        "is_series": season is not None or episode is not None,
        "has_meta": cut < len(region) or year is not None or bool(tag_starts),
    }


def norm_component(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.I)
_TRAILING_COUNTRY_RE = re.compile(r"\s+(us|uk)$", re.I)


def series_title_key(title: str) -> str:
    """Loose series identity: normalized key with the leading article
    dropped and a trailing US/UK country token folded away, so
    'Righteous Gemstones' == 'The Righteous Gemstones' and
    'Euphoria US' == 'Euphoria'. Year is deliberately NOT part of a
    series identity. Only for GROUPING — display titles stay untouched."""
    t = _TRAILING_COUNTRY_RE.sub("", (title or "").strip())
    return re.sub(r"^the", "", _ARTICLE_RE.sub("", normalize_key(t)))


def is_camera_name(stem: str) -> bool:
    """True when a file stem is a camera/phone default clip name
    (timestamp stamp or IMG_/VID_/DSC_ style)."""
    return bool(CAMERA_CLIP_RE.match((stem or "").strip()))


def watched_marker(path: str):
    """Walk ancestors of `path` deepest-first; return True (watched),
    False (explicitly unwatched marker), or None (no marker found)."""
    cur = os.path.abspath(path)
    while True:
        n = norm_component(os.path.basename(cur))
        if n in WATCHED_MARKERS:
            return True
        if n in UNWATCHED_MARKERS:
            return False
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def is_pure_season_dir(name: str):
    """Return season number if the directory name is *only* a season dir."""
    m = SEASON_DIR_RE.match(name.strip())
    return int(m.group(1)) if m and m.group(1) else (0 if m else None)


def in_miniseries_folder(path: str) -> bool:
    """True when any ancestor folder is named 'miniseries' (any aa*/a*
    spelling). Used to tag rows as miniseries at scan time — still a
    series (kind='series'), just flagged."""
    cur = os.path.abspath(path)
    while True:
        if norm_component(os.path.basename(cur)) in MINISERIES_MARKERS:
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def tokens(text: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

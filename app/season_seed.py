"""Web-researched seed data for the season calendar (researched 2026-09-10).

Seeds pre-fill `season_watch` rows with verified release info gathered from
press releases / trade sites. The periodic TMDB poll (seasons.py) keeps them
fresh afterwards: it promotes vague windows to exact dates and marks shows
finished. Rows are inserted ON CONFLICT DO NOTHING, so re-seeding never
overwrites fresher poll data.

status:  announced  — exact date(s) known (release_start[/release_end])
         vague      — rough window only (window_hint), re-checked weekly
         done       — series finished, finale aired (finished=1)
release_kind: all_at_once | weekly | unknown
"""
from datetime import datetime, timedelta, timezone


def _in_days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat(timespec="seconds")


SEEDS = {
    # ---- exact dates (announced) ------------------------------------------
    247718: [  # MobLand
        dict(season=2, status="announced", release_kind="weekly",
             release_start="2026-09-18", release_end="2026-11-20",
             source="web: Paramount+ press", next_check_at=None,
             note="10 episodes, one per Friday"),
    ],
    273247: [  # The Lowdown
        dict(season=2, status="announced", release_kind="weekly",
             release_start="2026-10-14", release_end="2026-12-02",
             source="web: FX/Variety", next_check_at=None,
             note="8 episodes, weekly Wednesdays (FX / Hulu)"),
    ],
    117488: [  # Yellowjackets
        dict(season=4, status="announced", release_kind="weekly",
             release_start="2026-11-20", release_end="2027-01-08",
             source="web: Paramount+ / Variety", next_check_at=None,
             note="final season · streams Fridays on Paramount+ (Showtime airs Sundays); finale date estimated"),
    ],
    97546: [  # Ted Lasso
        dict(season=4, status="announced", release_kind="weekly",
             release_start="2026-08-05", release_end="2026-10-07",
             source="web: Apple TV press", next_check_at=None,
             note="weekly Wednesdays through the Oct 7 finale"),
    ],
    125988: [  # Silo
        dict(season=4, status="announced", release_kind="weekly",
             release_start="2027-07-09", release_end="2027-09-10",
             source="web: Apple TV press", next_check_at=None,
             note="fourth and FINAL season · finale ~Sep 10, 2027"),
    ],
    # ---- vague windows (re-checked weekly) --------------------------------
    222766: [  # The Day of the Jackal
        dict(season=2, status="vague", window_hint="2027",
             source="web: Peacock/Sky upfront", next_check_at=_in_days(7),
             note="announced for 2027 at upfronts; no date yet"),
    ],
    106379: [  # Fallout
        dict(season=3, status="vague", window_hint="2027",
             source="web: Forbes/press", next_check_at=_in_days(7),
             note="expected ~Aug 2027; filming through 2026"),
    ],
    287527: [  # The Testaments
        dict(season=2, status="vague", window_hint="2027",
             source="web: Hulu/Deadline", next_check_at=_in_days(7),
             note="renewed ahead of the S1 finale; filming since Aug 2026"),
    ],
    124364: [  # FROM
        dict(season=5, status="vague", window_hint="2027",
             source="web: MGM+ press", next_check_at=_in_days(7),
             note="fifth and final season renewed; S4 aired spring 2026"),
    ],
    42009: [  # Black Mirror
        dict(season=8, status="vague", window_hint="2027",
             source="web: Variety", next_check_at=_in_days(7),
             note="season 8 in production (since Aug 2026)"),
    ],
    250307: [  # The Pitt
        dict(season=3, status="vague", window_hint="Jan 2027",
             release_kind="weekly", source="web: HBO Max teaser", next_check_at=_in_days(7),
             note="HBO Max confirmed January 2027 window"),
    ],
    126308: [  # Shōgun
        dict(season=2, status="vague", window_hint="early 2027",
             release_kind="weekly", source="web: FX/Hulu", next_check_at=_in_days(7),
             note="S2+S3 ordered; production began Jan 2026"),
    ],
    60622: [  # Fargo
        dict(season=6, status="vague", window_hint="2027",
             release_kind="weekly", source="web: FX", next_check_at=_in_days(7),
             note="2027 launch reported; not officially dated"),
    ],
    245703: [  # Dept. Q
        dict(season=2, status="vague", window_hint="2027",
             source="web: Netflix Tudum", next_check_at=_in_days(7),
             note="in production, wraps ~Dec 2026"),
    ],
    156933: [  # Presumed Innocent
        dict(season=2, status="vague", window_hint="2026–2027",
             release_kind="weekly", source="web: Apple TV", next_check_at=_in_days(7),
             note="S2 filming; no date announced"),
    ],
    115004: [  # Mare of Easttown
        dict(season=2, status="vague", window_hint="2027",
             release_kind="weekly", source="web: Deadline", next_check_at=_in_days(7),
             note="Winslet: filming 2027"),
    ],
    204284: [  # The Rehearsal
        dict(season=3, status="vague", window_hint="2027",
             source="web: HBO", next_check_at=_in_days(7),
             note="in production; expected 2027"),
    ],
    262819: [  # LOL: Last One Laughing UK
        dict(season=3, status="vague", window_hint="2027",
             source="web: Prime Video", next_check_at=_in_days(7),
             note="S2 aired Mar 2026; S3 slated for 2027"),
    ],
    # ---- no date at all yet (TBA, still re-checked) ------------------------
    59186: [  # Impractical Jokers
        dict(season=13, status="vague", window_hint=None,
             source="web: TBS", next_check_at=_in_days(7),
             note="S12 ended Mar 2026; next season not announced"),
    ],
    88989: [  # Nine Perfect Strangers
        dict(season=3, status="vague", window_hint=None,
             source="web: Hulu", next_check_at=_in_days(7),
             note="S3 not officially renewed yet"),
    ],
    243316: [  # The Four Seasons
        dict(season=3, status="vague", window_hint=None,
             release_kind="all_at_once", source="web: Netflix", next_check_at=_in_days(7),
             note="renewed June 2026 (S2 dropped May 28, 2026)"),
    ],
    228878: [  # Common Side Effects
        dict(season=2, status="vague", window_hint=None,
             release_kind="weekly", source="web", next_check_at=_in_days(7),
             note="renewed for season 2; date TBA"),
    ],
    291506: [  # Big Mistakes
        dict(season=2, status="vague", window_hint=None,
             source="web", next_check_at=_in_days(7),
             note="no season 2 news yet"),
    ],
    248394: [  # FOREVER
        dict(season=2, status="vague", window_hint=None,
             source="web", next_check_at=_in_days(7),
             note="next season not announced yet"),
    ],
    254953: [  # Rooster
        dict(season=2, status="vague", window_hint=None,
             source="web", next_check_at=_in_days(7),
             note="next season not announced yet"),
    ],
    # ---- finished (finale aired — no more checking) ------------------------
    233629: [  # Bodies (miniseries)
        dict(season=1, status="done", finished=1,
             source="web: Netflix", next_check_at=None,
             note="miniseries — limited run, story closed"),
    ],
}

# entries whose catalog row may lack a tmdb_id: resolved by title
# (exact -> prefix LIKE)
SEEDS_BY_TITLE = {
    "The Diplomat": [dict(kind="series", season=4, status="announced",
                          release_kind="all_at_once",
                          release_start="2026-10-15", release_end="2026-10-15",
                          source="web: Netflix Tudum", next_check_at=None,
                          note="all 8 episodes drop at once")],
    "Black Doves": [dict(kind="series", season=2, status="announced",
                         release_kind="all_at_once",
                         release_start="2026-11-05", release_end="2026-11-05",
                         source="web: Netflix Tudum / whats-on-netflix",
                         next_check_at=None,
                         note="Netflix confirmed 5 Nov 2026; catalog has episode-rip rows")],
    "Museum of Innocence": [dict(kind="series", season=1, status="done",
                                 finished=1,
                                 source="web: Netflix/Variety", next_check_at=None,
                                 note="limited series — 9 episodes dropped 13 Feb 2026")],
}

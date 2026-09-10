# Film Organizer

A local web app that catalogs movies & series scattered across your laptop and
external drives, enriches them with metadata (posters, cast, creators, IMDb /
TMDB ratings), tracks what is watched, and tells you **where** to find
anything when you want to watch it.

## Run

```bash
./run.sh
# then open http://127.0.0.1:8765
```

First run creates a venv, installs deps, seeds the default library roots
(`~/Videos`, `~/Downloads`) and starts the server. Roots are configurable
in **⚙ Settings**.

## First-time setup

1. Open **⚙ Settings**
2. Paste a **TMDB API key (v3)** — free at themoviedb.org → this powers all
   metadata (release years, stars, directors, creators, networks, posters,
   ratings, genres).
3. Optional: **OMDb API key** (free at omdbapi.com) for IMDb ratings & votes.
4. Click **⟳ Scan** to catalog files, then **✨ Enrich** to fetch metadata.

The LLM is preconfigured for **z.ai** (`https://api.z.ai/api/paas/v4`, model
`glm-5.3-flash`) — just paste your z.ai API key. It is used to clean up messy
release names when normal TMDB matching fails. Use the **Test TMDB** /
**Test LLM** buttons in Settings to verify keys immediately.

## Features

- **Scans** any number of library roots (external drives: just add the mount
  path in Settings; unplugged drives show as *offline* and their catalog is
  kept, files merely flagged missing).
- **Grouping** — episode files collapse into a single series row (even across
  roots); movie releases in folders collapse into a single movie row.
- **Watched tracking, two layers**
  - *Folder rule*: anything under a directory named (in any spelling)
    `Watched / aWatched / aaWatched / aaWatchedd` is auto-watched;
    `aUnwatched / aaUnwatched` is explicitly NOT watched — matching is
    exact-name so they never collide.
  - *Manual*: the ✓ button overrides the folder rule per title (folder rule
    wins only until you click).
- **date cataloged** — every title records when the system first cataloged it
  (sortable column).
- **Description toggle** — the detail drawer hides the synopsis behind a
  *Show description* button to keep the drawer compact.
- **Move internal ⇄ external** — set the *Internal folder* (this computer)
  and *External folder* (drive) in Settings; each title's drawer then has
  **💻 Internal** / **🔌 External** buttons. Files move with their folder
  structure, the catalog updates instantly (no rescan needed), emptied
  source folders are pruned, watched-folder rules are re-evaluated at the
  destination, and the destination is auto-registered as a library root.
  Unplugged drive → clear error, nothing moves.
- **Ratings on the main list** — content certification badges (PG-13, R,
  TV-MA…), a sortable **IMDb** column (logo header), and a sortable
  **🍅 Rotten Tomatoes** column (≥60% shows as fresh-red, below as
  splat-blue). Genre chips (top 3) sit under each title. Data comes from
  TMDB (certifications) + OMDb (RT scores, IMDb). `POST
  /api/backfill-ratings` backfills existing rows; new enrichments include
  them automatically.
- **Duplicates** — the ⧉ Duplicates view finds the same movie in multiple
  places, or the same episodes of a series doubled across roots (a series
  merely *split* across roots is not flagged). Each group shows the size of
  every copy and the difference, sorted biggest-first; delete the copy you
  don't want. Safety: the server refuses to delete a copy unless another
  real copy of the same title still exists on disk — deleting the only
  copy through this view is impossible.
- **Sort / filter / search** — by title, year, rating, size, cataloged date,
  watched state; filter by kind, genre, location root, match status,
  has-missing-files.
- **Detail drawer** — overview, cast, director/creator, network, ratings,
  genres, full file list with sizes, and an *open folder* link that reveals
  the file in your file manager.
- **Jobs** — scans and enrichment run in the background with a live progress
  bar and log.

## API

Interactive docs at `/api/docs` (FastAPI). Key endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/api/titles` | list (q, kind, watched, genre, root, match, person, wanted, sort, direction) |
| GET  | `/api/titles/{id}` | detail + files |
| PATCH| `/api/titles/{id}` | edit metadata (title/kind/year re-match; ratings, runtime, cast, genres, cert… are safe edits; `clear:["field"]` nulls wrong values). Edited fields are **locked** — Enrich never overwrites them; clearing a field unlocks it for refetch |
| POST | `/api/titles/{id}/watched` | manual watched toggle `{watched:bool}` |
| POST | `/api/titles/{id}/favorite` | favorite toggle `{value:bool}` |
| POST | `/api/titles/{id}/wanted` | wanted toggle on existing row `{value:bool}` |
| POST | `/api/titles/{id}/move` | move files `{target:"internal"|"external"}` (job) |
| GET  | `/api/wanted` | list the wishlist |
| POST | `/api/wanted` | **external submit**: `{title, kind:"movie"|"series", year?, note?, by?}` — idempotent, auto-enriched; scanner clears the flag when files appear |
| DELETE | `/api/wanted/{id}` | un-want (wishlist rows without files are removed) |
| POST | `/api/watched` | **external watched report**: `{title?, kind?, year?, tmdb_id?|imdb_id?, watched?, create_missing?}` — 404 if unknown unless `create_missing:true` adds it to the wishlist |
| GET  | `/api/duplicates` | duplicate groups (copies, sizes, deltas) |
| DELETE | `/api/duplicates/{id}` | delete one duplicate copy `{root}` (guarded) |
| POST | `/api/test/llm` · `/api/test/tmdb` | probe provider keys |
| GET/POST/DELETE | `/api/roots` | manage library roots |
| GET/POST | `/api/settings` | API keys (masked in responses) |
| POST | `/api/scan` · `/api/enrich` | start background job `{job_id}` |
| GET  | `/api/jobs/{id}` | job status/progress/log |

## Layout

```
app/
  config.py   paths, defaults
  db.py       SQLite schema + settings store
  parser.py   filename parsing + watched-marker rules
  scanner.py  filesystem walk, grouping, sync
  tmdb.py     TMDB search/details
  omdb.py     OMDb (IMDb rating) fill-in
  llm.py      OpenAI-compatible name cleanup
  enrich.py   enrichment pipeline
  jobs.py     background job tracking
  main.py     FastAPI routes + static mount
static/       index.html, app.js, style.css
data/         SQLite DB (gitignored)
```

## Changelog

- **2026-09-10** — **manual edits survive re-enrichment**: every field you fix
  in the edit form is locked (🔒 in the form); Enrich and the ratings backfill
  skip locked fields. Clearing a field unlocks it so Enrich refetches it.
  Changing title/kind/year resets locks together with the enrichment wipe.
  Wishlist & favorites: **wanted** flag (`POST /api/wanted` for
  external programs, auto-enriched; scanner flips it off once files show up and
  never deletes wishlist rows), **♥ favorites** on every row, **Length** column
  (runtime, or min/episode for series), **edit details** form in the drawer
  (ratings/runtime/cast/genres/cert — safe edits; title/kind/year re-key and
  reset enrichment), **person search** (directors/actors/creators match the
  search box; names are clickable in the list and drawer), description hidden
  under *Show description* in the main list (drawer keeps its toggle), removed
  the redundant Kind column, rebalanced row action buttons.
- **2026-09-09** — initial release: scanner, watched-folder rules, TMDB/OMDb/LLM
  enrichment, sortable/filterable UI, settings, background jobs.

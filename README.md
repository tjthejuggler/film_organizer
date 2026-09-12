# Film Organizer

A local web app that catalogs movies & series scattered across your laptop and
external drives, enriches them with metadata (posters, cast, creators, IMDb /
TMDB ratings), tracks what is watched, and tells you **where** to find
anything when you want to watch it.

## Run

```bash
./launch_film_organizer.sh
```

Starts the server if it isn't running yet, then opens
<http://127.0.0.1:8765> in your browser. Safe to run again while the app
is up (it just re-opens the UI). The plain server starter is still
available as `./run.sh`.

This launcher is registered in the **MyApps** tray launcher
(`~/Projects/MyApps/script_items.json`) with the clapperboard icon.

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

- **2026-09-12** — **organized backup drive + smarter Move buttons**: moves to
  the external drive now land in kind-based subfolders — movies in `Movies/`,
  series in `Series/` — so the backup drive stays browsable (internal storage
  keeps its existing layout; a legacy `external_root` setting pointing into
  `…/Movies` is normalized to the drive root so nothing double-nests, and
  moving a title back strips the kind hop). The title API now reports
  `storage_side`, and the drawer shows only the move button that makes sense:
  a title already on the drive offers just **💻 Internal**, everything else
  just **🔌 External**. Every title row also got a **⧉ copy** icon directly
  after the name (before the wanted ★ and other icons) that copies the title
  to the clipboard — works on plain-HTTP too via a fallback. Also fixed a
  pre-existing startup breaker by adding `python-multipart` to the venv +
  requirements.

- **2026-09-11** — **recently-released seasons on the calendar**: the 📅 popup
  now has a **Recently released** section above the upcoming list — seasons
  that started airing in the last 90 days of series you watch (or partly
  watched), one entry per show, showing the date range (or "airing now") and
  how long ago it started. A 👁 button per entry marks the season watched and
  clears it; the badge pill counts upcoming **and** unseen recent seasons.
  Backed by a new `season_watch.seen` flag, a `recent` branch in the TMDB
  sweep that tracks the current/just-finished season with its real air dates
  (`GET /api/seasons/recent`, `POST /api/seasons/{id}/seen`), and a full
  sweep backfill that seeded the catch-up list from TMDB's aired-episode
  data — the same data source that powers the upcoming list.

- **2026-09-11** — **episode-rip junk rows fixed at the root**: the parser's
  site-prefix stripper now knows many more TLDs (`www.1TamilMV.su -` etc. was
  previously kept in the title), and documentary packs named
  "Series 2 03of10 …" are recognized as season/episode tags. Names that
  still parse empty never become one junk row per episode: the scanner now
  falls back to the *show/folder* anchor instead of the raw file stem.
  Enrich retries after LLM name cleanup keep the cleaned title even when the
  kind was already right. Same-show duplicate rows can no longer flood the
  calendar (`calendar_entries` dedupes by tmdb_id), `_probe` uses the last
  *aired* season as its baseline (titles.seasons includes the upcoming
  season and blocked announcements), and `db.tx()` got a reentrant lock —
  nested `jobs.log()` inside the scan transaction used to deadlock the
  whole app at "writing database…". Rescan + reseed merged the six
  "www.1TamilMV.su - Black Doves S01E0x" rows into one matched
  **Black Doves** row (S2 · 5 Nov 2026 chip); search now finds it.

## Features

### 📅 Season calendar (added 2026-09-10)
Tracks upcoming seasons of every series you have watched:

- **Calendar popup** — click the 📅 button in the top bar: announced seasons
  (with exact dates), vague windows ("2027", "Jan 2027") still waiting for an
  exact date, and finished series, all in one list.
- **Release pattern** — each announced entry says whether episodes drop
  **all at once** or **weekly**, and weekly shows show the full first→finale
  date range.
- **Tag chips in the main list** — series with a known upcoming season show a
  `S2 · 18 Sep 2026` (or `S2 · 2027` when vague) chip under the name; series
  whose finale has aired get a `🏁 finished` chip.
- **Watch trigger** — marking a series watched immediately checks whether the
  next season is announced.
- **Weekly re-check** — once a week (on app startup) vague/TBA entries are
  re-polled via TMDB; entries that found their exact date are never checked
  again. Finished shows get a slow monthly look for revivals.
  Manual "check now" lives at the bottom of the calendar popup
  (`POST /api/seasons/poll`).
- **Recently released** — seasons that started airing in the last 90 days of
  the series you watch appear at the top of the popup until you mark the
  season 👁 seen; the sweep records current seasons with their real first→
  last air dates (all-at-one drops included). API: `GET /api/seasons/recent`,
  `POST /api/seasons/{id}/seen`.
- Initial data was seeded from web research on 2026-09-10
  (`app/season_seed.py`); new facts continue to come from the TMDB poll.
  API: `GET /api/seasons/calendar`.

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
- **Seen log (watched without owning)** — the **👁 Seen** button records a
  movie/series you have watched but no longer (or never) own a file of. The
  add flow checks your catalog first ("Already in your catalog?") and then
  shows up to six **TMDB candidate cards** (poster, year, synopsis) so you can
  confirm exactly which title you meant — picking one fetches its details
  immediately. Without a TMDB key (or for obscure titles) you can still
  record the entry plain. Entries live in the main catalog (badge **👁 seen**,
  filter *Seen log only*), count as watched, survive every scan. If a file
  with the same name later appears on disk, the entry automatically becomes a
  normal library row — still marked watched. External programs can use
  `POST /api/history` (accepts `tmdb_id`) / `GET /api/history` /
  `DELETE /api/history/{id}`.
- **Delete with memory** — the row trash button opens a **choice popup**:
  *Delete files + entry* (the old behavior) or *Delete files, keep record*.
  Keeping the record turns the entry into a fileless one that keeps its
  watched state, metadata and history badge — "watched it, deleted it, still
  remember it". Unwatched titles can be kept as records too (e.g. discarded
  downloads). Marking a fileless entry watched (✓) automatically turns it
  into a remembered record; rows WITH files don't need the flag.
- **Watch Next** — pin one movie and one series as "queued to watch" with the
  ▶ button (row or drawer). The title is **copied** into
  `<internal folder>/aaNext_Movie` or `aaNext_Series` (whatever the previous
  pin left there is wiped), and it **sticks to the top of the list** no
  matter how you sort or filter. If the title lives on an unplugged drive
  you get told exactly which drive to connect — nothing is copied until it
  is reachable. Unpinning leaves the copy in place; the next pin replaces it.
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
| POST | `/api/titles/{id}/watch-next` | pin as Watch Next: copy into `<internal>/aaNext_Movie|aaNext_Series` (job); 409 names offline drives |
| DELETE | `/api/titles/{id}/watch-next` | unpin (slot copy stays until the next pin) |
| POST | `/api/history/search` | seen-log candidate lookup: local catalog matches + up to 6 TMDB candidates with poster/year/synopsis for user confirmation |
| GET  | `/api/history` | list seen-log entries (watched, no file owned) |
| POST | `/api/history` | record a seen title `{title, kind?, year?, note?, watched_at?}` — idempotent, auto-enriched; becomes a normal row if files appear |
| DELETE | `/api/history/{id}` | remove a seen-log entry (fileless rows are deleted; owned rows just lose the flag) |
| GET  | `/api/wanted` | list the wishlist |
| POST | `/api/wanted` | **external submit**: `{title, kind:"movie"|"series", year?, note?, by?}` — idempotent, auto-enriched; scanner clears the flag when files appear |
| DELETE | `/api/wanted/{id}` | un-want (wishlist rows without files are removed) |
| POST | `/api/watched` | **external watched report**: `{title?, kind?, year?, tmdb_id?|imdb_id?, watched?, create_missing?}` — 404 if unknown unless `create_missing:true` adds it to the wishlist |
| GET  | `/api/notifications` | list notifications (`?status=pending|queued|done|rejected|all`) + pending count |
| GET  | `/api/notifications/count` | pending count (bell badge polling) |
| POST | `/api/notifications/{id}/decide` | `{decision:"accept"|"reject"}` — accept starts a **background** move job (response has `job_id`, UI shows progress toast); if the drive is offline the move goes to the drive queue and runs on connect |
| GET  | `/api/duplicates` | duplicate groups (copies, sizes, deltas) |
| DELETE | `/api/titles/{id}/files` | delete files (+ entry by default); `?keep_record=true` keeps the catalog entry as a fileless record |
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
  watchnext.py Watch Next pin: copy into aaNext_* slot + replace
  main.py     FastAPI routes + static mount
static/       index.html, app.js, style.css
data/         SQLite DB (gitignored)
```

## Changelog

- **2026-09-11 (13)** — **folder-aware moves & deletes + notification click
  actually feeling alive**: accepting "move to backup?" used to run the whole
  move synchronously inside the HTTP request — the click looked dead for
  minutes and impatient re-clicks started several racing move jobs. The move
  now runs as a **background job** (`status:"started"` + `job_id`), progress
  shows in the job toast, failures put the notification back to pending for
  an easy retry. Moves and deletes are now **whole-release-folder aware**:
  when a title lives in its own folder (the catalog confirms no other title's
  files are inside), the ENTIRE folder moves/deletes together — Subs/,
  artwork and .nfo files ride along. Loose files take matching same-stem
  sidecars (`.srt`, `.nfo`, …). Deletes carry a **size safety guard**: a
  folder is only wiped whole when its on-disk content is not way bigger than
  the title's cataloged bytes (≤ catalog×1.1 + 250 MB); otherwise only the
  cataloged files are removed one by one. New shared helpers live in
  [`app/fileops.py`](app/fileops.py).
- **2026-09-10 (12)** — **drive queues**: operations that need an unplugged
  drive no longer dead-end. Moving to internal/external, deleting a title's
  files, deleting a duplicate copy, and accepting a watched→backup
  notification are all placed in a persistent [`drive_queue`](app/db.py)
  table when their drive is offline and **run automatically the moment the
  drive is connected** — via a background poller
  ([`drivequeue.worker_loop()`](app/drivequeue.py)), an SSE hook on drive
  connect, and a "▶ Run ready now" button. New **Settings → Drive queues**
  panel lists pending work grouped per drive (with mounted status and a
  cancel button per entry). Queueing is deduplicated (same
  kind+title+drive is never doubled). API: `GET /api/drive-queue`,
  `DELETE /api/drive-queue/{id}`, `POST /api/drive-queue/run`; the delete
  endpoint grew `?queue=false` to restore the old hard refusal.
  [`notifications.decide()`](app/notifications.py) now marks accepted
  notifications as `queued` while the drive is away.
- **2026-09-10 (11)** — **miniseries tags fixed** (was: only 1 of 213 series
  tagged). Three compounding causes, all fixed: ① the ✨ Enrich button only
  processes *unmatched* rows, so already-matched series were never asked
  about TMDB's `type` field; ② the scanner **overwrote** `is_miniseries`
  with the folder-marker value on every scan, erasing any tag (now it's a
  MAX latch — folder/TMDB can set it, nothing silently clears it); ③ no
  job ever backfilled the flag. `run_backfill` now also checks TMDB
  `tv_type` (new [`tmdb.tv_type()`](app/tmdb.py)) for matched series, and
  the Enrich button chains the backfill pass automatically. Result: 70
  series correctly tagged (Band of Brothers, Baby Reindeer, Adolescence…).
  The header was also decluttered: Duplicates moved into Settings →
  Library roots; Scan/Enrich/Seen are icon-only.
- **2026-09-10 (10)** — **watched→backup notifications + manual-rating fix**:
  a 🔔 bell in the header now collects **server-persisted notifications**:
  when a title with files on this machine gets marked watched (UI button,
  external `/api/watched` report, or the watched-folder scan latch), a
  pending "move to backup?" notification is queued. Opening the bell lists
  the decisions: **✓ Move to backup** runs the move immediately; **✕ Keep
  here** dismisses. If the backup drive (`external_root`) is unplugged the
  accept **stays pending** and shows exactly which drive to plug in — retry
  anytime. Notifications are deduplicated per title and a decided one is
  never resurrected; titles already living fully on the backup drive are
  skipped. Also fixed **manual edits being wiped**: PATCH used to treat
  every edit-form save as an identity change (the form always sends
  title/kind/year), resetting enrichment and the per-field edit locks —
  so user-corrected ratings reverted on the next Enrich. Identity now only
  counts as changed when a value actually differs. Bonus fix: `mover`'s
  `sqlite3.Row.get()` crash that broke every Move after the first file.
- **2026-09-10 (9)** — **recommender hardening**: the popup now has **three
  verdict buttons** — ✓ Want it / 👁 Already seen it / ✕ Not for me — each one
  submits and immediately serves the next pick (no separate checkbox, no
  extra click). Titles already in the catalog can no longer be recommended:
  a hard server-side filter drops them when a batch is stored, again when a
  queued row's TMDB match turns out to be an owned tmdb/imdb id, and a final
  purge runs every time the next card is served (caught live: *Severance*
  was recommended despite being in the library).
- **2026-09-10 (8)** — **AI film recommender + miniseries tag**: a 🎲 **Recommend**
  button serves AI-researched movies/series one at a time in a popup — researched
  live on the web by the LLM via z.ai MCP tool-passing (`web-search-prime` +
  `web-reader` attached directly in the `chat/completions` `tools` array, so the
  stored z.ai key powers both chat and tool calls; no MCP client code).
  Each card shows poster/ratings/cast/runtime plus *why this was picked for you*
  and where to watch; you can **✓ Want it** (→ wanted list) or **✕ Not for me**,
  tick **"I've already seen this"** (reject+seen → seen-log row; accept+seen →
  marked watched) and leave a free-text note explaining your verdict — every
  decision feeds the next research batch, together with favorites, watched,
  wanted notes and the seen log (new `app/recommender.py`, new
  `recommendations` table). The queue refills itself in the background: when it
  drops to ≤15, a research job brings it back to 30 (triggers on startup,
  status checks and after each decision; `POST /api/recommendations/refill`
  for a manual kick). API: `GET /api/recommendations/status|next`,
  `POST /api/recommendations/{id}/decide`, `GET /api/recommendations`.
  **Miniseries**: new *Mini-series* chip in the kind filter (series + is_miniseries
  flag, shown as a `miniseries` badge), settable in the drawer, auto-derived
  from TMDB's TV type on enrich and from `Miniseries`-named folders on scan;
  "Miniseries" also appears in the genre dropdown as a filterable pseudo-genre.
- **2026-09-10 (7)** — **Delete with memory**: the trash button now asks
  *what* to delete — files + entry, or files only (keeping the catalog entry
  as a fileless record with its watched state intact). Unwatched titles can
  be kept as records too. Marking a fileless entry watched auto-remembers it
  (`history=1`), so scans never prune it.
- **2026-09-10 (6)** — **Seen log confirm flow + UI tweaks**: adding a seen
  entry now checks the local catalog first, then shows TMDB candidate cards
  (poster/year/synopsis) to confirm the exact title; the chosen candidate's
  details are fetched synchronously (`POST /api/history` accepts `tmdb_id`).
  Replaced the loud red primary add button with a quiet mini button, and the
  Settings button is now just a ⚙ gear icon.
- **2026-09-10 (5)** — **Seen log**: record watched movies/series you no longer
  own as fileless catalog entries (👁 Seen button in the header). New
  `history` column (auto-migrated), `GET/POST/DELETE /api/history`, a
  *Seen log only* filter, and a **👁 seen** badge. Entries survive scans
  (the fileless-row cleanup skips them), clear themselves automatically when
  matching files appear on disk, and are enriched with poster/ratings.
- **2026-09-10 (4)** — **Watch Next + row tint + favicon fix**: ▶ Watch Next
  pin (new `watchnext.py`, `POST/DELETE /api/titles/{id}/watch-next`) copies
  the pinned title into `<internal>/aaNext_Movie|aaNext_Series` (wiping the
  previous pin's copy) and sticks it to the top of the list across all
  sorts/filters; offline source drives are refused with a 409 naming the
  drive to connect. Rows are no longer dimmed for watched/offline — every
  row is full-brightness white; titles with at least one connected location
  get a green-tinted row background + green edge. Favicon is served with
  `Cache-Control: no-store` and the link tag is versioned, so the tab icon
  finally updates after the clapperboard redesign.
- **2026-09-10 (3)** — **new icon + MyApps launcher entry**: replaced the
  YouTube-like red play-button favicon with a purple-gradient clapperboard
  (`static/favicon.svg`, also inlined in the header); added
  `launch_film_organizer.sh` (starts the server if needed, opens the UI,
  idempotent) and registered "Film Organizer" with the new icon in
  `~/Projects/MyApps/script_items.json`.
- **2026-09-10 (2)** — **live drive events + remembered filters**: the table
  now reloads by itself when a drive is connected/disconnected (server-sent
  events on `/api/events/drives` watch `/proc/mounts` + per-root liveness, 2 s
  poll, debounced reload; the "Where" column marks disconnected locations in
  amber with a ⚠). Sort, filters and search are remembered between page
  refreshes and app restarts via localStorage.
  Also fixed: clicking ♥ returned *405 Method Not Allowed* because the running
  uvicorn process predated the favorite route (no `--reload`); restarting the
  server resolves it.
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

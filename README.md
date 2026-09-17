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

- **2026-09-17** — **Enrich no longer redoes old work**: pressing Enrich used
  to re-attempt every failed row forever AND the auto-chained Backfill
  re-fetched every matched row whose RT score stayed NULL (354 rows here —
  most simply have no RT/cert data at the providers) on EVERY run. Now:
  [`titles.enrich_attempts`](app/db.py) counts failed enrich passes and the
  default run stops selecting rows after 3 failures ([`run_enrich`](app/enrich.py));
  [`titles.backfill_miss`](app/db.py) counts backfill passes where none of a
  row's missing fields got filled and the row is dropped from selection
  after 2 such passes — any pass that DOES fill a goal resets its counter,
  so rows self-heal when providers add data. Force Enrich and per-row
  enriches ignore the caps; editing a title's identity (re-key) resets its
  budget. Verified live: enrich re-run selects 0/918 rows at steady state;
  backfill converged 354 → 313 → 0 (138 goals genuinely filled along the way).
- **2026-09-17** — **enrich 10× faster + full visibility into what it's doing**:
  two rounds of fixes. (1) *Why the corner toast told you nothing*: the job
  watcher never rendered `message`, and messages only updated after each
  title — now [`watchJob()`](static/app.js) shows a live line naming the
  title AND its on-disk file path (via new
  [`_first_path()`](app/enrich.py)) so junk entries (personal recordings,
  comedy specials, courses) are identifiable mid-run, plus a "LLM name
  cleanup running… (~15s)" warning logged BEFORE the slow step, and a
  per-title `<title> -> status (Ns)` duration log. (2) *Why it was slow*: a
  live test caught one title taking 96.5s — root cause was a fresh
  DNS+TCP+TLS handshake per API call; both providers now share keep-alive
  clients ([`tmdb._client`](app/tmdb.py), [`omdb._client`](app/omdb.py)),
  cert+OMDb fetch concurrently, and up to 4 titles enrich in parallel with
  the shared rate limiter keeping the total polite. Verified: 8 titles
  (two needing LLM cleanup) in 10s total, worst single title 10.1s.
  LLM timeout cut 30s→15s; skips no longer pay the 0.15s pause.
- **2026-09-17** — **enrich speed + live status**: Enrich was slow AND silent
  about it — the corner toast only updated after each title finished, and a
  stubborn name burned up to two 30s LLM calls plus five sequential provider
  round-trips with nothing on screen. Fixes: the toast now names the title
  being looked up *before* work starts and logs a per-title `-> status (Ns)`
  line ([`app/enrich.py`](app/enrich.py)); cert + OMDb fetches run
  concurrently (thread-safe TMDB throttle in [`app/tmdb.py`](app/tmdb.py));
  the LLM call timeout dropped 30s → 15s ([`app/llm.py`](app/llm.py)); and
  skipped rows no longer pay the 0.15s politeness sleep. Runs that are
  mostly matched rows now blast through at ~60 rows/s instead of ~6/s.
- **2026-09-16** — **mobile collapsible header**: on viewports ≤720px the
  stats line, search/action buttons and the whole filter bar now collapse
  into a single `☰ N titles ▾` row ([`.mToggle`](static/style.css:67)) —
  the movie list starts right at the top of the screen. Tapping it expands
  everything; the choice is remembered in localStorage. Desktop (wider than
  720px) is completely unchanged — the toggle is desktop-hidden via
  [`static/index.html`](static/index.html).
- **2026-09-15** — **QR device pairing (LAN access)**: the server now binds
  `0.0.0.0` ([`run.sh`](run.sh), [`launch_film_organizer.sh`](launch_film_organizer.sh),
  [`config.HOST`](app/config.py)) so phones/tablets can open the app, but a
  device gate ([`app/pairing.py`](app/pairing.py)) rejects every non-loopback
  request unless it carries a `fo_device` cookie granted by redeeming a
  6-digit pairing code. The code is shown only in **⚙ Settings → Devices on
  this network** as a QR code ([`app/qrsvg.py`](app/qrsvg.py) wraps the
  `qrcode` package; new dep in [`requirements.txt`](requirements.txt)) encoding
  `http://<lan-ip>:8765/pair?code=NNNNNN`. Codes are one-shot and expire after
  5 minutes; loopback is always trusted; devices can be revoked at any time
  (token hashes only are stored — SHA-256). Verified end-to-end: unpaired
  remote 403 → scan → paired 200 → replay 400 → revoke → 403 again.
- **2026-09-14** — **runaway enrichment root cause + guardrails**: a Move to the
  backup drive auto-registered the ENTIRE drive root (`X10 Pro`) as a library
  root ([`mover.py`](app/mover.py)); the next scan then ingested ~1,400 camera
  clips (Akaso/neon backups) as "titles" and Enrich burned an LLM call on each.
  Fixes: the mover now registers only the landing folder (Movies//Series/),
  camera/phone default clip names (`20250113_150345`, `IMG_…`, `VID_…`, `DSC_…`,
  `PXL_…`) are skipped at scan time and never LLM-cleaned at enrich time, the
  toast **X now actually cancels** the running job (cooperative flag checked in
  the scan/enrich loops), and jobs left 'running' by a crashed/restarted app are
  closed at boot. Database healed: 616 junk titles + 1,384 file rows removed,
  bad root deleted (pre-existing personal folders on the drive were kept).
- **2026-09-13** — **episode watch checklist in the series drawer**: series now
  get a collapsible "Mark episodes watched" section (between the flag row and
  the file list) listing every parsed episode file as `S1E2` checkboxes with a
  progress bar + `done/total` counter in the collapsed summary, and *all /
  none* bulk buttons. Each tick is stored per-file (`files.watched_manual`,
  migrated on boot, untouched by rescans); ticking the LAST episode auto-marks
  the whole series watched on the main list — same path as the manual button
  (backup notification + next-season probe included). Unticking episodes never
  un-watches the series. API: `POST /api/titles/{tid}/files/{fid}/watched`
  (one episode) and `POST /api/titles/{tid}/episodes-watched`
  (`{"watched":bool,"season":int|null}` bulk), both answering
  `promoted`/`episodes_watched`/`episodes_total` so the drawer updates in place
  without collapsing.
- **2026-09-12** — **calendar shows only what's actually NEW + hide-without-delete**:
  the 📅 badge used to show every upcoming season, so an "8" that never went away
  felt like unread news. Now each `season_watch` row carries `changed_at` (stamped
  whenever its information really changes: vague window promoted to exact dates,
  dates shifted, new season announced, series finished) and the pill counts only
  entries changed since you last opened the popup (`cal_opened_at` baseline, set
  right after the data loads — `POST /api/seasons/calendar/opened`). Inside the
  popup those entries get a green edge + **NEW** chip so you can see what's new at
  a glance. All header icon buttons (⟳ scan, ✨ enrich, 🔔, 📅, 👁 seen-log, ⚙)
  are now uniform 38px squares (`.iconbtn`) with glyphs that nearly fill the box,
  and the seen-log got a cleaner stroked SVG eye. Separately,
  every title row has a **🙈/👁** button to hide a title without deleting anything:
  hidden rows leave the default list (`titles.hidden` column), a **Hidden:**
  dropdown filter (default "not shown") reveals them via *Hidden only*, and
  *Include hidden* mixes them back in (`POST /api/titles/{id}/hidden`,
  `hidden=only|all` filter param on `/api/titles` + `/api/stats`).

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

- **2026-09-17 (17)** — **duplicate series rows merge into ONE show**:
  Euphoria was split across EIGHT catalog rows (one per file — a release
  named `Euphoria.US.S02E01…` was parsed as the website `euphoria.us`, the
  whole title vanished and every file anchored on its own raw stem); The
  Righteous Gemstones and The Life & Times of Tim each split in two
  (release folder `righteous.gemstones.s04` without "The", and `&` vs
  `and` producing different dedupe keys); Tampopo split in two
  (`Tampopo.cd2.avi` — the disc tag parsed as part of the title). Fixes,
  in layers:
  [parser.py](app/parser.py) — `.us`/`.uk` are no longer treated as site
  TLDs (they spell the country in release names), `&` normalizes to
  `and` in [`normalize_key()`](app/parser.py:132), new
  [`series_title_key()`](app/parser.py:251) folds leading article +
  trailing US/UK token, and `Title - 101 - Episode Name` pod numbering now
  parses as SxxEyy; new [consolidate.py](app/consolidate.py) heals the DB
  (merges rows sharing one tmdb_id, then folded-key rows whose matches
  agree — The Office UK/US style conflicts stay separate), moving files,
  season calendar, notifications, recommendations and queue entries onto
  the survivor and re-aggregating counts — now for MOVIES too (same
  tmdb_id, e.g. the Tampopo CDs; folded-title movies additionally need
  the same year); [scanner.py](app/scanner.py)
  keeps variants attached via a series alias map and runs consolidation
  after each scan; [main.py](app/main.py) runs it at startup and exposes
  the programmatic check: `GET /api/consolidation/suspects` reports every
  duplicate group the auto-merge deliberately leaves alone (user-locked
  rows, conflicting TMDB matches like Office UK/US, ambiguous no-year
  movies) for human review, `POST /api/consolidation/run` merges on
  demand. First run merged 12 stale rows (Euphoria 8→1, Gemstones 2→1,
  Tim 2→1, Human 2→1, Impractical Jokers 3→1) and the movie round merged
  Tampopo 2→1; the suspects report is the standing way to FIND stragglers
  instead of dealing with them one at a time.
- **2026-09-14 (16)** — **move only chosen seasons of a series**: the series
  drawer's *Move to:* row gains a ✏️ button that opens a season picker —
  tick the seasons to move, press 💻/🔌, and only those travel; unticked
  seasons stay exactly where they are. Partial moves go file-by-file (a
  release folder holding seasons left behind is never relocated whole),
  consolidation purges respect the selection, and the selection survives
  queueing for offline drives. Implementation:
  [`MoveIn.seasons`](app/main.py) → [`mover.move_title(seasons=…)`](app/mover.py)
  → [drivequeue payload](app/drivequeue.py); picker modal in
  [index.html](static/index.html) wired in [app.js](static/app.js).
- **2026-09-13 (15)** — **duplicate titles consolidate on move**: a title
  that lives on BOTH drives now shows **both** move buttons in its drawer
  (💻 Internal and 🔌 External), and whichever you pick leaves the title in
  exactly ONE place: the copy on the other drive is deleted after the move
  (release folders with Subs/sidecars go too, unplugged drives are left
  alone, the confirm dialog warns about the deletion). Implementation:
  [`mover.move_title(purge_others=True)`](app/mover.py) +
  `_purge_leftovers()`; backup notifications keep the old copy-preserving
  behavior (`purge_others` defaults False, honoured by the drive queue
  too). Root-cause fix: [`fileops.dedicated_folder()`](app/fileops.py) no
  longer claims a backup drive's `Movies/`/`Series/` hop as a title's
  "dedicated folder" on single-title drives — that used to make moves plan
  `Movies → Movies/Movies` and let duplicate-deletion threaten the layout
  folder.
- **2026-09-13 (14)** — **native system move dialog**: Move to
  internal/external now shows Plasma's **standard file-transfer progress
  dialog** — the same one Dolphin's moves live in — with real byte/file
  progress, transfer speed and working **pause/cancel**. How it works: a
  standalone helper ([`app/kio_jobview.py`](app/kio_jobview.py), run under
  the *system* python3 which has `python3-dbus`) requests a JobView from
  `org.kde.JobViewServer` and performs the move itself — atomic
  `os.rename` within a device, chunked copy + source delete across
  devices — streaming progress into the dialog over D-Bus.
  ([`app/kio.py`](app/kio.py) launches the helper with a guaranteed
  session-bus address and parses its verdict; `kioclient` was tried first
  but proven — via dbus-monitor — never to register with the job tracker,
  so its moves are invisible.) Cancel cleans up the partial destination
  and fails the job safely (catalog rows are only updated after a
  successful move); sidecar loss still never fails a move. **Settings**
  grew a *"Show system move dialog"* checkbox (`move_native_dialog`,
  default on): turning it off — or running on a headless box without a
  desktop session — restores the old silent `shutil.move` behaviour with
  a note in the job log.
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

# MyTaste Agent Guide

MyTaste is a Python 3.11+ FastAPI/Jinja application for browsing TMDB movies and TV shows
available on a user's selected streaming services. Read `README.md` before changing behavior and
`docs/deployment.md` before changing runtime or networking. Machine-specific notes, such as a
live instance on the current host, belong in an untracked `AGENTS.local.md`; read it when present.

## Git workflow

Follow [CONTRIBUTING.md](CONTRIBUTING.md) for branches, pull requests, and releases:

- Start every change on a new branch from `origin/dev` (`feature/…`, `fix/…`, `docs/…`, `chore/…`).
- Open pull requests against `dev` (`gh pr create --base dev`), never against `main`.
- Never commit or push directly to `dev` or `main`. Only the release process updates `main`, by
  merging `dev` into it.

## Project map

- `src/mytaste/config.py` — environment parsing and application settings.
- `src/mytaste/accounts/` — people on an instance: `models.py` (`User`, roles, password and PIN
  rules), `passwords.py` (scrypt), and `context.py`, which holds the signed-in user and the
  libraries they may see for the current request (`current_user_id`, `visible_libraries`).
- `src/mytaste/storage/users.py` — accounts, sign-in sessions (only token hashes are stored),
  and `library_access`; `settings.py` — instance-wide settings such as whether the profile screen is shown;
  `access.py` — the SQL condition that limits library queries to the user's libraries;
  `migrations.py` — `give_to_first_user`, which turns a one-profile table into a per-user one.
- `src/mytaste/catalog/models.py` — immutable domain and browse-query models.
- `src/mytaste/catalog/tmdb.py` — raw asynchronous TMDB requests and response normalization.
- `src/mytaste/catalog/service.py` — browsing, cross-media merging, availability checks,
  enrichment, and in-memory caching.
- `src/mytaste/catalog/mixing.py` — mixes the user's games into All's smart collections: merged
  by the shared sort key, or spread every fourth card in popularity order.
- `src/mytaste/catalog/filters.py` — browse filters (`TitleFilters`), TMDB discover parameters,
  and `title_matches`, the one rule every non-TMDB source is checked with; `filtering.py` applies
  it to library titles, collections, credits, and search results; `facts.py` reads per-title
  facts (ratings, origin, runtime, keywords, people) once and keeps them in SQLite
  (`storage/facts.py`).
- `src/mytaste/collections/models.py` — the predefined smart collections (genre/release-window
  filters with a default sort), user collection models, and the collection icon set.
- `src/mytaste/collections/service.py` — user collections: saving titles with a TMDB snapshot,
  availability on the user's services (stored per region, refreshed after a day), and browsing.
- `src/mytaste/storage/collections.py` — SQLite persistence for user collections: shared saved
  items (movies, series, games), ordered collection entries, and title availability.
- `src/mytaste/storage/preferences.py` — SQLite persistence for subscriptions and display options.
- `src/mytaste/storage/library.py` — SQLite persistence for storage libraries, their matched
  items, and local browse queries.
- `src/mytaste/library/parser.py` — pure filename/folder parsing (title, year, season, episode).
- `src/mytaste/library/scanner.py` — synchronous folder walk that groups video files into titles.
- `src/mytaste/library/service.py` — background scans, TMDB matching via `CatalogService`,
  folder validation and listing, and library browsing.
- `src/mytaste/playback/` — local playback: `probe.py` (ffprobe), `keyframes.py` (MKV cues and
  MP4 sync samples), `subtitles.py` (discovery, encodings, WebVTT), `decision.py` (direct play,
  remux, or transcode from browser capabilities), `sessions.py` (ffmpeg HLS sessions, seeking,
  throttling), `fmp4.py`, and `service.py` (watch targets, progress, Continue watching).
- `src/mytaste/storage/playback.py` — SQLite persistence for probe results and watch progress.
- `src/mytaste/games/` — Games: `models.py` (one `Game` per game, keyed `steam-<appid>` or
  `xbox-<product id>`, collections, the shared genre list), `gamepass.py` and `steam.py` (store
  clients; Steam sign-in and owned games), `matching.py` (Xbox ↔ Steam through IsThereAnyDeal,
  Wikidata, then title and year), `service.py` (`GamesService`: Game Pass lists, Steam rankings,
  owned games, merging both stores, ordering), and `http.py` (shared retries).
- `src/mytaste/storage/games.py`, `steam.py`, `game_links.py`, `gamepass_cache.py` — Game Pass
  preferences, the connected Steam account, Xbox ↔ Steam matches, and the optional store cache.
- `src/mytaste/web/app.py` — FastAPI factory, dependency wiring, templates, and static assets.
- `src/mytaste/web/signin.py` — `SignInMiddleware`: binds each request to its user, sends
  signed-out visitors to `/setup` or `/login`, keeps admin-only paths from members, and refuses
  changing requests from other sites; also the session cookie and the wrong-PIN throttle.
- `src/mytaste/web/accounts.py` — setup, sign-in and the profile picker, `/account`, and
  `/settings/people` for admins.
- `src/mytaste/web/routes.py` — page/API routes, query parsing, and template context construction.
- `src/mytaste/web/filter_options.py` — filter URL parameters and the sidebar's filter controls.
- `src/mytaste/web/playback.py` — `/watch/...` player pages and the streaming, subtitle, and
  progress APIs.
- `src/mytaste/web/games.py` — `/collections/games/...`, game details, Steam sign-in, and saving
  games to collections.
- `src/mytaste/web/templates/` — server-rendered Jinja pages; `_icons.html` draws collection icons.
- `src/mytaste/web/static/` — CSS and vanilla JavaScript with no frontend build step; the only
  third-party file is the vendored hls.js light build in `static/vendor/` (Apache-2.0).
- `tests/` — unit and route tests. Web tests inject fake catalog implementations through
  `create_app()` and must not call live services.

## Implementation conventions

- Put TMDB transport details in `TMDBClient`; put product rules, caching, and multi-request
  orchestration in `CatalogService`.
- When adding catalog data, normally update models, TMDB parsing, service behavior, route context,
  templates, and their corresponding tests in that order.
- Browsing is by collection (`/collections/<key>`, home at `/`); keep the remaining browse state
  (media type, sources, filters, sort) in URL query parameters. Every sort must order local titles
  exactly like TMDB (`catalog_sort_key` and the library `_ORDERINGS`) so merged pages stay in one
  global order. A filter must give the same answer for a title from any source: add it to
  `TitleFilters`, `discover_params` (when TMDB can apply it), and `title_matches` together.
  Display-only preferences are persisted in SQLite via
  `/api/preferences/display` and should update immediately in the UI.
- Per-user data is scoped in storage, not in routes: per-user tables have a `user_id` and every
  query uses `current_user_id()`, which raises outside a signed-in request (tests bind the first
  user through the autouse `first_user` fixture; web tests sign in with `owner_client`). Library
  queries add `visible_library_clause`, so a member never reaches another library's titles or
  files, including by id. In-memory caches must not hold one user's data for another: key them
  by user or bypass them when `visible_libraries()` is set, as `LibraryService.matched_keys`
  does. New admin-only paths go in `_ADMIN_PREFIXES` in `web/signin.py`.
- Preserve dependency injection in `create_app()` so tests can supply fake catalog, preference,
  and library implementations. Update the fakes when a service interface changes.
- Library scans must never block requests: filesystem walks run in a worker thread and matching
  runs as an `asyncio` task. Rescans keep the ids of files and titles that are still present;
  watch progress and `/watch/local/<id>` links depend on that.
- Playback never sends file paths to the browser. Every stream, subtitle, and probe resolves the
  file through `PlaybackService.resolve_path`, which checks it is inside a library folder.
  Tests that run ffmpeg use the synthetic clips from `tests/conftest.py`, never real media. Parser changes should be backed by real-world filename fixtures in
  `tests/test_library_parser.py`.
- Use semantic CSS custom properties. Dark mode is the original design; light overrides live in
  `@media (prefers-color-scheme: light)`. Check both OS themes and desktop/mobile layouts for UI
  changes.
- Keep templates accessible: labeled controls, keyboard-operable dialogs, visible focus styles,
  and useful empty/error states.
- SQLite schema initialization must remain safe for existing databases. Add an explicit migration
  path when changing an existing table; do not assume `CREATE TABLE IF NOT EXISTS` alters it.
- Games keep each store's facts on its own scale (Steam's review percentage, the Microsoft
  Store's five stars) and describe the user's access (`game_pass`, `owned`) separately from the
  game. Steam's rankings only page through Steam games; collections that include games only on
  Xbox must sort by something every store can compute. Never merge games by title alone
  outside `matching.py`.
- Never print or commit `.env`, TMDB tokens, the Steam Web API key, or the SQLite database.

## Local workflow

```bash
uv sync --extra dev
uv run mytaste serve --reload
```

`MYTASTE_TMDB_TOKEN` is required for a real server. By default, state is stored outside the repo at
`$XDG_DATA_HOME/mytaste/mytaste.db` or `~/.local/share/mytaste/mytaste.db`.

Run relevant checks before handing off:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
node --check src/mytaste/web/static/app.js
node --check src/mytaste/web/static/player.js
```

Run `uv build` when packaging, templates, or static assets change. For UI work, inspect the real
page at desktop and mobile widths and emulate both light and dark OS themes.

## Local instructions

Host-specific instructions, when present, live in the untracked `AGENTS.local.md` and are loaded
here:

@AGENTS.local.md

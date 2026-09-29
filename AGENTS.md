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
- `src/mytaste/catalog/models.py` — immutable domain and browse-query models.
- `src/mytaste/catalog/tmdb.py` — raw asynchronous TMDB requests and response normalization.
- `src/mytaste/catalog/service.py` — browsing, cross-media merging, availability checks,
  enrichment, and in-memory caching.
- `src/mytaste/storage/preferences.py` — SQLite persistence for subscriptions and display options.
- `src/mytaste/storage/library.py` — SQLite persistence for storage libraries, their matched
  items, and local browse queries.
- `src/mytaste/library/parser.py` — pure filename/folder parsing (title, year, season, episode).
- `src/mytaste/library/scanner.py` — synchronous folder walk that groups video files into titles.
- `src/mytaste/library/service.py` — background scans, TMDB matching via `CatalogService`,
  folder validation and listing, and library browsing.
- `src/mytaste/web/app.py` — FastAPI factory, dependency wiring, templates, and static assets.
- `src/mytaste/web/routes.py` — page/API routes, query parsing, and template context construction.
- `src/mytaste/web/templates/` — server-rendered Jinja pages.
- `src/mytaste/web/static/` — dependency-free CSS and vanilla JavaScript; there is no frontend
  build step.
- `tests/` — unit and route tests. Web tests inject fake catalog implementations through
  `create_app()` and must not call live services.

## Implementation conventions

- Put TMDB transport details in `TMDBClient`; put product rules, caching, and multi-request
  orchestration in `CatalogService`.
- When adding catalog data, normally update models, TMDB parsing, service behavior, route context,
  templates, and their corresponding tests in that order.
- Keep browse state in URL query parameters. Display-only preferences are persisted in SQLite via
  `/api/preferences/display` and should update immediately in the UI.
- Preserve dependency injection in `create_app()` so tests can supply fake catalog, preference,
  and library implementations. Update the fakes when a service interface changes.
- Library scans must never block requests: filesystem walks run in a worker thread and matching
  runs as an `asyncio` task. Parser changes should be backed by real-world filename fixtures in
  `tests/test_library_parser.py`.
- Use semantic CSS custom properties. Dark mode is the original design; light overrides live in
  `@media (prefers-color-scheme: light)`. Check both OS themes and desktop/mobile layouts for UI
  changes.
- Keep templates accessible: labeled controls, keyboard-operable dialogs, visible focus styles,
  and useful empty/error states.
- SQLite schema initialization must remain safe for existing databases. Add an explicit migration
  path when changing an existing table; do not assume `CREATE TABLE IF NOT EXISTS` alters it.
- Never print or commit `.env`, TMDB tokens, or the SQLite database.

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
```

Run `uv build` when packaging, templates, or static assets change. For UI work, inspect the real
page at desktop and mobile widths and emulate both light and dark OS themes.

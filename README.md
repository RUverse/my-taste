# MyTaste

MyTaste is a small self-hosted guide to recent movies and shows available on the streaming
services you already pay for. Choose your country and subscriptions, then browse the latest
matching releases in a clean poster grid.

## Features

- Region-aware subscription choices such as Netflix, Prime Video, and Apple TV+
- Unified All, Movies, and TV Shows browsing
- Latest, Most Popular, and common genre categories
- Title search restricted to the configured streaming subscriptions and local folders
- Animated movie and show details with YouTube trailers, descriptions, and cast
- A Watch button that opens the title on each of your services that carries it, and an episode
  grid for series with one row per season, marking the episodes you have in a local library
- Collapsible filters and display sidebar: sources with one-click “Only”, release-year presets,
  rating steps, and removable active-filter chips; filters apply instantly and stay in the URL
- Storage libraries: connect local folders (USB drive, NAS share, the same folders Plex uses),
  scan and match them on TMDB, and see them mixed into every category with your streaming picks
- Persisted display controls for card metadata, sizing, trailer autoplay, and the sidebar state,
  including optional icons of the subscribed services that carry each title
- Single-profile preferences persisted in SQLite
- Responsive, server-rendered interface that follows the OS light or dark theme
- Native Python and Docker deployment

Catalog metadata comes from TMDB. Streaming availability data is provided by JustWatch via
TMDB and varies by country. “Latest” means newest released or first-aired titles currently
listed for the selected services, not the date a service added a title.

TMDB's API does not provide links to a title on each service, so Watch reads them from the title's
public TMDB watch page. If that page changes or has no link for a service, Watch falls back to the
TMDB page, which lists every offer.

## Quick start

Python 3.11 or newer and a TMDB v3 API key or API Read Access Token are required. You can request
either credential from the API section of your TMDB account settings.

```bash
uv sync --extra dev
export MYTASTE_TMDB_TOKEN=your-read-access-token
uv run mytaste serve
```

Open <http://127.0.0.1:8000>, then add your streaming subscriptions, a local folder, or both.

To serve on a LAN or from a container:

```bash
uv run mytaste serve --host 0.0.0.0 --port 8000
```

MyTaste has no built-in user authentication. Keep the default loopback binding for local use,
or place it behind an authenticated HTTPS reverse proxy when exposing it to an untrusted
network.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `MYTASTE_TMDB_TOKEN` | required | TMDB v3 API key or API Read Access Token |
| `MYTASTE_DATA_DIR` | XDG data directory | Directory containing `mytaste.db` |
| `MYTASTE_HOST` | `127.0.0.1` | Default server bind address |
| `MYTASTE_PORT` | `8000` | Default server port |
| `MYTASTE_LANGUAGE` | `en-US` | TMDB metadata language |
| `MYTASTE_REQUEST_TIMEOUT` | `10` | TMDB request timeout in seconds |
| `MYTASTE_LIBRARY_ROOTS` | unset | Colon-separated folders that storage libraries must live inside |
| `MYTASTE_LIBRARY_RESCAN_MINUTES` | `60` | Automatic library rescan interval; `0` disables it |

## Storage libraries

The Services page lists what you have enabled: streaming subscriptions and local libraries side
by side. Choose **Add**, pick **Local library**, browse to a folder, and say whether it holds
movies or TV shows. A library can span several folders, for example a `Movies` and a `TV Shows`
folder on the same drive: use **Add another folder** in the dialog, or the **+** button on an
existing library card; the pencil button next to it renames the library. MyTaste scans every
folder in the background, groups files into titles, and matches each one on TMDB so posters,
trailers, and cast work exactly like streaming titles. A folder can belong to only one library,
and folders may not be nested inside each other.

Local titles are not a separate catalog. Every category mixes them with your streaming titles in
one order, so a local movie appears exactly where it ranks by release date or popularity, and a
title you own that is also streaming is shown once with a folder badge. Use the **Sources** filter
to narrow the view to particular services or folders; with only folders selected, **Recently
Added** and **A–Z** tabs become available.

The scanner understands common layouts: loose files named `Title.2019.1080p.mkv`, one folder per
movie such as `Title (2019)/`, director folders like `Nolan/2010 - Inception/`, and shows laid out
as `Show/S01/Show.S01E01.mkv` or `Show/Season 1/`. Titles that cannot be matched still appear
with a placeholder poster. Files are only read for their names and sizes; nothing is played or
copied.

Because the app has no authentication, anyone who can reach it can add folders and see file
names. Set `MYTASTE_LIBRARY_ROOTS` to limit which folders may be connected.

See [deployment options](docs/deployment.md) for Docker and native-host guidance.

## Development

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

The test suite does not call TMDB; external services are replaced with deterministic test doubles.

## Contributing

Work happens on branches from `dev`, and pull requests target `dev`; `main` only receives
releases. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and the checks to run
before opening a pull request.

## License

MyTaste is released under the [MIT License](LICENSE).

## Credits

This product uses the TMDB API but is not endorsed or certified by TMDB.

Movie, TV, and image metadata is supplied by [TMDB](https://www.themoviedb.org/). Streaming
availability is powered by [JustWatch](https://www.justwatch.com/).

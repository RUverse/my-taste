# MyTaste

MyTaste is a small self-hosted guide to recent movies and shows available on the streaming
services you already pay for. Choose your country and subscriptions, then browse the latest
matching releases in a clean poster grid.

## Features

- Region-aware subscription choices such as Netflix, Prime Video, and Apple TV+
- Unified All, Movies, Series, and Games browsing: All mixes the games you can play with the
  movies and series on your services
- Games from Steam and Xbox Game Pass in one catalog: a game sold in both stores is one card
  with both services, your Steam library (signed in through Steam) and Game Pass plan are marked
  on every cover, and the whole Steam store stays browsable
- Collections: predefined views such as Popular, Latest, and genres, plus your own lists, starting
  with an empty Watchlist and My favourites, which can mix movies, series, and games
- Sort any collection by popularity, release date, rating, or title (and date added for your own
  lists and local folders)
- Title search restricted to the configured streaming subscriptions and local folders
- Animated movie and show details with YouTube trailers, descriptions, and cast
- A Watch button that opens the title on each of your services that carries it, and an episode
  grid for series with one row per season, marking the episodes you have in a local library
- A sidebar for the open collection: its services and folders (hide one, or show only one, right
  there), sort, Plex-style filters (see [Filters](#filters)), grouping into rows (by director,
  genre, decade, or type), and card appearance; changes apply instantly and
  stay in the URL. Collapsed, it becomes a rail of icons that marks changed sections with a dot
- Storage libraries: connect local folders (USB drive, NAS share, the same folders Plex uses),
  scan and match them on TMDB, and see them mixed into every category with your streaming picks
- A built-in player for local movies and episodes: direct play, or streamed with ffmpeg when the
  browser needs it; resume where you stopped; subtitles (files next to the video and embedded
  tracks); audio and quality choices; Continue watching and Up next; and links such as
  `/watch/tv/1399/1/3` that always open that exact episode
- Persisted display controls for card metadata, sizing, trailer autoplay, and the sidebar state,
  including optional icons of the subscribed services that carry each title
- Infinite scroll: more titles (or rows, when grouped) load as you near the bottom; without
  JavaScript the page links still work
- Accounts for everyone at home, each with their own services, collections, display options,
  Steam and Game Pass, and Continue watching. A "Who's watching?" screen lets people switch
  profiles like on a TV; a profile can have a password, a short PIN, or nothing. The owner and
  admins manage the server and choose which local libraries each member sees
  (see [Accounts](#accounts))
- Preferences persisted in SQLite; the header shows the name of whoever is signed in
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

Open <http://127.0.0.1:8000> and create the owner account, then add your streaming
subscriptions, a local folder, or both.

To serve on a LAN or from a container:

```bash
uv run mytaste serve --host 0.0.0.0 --port 8000
```

Everyone signs in (see [Accounts](#accounts)), but the "Who's watching?" screen lets anyone who
can reach the server open a profile without a password. Keep the default loopback binding or a
home network or VPN; to expose MyTaste more widely, turn the profile screen off, give everyone a
password, and put it behind an HTTPS reverse proxy.

## Accounts

The first visit asks for the owner account. Data from before accounts existed (services,
collections, watch progress, display options, Game Pass and Steam) becomes the owner's. Under
**People** in the account menu, the owner adds the people at home:

- **Members** use the app with their own services, collections, display options, Steam account,
  Game Pass plan, and Continue watching, and see only the local libraries they are given.
- **Admins** also manage libraries and folders, and people. Only the owner can
  add admins, change roles, or hand over ownership.
- A profile signs in with a password, a 4–8 digit PIN, or nothing (members only); the owner and
  admins always use a password. After five wrong tries a profile waits before the next one.
- The **profile screen** shows everyone's profiles when signing in, like a TV at home. Turned
  off, everyone signs in with a username and password.

Sessions stay signed in on a device for 180 days of inactivity, so a TV doesn't keep asking.

### Sign in with MyTaste

With `MYTASTE_HUB_URL` set to a MyTaste Hub, **People** offers **Connect to MyTaste**, which
registers this server with the hub once. Then:

- Anyone can link their profile to their MyTaste account under **Your account**, and sign in
  with it from the sign-in screen.
- The owner and admins can invite friends by MyTaste username, choosing the libraries they see.
  The first time an invited friend signs in with MyTaste, they get a member account here. Such
  accounts sign in only with MyTaste and don't appear on the profile screen, which is for the
  people at home.
- Nothing else is shared with the hub: it learns only that someone signed in to this server.

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
| `MYTASTE_CACHE_DIR` | XDG cache directory | Stream segments and extracted subtitles |
| `MYTASTE_FFMPEG` / `MYTASTE_FFPROBE` | `ffmpeg` / `ffprobe` | Programs used for playback |
| `MYTASTE_MAX_TRANSCODES` | `1` | Conversions that may run at once; `0` turns conversion off |
| `MYTASTE_HWACCEL` | `auto` | Hardware video decoding for conversions: `auto`, `drm`, or `none` |
| `MYTASTE_STEAM_API_KEY` | unset | Steam Web API key, needed to list owned Steam games |
| `MYTASTE_STEAM_ENABLED` | `true` | `false` hides Steam and keeps Games to Game Pass |
| `MYTASTE_GAMEPASS_CACHE_ENABLED` | `false` | Keep game store data between requests (see [deployment](docs/deployment.md#game-pass-caching)) |
| `MYTASTE_HUB_URL` | unset | A MyTaste Hub to offer "Sign in with MyTaste" through (see [Sign in with MyTaste](#sign-in-with-mytaste)) |

## Collections

Everything you browse is a collection, shown as a tab above the grid:

- **Predefined collections** are filters over every title on your services and in your folders,
  and cannot be edited. **Popular** (the home page) holds the 200 most popular titles right now;
  **Latest** holds titles released in the past year; genre collections such as **Comedy** or
  **Thriller** hold that genre. TMDB has no Thriller, Romance, or Horror genre for series, so
  those tabs only appear for movies.
- **Your collections** are lists you fill yourself. MyTaste starts you with an empty
  **Watchlist** and **My favourites**; you can rename, re-icon, describe, or delete them. A list
  only shows titles you can watch on your services or from your folders, and says how many of its
  titles that leaves (for example “12 of 23 titles are on your services”).

In **All**, the predefined collections also hold the games you can play: those in your Game Pass
plan and your Steam library. Sorted by release date, rating, or title, a game sits exactly where
it ranks. TMDB's popularity has no equivalent for games, so in popularity order every fourth card
is a game, most popular first (Game Pass's popular list, then review counts). Popular takes the
games that fit beside its 200 titles; **Latest** takes games released in the past year; genre
collections take games whose top Steam tags (or, for games not on Steam, Microsoft Store
categories) clearly match, such as shooters in **Action**. Romance, Drama, Animation, and
Documentary stay films and series. Game Pass and Steam appear under **Services** in the sidebar,
and the year and rating filters apply to games too; any other filter shows only films and
series.

To add a title to your collections, open it and choose **Save**; the menu lists your collections
with checkmarks, and **New collection…** makes one and saves the title into it. The **+** at the
end of the collection bar (or **New collection…** under **Show all**) creates an empty one, and the
pencil beside a collection's name in the sidebar edits its name, icon, description, and default
order, or deletes it. Deleting a collection never touches your services or files.

**Grouping** in the sidebar splits a collection into rows that scroll sideways: one row per
director (or series creator), genre, decade, or type. It looks at the first 100 titles of the
collection in its current order; directors and genres with the most titles come first, and a title
by two directors or with two genres appears in each row.

Collections that do not fit in the bar above the grid are listed under **Show all**. The
sidebar's sources, sort, and filters apply on top of whichever collection is open. Moving to
another collection keeps the sources and filters and returns to that collection's own sort.
Collections live at `/collections/<name>` (or `/collections/<number>` for your own), and the
older `/?category=…` links redirect there.

## Filters

The **+** in the sidebar's **Filter** section adds a filter. Values within one filter are
alternatives (Drama *or* Crime); different filters must all match. Each active filter also shows
as a chip above the grid that removes it.

- **Details:** release year (with decade shortcuts), rating, genre, genres to exclude, content
  rating (as rated in your region), runtime, country of origin, original language, and TMDB
  keywords such as “time travel”.
- **People:** actor, director, writer, and producer. Type a name and pick the person; series
  creators count as directors and writers. Appearances as oneself, such as talk-show guests, do
  not count as acting.
- **Local files** (only with a library): watch status, resolution, video codec, dynamic range,
  audio codec, audio channels, audio language, and subtitle language. The choices are what your
  probed files contain. These filters only match titles in your libraries, so streaming titles
  are left out while one is on; **Unwatched** keeps streaming titles, since you have not played
  them here.

TMDB applies the detail filters to streaming titles itself. Local titles, your own collections,
search results, and people's credits are checked against the same facts, which MyTaste reads
from TMDB once per title and keeps in its database; titles in your libraries are read ahead of
time. Runtime uses episode length for series, and series without one on TMDB do not match.

## Games

**Games** combines Steam and Xbox Game Pass. A game sold in both stores is listed once, with
Steam's details and both stores' services; its details open **Open in Steam** with Xbox beside
it. Covers show the Game Pass logo when your plan includes the game and the Steam logo when it is
in your Steam library, with the hours you played below the title.

- **Game Pass:** on the **Services** page (**Manage** in the games sidebar), add **Xbox Game
  Pass** and choose Ultimate, Premium, Essential, or PC Game Pass and your platform; change them
  later on its card. Plan selection is manual and needs no Xbox login; the country is shared
  with movies and series.
- **Steam:** on the **Services** page, add **Steam** and use **Sign in through Steam**, or paste
  your profile link. MyTaste learns only your public Steam ID. Listing owned games needs the
  server's Steam Web API key (`MYTASTE_STEAM_API_KEY`) and your profile's Game details set to
  Public; Steam's store can be browsed without either.

Collections:

| Group | Collections | Order |
| --- | --- | --- |
| All games | The whole Steam store with Game Pass, including games only on Xbox | Newest release or title |
| Yours | My games (Game Pass and your Steam library), Recently played, Most played | Last played, time played, title, release, or rating |
| Steam | Most played, Top sellers, New and trending, Top rated, Coming soon | Steam's ranking |
| Game Pass | Popular, Recently added, Coming soon, Leaving soon | Microsoft's order, or any sort |

Steam's rankings cannot place games that are only on Xbox, so those appear in All games, the
Game Pass collections, and My games. The sidebar's **Services** toggles Game Pass and Steam for
All games and My games. One genre list serves both stores: Steam games get their genres from
their store tags, so a genre filter gives the same answer for a game in every collection. Steam
reviews show as a percentage and Microsoft Store ratings out of five.

A game on both stores is matched by its Steam and Microsoft Store IDs through
[IsThereAnyDeal](https://isthereanydeal.com/), then [Wikidata](https://www.wikidata.org/), then by
the same title and release year. Matches are kept for a week. If two listings are not the same
game, **Not the same game?** in its details shows them separately for good.

Games can be saved to your collections with the **+** on a cover or **Save** in its details,
next to movies and series. A saved game keeps its details even if it leaves Game Pass; like
titles, a collection lists the games you can play once you have set up Game Pass or Steam.

Game Pass caching is **disabled by default**; it also keeps Steam's game details when enabled.
See [cache configuration](docs/deployment.md#game-pass-caching) and [Steam](docs/deployment.md#steam).
Microsoft's and Steam's store feeds are isolated behind adapters; an outage shows a Games error
without affecting other categories. The design is in the
[Game Pass](docs/game-pass-implementation.md) and [Steam](docs/steam-implementation.md) plans and
the [portable collections design](docs/taste-collections.md).

## Storage libraries

The sidebar's **Services** section lists what you have enabled: streaming subscriptions and local
libraries. Collapsed, it shows their icons beside its name; expanded, each one has a checkmark
that hides it from (or shows it in) the current view and an **Only** link that shows just that
one. Nothing is removed there: **Manage** opens the Services page, which adds and removes them. The
Services page (**Sources and region** in the menu under your name) also lets you change
the region, add folders to a library, rename it, and rescan it. To add a library, choose **Add**,
pick **Local library**, browse to a folder, and say whether it holds
movies or TV shows. A library can span several folders, for example a `Movies` and a `TV Shows`
folder on the same drive: use **Add another folder** in the dialog, or the **+** button on an
existing library card; the pencil button next to it renames the library. MyTaste scans every
folder in the background, groups files into titles, and matches each one on TMDB so posters,
trailers, and cast work exactly like streaming titles. A folder can belong to only one library,
and folders may not be nested inside each other.

Local titles are not a separate catalog. Every category mixes them with your streaming titles in
one order, so a local movie appears exactly where it ranks by release date or popularity, and a
title you own that is also streaming is shown once with a folder badge. Click a source in the
sidebar to leave it out of the current view; with only folders included, the **Date added** sort
becomes available.

The scanner understands common layouts: loose files named `Title.2019.1080p.mkv`, one folder per
movie such as `Title (2019)/`, director folders like `Nolan/2010 - Inception/`, and shows laid out
as `Show/S01/Show.S01E01.mkv` or `Show/Season 1/`. Titles that cannot be matched still appear
with a placeholder poster. Files are never modified or copied.

Only admins can add folders and see their paths; members play the files of the libraries they
are given. Set `MYTASTE_LIBRARY_ROOTS` to limit which folders may be connected.

## Watching local files

Titles in a local library have a **Play** button in their details (**Resume** when you stopped
part way, **Play S1 E3** for the next episode of a series), and each episode on disk can be
played from the episode rows. The player opens at a link you can copy anywhere:

| Link | Opens |
| --- | --- |
| `/watch/movie/<TMDB id>` | The movie; add `?file=<id>` to pick one of several versions |
| `/watch/tv/<TMDB id>/<season>/<episode>` | That episode |
| `/watch/tv/<TMDB id>` | The next episode to watch |
| `/watch/local/<file id>` | A file that is not matched on TMDB |

Add `?t=<seconds>` to start at a given time. The player has keyboard shortcuts (Space or K to
pause, ←/→ or J/L to skip 10 seconds, ↑/↓ for volume, F for full screen, M to mute, C to cycle
subtitles, N for the next episode), remembers your volume, subtitle, audio-language and quality
choices on each device, offers to resume, and counts down to the next episode at the end.
Progress is saved as you watch; a title counts as watched at 90 %.

The server uses the least work that the browser can play: the original file when possible,
otherwise the video copied into an HLS stream, and only as a last resort a conversion. Subtitles
next to the video (`.srt`, `.ass`, `.vtt`, also in a `Subs` folder) and text subtitles inside the
file are converted to WebVTT, including Persian and Arabic files saved in Windows-1256. Image
subtitles (PGS, VobSub) are drawn into a converted video. Playback needs ffmpeg on the server; see
[deployment](docs/deployment.md#playing-local-videos) for how streaming works and what it costs.

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

Game metadata and availability is supplied by [Xbox and Microsoft Store](https://www.xbox.com/xbox-game-pass/games)
and [Steam](https://store.steampowered.com/). [IsThereAnyDeal](https://isthereanydeal.com/) and
[Wikidata](https://www.wikidata.org/) help match games sold in both stores. MyTaste is not
affiliated with Microsoft or Valve.

# Deployment

MyTaste is a single-process Python web application. Its only persistent application state is a
SQLite database, so it runs well on a workstation, home server, NAS, small VM, or container host.

## Docker Compose

Copy `compose.yaml.example` to `compose.yaml`, set the token, and start the service:

```bash
cp compose.yaml.example compose.yaml
export MYTASTE_TMDB_TOKEN=your-read-access-token
docker compose up -d --build
```

The named `mytaste-data` volume stores subscription preferences across container replacements.
The container runs as an unprivileged user and exposes a health check at `/healthz`.

## Native install

```bash
uv tool install .
export MYTASTE_TMDB_TOKEN=your-read-access-token
mytaste serve --host 127.0.0.1 --port 8000
```

By default, the database is stored at `$XDG_DATA_HOME/mytaste/mytaste.db` or
`~/.local/share/mytaste/mytaste.db`. Set `MYTASTE_DATA_DIR` to place it elsewhere.

## Storage libraries

Storage libraries read media folders on the host. When running natively, the service user needs
read and execute permission on each folder. In Docker, bind-mount the folders read-only and
point `MYTASTE_LIBRARY_ROOTS` at the mount points:

```yaml
    volumes:
      - mytaste-data:/data
      - /mnt/media/Movies:/media/Movies:ro
      - /mnt/media/Shows:/media/Shows:ro
    environment:
      MYTASTE_LIBRARY_ROOTS: /media
```

Scans run inside the web process in a background task. Matching contacts TMDB once per new
title, and results are stored in the SQLite database so rescans are cheap. If a folder is
unplugged the previous index of its library is kept and the library is marked unavailable until
the next scan.

## Playing local videos

The player streams library files from the server, so ffmpeg and ffprobe must be installed (the
Docker image includes them; on Debian or Raspberry Pi OS run `sudo apt install ffmpeg`). Without
them the app still browses libraries, but files can only be played when the browser supports
them as they are.

For each file the browser reports which codecs it can decode, and the server picks the cheapest
method that works:

- **Direct play** sends the original file with byte-range requests. It costs nothing.
- **Direct stream** copies the video into HLS (fragmented MP4) segments that start on the file's
  own keyframes, converting only unsupported audio such as DTS to AAC. This is typical for MKV
  files and uses little CPU.
- **Converting** re-encodes the video with x264 at up to 720p (or the quality chosen in the
  player) for codecs the browser cannot decode, such as HEVC in Chrome on Linux, or when an image
  subtitle has to be drawn into the picture.

Conversion is CPU-heavy. `MYTASTE_MAX_TRANSCODES` (default 1) limits how many run at once; when a
new viewer needs one, the oldest is stopped. ffmpeg runs at lower priority, stays only a short
way ahead of the viewer, and is stopped when nobody has requested a segment for two minutes. On a
Raspberry Pi 5, a 1080p HEVC film converts to 720p at about 2.5 times real time using the Pi's
HEVC decoder, which is detected automatically (`MYTASTE_HWACCEL=auto`); set it to `none` to
decode in software.

Segments and extracted subtitles are written to `MYTASTE_CACHE_DIR` (by default
`$XDG_CACHE_HOME/mytaste`). Segments are deleted when a session ends, and nothing in the cache
needs a backup. Each file is inspected with ffprobe once, in the background after a scan, and the
result is stored in the database.

## Game Pass caching

Games browsing is available by default. Only the Game Pass catalog cache is disabled. To enable
it later, add these settings to the service environment and restart the single worker:

```bash
MYTASTE_GAMEPASS_CACHE_ENABLED=true
MYTASTE_GAMEPASS_CATALOG_TTL_SECONDS=7200
MYTASTE_GAMEPASS_METADATA_TTL_SECONDS=86400
MYTASTE_GAMEPASS_STALE_TTL_SECONDS=86400
```

The Compose example exposes the enabled flag, defaulting to `false`. Set it to `true` in your
deployment environment and recreate the container with `docker compose up -d`. To override
TTLs, also add those variables to Compose's `environment` block. TTLs must be finite and
positive; the stale TTL can be zero to require fresh data.

The cache is lazy: opening Games loads the first context; startup and `/healthz` make no
Microsoft requests. SQLite is stored at `MYTASTE_CACHE_DIR/gamepass/catalog.sqlite3`, or
`$XDG_CACHE_HOME/mytaste/gamepass/catalog.sqlite3` (normally `~/.cache/mytaste/...`). The Docker
image uses `/data/cache`, already on the `mytaste-data` volume. Use writable persistent storage
for cache reuse across restarts. Metadata and membership keys separate regions and languages;
membership also separates plans, platforms, and collections. The cache holds at most 16,384
entries, including Steam's details of owned games and of Game Pass games sold on Steam.

Fresh data is reused for its TTL. Expired data within the additional stale TTL is returned with
an earlier-catalog notice while a background task refreshes it. Failed refreshes retain the
previous snapshot; beyond the stale limit, Games displays an upstream error. The notice's time
is the oldest snapshot used, including metadata. Identical refresh batches are shared within
one worker. Refresh tasks are canceled on shutdown. Corrupt or unwritable cache storage falls
back to live requests and logs a warning; stale outage fallback is then unavailable.

With the enabled flag `false`, Game Pass performs no cache reads/writes, creates no catalog
cache database, and schedules no background refresh. Existing cache files are ignored. Plan
and platform preferences and stable collection identities remain in the application database.
TMDB and playback caches are independent of this switch.

To clear the disposable Game Pass cache, stop the process, remove only
`MYTASTE_CACHE_DIR/gamepass/catalog.sqlite3`, and restart. It needs no backup. Do not delete the
application database or future imported collection assets. Catalog removal never deletes saved
movie/TV collections. See the [portable collections design](taste-collections.md) for the later
shared saved-item and import/export migration.

Outbound HTTPS goes to `catalog.gamepass.com` and `displaycatalog.mp.microsoft.com`; covers and
screenshots use Microsoft's image CDN URLs in browsers. Browsing needs no Xbox account or extra
API key. Microsoft can change its website feeds; catalog errors affect Games rather than health.

## Steam

Steam's store is browsable without configuration. To list owned games, set a
[Steam Web API key](https://steamcommunity.com/dev/apikey) in the service environment:

```bash
MYTASTE_STEAM_API_KEY=your-key
```

The key belongs to the person running the server (Steam issues keys only to accounts that have
spent at least $5) and reads the owned games of any public profile, so people using the
instance only connect their profile. It stays on the server: it is never stored in the
database, shown in the UI, or sent to browsers. `MYTASTE_STEAM_ENABLED=false` turns Steam off.

**Sign in through Steam** uses Steam's OpenID: the browser goes to Steam and back to
`/games/steam/callback`, and the server confirms the reply with Steam before saving the account's
ID. Steam only redirects the browser, so the server needs no public address; it must build its
callback from the address the browser used. Uvicorn trusts `X-Forwarded-Proto` from
`127.0.0.1`, which covers Tailscale Serve and proxies on the same host. Behind a proxy on
another address, set `FORWARDED_ALLOW_IPS` to it (for Docker, the proxy's container network).

The connected account and its owned-games list (refreshed every six hours, or from
**Services › Refresh**) are kept in the application database, as are the Steam ↔ Xbox matches
(`game_links`, rechecked weekly). With the Game Pass cache enabled, Steam's game details share it.

Outbound HTTPS goes to `api.steampowered.com`, `steamcommunity.com` (sign-in and profile
names), `api.isthereanydeal.com`, and `query.wikidata.org`; covers and screenshots load from
Steam's image CDN in browsers. A Steam outage affects Games only.

### Saved games and the database

The first start of a version with Steam moves saved movies and series into shared saved items
and ordered collection entries, which can also hold games. The move runs once, in one
transaction, and keeps every collection's order and dates. The previous `collection_items`
table is left as it was and is no longer used. Back up the database before upgrading, as before
any release.

### Accounts and the database

The first start of a version with accounts gives each one-profile table (region, services,
display options, watch progress, Game Pass plan, Steam account) a `user_id` and assigns the
existing rows to user 1; collections get a `user_id` column. Each table is rebuilt in its own
transaction, and nothing is copied or deleted otherwise. Until someone opens the app and creates
the owner on the setup page, every page redirects there; the owner becomes user 1 and finds
everything as before. The old site name is no longer used: the header shows the signed-in
person's name. There is
no way back to the one-profile layout except restoring a backup, so back up the database first.

## Network exposure

Binding to `0.0.0.0` makes MyTaste reachable through the host's network interfaces. Everyone
signs in, but by default the "Who's watching?" screen lets anyone who can reach the app open a
profile that has no password or PIN, and play that profile's libraries. For Internet or
untrusted-LAN exposure, turn the profile screen off under **People**, give every account a
password, and put MyTaste behind a reverse proxy that provides HTTPS. Sign-in cookies are marked
`Secure` when the request arrives over HTTPS (uvicorn trusts `X-Forwarded-Proto` from a proxy
on the same host).

Run one application worker against a database. The in-memory TMDB cache is intentionally local to
the process, and the workload does not benefit from multiple workers for a household deployment.

## Health check

`GET /healthz` returns:

```json
{"status":"ok"}
```

This confirms that the process and routing layer are alive. It deliberately does not call TMDB,
so a temporary upstream outage does not cause container restarts.

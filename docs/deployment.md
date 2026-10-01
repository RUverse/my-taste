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

## Network exposure

Binding to `0.0.0.0` makes MyTaste reachable through the host's network interfaces. The app does
not provide accounts or authentication in this release. For Internet or untrusted-LAN exposure,
put it behind a reverse proxy that provides HTTPS and access control. With a storage library,
anyone who can reach the app can also play its files.

Run one application worker against a database. The in-memory TMDB cache is intentionally local to
the process, and the workload does not benefit from multiple workers for a household deployment.

## Health check

`GET /healthz` returns:

```json
{"status":"ok"}
```

This confirms that the process and routing layer are alive. It deliberately does not call TMDB,
so a temporary upstream outage does not cause container restarts.

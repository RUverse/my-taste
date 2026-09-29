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
unplugged the previous index is kept and the library is marked unavailable until the next scan.

## Network exposure

Binding to `0.0.0.0` makes MyTaste reachable through the host's network interfaces. The app does
not provide accounts or authentication in this release. For Internet or untrusted-LAN exposure,
put it behind a reverse proxy that provides HTTPS and access control.

Run one application worker against a database. The in-memory TMDB cache is intentionally local to
the process, and the workload does not benefit from multiple workers for a household deployment.

## Health check

`GET /healthz` returns:

```json
{"status":"ok"}
```

This confirms that the process and routing layer are alive. It deliberately does not call TMDB,
so a temporary upstream outage does not cause container restarts.

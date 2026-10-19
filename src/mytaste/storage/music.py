"""SQLite cache for the music catalog, shared by everyone on the instance.

Album facts come from MusicBrainz and ListenBrainz (CC0), so they can be kept; they are
refreshed after a week. Popularity is refreshed daily. ``music_links`` remembers which
MusicBrainz album a chart entry or a service's album is, including entries that matched
nothing, so each is looked up once.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

from mytaste.music.models import Album, AlbumDetails

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS music_release_groups (
        mbid TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        fetched_at REAL NOT NULL,
        enriched_at REAL,
        details TEXT,
        details_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS music_popularity (
        mbid TEXT PRIMARY KEY,
        listens INTEGER NOT NULL DEFAULT 0,
        listeners INTEGER NOT NULL DEFAULT 0,
        checked_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS music_links (
        source TEXT NOT NULL,
        source_id TEXT NOT NULL,
        mbid TEXT,
        method TEXT NOT NULL DEFAULT '',
        checked_at REAL NOT NULL,
        PRIMARY KEY (source, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS music_links_mbid ON music_links (mbid) WHERE mbid IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS music_charts (
        source TEXT NOT NULL,
        region TEXT NOT NULL,
        entries TEXT NOT NULL,
        fetched_at REAL NOT NULL,
        PRIMARY KEY (source, region)
    )
    """,
)
# SQLite allows a limited number of parameters per statement.
_CHUNK = 500


def _chunks(values: Sequence[str]) -> Iterator[Sequence[str]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


class MusicCacheRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            for statement in _SCHEMA:
                connection.execute(statement)

    # Album facts ------------------------------------------------------------------------

    def albums(self, mbids: Iterable[str]) -> dict[str, tuple[Album, float, float | None]]:
        """Each known album with when it was fetched and when MusicBrainz last added to it."""

        wanted = list(dict.fromkeys(mbids))
        found: dict[str, tuple[Album, float, float | None]] = {}
        with self._connect() as connection:
            for chunk in _chunks(wanted):
                marks = ",".join("?" * len(chunk))
                rows = connection.execute(
                    f"SELECT mbid, data, fetched_at, enriched_at FROM music_release_groups "
                    f"WHERE mbid IN ({marks})",
                    tuple(chunk),
                ).fetchall()
                for key, data, fetched, enriched in rows:
                    try:
                        album = Album.from_payload(json.loads(data))
                    except (TypeError, ValueError):
                        continue
                    found[str(key)] = (album, float(fetched), enriched)
        return found

    def save_albums(
        self, albums: Iterable[Album], *, enriched: bool = False, fetched: bool = True
    ) -> None:
        """Store albums. ``fetched`` marks fresh ListenBrainz facts, ``enriched`` facts that
        MusicBrainz has added to."""

        now = time.time()
        rows = [
            (album.mbid, json.dumps(_facts(album)), now, now if enriched else None, fetched)
            for album in albums
        ]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO music_release_groups (mbid, data, fetched_at, enriched_at)
                VALUES (?1, ?2, ?3, ?4)
                ON CONFLICT(mbid) DO UPDATE SET
                    data = excluded.data,
                    fetched_at = CASE WHEN ?5 THEN excluded.fetched_at ELSE fetched_at END,
                    enriched_at = COALESCE(excluded.enriched_at, enriched_at)
                """,
                rows,
            )

    # Popularity -------------------------------------------------------------------------

    def popularity(self, mbids: Iterable[str]) -> dict[str, tuple[int, int, float]]:
        wanted = list(dict.fromkeys(mbids))
        found: dict[str, tuple[int, int, float]] = {}
        with self._connect() as connection:
            for chunk in _chunks(wanted):
                marks = ",".join("?" * len(chunk))
                for key, listens, listeners, checked in connection.execute(
                    f"SELECT mbid, listens, listeners, checked_at FROM music_popularity "
                    f"WHERE mbid IN ({marks})",
                    tuple(chunk),
                ):
                    found[str(key)] = (int(listens), int(listeners), float(checked))
        return found

    def save_popularity(self, counts: dict[str, tuple[int, int]]) -> None:
        if not counts:
            return
        now = time.time()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO music_popularity (mbid, listens, listeners, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(mbid) DO UPDATE SET listens = excluded.listens,
                    listeners = excluded.listeners, checked_at = excluded.checked_at
                """,
                ((key, listens, users, now) for key, (listens, users) in counts.items()),
            )

    # Matches ----------------------------------------------------------------------------

    def links(self, source: str, ids: Iterable[str]) -> dict[str, tuple[str | None, float]]:
        """What each of ``source``'s IDs matched (``None`` for nothing) and when it was tried."""

        wanted = list(dict.fromkeys(ids))
        found: dict[str, tuple[str | None, float]] = {}
        with self._connect() as connection:
            for chunk in _chunks(wanted):
                marks = ",".join("?" * len(chunk))
                for key, mbid, checked in connection.execute(
                    f"SELECT source_id, mbid, checked_at FROM music_links "
                    f"WHERE source = ? AND source_id IN ({marks})",
                    (source, *chunk),
                ):
                    found[str(key)] = (str(mbid) if mbid else None, float(checked))
        return found

    def save_links(self, source: str, rows: Iterable[tuple[str, str | None, str]]) -> None:
        now = time.time()
        values = [(source, key, mbid, method, now) for key, mbid, method in rows]
        if not values:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO music_links (source, source_id, mbid, method, checked_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, source_id) DO UPDATE SET mbid = excluded.mbid,
                    method = excluded.method, checked_at = excluded.checked_at
                """,
                values,
            )

    # Charts -----------------------------------------------------------------------------

    def chart(self, source: str, region: str) -> tuple[list[dict[str, str]], float] | None:
        """The last chart read for ``region``, kept for when the service is down."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT entries, fetched_at FROM music_charts WHERE source = ? AND region = ?",
                (source, region),
            ).fetchone()
        if row is None:
            return None
        try:
            entries = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return (entries if isinstance(entries, list) else []), float(row[1])

    def save_chart(self, source: str, region: str, entries: list[dict[str, str]]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO music_charts (source, region, entries, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, region) DO UPDATE SET entries = excluded.entries,
                    fetched_at = excluded.fetched_at
                """,
                (source, region, json.dumps(entries), time.time()),
            )

    # Details ----------------------------------------------------------------------------

    def details(self, mbid: str) -> tuple[AlbumDetails, float] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT details, details_at FROM music_release_groups "
                "WHERE mbid = ? AND details IS NOT NULL",
                (mbid,),
            ).fetchone()
        if row is None:
            return None
        try:
            return AlbumDetails.from_payload(json.loads(row[0])), float(row[1])
        except (TypeError, ValueError):
            return None

    def save_details(self, details: AlbumDetails) -> None:
        album = details.album
        now = time.time()
        payload = details.payload()
        payload["album"] = _facts(album)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO music_release_groups (mbid, data, fetched_at, details, details_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(mbid) DO UPDATE SET details = excluded.details,
                    details_at = excluded.details_at
                """,
                (album.mbid, json.dumps(_facts(album)), now, json.dumps(payload), now),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=5)


def _facts(album: Album) -> dict[str, object]:
    """What the cache keeps of an album; popularity has its own table."""

    data = album.payload()
    data.pop("listens", None)
    data.pop("listeners", None)
    return data

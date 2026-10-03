"""SQLite persistence for user collections, their titles, and where titles stream."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mytaste.catalog.models import SORT_KEYS, MediaType
from mytaste.collections.models import ICONS, Collection, CollectionItem

_MAX_NAME_LENGTH = 60
_MAX_DESCRIPTION_LENGTH = 200
_MEDIA_TYPES = frozenset({"movie", "tv"})

# Created once, the first time the collections table appears; deleting them is final.
_DEFAULT_COLLECTIONS = (
    ("Watchlist", "Titles to watch next.", "bookmark"),
    ("My favourites", "Titles you love.", "heart"),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CollectionRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            existed = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'collections'"
            ).fetchone()
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    icon TEXT NOT NULL DEFAULT '',
                    default_sort TEXT NOT NULL DEFAULT 'added',
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_items (
                    collection_id INTEGER NOT NULL
                        REFERENCES collections(id) ON DELETE CASCADE,
                    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
                    tmdb_id INTEGER NOT NULL CHECK (tmdb_id > 0),
                    added_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    release_date TEXT NOT NULL DEFAULT '',
                    overview TEXT NOT NULL DEFAULT '',
                    rating REAL NOT NULL DEFAULT 0,
                    popularity REAL NOT NULL DEFAULT 0,
                    poster_path TEXT,
                    genre_ids TEXT NOT NULL DEFAULT ',',
                    genres TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (collection_id, media_type, tmdb_id)
                );
                CREATE INDEX IF NOT EXISTS collection_items_title
                    ON collection_items (media_type, tmdb_id);
                CREATE TABLE IF NOT EXISTS title_availability (
                    region TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    provider_ids TEXT NOT NULL,
                    checked_at TEXT NOT NULL,
                    PRIMARY KEY (region, media_type, tmdb_id)
                );
                """
            )
            if existed is None:
                now = utc_now()
                connection.executemany(
                    """
                    INSERT INTO collections (name, description, icon, position, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (name, description, icon, position, now)
                        for position, (name, description, icon) in enumerate(_DEFAULT_COLLECTIONS)
                    ),
                )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(collections)")}
            if "portable_id" not in columns:
                connection.execute("ALTER TABLE collections ADD COLUMN portable_id TEXT")
            missing = connection.execute(
                "SELECT id FROM collections WHERE portable_id IS NULL"
            ).fetchall()
            connection.executemany(
                "UPDATE collections SET portable_id = ? WHERE id = ?",
                [(f"collection-{uuid4().hex}", row[0]) for row in missing],
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS collections_portable_id "
                "ON collections(portable_id)"
            )

    # Collections ------------------------------------------------------------------------

    def list(self) -> tuple[Collection, ...]:
        with self._connect() as connection:
            rows = connection.execute(f"{_COLLECTION_SELECT} ORDER BY c.position, c.id").fetchall()
        return tuple(_collection_from_row(row) for row in rows)

    def get(self, collection_id: int) -> Collection | None:
        with self._connect() as connection:
            row = connection.execute(
                f"{_COLLECTION_SELECT} WHERE c.id = ?", (collection_id,)
            ).fetchone()
        return _collection_from_row(row) if row else None

    def create(
        self,
        name: str,
        *,
        description: str = "",
        icon: str = "",
        default_sort: str = "added",
    ) -> Collection:
        values = _validated(name, description, icon, default_sort)
        with self._connect() as connection:
            position = connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM collections"
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO collections (name, description, icon, default_sort, position,
                                         created_at, portable_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*values, int(position), utc_now(), f"collection-{uuid4().hex}"),
            )
            collection_id = int(cursor.lastrowid or 0)
        created = self.get(collection_id)
        assert created is not None
        return created

    def update(
        self,
        collection_id: int,
        *,
        name: str,
        description: str = "",
        icon: str = "",
        default_sort: str = "added",
    ) -> Collection | None:
        values = _validated(name, description, icon, default_sort)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE collections SET name = ?, description = ?, icon = ?, default_sort = ?
                WHERE id = ?
                """,
                (*values, collection_id),
            )
        return self.get(collection_id) if cursor.rowcount else None

    def delete(self, collection_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM collections WHERE id = ?", (collection_id,))
        return cursor.rowcount > 0

    # Titles -----------------------------------------------------------------------------

    def items(self, collection_id: int) -> tuple[CollectionItem, ...]:
        """Return a collection's titles in the order they were added."""

        with self._connect() as connection:
            rows = connection.execute(
                f"{_ITEM_SELECT} WHERE collection_id = ? ORDER BY added_at, rowid",
                (collection_id,),
            ).fetchall()
        return tuple(_item_from_row(row) for row in rows)

    def add_item(self, collection_id: int, item: CollectionItem) -> bool:
        """Save a title in a collection; ``False`` when it was already there."""

        if item.media_type not in _MEDIA_TYPES or item.tmdb_id <= 0:
            raise ValueError("Only titles matched on TMDB can be saved")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO collection_items (
                    collection_id, media_type, tmdb_id, added_at, title, release_date,
                    overview, rating, popularity, poster_path, genre_ids, genres
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    collection_id,
                    item.media_type,
                    item.tmdb_id,
                    item.added_at or utc_now(),
                    item.title,
                    item.release_date,
                    item.overview,
                    item.rating,
                    item.popularity,
                    item.poster_path,
                    _encode_ids(item.genre_ids),
                    json.dumps(list(item.genres)),
                ),
            )
        return cursor.rowcount > 0

    def remove_item(self, collection_id: int, media_type: MediaType, tmdb_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM collection_items
                WHERE collection_id = ? AND media_type = ? AND tmdb_id = ?
                """,
                (collection_id, media_type, tmdb_id),
            )
        return cursor.rowcount > 0

    def memberships(self, media_type: MediaType, tmdb_id: int) -> frozenset[int]:
        """Return the ids of the collections that hold a title."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT collection_id FROM collection_items WHERE media_type = ? AND tmdb_id = ?",
                (media_type, tmdb_id),
            ).fetchall()
        return frozenset(int(row[0]) for row in rows)

    # Availability -----------------------------------------------------------------------

    def availability(
        self, region: str, keys: Iterable[tuple[str, int]]
    ) -> dict[tuple[str, int], tuple[frozenset[int], str]]:
        """Return the stored providers and check time of each title known for ``region``."""

        wanted = set(keys)
        if not wanted:
            return {}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT media_type, tmdb_id, provider_ids, checked_at
                FROM title_availability WHERE region = ?
                """,
                (region,),
            ).fetchall()
        found: dict[tuple[str, int], tuple[frozenset[int], str]] = {}
        for row in rows:
            key = (str(row[0]), int(row[1]))
            if key in wanted:
                found[key] = (frozenset(_decode_ids(str(row[2]))), str(row[3]))
        return found

    def save_availability(
        self,
        region: str,
        entries: Sequence[tuple[tuple[str, int], frozenset[int]]],
        *,
        checked_at: str | None = None,
    ) -> None:
        if not entries:
            return
        stamp = checked_at or utc_now()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO title_availability (
                    region, media_type, tmdb_id, provider_ids, checked_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(region, media_type, tmdb_id) DO UPDATE SET
                    provider_ids = excluded.provider_ids,
                    checked_at = excluded.checked_at
                """,
                (
                    (region, media_type, tmdb_id, _encode_ids(sorted(providers)), stamp)
                    for (media_type, tmdb_id), providers in entries
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


_COLLECTION_SELECT = """
    SELECT c.id, c.name, c.description, c.icon, c.default_sort, c.position, c.created_at,
           (SELECT COUNT(*) FROM collection_items i WHERE i.collection_id = c.id),
           c.portable_id
    FROM collections c
"""

_ITEM_SELECT = """
    SELECT media_type, tmdb_id, added_at, title, release_date, overview, rating, popularity,
           poster_path, genre_ids, genres, rowid
    FROM collection_items
"""


def _validated(name: str, description: str, icon: str, default_sort: str) -> tuple[str, ...]:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValueError("Give the collection a name")
    if len(cleaned) > _MAX_NAME_LENGTH:
        raise ValueError(f"Keep the name under {_MAX_NAME_LENGTH} characters")
    text = " ".join(description.split())
    if len(text) > _MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"Keep the description under {_MAX_DESCRIPTION_LENGTH} characters")
    if icon and icon not in ICONS:
        raise ValueError("Choose one of the offered icons")
    if default_sort not in SORT_KEYS:
        raise ValueError("Choose a supported sort order")
    return cleaned, text, icon, default_sort


def _encode_ids(values: Iterable[int]) -> str:
    return "," + "".join(f"{value}," for value in values)


def _decode_ids(raw: str) -> tuple[int, ...]:
    return tuple(int(token) for token in raw.split(",") if token)


def _collection_from_row(row: sqlite3.Row | tuple[object, ...]) -> Collection:
    sort = str(row[4])
    return Collection(
        id=int(row[0]),
        name=str(row[1]),
        description=str(row[2]),
        icon=str(row[3]),
        default_sort=sort if sort in SORT_KEYS else "added",  # type: ignore[arg-type]
        position=int(row[5]),
        created_at=str(row[6]),
        item_count=int(row[7]),
        portable_id=str(row[8]),
    )


def _item_from_row(row: sqlite3.Row | tuple[object, ...]) -> CollectionItem:
    try:
        genres = tuple(str(name) for name in json.loads(str(row[10])))
    except (TypeError, ValueError):
        genres = ()
    return CollectionItem(
        media_type=str(row[0]),  # type: ignore[arg-type]
        tmdb_id=int(row[1]),
        added_at=str(row[2]),
        title=str(row[3]),
        release_date=str(row[4]),
        overview=str(row[5]),
        rating=float(row[6]),
        popularity=float(row[7]),
        poster_path=str(row[8]) if row[8] else None,
        genre_ids=_decode_ids(str(row[9])),
        genres=genres,
        sequence=int(row[11]),
    )

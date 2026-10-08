"""SQLite persistence for user collections, their titles, and where titles stream."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mytaste.accounts.context import current_user_id
from mytaste.catalog.models import SORT_KEYS, MediaType
from mytaste.collections.models import ICONS, Collection, CollectionItem, SavedGame
from mytaste.games.models import Game, game_key, parse_game_key
from mytaste.storage.migrations import FIRST_USER_ID

_MAX_NAME_LENGTH = 60
_MAX_DESCRIPTION_LENGTH = 200
_MEDIA_TYPES = frozenset({"movie", "tv"})

# Every account starts with these; deleting them is final.
_DEFAULT_COLLECTIONS = (
    ("Watchlist", "Titles to watch next.", "bookmark"),
    ("My favourites", "Titles you love.", "heart"),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_default_collections(connection: sqlite3.Connection, user_id: int) -> None:
    """Give an account the starting collections, unless it already has collections."""

    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'collections'"
    ).fetchone():
        return
    if connection.execute("SELECT 1 FROM collections WHERE user_id = ?", (user_id,)).fetchone():
        return
    now = utc_now()
    connection.executemany(
        """
        INSERT INTO collections (user_id, name, description, icon, position, created_at,
                                 portable_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (user_id, name, description, icon, position, now, f"collection-{uuid4().hex}")
            for position, (name, description, icon) in enumerate(_DEFAULT_COLLECTIONS)
        ),
    )


class CollectionRepository:
    """Each user's collections, the titles saved in them, and where titles stream (shared)."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL DEFAULT 1,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    icon TEXT NOT NULL DEFAULT '',
                    default_sort TEXT NOT NULL DEFAULT 'added',
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
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
        self._migrate_saved_items("saved_items" not in tables and "collection_items" in tables)
        with self._connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(collections)")}
            if "user_id" not in columns:
                # Collections from before accounts belong to the first user.
                connection.execute(
                    f"ALTER TABLE collections ADD COLUMN user_id INTEGER NOT NULL "
                    f"DEFAULT {FIRST_USER_ID}"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS collections_user ON collections (user_id, position)"
            )
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
            if "collections" not in tables:
                create_default_collections(connection, FIRST_USER_ID)

    def _migrate_saved_items(self, copy_titles: bool) -> None:
        """Create the shared saved items and ordered collection entries.

        Saved movies and series used to live in ``collection_items``, one snapshot per
        collection, which could hold neither games nor an explicit order. The first start with
        the new tables copies them over in one transaction, keeping each collection's order and
        dates; each title keeps its most recently saved snapshot. ``collection_items`` is left
        untouched and unused, so the copy loses nothing.
        """

        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'saved_items'"
            ).fetchone()
            for statement in _SAVED_ITEMS_SCHEMA:
                connection.execute(statement)
            if copy_titles and not exists:
                connection.execute(
                    """
                    INSERT INTO saved_items (
                        portable_id, kind, tmdb_id, title, release_date, overview, rating,
                        popularity, poster_path, genre_ids, genres
                    )
                    SELECT media_type || '-tmdb-' || tmdb_id, media_type, tmdb_id, title,
                           release_date, overview, rating, popularity, poster_path, genre_ids,
                           genres
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY media_type, tmdb_id ORDER BY added_at DESC, rowid DESC
                        ) AS newest
                        FROM collection_items
                        WHERE collection_id IN (SELECT id FROM collections)
                    )
                    WHERE newest = 1
                    """
                )
                connection.execute(
                    """
                    INSERT INTO collection_entries (collection_id, item_id, position, added_at)
                    SELECT old.collection_id, saved.id,
                           ROW_NUMBER() OVER (
                               PARTITION BY old.collection_id ORDER BY old.added_at, old.rowid
                           ),
                           old.added_at
                    FROM collection_items AS old
                    JOIN collections ON collections.id = old.collection_id
                    JOIN saved_items AS saved
                      ON saved.kind = old.media_type AND saved.tmdb_id = old.tmdb_id
                    """
                )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    # Collections ------------------------------------------------------------------------

    def list(self) -> tuple[Collection, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"{_COLLECTION_SELECT} WHERE c.user_id = ? ORDER BY c.position, c.id",
                (current_user_id(),),
            ).fetchall()
        return tuple(_collection_from_row(row) for row in rows)

    def get(self, collection_id: int) -> Collection | None:
        with self._connect() as connection:
            row = connection.execute(
                f"{_COLLECTION_SELECT} WHERE c.id = ? AND c.user_id = ?",
                (collection_id, current_user_id()),
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
        user_id = current_user_id()
        with self._connect() as connection:
            position = connection.execute(
                "SELECT COALESCE(MAX(position) + 1, 0) FROM collections WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
            cursor = connection.execute(
                """
                INSERT INTO collections (user_id, name, description, icon, default_sort,
                                         position, created_at, portable_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, *values, int(position), utc_now(), f"collection-{uuid4().hex}"),
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
                WHERE id = ? AND user_id = ?
                """,
                (*values, collection_id, current_user_id()),
            )
        return self.get(collection_id) if cursor.rowcount else None

    def delete(self, collection_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM collections WHERE id = ? AND user_id = ?",
                (collection_id, current_user_id()),
            )
        return cursor.rowcount > 0

    # Titles and games -------------------------------------------------------------------

    def items(self, collection_id: int) -> tuple[CollectionItem, ...]:
        """Return a collection's movies and series in the order they were added."""

        with self._connect() as connection:
            rows = connection.execute(
                f"{_ITEM_SELECT} WHERE e.collection_id = ? AND s.kind IN ('movie', 'tv') "
                f"AND {_OWN_COLLECTION} ORDER BY e.position",
                (collection_id, current_user_id()),
            ).fetchall()
        return tuple(_item_from_row(row) for row in rows)

    def games(self, collection_id: int) -> tuple[SavedGame, ...]:
        """Return a collection's games, in the order they were added."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.snapshot, s.steam_appid, s.xbox_id, e.added_at, e.position, "
                "s.portable_id FROM collection_entries e JOIN saved_items s ON s.id = e.item_id "
                f"WHERE e.collection_id = ? AND s.kind = 'game' AND {_OWN_COLLECTION} "
                "ORDER BY e.position",
                (collection_id, current_user_id()),
            ).fetchall()
        return tuple(_saved_game(row) for row in rows)

    def add_item(self, collection_id: int, item: CollectionItem) -> bool:
        """Save a title in a collection; ``False`` when it was already there."""

        if item.media_type not in _MEDIA_TYPES or item.tmdb_id <= 0:
            raise ValueError("Only titles matched on TMDB can be saved")
        with self._connect() as connection:
            values = (
                item.title,
                item.release_date,
                item.overview,
                item.rating,
                item.popularity,
                item.poster_path,
                _encode_ids(item.genre_ids),
                json.dumps(list(item.genres)),
            )
            row = connection.execute(
                "SELECT id FROM saved_items WHERE kind = ? AND tmdb_id = ?",
                (item.media_type, item.tmdb_id),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO saved_items (
                        portable_id, kind, tmdb_id, title, release_date, overview, rating,
                        popularity, poster_path, genre_ids, genres
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (item.portable_id, item.media_type, item.tmdb_id, *values),
                )
                item_id = int(cursor.lastrowid or 0)
            else:
                item_id = int(row[0])
                connection.execute(
                    """
                    UPDATE saved_items SET title = ?, release_date = ?, overview = ?, rating = ?,
                        popularity = ?, poster_path = ?, genre_ids = ?, genres = ?
                    WHERE id = ?
                    """,
                    (*values, item_id),
                )
            return _add_entry(connection, collection_id, item_id, item.added_at or utc_now())

    def add_game(self, collection_id: int, game: Game, added_at: str = "") -> bool:
        """Save a game in a collection with a snapshot of its details; ``False`` if already
        there. A game saved before its Steam or Xbox listing was known keeps its saved ID."""

        with self._connect() as connection:
            item_id = _find_game(connection, game.steam_appid, game.xbox_id)
            snapshot = json.dumps(game.payload())
            summary = (game.title, game.release_date, game.overview, game.score or 0.0)
            if item_id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO saved_items (
                        portable_id, kind, steam_appid, xbox_id, title, release_date, overview,
                        rating, snapshot
                    ) VALUES (?, 'game', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        game.portable_id,
                        game.steam_appid or None,
                        game.xbox_id or None,
                        *summary,
                        snapshot,
                    ),
                )
                item_id = int(cursor.lastrowid or 0)
            else:
                # Learn the other store's ID, unless another saved game already has it (both
                # listings were saved separately before they were matched).
                steam = game.steam_appid or None
                xbox = game.xbox_id or None
                if steam and _find_game(connection, steam, "") not in (None, item_id):
                    steam = None
                if xbox and _find_game(connection, 0, xbox) not in (None, item_id):
                    xbox = None
                connection.execute(
                    """
                    UPDATE saved_items SET steam_appid = COALESCE(steam_appid, ?),
                        xbox_id = COALESCE(xbox_id, ?), title = ?, release_date = ?,
                        overview = ?, rating = ?, snapshot = ?
                    WHERE id = ?
                    """,
                    (steam, xbox, *summary, snapshot, item_id),
                )
            return _add_entry(connection, collection_id, item_id, added_at or utc_now())

    def remove_item(self, collection_id: int, media_type: MediaType, tmdb_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                DELETE FROM collection_entries AS e
                WHERE e.collection_id = ? AND e.item_id IN (
                    SELECT id FROM saved_items WHERE kind = ? AND tmdb_id = ?
                ) AND {_OWN_COLLECTION}
                """,
                (collection_id, media_type, tmdb_id, current_user_id()),
            )
        return cursor.rowcount > 0

    def remove_game(self, collection_id: int, key: str) -> bool:
        steam, xbox = _game_ids(key)
        with self._connect() as connection:
            item_id = _find_game(connection, steam, xbox)
            if item_id is None:
                return False
            cursor = connection.execute(
                f"DELETE FROM collection_entries AS e WHERE e.collection_id = ? "
                f"AND e.item_id = ? AND {_OWN_COLLECTION}",
                (collection_id, item_id, current_user_id()),
            )
        return cursor.rowcount > 0

    def memberships(self, media_type: MediaType, tmdb_id: int) -> frozenset[int]:
        """Return the ids of the collections that hold a title."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT e.collection_id FROM collection_entries e "
                "JOIN saved_items s ON s.id = e.item_id WHERE s.kind = ? AND s.tmdb_id = ? "
                f"AND {_OWN_COLLECTION}",
                (media_type, tmdb_id, current_user_id()),
            ).fetchall()
        return frozenset(int(row[0]) for row in rows)

    def game_memberships(self, key: str) -> frozenset[int]:
        steam, xbox = _game_ids(key)
        with self._connect() as connection:
            item_id = _find_game(connection, steam, xbox)
            if item_id is None:
                return frozenset()
            rows = connection.execute(
                f"SELECT e.collection_id FROM collection_entries e WHERE e.item_id = ? "
                f"AND {_OWN_COLLECTION}",
                (item_id, current_user_id()),
            ).fetchall()
        return frozenset(int(row[0]) for row in rows)

    def saved_memberships(self) -> dict[tuple[str, int], frozenset[int]]:
        """Map every saved ``(media_type, tmdb_id)`` to the ids of the collections holding it."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.kind, s.tmdb_id, e.collection_id FROM collection_entries e "
                "JOIN saved_items s ON s.id = e.item_id WHERE s.kind IN ('movie', 'tv') "
                f"AND {_OWN_COLLECTION}",
                (current_user_id(),),
            ).fetchall()
        saved: dict[tuple[str, int], set[int]] = {}
        for media_type, tmdb_id, collection_id in rows:
            saved.setdefault((str(media_type), int(tmdb_id)), set()).add(int(collection_id))
        return {key: frozenset(ids) for key, ids in saved.items()}

    def saved_game_memberships(self) -> dict[str, frozenset[int]]:
        """Map every key a saved game is known by (Steam and Xbox) to its collections."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.steam_appid, s.xbox_id, e.collection_id FROM collection_entries e "
                f"JOIN saved_items s ON s.id = e.item_id WHERE s.kind = 'game' "
                f"AND {_OWN_COLLECTION}",
                (current_user_id(),),
            ).fetchall()
        saved: dict[str, set[int]] = {}
        for steam, xbox, collection_id in rows:
            for key in (f"steam-{steam}" if steam else "", f"xbox-{xbox}" if xbox else ""):
                if key:
                    saved.setdefault(key, set()).add(int(collection_id))
        return {key: frozenset(ids) for key, ids in saved.items()}

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
           (SELECT COUNT(*) FROM collection_entries e WHERE e.collection_id = c.id),
           c.portable_id
    FROM collections c
"""

# Limits collection entries (aliased ``e``) to the current user's collections.
_OWN_COLLECTION = "e.collection_id IN (SELECT id FROM collections WHERE user_id = ?)"

_ITEM_SELECT = """
    SELECT s.kind, s.tmdb_id, e.added_at, s.title, s.release_date, s.overview, s.rating,
           s.popularity, s.poster_path, s.genre_ids, s.genres, e.position
    FROM collection_entries e JOIN saved_items s ON s.id = e.item_id
"""

# A saved item is one movie, series, or game, shared by every collection holding it; an entry
# is its place in one collection. Movies and series are identified by TMDB, games by their
# Steam app or Xbox product (either can be found later).
_SAVED_ITEMS_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS saved_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portable_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('movie', 'tv', 'game')),
        tmdb_id INTEGER CHECK (tmdb_id > 0),
        steam_appid INTEGER CHECK (steam_appid > 0),
        xbox_id TEXT,
        title TEXT NOT NULL,
        release_date TEXT NOT NULL DEFAULT '',
        overview TEXT NOT NULL DEFAULT '',
        rating REAL NOT NULL DEFAULT 0,
        popularity REAL NOT NULL DEFAULT 0,
        poster_path TEXT,
        genre_ids TEXT NOT NULL DEFAULT ',',
        genres TEXT NOT NULL DEFAULT '[]',
        snapshot TEXT NOT NULL DEFAULT '{}',
        CHECK ((kind = 'game') = (tmdb_id IS NULL)),
        CHECK (kind != 'game' OR steam_appid IS NOT NULL OR xbox_id IS NOT NULL)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS saved_items_tmdb ON saved_items (kind, tmdb_id) "
    "WHERE tmdb_id IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS saved_items_steam ON saved_items (steam_appid) "
    "WHERE steam_appid IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS saved_items_xbox ON saved_items (xbox_id) "
    "WHERE xbox_id IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS collection_entries (
        collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES saved_items(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        added_at TEXT NOT NULL,
        PRIMARY KEY (collection_id, item_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS collection_entries_item ON collection_entries (item_id)",
)


def _add_entry(
    connection: sqlite3.Connection, collection_id: int, item_id: int, added_at: str
) -> bool:
    found = connection.execute(
        "SELECT 1 FROM collections WHERE id = ? AND user_id = ?",
        (collection_id, current_user_id()),
    )
    if found.fetchone() is None:
        raise LookupError("Unknown collection")
    cursor = connection.execute(
        """
        INSERT INTO collection_entries (collection_id, item_id, position, added_at)
        SELECT ?, ?, COALESCE(MAX(position), 0) + 1, ? FROM collection_entries
        WHERE collection_id = ?
        ON CONFLICT DO NOTHING
        """,
        (collection_id, item_id, added_at, collection_id),
    )
    return cursor.rowcount > 0


def _find_game(connection: sqlite3.Connection, steam: int, xbox: str) -> int | None:
    row = connection.execute(
        "SELECT id FROM saved_items WHERE kind = 'game' AND "
        "(steam_appid = ? OR xbox_id = ?) ORDER BY steam_appid = ? DESC LIMIT 1",
        (steam or None, xbox or None, steam or None),
    ).fetchone()
    return int(row[0]) if row else None


def _game_ids(key: str) -> tuple[int, str]:
    store, value = parse_game_key(key)
    return (int(value), "") if store == "steam" else (0, value)


def _saved_game(row: sqlite3.Row | tuple[object, ...]) -> SavedGame:
    try:
        game = Game.from_payload(json.loads(str(row[0])))
    except (TypeError, ValueError):
        game = Game(str(row[5]).removeprefix("game-"), "Saved game")
    steam, xbox = int(row[1] or 0), str(row[2] or "")
    # Keys found since the snapshot was taken still apply.
    game = replace(
        game,
        steam_appid=game.steam_appid or steam,
        xbox_id=game.xbox_id or xbox,
        id=game_key(steam=game.steam_appid or steam, xbox=game.xbox_id or xbox),
    )
    return SavedGame(game=game, added_at=str(row[3]), sequence=int(row[4]))


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

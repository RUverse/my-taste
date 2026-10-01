from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mytaste.catalog.models import (
    BrowseCategory,
    BrowseMediaType,
    BrowseQuery,
    CatalogPage,
    MediaType,
)
from mytaste.library.models import Library, LibraryFolder, LibraryItem, ScannedFile

_MEDIA_TYPES: frozenset[str] = frozenset({"movie", "tv"})


@dataclass(frozen=True, slots=True)
class ItemDraft:
    """A grouped, optionally matched title ready to be written for a library."""

    group_key: str
    media_type: MediaType
    title: str
    files: tuple[ScannedFile, ...]
    year: int | None = None
    tmdb_id: int | None = None
    release_date: str = ""
    overview: str = ""
    rating: float = 0.0
    poster_path: str | None = None
    genre_ids: tuple[int, ...] = ()
    popularity: float = 0.0

    @property
    def added_at(self) -> str:
        return max((file.modified_at for file in self.files), default="")

    @property
    def season_count(self) -> int:
        return len({file.season for file in self.files if file.season is not None})

    @property
    def episode_count(self) -> int:
        return len({(file.season, file.episode) for file in self.files if file.episode is not None})


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LibraryRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._migrate_single_folder_libraries()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS libraries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_scanned_at TEXT,
                    last_error TEXT
                );
                CREATE TABLE IF NOT EXISTS library_folders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
                    path TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv'))
                );
                CREATE INDEX IF NOT EXISTS library_folders_library
                    ON library_folders (library_id);
                CREATE TABLE IF NOT EXISTS library_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
                    media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
                    group_key TEXT NOT NULL,
                    title TEXT NOT NULL,
                    year INTEGER,
                    tmdb_id INTEGER,
                    release_date TEXT NOT NULL DEFAULT '',
                    overview TEXT NOT NULL DEFAULT '',
                    rating REAL NOT NULL DEFAULT 0,
                    poster_path TEXT,
                    genre_ids TEXT NOT NULL DEFAULT ',',
                    popularity REAL NOT NULL DEFAULT 0,
                    added_at TEXT NOT NULL DEFAULT '',
                    file_count INTEGER NOT NULL DEFAULT 0,
                    season_count INTEGER NOT NULL DEFAULT 0,
                    episode_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (library_id, group_key)
                );
                CREATE INDEX IF NOT EXISTS library_items_tmdb
                    ON library_items (media_type, tmdb_id);
                CREATE TABLE IF NOT EXISTS library_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES library_items(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    modified_at TEXT NOT NULL DEFAULT '',
                    season INTEGER,
                    episode INTEGER
                );
                CREATE INDEX IF NOT EXISTS library_files_item ON library_files (item_id);
                """
            )

    def _migrate_single_folder_libraries(self) -> None:
        """Move the folder of libraries created before multi-folder support into its own table.

        Early databases stored one ``path`` and ``media_type`` on each ``libraries`` row. The
        table is rebuilt without them (SQLite cannot drop a UNIQUE column) while foreign keys are
        off, so existing items, files, and TMDB matches are kept.
        """

        connection = sqlite3.connect(self.database_path, timeout=5, isolation_level=None)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(libraries)")}
            if "path" not in columns:
                return
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SINGLE_FOLDER_MIGRATION:
                    connection.execute(statement)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.IntegrityError("library migration broke a foreign key")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def list_libraries(self) -> tuple[Library, ...]:
        with self._connect() as connection:
            rows = connection.execute(f"{_LIBRARY_SELECT} ORDER BY l.created_at, l.id").fetchall()
            folders = self._folders(connection)
        return tuple(_library_from_row(row, folders.get(int(row[0]), ())) for row in rows)

    def get_library(self, library_id: int) -> Library | None:
        with self._connect() as connection:
            row = connection.execute(f"{_LIBRARY_SELECT} WHERE l.id = ?", (library_id,)).fetchone()
            folders = self._folders(connection, library_id)
        return _library_from_row(row, folders.get(library_id, ())) if row else None

    def add_library(self, name: str, folders: Sequence[tuple[str, str]]) -> Library:
        """Create a library from ``(path, media_type)`` pairs."""

        cleaned_name = _clean_name(name)
        _validate_folders(folders)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO libraries (name, created_at) VALUES (?, ?)",
                (cleaned_name, utc_now()),
            )
            library_id = int(cursor.lastrowid or 0)
            _insert_folders(connection, library_id, folders)
        library = self.get_library(library_id)
        assert library is not None
        return library

    def add_folders(self, library_id: int, folders: Sequence[tuple[str, str]]) -> Library:
        _validate_folders(folders)
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM libraries WHERE id = ?", (library_id,)).fetchone():
                _insert_folders(connection, library_id, folders)
            else:
                raise ValueError("Unknown library")
        library = self.get_library(library_id)
        assert library is not None
        return library

    def remove_folder(self, library_id: int, folder_id: int) -> bool:
        with self._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM library_folders WHERE library_id = ?", (library_id,)
            ).fetchone()[0]
            exists = connection.execute(
                "SELECT 1 FROM library_folders WHERE id = ? AND library_id = ?",
                (folder_id, library_id),
            ).fetchone()
            if exists is None:
                return False
            if count <= 1:
                raise ValueError("A library needs at least one folder; remove the library instead")
            connection.execute("DELETE FROM library_folders WHERE id = ?", (folder_id,))
        return True

    def rename_library(self, library_id: int, name: str) -> bool:
        cleaned_name = _clean_name(name)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE libraries SET name = ? WHERE id = ?", (cleaned_name, library_id)
            )
        return cursor.rowcount > 0

    def remove_library(self, library_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
        return cursor.rowcount > 0

    def record_error(self, library_id: int, message: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE libraries SET last_error = ? WHERE id = ?",
                (message, library_id),
            )

    def items(self, library_id: int) -> tuple[LibraryItem, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"{_ITEM_SELECT} WHERE i.library_id = ? ORDER BY i.title COLLATE NOCASE",
                (library_id,),
            ).fetchall()
        return tuple(_item_from_row(row) for row in rows)

    def replace_items(
        self,
        library_id: int,
        drafts: Sequence[ItemDraft],
        *,
        scanned_at: str | None = None,
    ) -> None:
        library = self.get_library(library_id)
        if library is None:
            raise ValueError("Unknown library")
        with self._connect() as connection:
            keys = tuple(draft.group_key for draft in drafts)
            connection.execute("DELETE FROM library_items WHERE library_id = ?", (library_id,))
            for draft in drafts:
                cursor = connection.execute(
                    """
                    INSERT INTO library_items (
                        library_id, media_type, group_key, title, year, tmdb_id,
                        release_date, overview, rating, poster_path, genre_ids, popularity,
                        added_at, file_count, season_count, episode_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        library_id,
                        draft.media_type,
                        draft.group_key,
                        draft.title,
                        draft.year,
                        draft.tmdb_id,
                        draft.release_date,
                        draft.overview,
                        draft.rating,
                        draft.poster_path,
                        "," + ",".join(str(value) for value in draft.genre_ids) + ",",
                        draft.popularity,
                        draft.added_at,
                        len(draft.files),
                        draft.season_count,
                        draft.episode_count,
                    ),
                )
                item_id = cursor.lastrowid
                connection.executemany(
                    """
                    INSERT INTO library_files (item_id, path, size, modified_at, season, episode)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (item_id, file.path, file.size, file.modified_at, file.season, file.episode)
                        for file in draft.files
                    ),
                )
            del keys
            connection.execute(
                "UPDATE libraries SET last_scanned_at = ?, last_error = NULL WHERE id = ?",
                (scanned_at or utc_now(), library_id),
            )

    def matched_keys(self) -> frozenset[tuple[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT media_type, tmdb_id FROM library_items WHERE tmdb_id IS NOT NULL"
            ).fetchall()
        return frozenset((str(row[0]), int(row[1])) for row in rows)

    def episode_keys(self, tmdb_id: int) -> frozenset[tuple[int, int]]:
        """Return the ``(season, episode)`` pairs on disk for a matched series."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT library_files.season, library_files.episode
                FROM library_files
                JOIN library_items ON library_items.id = library_files.item_id
                WHERE library_items.media_type = 'tv'
                    AND library_items.tmdb_id = ?
                    AND library_files.season IS NOT NULL
                    AND library_files.episode IS NOT NULL
                """,
                (tmdb_id,),
            ).fetchall()
        return frozenset((int(row[0]), int(row[1])) for row in rows)

    def genre_ids(self, media_type: MediaType) -> dict[int, int]:
        """Return genre ids present for a media type with the number of matching items."""

        counts: dict[int, int] = {}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT genre_ids FROM library_items WHERE media_type = ?", (media_type,)
            ).fetchall()
        for row in rows:
            for token in str(row[0]).split(","):
                if token:
                    counts[int(token)] = counts.get(int(token), 0) + 1
        return counts

    def browse(
        self,
        query: BrowseQuery,
        category: BrowseCategory,
        *,
        page_size: int = 24,
    ) -> CatalogPage:
        clauses: list[str] = []
        params: list[object] = []
        if query.media_type != "all":
            clauses.append("i.media_type = ?")
            params.append(query.media_type)
        if query.library_ids:
            placeholders = ",".join("?" for _ in query.library_ids)
            clauses.append(f"i.library_id IN ({placeholders})")
            params.extend(query.library_ids)
        if query.search:
            clauses.append("i.title LIKE ? ESCAPE '\\'")
            escaped = query.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if query.year_from is not None:
            clauses.append("i.year >= ?")
            params.append(query.year_from)
        if query.year_to is not None:
            clauses.append("i.year <= ?")
            params.append(query.year_to)
        if query.minimum_rating is not None:
            if query.include_unrated:
                clauses.append("(i.rating >= ? OR i.rating = 0)")
            else:
                clauses.append("i.rating >= ?")
            params.append(query.minimum_rating)
        genre_clause = _genre_clause(category, query.media_type)
        if genre_clause is not None:
            clauses.append(genre_clause[0])
            params.extend(genre_clause[1])

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = _ORDERINGS.get(category.slug, _ORDERINGS["popular"])
        page = max(query.page, 1)
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM library_items i {where}", params
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"{_ITEM_SELECT} {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        total_pages = max((total + page_size - 1) // page_size, 1)
        return CatalogPage(
            items=tuple(_item_from_row(row).to_catalog_item() for row in rows),
            page=page,
            total_pages=total_pages,
            total_results=total,
        )

    def _folders(
        self, connection: sqlite3.Connection, library_id: int | None = None
    ) -> dict[int, tuple[LibraryFolder, ...]]:
        sql = "SELECT id, library_id, path, media_type FROM library_folders"
        params: tuple[object, ...] = ()
        if library_id is not None:
            sql += " WHERE library_id = ?"
            params = (library_id,)
        grouped: dict[int, list[LibraryFolder]] = {}
        for row in connection.execute(f"{sql} ORDER BY id", params):
            grouped.setdefault(int(row[1]), []).append(
                LibraryFolder(id=int(row[0]), path=str(row[2]), media_type=str(row[3]))  # type: ignore[arg-type]
            )
        return {key: tuple(value) for key, value in grouped.items()}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


# Statements run one by one: ``executescript`` would commit the surrounding transaction.
_SINGLE_FOLDER_MIGRATION = (
    """
    CREATE TABLE libraries_migrated (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_scanned_at TEXT,
        last_error TEXT
    )
    """,
    """
    INSERT INTO libraries_migrated (id, name, created_at, last_scanned_at, last_error)
        SELECT id, name, created_at, last_scanned_at, last_error FROM libraries
    """,
    """
    CREATE TABLE IF NOT EXISTS library_folders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
        path TEXT NOT NULL UNIQUE,
        media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv'))
    )
    """,
    """
    INSERT INTO library_folders (library_id, path, media_type)
        SELECT id, path, media_type FROM libraries ORDER BY id
    """,
    "DROP TABLE libraries",
    "ALTER TABLE libraries_migrated RENAME TO libraries",
)

_LIBRARY_SELECT = """
    SELECT l.id, l.name, l.created_at, l.last_scanned_at, l.last_error,
           (SELECT COUNT(*) FROM library_items i WHERE i.library_id = l.id),
           (SELECT COALESCE(SUM(i.file_count), 0) FROM library_items i WHERE i.library_id = l.id),
           (SELECT COUNT(*) FROM library_items i
             WHERE i.library_id = l.id AND i.tmdb_id IS NULL),
           (SELECT COUNT(*) FROM library_items i
             WHERE i.library_id = l.id AND i.media_type = 'movie'),
           (SELECT COUNT(*) FROM library_items i
             WHERE i.library_id = l.id AND i.media_type = 'tv'),
           (SELECT COALESCE(SUM(i.file_count), 0) FROM library_items i
             WHERE i.library_id = l.id AND i.media_type = 'tv')
    FROM libraries l
"""

_ITEM_SELECT = """
    SELECT i.id, i.library_id, i.media_type, i.group_key, i.title, i.year, i.tmdb_id,
           i.release_date, i.overview, i.rating, i.poster_path, i.genre_ids, i.popularity,
           i.added_at, i.file_count, i.season_count, i.episode_count
    FROM library_items i
"""

_ORDERINGS = {
    "recent": "i.added_at DESC, i.title COLLATE NOCASE",
    "latest": "CASE WHEN i.release_date = '' THEN 1 ELSE 0 END, i.release_date DESC, "
    "i.popularity DESC",
    "popular": "i.popularity DESC, i.rating DESC, i.title COLLATE NOCASE",
    "alphabetical": "i.title COLLATE NOCASE, i.year",
}


def _genre_clause(
    category: BrowseCategory, media_type: BrowseMediaType
) -> tuple[str, list[object]] | None:
    if category.slug in _ORDERINGS:
        return None
    options: list[str] = []
    params: list[object] = []
    for candidate, genre_id in (("movie", category.movie_genre_id), ("tv", category.tv_genre_id)):
        if genre_id is None or (media_type != "all" and media_type != candidate):
            continue
        options.append("(i.media_type = ? AND i.genre_ids LIKE ?)")
        params.extend((candidate, f"%,{genre_id},%"))
    if not options:
        return "0", []
    return f"({' OR '.join(options)})", params


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())[:80]
    if not cleaned:
        raise ValueError("Give the library a name")
    return cleaned


def _validate_folders(folders: Sequence[tuple[str, str]]) -> None:
    if not folders:
        raise ValueError("Choose a folder")
    seen: set[str] = set()
    for path, media_type in folders:
        if not path:
            raise ValueError("Choose a folder")
        if media_type not in _MEDIA_TYPES:
            raise ValueError("Choose whether each folder holds movies or TV shows")
        if path in seen:
            raise ValueError(f"{path} was added twice")
        seen.add(path)


def _insert_folders(
    connection: sqlite3.Connection, library_id: int, folders: Sequence[tuple[str, str]]
) -> None:
    for path, media_type in folders:
        try:
            connection.execute(
                "INSERT INTO library_folders (library_id, path, media_type) VALUES (?, ?, ?)",
                (library_id, path, media_type),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"{path} is already in a library") from exc


def _library_from_row(
    row: sqlite3.Row | tuple[object, ...], folders: tuple[LibraryFolder, ...]
) -> Library:
    return Library(
        id=int(row[0]),
        name=str(row[1]),
        created_at=str(row[2]),
        folders=folders,
        last_scanned_at=str(row[3]) if row[3] else None,
        last_error=str(row[4]) if row[4] else None,
        item_count=int(row[5]),
        file_count=int(row[6]),
        unmatched_count=int(row[7]),
        movie_count=int(row[8]),
        show_count=int(row[9]),
        episode_count=int(row[10]),
    )


def _item_from_row(row: sqlite3.Row | tuple[object, ...]) -> LibraryItem:
    genre_ids = tuple(int(token) for token in str(row[11]).split(",") if token)
    return LibraryItem(
        id=int(row[0]),
        library_id=int(row[1]),
        media_type=str(row[2]),  # type: ignore[arg-type]
        group_key=str(row[3]),
        title=str(row[4]),
        year=int(row[5]) if row[5] is not None else None,
        tmdb_id=int(row[6]) if row[6] is not None else None,
        release_date=str(row[7]),
        overview=str(row[8]),
        rating=float(row[9]),
        poster_path=str(row[10]) if row[10] else None,
        genre_ids=genre_ids,
        popularity=float(row[12]),
        added_at=str(row[13]),
        file_count=int(row[14]),
        season_count=int(row[15]),
        episode_count=int(row[16]),
    )

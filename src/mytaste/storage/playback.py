"""SQLite persistence for probed media information and watch progress."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mytaste.playback.models import MediaInfo


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class PlayState:
    """Watch progress for a movie, an episode, or an unmatched file (see ``state_key``)."""

    key: str
    file_id: int | None = None
    position: float = 0.0
    duration: float = 0.0
    watched: bool = False
    play_count: int = 0
    updated_at: str = ""

    @property
    def progress(self) -> float:
        if self.duration <= 0:
            return 0.0
        return min(max(self.position / self.duration, 0.0), 1.0)

    @property
    def resumable(self) -> bool:
        return self.position >= 30 and self.progress < 0.95


@dataclass(frozen=True, slots=True)
class FileRow:
    item_id: int
    media_type: str
    tmdb_id: int | None
    file_id: int
    season: int | None
    episode: int | None
    probed_at: str
    data: str | None


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    file_id: int
    size: int
    modified_at: str
    info: MediaInfo | None
    error: str | None


class PlaybackRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS media_info (
                    file_id INTEGER PRIMARY KEY,
                    size INTEGER NOT NULL,
                    modified_at TEXT NOT NULL,
                    probed_at TEXT NOT NULL,
                    data TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS playstate (
                    key TEXT PRIMARY KEY,
                    file_id INTEGER,
                    position REAL NOT NULL DEFAULT 0,
                    duration REAL NOT NULL DEFAULT 0,
                    watched INTEGER NOT NULL DEFAULT 0,
                    play_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS playstate_updated ON playstate (updated_at);
                """
            )

    # Media information ---------------------------------------------------------------

    def probe_record(self, file_id: int) -> ProbeRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT file_id, size, modified_at, data, error FROM media_info WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        if row is None:
            return None
        info = None
        if row[3]:
            try:
                info = MediaInfo.from_dict(json.loads(row[3]))
            except (ValueError, TypeError, KeyError):
                info = None
        return ProbeRecord(int(row[0]), int(row[1]), str(row[2]), info, row[4])

    def save_probe(
        self,
        file_id: int,
        size: int,
        modified_at: str,
        info: MediaInfo | None,
        error: str | None = None,
    ) -> None:
        data = json.dumps(info.to_dict(), separators=(",", ":")) if info is not None else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_info (file_id, size, modified_at, probed_at, data, error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (file_id) DO UPDATE SET
                    size = excluded.size, modified_at = excluded.modified_at,
                    probed_at = excluded.probed_at, data = excluded.data, error = excluded.error
                """,
                (file_id, size, modified_at, utc_now(), data, error),
            )

    def files_needing_probe(self, limit: int = 50) -> tuple[int, ...]:
        """Indexed files that were never probed or changed since (newest first)."""

        with self._connect() as connection:
            if not _has_table(connection, "library_files"):
                return ()
            rows = connection.execute(
                """
                SELECT f.id FROM library_files f
                LEFT JOIN media_info m ON m.file_id = f.id
                WHERE m.file_id IS NULL OR m.size != f.size OR m.modified_at != f.modified_at
                ORDER BY f.modified_at DESC, f.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(int(row[0]) for row in rows)

    def forget_missing_files(self) -> None:
        with self._connect() as connection:
            if _has_table(connection, "library_files"):
                connection.execute(
                    "DELETE FROM media_info WHERE file_id NOT IN (SELECT id FROM library_files)"
                )

    def probe_summary(self) -> dict[str, int]:
        with self._connect() as connection:
            if not _has_table(connection, "library_files"):
                return {"files": 0, "probed": 0}
            files, probed = connection.execute(
                """
                SELECT COUNT(*), COUNT(m.file_id) FROM library_files f
                LEFT JOIN media_info m ON m.file_id = f.id
                    AND m.size = f.size AND m.modified_at = f.modified_at
                """
            ).fetchone()
        return {"files": int(files), "probed": int(probed)}

    def library_files(self) -> tuple[FileRow, ...]:
        """Every indexed file with its title and probed information, for filtering.

        ``data`` is the stored probe JSON, ``None`` until the file is probed.
        """

        with self._connect() as connection:
            if not _has_table(connection, "library_files"):
                return ()
            rows = connection.execute(
                """
                SELECT i.id, i.media_type, i.tmdb_id, f.id, f.season, f.episode, m.probed_at,
                       m.data
                FROM library_items i
                JOIN library_files f ON f.item_id = i.id
                LEFT JOIN media_info m ON m.file_id = f.id
                """
            ).fetchall()
        return tuple(
            FileRow(
                item_id=int(row[0]),
                media_type=str(row[1]),
                tmdb_id=int(row[2]) if row[2] is not None else None,
                file_id=int(row[3]),
                season=int(row[4]) if row[4] is not None else None,
                episode=int(row[5]) if row[5] is not None else None,
                probed_at=str(row[6] or ""),
                data=str(row[7]) if row[7] else None,
            )
            for row in rows
        )

    def all_states(self) -> tuple[PlayState, ...]:
        with self._connect() as connection:
            rows = connection.execute(_STATE_SELECT).fetchall()
        return tuple(_state_from_row(row) for row in rows)

    # Watch state ---------------------------------------------------------------------

    def state(self, key: str) -> PlayState | None:
        with self._connect() as connection:
            row = connection.execute(f"{_STATE_SELECT} WHERE key = ?", (key,)).fetchone()
        return _state_from_row(row) if row else None

    def states(self, keys: Iterable[str]) -> dict[str, PlayState]:
        wanted = tuple(dict.fromkeys(keys))
        found: dict[str, PlayState] = {}
        with self._connect() as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                for row in connection.execute(
                    f"{_STATE_SELECT} WHERE key IN ({placeholders})", chunk
                ):
                    state = _state_from_row(row)
                    found[state.key] = state
        return found

    def states_with_prefix(self, prefix: str) -> tuple[PlayState, ...]:
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._connect() as connection:
            rows = connection.execute(
                f"{_STATE_SELECT} WHERE key LIKE ? ESCAPE '\\'", (f"{escaped}%",)
            ).fetchall()
        return tuple(_state_from_row(row) for row in rows)

    def recent_states(self, limit: int = 50) -> tuple[PlayState, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"{_STATE_SELECT} WHERE position > 0 OR watched = 1 "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(_state_from_row(row) for row in rows)

    def save_state(self, state: PlayState) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO playstate
                    (key, file_id, position, duration, watched, play_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (key) DO UPDATE SET
                    file_id = excluded.file_id, position = excluded.position,
                    duration = excluded.duration, watched = excluded.watched,
                    play_count = excluded.play_count, updated_at = excluded.updated_at
                """,
                (
                    state.key,
                    state.file_id,
                    state.position,
                    state.duration,
                    int(state.watched),
                    state.play_count,
                    state.updated_at or utc_now(),
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=5)


_STATE_SELECT = (
    "SELECT key, file_id, position, duration, watched, play_count, updated_at FROM playstate"
)


def _state_from_row(row: sqlite3.Row | tuple[object, ...]) -> PlayState:
    return PlayState(
        key=str(row[0]),
        file_id=int(row[1]) if row[1] is not None else None,
        position=float(row[2]),
        duration=float(row[3]),
        watched=bool(row[4]),
        play_count=int(row[5]),
        updated_at=str(row[6]),
    )


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )

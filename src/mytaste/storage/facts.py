"""SQLite persistence for the per-title facts filters check (see ``catalog.filters``)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from mytaste.catalog.filters import TitleFacts

TitleKey = tuple[str, int]


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class FactsRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS title_facts (
                    media_type TEXT NOT NULL,
                    tmdb_id INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (media_type, tmdb_id)
                )
                """
            )

    def get(self, keys: Iterable[TitleKey]) -> dict[TitleKey, tuple[TitleFacts, str]]:
        """Return the stored facts of each title that has them, with when they were read."""

        wanted = tuple(dict.fromkeys(keys))
        found: dict[TitleKey, tuple[TitleFacts, str]] = {}
        with self._connect() as connection:
            for start in range(0, len(wanted), 400):
                chunk = wanted[start : start + 400]
                condition = " OR ".join("(media_type = ? AND tmdb_id = ?)" for _ in chunk)
                rows = connection.execute(
                    f"SELECT media_type, tmdb_id, data, fetched_at FROM title_facts "
                    f"WHERE {condition}",
                    [value for key in chunk for value in key],
                ).fetchall()
                for media_type, tmdb_id, data, fetched_at in rows:
                    try:
                        facts = TitleFacts.from_dict(json.loads(data))
                    except (TypeError, ValueError):
                        continue
                    found[(str(media_type), int(tmdb_id))] = (facts, str(fetched_at))
        return found

    def save(self, entries: Sequence[tuple[TitleKey, TitleFacts]]) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO title_facts (media_type, tmdb_id, data, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (media_type, tmdb_id) DO UPDATE SET
                    data = excluded.data, fetched_at = excluded.fetched_at
                """,
                (
                    (
                        media_type,
                        tmdb_id,
                        json.dumps(facts.to_dict(), separators=(",", ":")),
                        now,
                    )
                    for (media_type, tmdb_id), facts in entries
                ),
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=5)

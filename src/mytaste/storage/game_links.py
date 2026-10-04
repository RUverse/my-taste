"""Which Steam app each Xbox product is, so a game sold in both stores is listed once.

Links found automatically are rechecked after a while; a user's correction is kept for good.
``steam_appid`` 0 records that a product has no Steam counterpart.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GameLink:
    xbox_id: str
    steam_appid: int
    source: str
    checked_at: float


class GameLinkRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS game_links (
                    xbox_id TEXT PRIMARY KEY,
                    steam_appid INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL,
                    checked_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS game_links_steam ON game_links (steam_appid)"
            )

    def get(self, xbox_ids: Iterable[str]) -> dict[str, GameLink]:
        wanted = list(dict.fromkeys(xbox_ids))
        found: dict[str, GameLink] = {}
        with sqlite3.connect(self.database_path) as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                rows = connection.execute(
                    "SELECT xbox_id, steam_appid, source, checked_at FROM game_links "
                    f"WHERE xbox_id IN ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
                found.update({row[0]: GameLink(*row) for row in rows})
        return found

    def for_steam(self, appid: int) -> list[GameLink]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT xbox_id, steam_appid, source, checked_at FROM game_links "
                "WHERE steam_appid = ? ORDER BY xbox_id",
                (appid,),
            ).fetchall()
        return [GameLink(*row) for row in rows]

    def save(self, links: Iterable[GameLink]) -> None:
        """Store automatic findings without overwriting a user's correction."""

        with sqlite3.connect(self.database_path) as connection:
            connection.executemany(
                "INSERT INTO game_links (xbox_id, steam_appid, source, checked_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(xbox_id) DO UPDATE SET "
                "steam_appid = excluded.steam_appid, source = excluded.source, "
                "checked_at = excluded.checked_at WHERE game_links.source != 'user' "
                "OR excluded.source = 'user'",
                [(link.xbox_id, link.steam_appid, link.source, link.checked_at) for link in links],
            )

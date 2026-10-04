"""The connected Steam account, kept in the application database.

Owned games are a copy of what Steam last said, refreshed by ``GamesService``; the Web API key
is configuration and is never stored here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from mytaste.games.models import OwnedGame, SteamAccount


class SteamAccountRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS steam_account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    steam_id TEXT NOT NULL,
                    persona TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT '',
                    profile_url TEXT NOT NULL DEFAULT '',
                    owned TEXT NOT NULL DEFAULT '[]',
                    owned_checked_at REAL,
                    owned_status TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def get(self) -> SteamAccount | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT steam_id, persona, avatar_url, profile_url, owned, owned_checked_at, "
                "owned_status FROM steam_account WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        try:
            owned = tuple(OwnedGame(*entry) for entry in json.loads(row[4]))
        except (TypeError, ValueError):
            owned = ()
        return SteamAccount(row[0], row[1], row[2], row[3], owned, row[5], row[6])

    def connect(self, account: SteamAccount) -> SteamAccount:
        """Replace the connected account; another account's games are never kept."""

        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM steam_account")
            connection.execute(
                "INSERT INTO steam_account (id, steam_id, persona, avatar_url, profile_url) "
                "VALUES (1, ?, ?, ?, ?)",
                (account.steam_id, account.persona, account.avatar_url, account.profile_url),
            )
        return replace(account, owned=(), owned_checked_at=None, owned_status="")

    def save_owned(
        self,
        steam_id: str,
        owned: tuple[OwnedGame, ...],
        checked_at: float,
        status: str,
        profile: dict[str, str] | None = None,
    ) -> None:
        """Store a refresh for ``steam_id``; a refresh for a since-replaced account is dropped."""

        payload = json.dumps(
            [[game.appid, game.name, game.playtime, game.last_played] for game in owned]
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE steam_account SET owned = ?, owned_checked_at = ?, owned_status = ? "
                "WHERE id = 1 AND steam_id = ?",
                (payload, checked_at, status, steam_id),
            )
            if profile:
                connection.execute(
                    "UPDATE steam_account SET persona = ?, avatar_url = ?, profile_url = ? "
                    "WHERE id = 1 AND steam_id = ?",
                    (profile["persona"], profile["avatar_url"], profile["profile_url"], steam_id),
                )

    def disconnect(self) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            return connection.execute("DELETE FROM steam_account").rowcount > 0

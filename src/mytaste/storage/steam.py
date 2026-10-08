"""The connected Steam account, kept in the application database.

Owned games are a copy of what Steam last said, refreshed by ``GamesService``; the Web API key
is configuration and is never stored here.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from mytaste.accounts.context import current_user_id
from mytaste.games.models import OwnedGame, SteamAccount
from mytaste.storage.migrations import give_to_first_user

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS steam_account (
        user_id INTEGER PRIMARY KEY,
        steam_id TEXT NOT NULL,
        persona TEXT NOT NULL DEFAULT '',
        avatar_url TEXT NOT NULL DEFAULT '',
        profile_url TEXT NOT NULL DEFAULT '',
        owned TEXT NOT NULL DEFAULT '[]',
        owned_checked_at REAL,
        owned_status TEXT NOT NULL DEFAULT ''
    )
"""
_COLUMNS = (
    "steam_id",
    "persona",
    "avatar_url",
    "profile_url",
    "owned",
    "owned_checked_at",
    "owned_status",
)


class SteamAccountRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        # A Steam account connected before accounts existed becomes the first user's.
        give_to_first_user(self.database_path, "steam_account", _SCHEMA, _COLUMNS)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(_SCHEMA)

    def get(self) -> SteamAccount | None:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT steam_id, persona, avatar_url, profile_url, owned, owned_checked_at, "
                "owned_status FROM steam_account WHERE user_id = ?",
                (current_user_id(),),
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

        user_id = current_user_id()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("DELETE FROM steam_account WHERE user_id = ?", (user_id,))
            connection.execute(
                "INSERT INTO steam_account (user_id, steam_id, persona, avatar_url, profile_url) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    account.steam_id,
                    account.persona,
                    account.avatar_url,
                    account.profile_url,
                ),
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
        user_id = current_user_id()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE steam_account SET owned = ?, owned_checked_at = ?, owned_status = ? "
                "WHERE user_id = ? AND steam_id = ?",
                (payload, checked_at, status, user_id, steam_id),
            )
            if profile:
                connection.execute(
                    "UPDATE steam_account SET persona = ?, avatar_url = ?, profile_url = ? "
                    "WHERE user_id = ? AND steam_id = ?",
                    (
                        profile["persona"],
                        profile["avatar_url"],
                        profile["profile_url"],
                        user_id,
                        steam_id,
                    ),
                )

    def disconnect(self) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            return (
                connection.execute(
                    "DELETE FROM steam_account WHERE user_id = ?", (current_user_id(),)
                ).rowcount
                > 0
            )

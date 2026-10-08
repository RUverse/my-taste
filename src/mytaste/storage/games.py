from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mytaste.accounts.context import current_user_id
from mytaste.games.models import GameQuery
from mytaste.storage.migrations import give_to_first_user

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS game_preferences "
    "(user_id INTEGER PRIMARY KEY, plan TEXT NOT NULL, platform TEXT NOT NULL)"
)


@dataclass(frozen=True, slots=True)
class GamePreferences:
    plan: str = "ultimate"
    platform: str = "pc"


class GamePreferenceRepository:
    """Each user's Game Pass plan and platform; unrelated to the catalog cache."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        # The Game Pass plan chosen before accounts becomes the first user's.
        give_to_first_user(self.database_path, "game_preferences", _SCHEMA, ("plan", "platform"))
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(_SCHEMA)

    def get(self) -> GamePreferences:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT plan, platform FROM game_preferences WHERE user_id = ?",
                (current_user_id(),),
            ).fetchone()
        return GamePreferences(*row) if row else GamePreferences()

    def configured(self) -> bool:
        """Whether the user has chosen a Game Pass plan, rather than relying on the defaults."""

        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT 1 FROM game_preferences WHERE user_id = ?", (current_user_id(),)
            ).fetchone()
        return row is not None

    def save(self, plan: str, platform: str) -> GamePreferences:
        GameQuery(plan=plan, platform=platform).validate()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO game_preferences (user_id, plan, platform) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET plan=excluded.plan, platform=excluded.platform",
                (current_user_id(), plan, platform),
            )
        return GamePreferences(plan, platform)

    def clear(self) -> None:
        """Forget the chosen plan; Game Pass stops counting as one of the user's services."""

        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "DELETE FROM game_preferences WHERE user_id = ?", (current_user_id(),)
            )

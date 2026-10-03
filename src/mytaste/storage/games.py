from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mytaste.games.models import GameQuery


@dataclass(frozen=True, slots=True)
class GamePreferences:
    plan: str = "ultimate"
    platform: str = "pc"


class GamePreferenceRepository:
    """Game defaults are durable user preferences, unrelated to the catalog cache."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS game_preferences "
                "(id INTEGER PRIMARY KEY CHECK (id=1), plan TEXT NOT NULL, platform TEXT NOT NULL)"
            )

    def get(self) -> GamePreferences:
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT plan, platform FROM game_preferences WHERE id=1"
            ).fetchone()
        return GamePreferences(*row) if row else GamePreferences()

    def save(self, plan: str, platform: str) -> GamePreferences:
        GameQuery(plan=plan, platform=platform).validate()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO game_preferences VALUES (1, ?, ?) ON CONFLICT(id) "
                "DO UPDATE SET plan=excluded.plan, platform=excluded.platform",
                (plan, platform),
            )
        return GamePreferences(plan, platform)

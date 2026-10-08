"""Settings of the whole instance, as opposed to one person's preferences."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS instance_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


class InstanceSettings:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path, timeout=5) as connection:
            connection.execute(SCHEMA)

    def get(self, key: str, default: str = "") -> str:
        with sqlite3.connect(self.database_path, timeout=5) as connection:
            row = connection.execute(
                "SELECT value FROM instance_settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else default

    def save(self, key: str, value: str) -> None:
        with sqlite3.connect(self.database_path, timeout=5) as connection:
            connection.execute(
                "INSERT INTO instance_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def flag(self, key: str, default: bool) -> bool:
        return self.get(key, "1" if default else "0") == "1"

    def save_flag(self, key: str, value: bool) -> None:
        self.save(key, "1" if value else "0")

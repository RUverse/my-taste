from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_REGION_PATTERN = re.compile(r"^[A-Z]{2}$")


@dataclass(frozen=True, slots=True)
class Preferences:
    region: str = ""
    provider_ids: tuple[int, ...] = ()

    @property
    def configured(self) -> bool:
        return bool(self.region and self.provider_ids)


@dataclass(frozen=True, slots=True)
class DisplayPreferences:
    show_year: bool = True
    show_rating: bool = True
    show_media_type: bool = True
    show_genres: bool = False
    show_people: bool = False
    show_providers: bool = False
    card_size: str = "comfortable"
    autoplay_trailer: bool = True
    sidebar_open: bool = True


class PreferenceRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    region TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    provider_id INTEGER PRIMARY KEY CHECK (provider_id > 0)
                );
                CREATE TABLE IF NOT EXISTS display_preferences (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    show_year INTEGER NOT NULL DEFAULT 1,
                    show_rating INTEGER NOT NULL DEFAULT 1,
                    show_media_type INTEGER NOT NULL DEFAULT 1,
                    show_genres INTEGER NOT NULL DEFAULT 0,
                    show_people INTEGER NOT NULL DEFAULT 0,
                    card_size TEXT NOT NULL DEFAULT 'comfortable'
                        CHECK (card_size IN ('compact', 'comfortable')),
                    autoplay_trailer INTEGER NOT NULL DEFAULT 1,
                    sidebar_open INTEGER NOT NULL DEFAULT 1,
                    show_providers INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            display_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(display_preferences)")
            }
            if "autoplay_trailer" not in display_columns:
                connection.execute(
                    """
                    ALTER TABLE display_preferences
                    ADD COLUMN autoplay_trailer INTEGER NOT NULL DEFAULT 1
                    """
                )
            if "show_providers" not in display_columns:
                connection.execute(
                    """
                    ALTER TABLE display_preferences
                    ADD COLUMN show_providers INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "sidebar_open" not in display_columns:
                connection.execute(
                    """
                    ALTER TABLE display_preferences
                    ADD COLUMN sidebar_open INTEGER NOT NULL DEFAULT 1
                    """
                )

    def get(self) -> Preferences:
        with self._connect() as connection:
            preference = connection.execute(
                "SELECT region FROM preferences WHERE id = 1"
            ).fetchone()
            providers = connection.execute(
                "SELECT provider_id FROM subscriptions ORDER BY provider_id"
            ).fetchall()
        return Preferences(
            region=str(preference[0]) if preference else "",
            provider_ids=tuple(int(row[0]) for row in providers),
        )

    def save(self, region: str, provider_ids: tuple[int, ...]) -> Preferences:
        normalized_region = region.strip().upper()
        normalized_ids = tuple(sorted(set(provider_ids)))
        if not _REGION_PATTERN.fullmatch(normalized_region):
            raise ValueError("Region must be a two-letter country code")
        if any(provider_id <= 0 for provider_id in normalized_ids):
            raise ValueError("Streaming service ids must be positive")

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO preferences (id, region) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET region = excluded.region
                """,
                (normalized_region,),
            )
            connection.execute("DELETE FROM subscriptions")
            connection.executemany(
                "INSERT INTO subscriptions (provider_id) VALUES (?)",
                ((provider_id,) for provider_id in normalized_ids),
            )
        return Preferences(region=normalized_region, provider_ids=normalized_ids)

    def get_display(self) -> DisplayPreferences:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT show_year, show_rating, show_media_type, show_genres,
                       show_people, card_size, autoplay_trailer, sidebar_open,
                       show_providers
                FROM display_preferences WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return DisplayPreferences()
        return DisplayPreferences(
            show_year=bool(row[0]),
            show_rating=bool(row[1]),
            show_media_type=bool(row[2]),
            show_genres=bool(row[3]),
            show_people=bool(row[4]),
            card_size=str(row[5]),
            autoplay_trailer=bool(row[6]),
            sidebar_open=bool(row[7]),
            show_providers=bool(row[8]),
        )

    def save_display(self, preferences: DisplayPreferences) -> DisplayPreferences:
        if preferences.card_size not in {"compact", "comfortable"}:
            raise ValueError("Card size must be compact or comfortable")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO display_preferences (
                    id, show_year, show_rating, show_media_type,
                    show_genres, show_people, card_size, autoplay_trailer, sidebar_open,
                    show_providers
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    show_year = excluded.show_year,
                    show_rating = excluded.show_rating,
                    show_media_type = excluded.show_media_type,
                    show_genres = excluded.show_genres,
                    show_people = excluded.show_people,
                    card_size = excluded.card_size,
                    autoplay_trailer = excluded.autoplay_trailer,
                    sidebar_open = excluded.sidebar_open,
                    show_providers = excluded.show_providers
                """,
                (
                    preferences.show_year,
                    preferences.show_rating,
                    preferences.show_media_type,
                    preferences.show_genres,
                    preferences.show_people,
                    preferences.card_size,
                    preferences.autoplay_trailer,
                    preferences.sidebar_open,
                    preferences.show_providers,
                ),
            )
        return preferences

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

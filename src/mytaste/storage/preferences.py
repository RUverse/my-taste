from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

from mytaste.accounts.context import current_user_id
from mytaste.storage.migrations import give_to_first_user, table_columns
from mytaste.storage.settings import SCHEMA as SETTINGS_SCHEMA

_REGION_PATTERN = re.compile(r"^[A-Z]{2}$")
DEFAULT_SITE_TITLE = "MyTaste"
_MAX_SITE_TITLE_LENGTH = 40


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
    site_title: str = DEFAULT_SITE_TITLE


_PREFERENCES = """
    CREATE TABLE IF NOT EXISTS preferences (
        user_id INTEGER PRIMARY KEY,
        region TEXT NOT NULL
    )
"""
_SUBSCRIPTIONS = """
    CREATE TABLE IF NOT EXISTS subscriptions (
        user_id INTEGER NOT NULL,
        provider_id INTEGER NOT NULL CHECK (provider_id > 0),
        PRIMARY KEY (user_id, provider_id)
    )
"""
_DISPLAY = """
    CREATE TABLE IF NOT EXISTS display_preferences (
        user_id INTEGER PRIMARY KEY,
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
    )
"""
_DISPLAY_COLUMNS = (
    "show_year",
    "show_rating",
    "show_media_type",
    "show_genres",
    "show_people",
    "card_size",
    "autoplay_trailer",
    "sidebar_open",
    "show_providers",
)


class PreferenceRepository:
    """Each user's region, streaming services, and display options."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(SETTINGS_SCHEMA)
            self._upgrade_one_profile_display(connection)
        # Before accounts these held one profile; they become the first user's.
        give_to_first_user(self.database_path, "preferences", _PREFERENCES, ("region",))
        give_to_first_user(self.database_path, "subscriptions", _SUBSCRIPTIONS, ("provider_id",))
        give_to_first_user(self.database_path, "display_preferences", _DISPLAY, _DISPLAY_COLUMNS)
        with self._connect() as connection:
            connection.execute(_PREFERENCES)
            connection.execute(_SUBSCRIPTIONS)
            connection.execute(_DISPLAY)

    def _upgrade_one_profile_display(self, connection: sqlite3.Connection) -> None:
        """Bring a display table from before accounts up to date before it is converted.

        Its site name becomes an instance setting, since everyone shares the site.
        """

        columns = table_columns(connection, "display_preferences")
        if not columns or "user_id" in columns:
            return
        for column, definition in (
            ("autoplay_trailer", "INTEGER NOT NULL DEFAULT 1"),
            ("show_providers", "INTEGER NOT NULL DEFAULT 0"),
            ("sidebar_open", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE display_preferences ADD COLUMN {column} {definition}"
                )
        if "site_title" in columns:
            row = connection.execute(
                "SELECT site_title FROM display_preferences WHERE id = 1"
            ).fetchone()
            if row and str(row[0]).strip() and str(row[0]) != DEFAULT_SITE_TITLE:
                connection.execute(
                    "INSERT INTO instance_settings (key, value) VALUES ('site_title', ?) "
                    "ON CONFLICT(key) DO NOTHING",
                    (str(row[0]),),
                )

    def get(self) -> Preferences:
        user_id = current_user_id()
        with self._connect() as connection:
            preference = connection.execute(
                "SELECT region FROM preferences WHERE user_id = ?", (user_id,)
            ).fetchone()
            providers = connection.execute(
                "SELECT provider_id FROM subscriptions WHERE user_id = ? ORDER BY provider_id",
                (user_id,),
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

        user_id = current_user_id()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO preferences (user_id, region) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET region = excluded.region
                """,
                (user_id, normalized_region),
            )
            connection.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
            connection.executemany(
                "INSERT INTO subscriptions (user_id, provider_id) VALUES (?, ?)",
                ((user_id, provider_id) for provider_id in normalized_ids),
            )
        return Preferences(region=normalized_region, provider_ids=normalized_ids)

    def get_display(self) -> DisplayPreferences:
        """The current user's display options, with the instance's site name."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT show_year, show_rating, show_media_type, show_genres,
                       show_people, card_size, autoplay_trailer, sidebar_open,
                       show_providers
                FROM display_preferences WHERE user_id = ?
                """,
                (current_user_id(),),
            ).fetchone()
        site_title = self.site_title()
        if row is None:
            return DisplayPreferences(site_title=site_title)
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
            site_title=site_title,
        )

    def display_for_anyone(self) -> DisplayPreferences:
        """Display options for pages shown before anyone signs in."""

        return DisplayPreferences(site_title=self.site_title())

    def site_title(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM instance_settings WHERE key = 'site_title'"
            ).fetchone()
        return str(row[0]) if row and str(row[0]) else DEFAULT_SITE_TITLE

    def save_site_title(self, title: str) -> str:
        """Rename the site for everyone on this instance."""

        site_title = " ".join(title.split())
        if not site_title:
            raise ValueError("Give the site a name")
        if len(site_title) > _MAX_SITE_TITLE_LENGTH:
            raise ValueError(f"Keep the name under {_MAX_SITE_TITLE_LENGTH} characters")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO instance_settings (key, value) VALUES ('site_title', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (site_title,),
            )
        return site_title

    def save_display(self, preferences: DisplayPreferences) -> DisplayPreferences:
        """Save the current user's display options; the site name is saved separately."""

        if preferences.card_size not in {"compact", "comfortable"}:
            raise ValueError("Card size must be compact or comfortable")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO display_preferences (
                    user_id, show_year, show_rating, show_media_type,
                    show_genres, show_people, card_size, autoplay_trailer, sidebar_open,
                    show_providers
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
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
                    current_user_id(),
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
        return replace(preferences, site_title=self.site_title())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

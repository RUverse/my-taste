import sqlite3
from pathlib import Path

import pytest

from mytaste.storage.preferences import DisplayPreferences, PreferenceRepository


def test_preferences_are_persisted_and_normalized(tmp_path: Path) -> None:
    repository = PreferenceRepository(tmp_path / "data" / "mytaste.db")
    repository.initialize()

    saved = repository.save("de", (337, 8, 8))
    loaded = repository.get()

    assert saved.region == "DE"
    assert loaded.region == "DE"
    assert loaded.provider_ids == (8, 337)
    assert loaded.configured is True


def test_preferences_allow_no_streaming_services(tmp_path: Path) -> None:
    repository = PreferenceRepository(tmp_path / "mytaste.db")
    repository.initialize()
    repository.save("DE", (8,))

    cleared = repository.save("DE", ())

    assert cleared.region == "DE"
    assert repository.get().provider_ids == ()
    assert repository.get().configured is False
    with pytest.raises(ValueError, match="positive"):
        repository.save("DE", (0,))


def test_display_preferences_are_persisted(tmp_path: Path) -> None:
    repository = PreferenceRepository(tmp_path / "mytaste.db")
    repository.initialize()
    display = DisplayPreferences(
        show_year=False,
        show_rating=True,
        show_media_type=False,
        show_genres=True,
        show_people=True,
        card_size="compact",
        autoplay_trailer=False,
        sidebar_open=False,
        show_providers=True,
    )

    repository.save_display(display)

    assert repository.get_display() == display


def test_existing_display_preferences_gain_new_defaults(tmp_path: Path) -> None:
    database_path = tmp_path / "mytaste.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE display_preferences (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                show_year INTEGER NOT NULL DEFAULT 1,
                show_rating INTEGER NOT NULL DEFAULT 1,
                show_media_type INTEGER NOT NULL DEFAULT 1,
                show_genres INTEGER NOT NULL DEFAULT 0,
                show_people INTEGER NOT NULL DEFAULT 0,
                card_size TEXT NOT NULL DEFAULT 'comfortable'
                    CHECK (card_size IN ('compact', 'comfortable'))
            )
            """
        )
        connection.execute(
            """
            INSERT INTO display_preferences (
                id, show_year, show_rating, show_media_type,
                show_genres, show_people, card_size
            ) VALUES (1, 0, 1, 1, 1, 0, 'compact')
            """
        )

    repository = PreferenceRepository(database_path)
    repository.initialize()

    display = repository.get_display()
    assert display.card_size == "compact"
    assert display.show_year is False
    assert display.autoplay_trailer is True
    assert display.sidebar_open is True
    assert display.show_providers is False

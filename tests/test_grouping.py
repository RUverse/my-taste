from __future__ import annotations

import pytest

from mytaste.catalog.grouping import group_titles
from mytaste.catalog.models import CatalogItem


def title(item_id: int, name: str, date: str, media: str = "movie", genres=()) -> CatalogItem:
    return CatalogItem(item_id, media, name, date, "", 7, genres=tuple(genres))  # type: ignore[arg-type]


TITLES = [
    title(1, "Interstellar", "2014-11-05", genres=("Science Fiction", "Drama")),
    title(2, "Fargo", "1996-03-08", genres=("Crime",)),
    title(3, "Inception", "2010-07-15", genres=("Science Fiction",)),
    title(4, "No Country", "2007-11-09", genres=("Crime", "Drama")),
    title(5, "Dark", "2017-12-01", media="tv", genres=("Drama",)),
    title(0, "Unmatched home video", ""),
]


def labels(groups) -> list[tuple[str, list[str]]]:
    return [(group.label, [item.title for item in group.items]) for group in groups]


def test_directors_with_more_titles_come_first_and_shared_titles_repeat() -> None:
    people = {
        ("movie", 1): ("Christopher Nolan",),
        ("movie", 2): ("Joel Coen", "Ethan Coen"),
        ("movie", 3): ("Christopher Nolan",),
        ("movie", 4): ("Joel Coen", "Ethan Coen"),
        ("tv", 5): ("Baran bo Odar",),
    }

    assert labels(group_titles(TITLES, "director", people)) == [
        ("Christopher Nolan", ["Interstellar", "Inception"]),
        ("Joel Coen", ["Fargo", "No Country"]),
        ("Ethan Coen", ["Fargo", "No Country"]),
        ("Baran bo Odar", ["Dark"]),
        ("Unknown director", ["Unmatched home video"]),
    ]


def test_genres_decades_and_types() -> None:
    assert labels(group_titles(TITLES, "genre"))[:3] == [
        ("Drama", ["Interstellar", "No Country", "Dark"]),
        ("Science Fiction", ["Interstellar", "Inception"]),
        ("Crime", ["Fargo", "No Country"]),
    ]
    assert labels(group_titles(TITLES, "decade")) == [
        ("2010s", ["Interstellar", "Inception", "Dark"]),
        ("2000s", ["No Country"]),
        ("1990s", ["Fargo"]),
        ("Unknown year", ["Unmatched home video"]),
    ]
    assert [group.label for group in group_titles(TITLES, "type")] == ["Movies", "Series"]
    with pytest.raises(ValueError):
        group_titles(TITLES, "colour")

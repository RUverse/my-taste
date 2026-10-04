"""Group a collection's titles into labelled rows, such as one row per director."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from mytaste.catalog.models import CatalogItem

GROUPINGS: dict[str, str] = {
    "director": "Director",
    "genre": "Genre",
    "decade": "Decade",
    "type": "Type",
}
GROUP_TITLE_LIMIT = 100
"""Grouping looks at this many titles from the top of the collection, in its sort order."""

_UNKNOWN = {
    "director": "Unknown director",
    "genre": "No genre",
    "decade": "Unknown year",
}


@dataclass(frozen=True, slots=True)
class TitleGroup:
    label: str
    items: tuple[CatalogItem, ...]


def group_titles(
    items: Sequence[CatalogItem],
    by: str,
    people: Mapping[tuple[str, int], Sequence[str]] | None = None,
) -> tuple[TitleGroup, ...]:
    """Split titles into rows that keep the collection's order within each row.

    A title appears in every row it belongs to (a film by two directors, a title with two
    genres). Directors and genres with the most titles come first; decades run newest first;
    movies come before series and games. Titles without a value go to a last row.
    """

    if by not in GROUPINGS:
        raise ValueError(f"Unknown grouping: {by}")
    buckets: dict[str, list[CatalogItem]] = {}
    first_seen: dict[str, int] = {}
    missing: list[CatalogItem] = []
    for position, item in enumerate(items):
        labels = list(dict.fromkeys(_labels(item, by, people or {})))
        if not labels:
            missing.append(item)
        for label in labels:
            buckets.setdefault(label, []).append(item)
            first_seen.setdefault(label, position)

    if by == "decade":
        order = sorted(buckets, key=lambda label: label, reverse=True)
    elif by == "type":
        order = [label for label in ("Movies", "Series", "Games") if label in buckets]
    else:
        order = sorted(buckets, key=lambda label: (-len(buckets[label]), first_seen[label]))
    groups = [TitleGroup(label, tuple(buckets[label])) for label in order]
    if missing:
        groups.append(TitleGroup(_UNKNOWN[by], tuple(missing)))
    return tuple(groups)


def _labels(
    item: CatalogItem, by: str, people: Mapping[tuple[str, int], Sequence[str]]
) -> Iterable[str]:
    if by == "director":
        return people.get((item.media_type, item.id), ()) if item.id > 0 else ()
    if by == "genre":
        return item.genres
    if by == "decade":
        return (f"{int(item.year) // 10 * 10}s",) if item.year.isdigit() else ()
    return ({"movie": "Movies", "tv": "Series"}.get(item.media_type, "Games"),)

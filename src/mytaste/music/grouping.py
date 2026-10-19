"""Shelves: a collection's albums in labelled rows, such as one row per artist."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from mytaste.music.models import GENRES, GROUPINGS, TYPES, Album, genre_matches

GROUP_ALBUM_LIMIT = 200
"""Shelves look at this many albums from the top of the collection, in its sort order."""

_UNKNOWN = {"artist": "Unknown artist", "decade": "Unknown year", "genre": "No genre"}
_EARLIER = "Before 1960"
_OTHER = "Other releases"


@dataclass(frozen=True, slots=True)
class AlbumGroup:
    label: str
    items: tuple[Album, ...]


def group_albums(albums: Sequence[Album], by: str) -> tuple[AlbumGroup, ...]:
    """Rows that keep the collection's order within each row.

    Artists and genres with the most albums come first, decades run newest first, and types
    follow the filter's order. An album appears in every genre row it belongs to; albums
    without a value go to a last row.
    """

    if by not in GROUPINGS:
        raise ValueError(f"Unknown grouping: {by}")
    buckets: dict[str, list[Album]] = {}
    first_seen: dict[str, int] = {}
    missing: list[Album] = []
    for position, album in enumerate(albums):
        labels = list(dict.fromkeys(_labels(album, by)))
        if not labels:
            missing.append(album)
        for label in labels:
            buckets.setdefault(label, []).append(album)
            first_seen.setdefault(label, position)
    if by == "decade":
        order = sorted((label for label in buckets if label != _EARLIER), reverse=True)
        order += [_EARLIER] if _EARLIER in buckets else []
    elif by == "type":
        order = [label for label in (*TYPES.values(), _OTHER) if label in buckets]
    else:
        order = sorted(buckets, key=lambda label: (-len(buckets[label]), first_seen[label]))
    groups = [AlbumGroup(label, tuple(buckets[label])) for label in order]
    if missing and by in _UNKNOWN:
        groups.append(AlbumGroup(_UNKNOWN[by], tuple(missing)))
    return tuple(groups)


def _labels(album: Album, by: str) -> Iterable[str]:
    if by == "artist":
        return (album.artist,) if album.artist else ()
    if by == "genre":
        return (
            label for key, (label, _words) in GENRES.items() if genre_matches(key, album.genres)
        )
    if by == "decade":
        decade = album.decade
        if not decade:
            return ()
        return (_EARLIER,) if decade == "earlier" else (f"{decade}s",)
    secondary = [TYPES[kind] for kind in album.secondary_types if kind in TYPES]
    if secondary:
        return (secondary[0],)
    return (TYPES.get(album.primary_type or "album", _OTHER),)

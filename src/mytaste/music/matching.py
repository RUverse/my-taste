"""Matching albums from other services to MusicBrainz release groups.

Albums are merged only by a MusicBrainz link, a barcode, or (for a chart entry) artist,
title, and release year together. Never by title alone.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Protocol

from mytaste.music.apple import ChartEntry
from mytaste.music.models import Album

_APPLE_ALBUM = re.compile(r"/album/(?:[^/]+/)?(\d+)/?$")
# Edition notes Apple adds to titles that MusicBrainz keeps out of the album's name.
_EDITION = re.compile(
    r"\s*(?:[\(\[][^)\]]*\b(?:deluxe|edition|version|remaster(?:ed)?|expanded|anniversary|"
    r"bonus|explicit|clean)\b[^)\]]*[\)\]]|-\s*(?:single|ep)$)",
    re.IGNORECASE,
)
_PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "‐": "-", "‑": "-", "–": "-", "—": "-"})


class MatchingCatalog(Protocol):
    async def releases_for_urls(self, urls: list[str]) -> dict[str, list[str]]: ...

    async def groups_for_releases(self, releases: list[str]) -> dict[str, str]: ...

    async def releases_by_barcode(self, barcodes: list[str]) -> dict[str, list[Album]]: ...

    async def find(self, title: str, artist: str, limit: int = 5) -> list[Album]: ...


def normalized(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", folded).split())


def plain_title(title: str) -> str:
    return _EDITION.sub("", title).strip() or title


def first_artist(name: str) -> str:
    """The first of Apple's "A, B & C" names."""

    return re.split(r",\s+|\s+&\s+|\s+x\s+|\s+feat\.?\s+", name, maxsplit=1)[0].strip() or name


def same_artist(name: str, credit: str) -> bool:
    wanted = normalized(first_artist(name))
    found = normalized(credit)
    return bool(wanted and found) and (wanted in found or found in normalized(name))


def same_album(entry: ChartEntry, album: Album) -> bool:
    return (
        normalized(plain_title(entry.title)) == normalized(plain_title(album.title))
        and same_artist(entry.artist, album.artist)
        and bool(album.year)
        and album.year == entry.release_date[:4]
    )


def _barcode_forms(code: str) -> list[str]:
    """MusicBrainz keeps a UPC as printed, with or without the EAN's leading zero."""

    return [code, f"0{code}"] if len(code) == 12 else [code]


Matches = dict[str, tuple[str | None, str]]


async def match_chart(
    entries: list[ChartEntry],
    country: str,
    catalog: MatchingCatalog,
    found: Callable[[Matches], None] | None = None,
) -> Matches:
    """Each entry's release group (or ``None``) and how it was found: ``url``, ``barcode``,
    or ``search``. ``found`` receives the matches of each step as soon as they are known, so
    a chart can be shown while its last entries are still being searched for."""

    matched: Matches = {}

    def report(step: Matches) -> None:
        matched.update(step)
        if found is not None and step:
            found(step)

    if not entries:
        return matched
    # MusicBrainz stores Apple Music links with whichever storefront the editor used.
    storefronts = list(dict.fromkeys(("us", country.lower())))
    urls = [
        f"https://music.apple.com/{front}/album/{entry.id}"
        for entry in entries
        for front in storefronts
    ]
    release_of: dict[str, str] = {}
    for url, releases in (await catalog.releases_for_urls(urls)).items():
        link = _APPLE_ALBUM.search(url)
        if link and releases:
            release_of.setdefault(link.group(1), releases[0])
    if release_of:
        groups = await catalog.groups_for_releases(list(dict.fromkeys(release_of.values())))
        report(
            {
                key: (groups[release], "url")
                for key, release in release_of.items()
                if release in groups
            }
        )

    rest = [entry for entry in entries if entry.id not in matched]
    codes = {
        form: entry for entry in rest if entry.barcode for form in _barcode_forms(entry.barcode)
    }
    if codes:
        step: Matches = {}
        for code, albums in (await catalog.releases_by_barcode(list(codes))).items():
            entry = codes.get(code)
            if entry is None or entry.id in matched or entry.id in step:
                continue
            album = next((item for item in albums if same_artist(entry.artist, item.artist)), None)
            if album is not None:
                step[entry.id] = (album.mbid, "barcode")
        report(step)

    for entry in entries:
        if entry.id in matched:
            continue
        candidates = await catalog.find(plain_title(entry.title), first_artist(entry.artist))
        album = next((item for item in candidates if same_album(entry, item)), None)
        report({entry.id: (album.mbid, "search") if album else (None, "")})
    return matched

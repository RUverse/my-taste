"""Apple Music's most played albums per country, from Apple's keyless marketing feed."""

from __future__ import annotations

import re
from dataclasses import dataclass

from mytaste.music.http import MusicHTTP
from mytaste.music.models import web_url

FEED = "https://rss.marketingtools.apple.com/api/v2/{country}/music/most-played/{limit}/albums.json"
_ALBUM_ID = re.compile(r"^[0-9]{1,15}$")
# Apple names many cover files after the album's barcode, such as 093624812647.jpg.
_BARCODE_FILE = re.compile(r"^(\d{12,14})(?:[._-]|$)")


@dataclass(frozen=True, slots=True)
class ChartEntry:
    id: str
    title: str
    artist: str
    release_date: str = ""
    url: str = ""
    artwork: str = ""

    @property
    def barcode(self) -> str:
        """The barcode the cover's file name suggests; MusicBrainz confirms it or not."""

        name = self.artwork.rsplit("/", 2)[-2] if self.artwork.count("/") >= 2 else ""
        found = _BARCODE_FILE.match(name)
        return found.group(1) if found else ""


class AppleChartClient:
    def __init__(self, http: MusicHTTP | None = None, *, timeout: float = 20) -> None:
        self.http = http or MusicHTTP("Apple Music", timeout=timeout, attempts=3)

    async def close(self) -> None:
        await self.http.close()

    async def most_played(self, country: str, limit: int = 100) -> list[ChartEntry] | None:
        """The chart for ``country``, or ``None`` where Apple has no chart."""

        response = await self.http.send(
            "GET",
            FEED.format(country=country.lower(), limit=limit),
            accept=frozenset({400, 404}),
        )
        if response.status_code in {400, 404}:
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        entries: list[ChartEntry] = []
        for row in ((data or {}).get("feed") or {}).get("results") or ():
            key = str(row.get("id") or "")
            title = str(row.get("name") or "")
            if not _ALBUM_ID.fullmatch(key) or not title:
                continue
            entries.append(
                ChartEntry(
                    id=key,
                    title=title,
                    artist=str(row.get("artistName") or ""),
                    release_date=str(row.get("releaseDate") or ""),
                    url=web_url(row.get("url")) or "",
                    artwork=web_url(row.get("artworkUrl100")) or "",
                )
            )
        return entries

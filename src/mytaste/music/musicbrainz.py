"""MusicBrainz: album search, matching lookups, and one release's tracklist and links.

MusicBrainz allows one request per second per address, so every request waits its turn on
one shared queue; grids are filled from ListenBrainz instead, a hundred albums at a time.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlparse

from mytaste.music.http import MusicHTTP
from mytaste.music.models import (
    MBID,
    SPOTIFY_ID,
    Album,
    AlbumDetails,
    ArtistCredit,
    Medium,
    Track,
    web_url,
)

API = "https://musicbrainz.org/ws/2"
_SEARCH_LIMIT = 100
_BATCH = 50


def quoted(text: str) -> str:
    """``text`` as one Lucene phrase."""

    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class MusicBrainzClient:
    def __init__(self, http: MusicHTTP | None = None, *, timeout: float = 20) -> None:
        self.http = http or MusicHTTP(
            "MusicBrainz", interval=1.1, timeout=timeout, attempts=5, max_wait=10
        )

    async def close(self) -> None:
        await self.http.close()

    async def _get(self, path: str, *, background: bool = False, **params: Any) -> Any:
        return await self.http.json(
            "GET",
            f"{API}/{path}",
            params={**params, "fmt": "json"},
            accept=frozenset({404}),
            background=background,
        )

    async def search(self, text: str, limit: int = _SEARCH_LIMIT) -> list[tuple[Album, int]]:
        """Release groups for free text, matching titles and artists, with MusicBrainz's
        relevance score (up to 100), best first."""

        data = await self._get("release-group", query=text, dismax="true", limit=limit)
        return [
            (album, int(row.get("score") or 0))
            for row in (data or {}).get("release-groups") or ()
            if (album := _album(row))
        ]

    async def search_artists(self, text: str, limit: int = 3) -> list[tuple[str, str, int]]:
        """Artists named like ``text``: MBID, name, and relevance score (up to 100)."""

        data = await self._get("artist", query=text, dismax="true", limit=limit)
        return [
            (str(row["id"]), str(row.get("name") or ""), int(row.get("score") or 0))
            for row in (data or {}).get("artists") or ()
            if MBID.fullmatch(str(row.get("id") or ""))
        ]

    async def release_groups(
        self, mbids: list[str], *, background: bool = False
    ) -> dict[str, Album]:
        """Facts ListenBrainz lacks, such as live or compilation, for many albums at once."""

        found: dict[str, Album] = {}
        for start in range(0, len(mbids), _BATCH):
            batch = mbids[start : start + _BATCH]
            data = await self._get(
                "release-group",
                query=" OR ".join(f"rgid:{key}" for key in batch),
                limit=_BATCH,
                background=background,
            )
            for row in (data or {}).get("release-groups") or ():
                album = _album(row)
                if album is not None and album.mbid in batch:
                    found[album.mbid] = album
        return found

    async def find(self, title: str, artist: str, limit: int = 5) -> list[Album]:
        query = f"releasegroup:{quoted(title)} AND artist:{quoted(artist)}"
        data = await self._get("release-group", query=query, limit=limit)
        return [album for row in (data or {}).get("release-groups") or () if (album := _album(row))]

    async def releases_for_urls(self, urls: list[str]) -> dict[str, list[str]]:
        """The releases linked to each page, such as an Apple Music album, by its URL."""

        found: dict[str, list[str]] = {}
        for start in range(0, len(urls), 100):
            batch = urls[start : start + 100]
            params = [("resource", url) for url in batch]
            data = await self.http.json(
                "GET",
                f"{API}/url",
                params=[*params, ("inc", "release-rels"), ("fmt", "json")],
                accept=frozenset({404}),
            )
            rows = (data or {}).get("urls") if isinstance(data, dict) else None
            if rows is None and isinstance(data, dict) and data.get("resource"):
                rows = [data]
            for row in rows or ():
                releases = [
                    str(relation["release"]["id"])
                    for relation in row.get("relations") or ()
                    if isinstance(relation.get("release"), dict)
                    and MBID.fullmatch(str(relation["release"].get("id", "")))
                ]
                if releases:
                    found[str(row.get("resource", ""))] = releases
        return found

    async def groups_for_releases(self, releases: list[str]) -> dict[str, str]:
        """The release group of each release."""

        found: dict[str, str] = {}
        for start in range(0, len(releases), _BATCH):
            batch = releases[start : start + _BATCH]
            data = await self._get(
                "release", query=" OR ".join(f"reid:{key}" for key in batch), limit=_BATCH
            )
            for row in (data or {}).get("releases") or ():
                group = str((row.get("release-group") or {}).get("id") or "")
                if MBID.fullmatch(group):
                    found[str(row.get("id"))] = group
        return found

    async def releases_by_barcode(self, barcodes: list[str]) -> dict[str, list[Album]]:
        """Release groups whose releases carry each barcode, as partial albums."""

        found: dict[str, list[Album]] = {}
        for start in range(0, len(barcodes), _BATCH):
            batch = barcodes[start : start + _BATCH]
            data = await self._get(
                "release", query=" OR ".join(f"barcode:{code}" for code in batch), limit=100
            )
            for row in (data or {}).get("releases") or ():
                group = row.get("release-group") or {}
                album = _album({**group, "artist-credit": row.get("artist-credit") or ()})
                code = str(row.get("barcode") or "")
                if album is not None and code:
                    found.setdefault(code, []).append(album)
        return found

    async def details(self, album: Album) -> AlbumDetails:
        """The release whose tracklist the details show, and where the album plays.

        Two requests: the album's official releases with their links, then the tracklist of
        the one linked to Spotify (else the earliest), preferring the usual edition.
        """

        data = await self._get(
            "release",
            **{"release-group": album.mbid},
            inc="url-rels media labels",
            status="official",
            limit=100,
        )
        releases = [row for row in (data or {}).get("releases") or () if row.get("id")]
        if not releases:
            data = await self._get(
                "release", **{"release-group": album.mbid}, inc="url-rels media", limit=100
            )
            releases = [row for row in (data or {}).get("releases") or () if row.get("id")]
        if not releases:
            return AlbumDetails(album=album)
        chosen = pick_release(releases, album.title)
        release = await self._get(f"release/{chosen['id']}", inc="recordings artist-credits labels")
        links = listen_links(chosen, releases)
        return AlbumDetails(
            album=album,
            release_mbid=str(chosen["id"]),
            release_title=str((release or chosen).get("title") or ""),
            label=_label(release or chosen),
            media=_media(release or {}, album.artist),
            links=links,
            release_count=len(releases),
        )


def _credits(rows: Any) -> tuple[ArtistCredit, ...]:
    credits: list[ArtistCredit] = []
    for part in rows or ():
        if not isinstance(part, dict):
            continue
        artist = part.get("artist") or {}
        credits.append(
            ArtistCredit(
                name=str(part.get("name") or artist.get("name") or ""),
                mbid=str(artist.get("id") or ""),
                join=str(part.get("joinphrase") or ""),
            )
        )
    return tuple(credits)


def _album(row: Any) -> Album | None:
    if not isinstance(row, dict):
        return None
    key = str(row.get("id") or "")
    title = str(row.get("title") or "")
    if not MBID.fullmatch(key) or not title:
        return None
    return Album(
        mbid=key,
        title=title,
        artists=_credits(row.get("artist-credit")),
        release_date=str(row.get("first-release-date") or ""),
        primary_type=str(row.get("primary-type") or "").casefold(),
        secondary_types=tuple(str(kind).casefold() for kind in row.get("secondary-types") or ()),
    )


def _links(release: dict[str, Any]) -> list[str]:
    return [
        str(relation["url"]["resource"])
        for relation in release.get("relations") or ()
        if isinstance(relation.get("url"), dict) and relation["url"].get("resource")
    ]


def service_for(url: str) -> str:
    """Which listening service a link opens, if any."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    path = parsed.path
    if host == "open.spotify.com" and path.startswith("/album/"):
        return "spotify" if SPOTIFY_ID.fullmatch(path.rstrip("/").rsplit("/", 1)[-1]) else ""
    if host in {"music.apple.com", "itunes.apple.com"} and "/album/" in path:
        return "apple-music"
    if host.endswith(".bandcamp.com") and path.startswith("/album/"):
        return "bandcamp"
    if host == "deezer.com" and "/album/" in path:
        return "deezer"
    if host == "soundcloud.com" and "/sets/" in path:
        return "soundcloud"
    return ""


def listen_links(chosen: dict[str, Any], releases: list[dict[str, Any]]) -> dict[str, str]:
    """One link per service, the chosen release's first."""

    found: dict[str, str] = {}
    for release in (chosen, *releases):
        for url in _links(release):
            service = service_for(url)
            safe = web_url(url)
            if service and safe and service not in found:
                found[service] = safe
    return found


def _track_total(release: dict[str, Any]) -> int:
    return sum(int(medium.get("track-count") or 0) for medium in release.get("media") or ())


def pick_release(releases: list[dict[str, Any]], title: str) -> dict[str, Any]:
    """The release linked to Spotify, else the earliest, preferring the usual edition: the
    album's own title and the most common number of tracks."""

    counts = Counter(_track_total(release) for release in releases)
    usual = counts.most_common(1)[0][0] if counts else 0
    wanted = title.casefold()

    def rank(release: dict[str, Any]) -> tuple[Any, ...]:
        on_spotify = any(service_for(url) == "spotify" for url in _links(release))
        date = str(release.get("date") or "")
        return (
            str(release.get("title") or "").casefold() != wanted,
            _track_total(release) != usual,
            not on_spotify,
            not date,
            date,
        )

    return min(releases, key=rank)


def _label(release: dict[str, Any]) -> str:
    for info in release.get("label-info") or ():
        name = str((info.get("label") or {}).get("name") or "")
        if name and name != "[no label]":
            return name
    return ""


def _media(release: dict[str, Any], album_artist: str) -> tuple[Medium, ...]:
    media: list[Medium] = []
    for medium in release.get("media") or ():
        tracks: list[Track] = []
        for track in medium.get("tracks") or ():
            credit = "".join(
                part.name + part.join for part in _credits(track.get("artist-credit"))
            ).strip()
            recording = track.get("recording") or {}
            tracks.append(
                Track(
                    position=str(track.get("number") or track.get("position") or ""),
                    title=str(track.get("title") or recording.get("title") or ""),
                    length=int(track.get("length") or recording.get("length") or 0),
                    artist=credit if credit and credit != album_artist else "",
                )
            )
        media.append(
            Medium(
                position=int(medium.get("position") or len(media) + 1),
                format=str(medium.get("format") or ""),
                title=str(medium.get("title") or ""),
                tracks=tuple(tracks),
            )
        )
    return tuple(media)

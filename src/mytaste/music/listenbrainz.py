"""ListenBrainz: popularity, charts, fresh releases, and album facts in batches, keyless."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mytaste.music.http import MusicError, MusicHTTP
from mytaste.music.models import MBID, Album, ArtistCredit, main_genres

API = "https://api.listenbrainz.org/1"
# Large answers fail with 502, so batches stay small and a failing one is split in half.
_METADATA_BATCH = 50
_POPULARITY_BATCH = 1000


@dataclass(frozen=True, slots=True)
class Ranked:
    """A release group in a ranked list, with that list's own count."""

    mbid: str
    count: int = 0
    album: Album | None = None


class ListenBrainzClient:
    def __init__(self, http: MusicHTTP | None = None, *, timeout: float = 20) -> None:
        # About 30 requests per 10 seconds are allowed; staying well under leaves room for
        # other instances on the same address.
        self.http = http or MusicHTTP("ListenBrainz", interval=0.4, timeout=timeout, attempts=3)

    async def close(self) -> None:
        await self.http.close()

    async def sitewide(self, period: str = "month", count: int = 1000) -> list[Ranked]:
        """The most listened release groups across ListenBrainz in ``period``."""

        data = await self.http.json(
            "GET",
            f"{API}/stats/sitewide/release-groups",
            params={"range": period, "count": count},
            accept=frozenset({204}),
        )
        rows = ((data or {}).get("payload") or {}).get("release_groups") or []
        ranked: dict[str, Ranked] = {}
        for row in rows:
            key = str(row.get("release_group_mbid") or "")
            if MBID.fullmatch(key) and key not in ranked:
                ranked[key] = Ranked(key, int(row.get("listen_count") or 0))
        return list(ranked.values())

    async def fresh_releases(self, days: int = 30) -> list[Ranked]:
        """Releases of the last ``days`` days, newest first, as partial albums."""

        data = await self.http.json(
            "GET",
            f"{API}/explore/fresh-releases/",
            params={"days": days, "sort": "release_date", "past": "true", "future": "false"},
        )
        releases = ((data or {}).get("payload") or {}).get("releases") or []
        found: dict[str, Ranked] = {}
        for row in releases:
            key = str(row.get("release_group_mbid") or "")
            if not MBID.fullmatch(key) or key in found:
                continue
            secondary = row.get("release_group_secondary_type")
            found[key] = Ranked(
                key,
                0,
                Album(
                    mbid=key,
                    title=str(row.get("release_name") or ""),
                    artists=(ArtistCredit(str(row.get("artist_credit_name") or "")),),
                    release_date=str(row.get("release_date") or ""),
                    primary_type=str(row.get("release_group_primary_type") or "").casefold(),
                    secondary_types=(str(secondary).casefold(),) if secondary else (),
                    cover_release=str(row.get("caa_release_mbid") or ""),
                    cover_id=int(row.get("caa_id") or 0),
                ),
            )
        return sorted(
            found.values(),
            key=lambda item: item.album.release_date if item.album else "",
            reverse=True,
        )

    async def popularity(self, mbids: list[str]) -> dict[str, tuple[int, int]]:
        """All-time ``(listens, listeners)`` per release group; unknown ones count zero."""

        result: dict[str, tuple[int, int]] = {}
        for start in range(0, len(mbids), _POPULARITY_BATCH):
            batch = mbids[start : start + _POPULARITY_BATCH]
            rows = await self.http.json(
                "POST", f"{API}/popularity/release-group", json={"release_group_mbids": batch}
            )
            for row in rows if isinstance(rows, list) else []:
                key = str(row.get("release_group_mbid") or "")
                if key:
                    result[key] = (
                        int(row.get("total_listen_count") or 0),
                        int(row.get("total_user_count") or 0),
                    )
            for key in batch:
                result.setdefault(key, (0, 0))
        return result

    async def metadata(self, mbids: list[str]) -> dict[str, Album]:
        """Album facts (title, artists, date, type, genres, cover) per release group."""

        found: dict[str, Album] = {}
        for start in range(0, len(mbids), _METADATA_BATCH):
            await self._metadata_batch(mbids[start : start + _METADATA_BATCH], found)
        return found

    async def _metadata_batch(self, batch: list[str], found: dict[str, Album]) -> None:
        try:
            data = await self.http.json(
                "GET",
                f"{API}/metadata/release_group/",
                params={"release_group_mbids": ",".join(batch), "inc": "artist tag"},
            )
        except MusicError:
            if len(batch) == 1:
                return
            middle = len(batch) // 2
            await self._metadata_batch(batch[:middle], found)
            await self._metadata_batch(batch[middle:], found)
            return
        for key, value in (data or {}).items():
            album = album_from_metadata(str(key), value)
            if album is not None:
                found[album.mbid] = album

    async def artist_albums(self, artist: str) -> tuple[str, list[Ranked]]:
        """An artist's name and their release groups, most listened first."""

        rows = await self.http.json(
            "GET",
            f"{API}/popularity/top-release-groups-for-artist/{artist}",
            accept=frozenset({404}),
        )
        name = ""
        ranked: list[Ranked] = []
        for row in rows if isinstance(rows, list) else []:
            key = str(row.get("release_group_mbid") or "")
            album = album_from_metadata(key, row)
            if album is None:
                continue
            for credit in album.artists:
                if credit.mbid == artist and not name:
                    name = credit.name
            ranked.append(Ranked(key, int(row.get("total_listen_count") or 0), album))
        return name, ranked


def album_from_metadata(key: str, data: Any) -> Album | None:
    """An album from ListenBrainz's metadata for one release group."""

    if not MBID.fullmatch(key) or not isinstance(data, dict):
        return None
    group = data.get("release_group") or {}
    title = str(group.get("name") or (data.get("release") or {}).get("name") or "")
    if not title:
        return None
    artist = data.get("artist") or {}
    credits = tuple(
        ArtistCredit(
            name=str(part.get("name") or ""),
            mbid=str(part.get("artist_mbid") or ""),
            join=str(part.get("join_phrase") or ""),
        )
        for part in artist.get("artists") or ()
        if isinstance(part, dict)
    )
    if not credits and artist.get("name"):
        credits = (ArtistCredit(str(artist["name"])),)
    tags = data.get("tag") or {}
    genre_tags = [
        (str(tag.get("tag")), int(tag.get("count") or 0))
        for tag in tags.get("release_group") or ()
        if tag.get("genre_mbid") and tag.get("tag")
    ]
    if not genre_tags:
        votes: dict[str, int] = {}
        for tag in tags.get("artist") or ():
            if tag.get("genre_mbid") and tag.get("tag"):
                name = str(tag["tag"])
                votes[name] = votes.get(name, 0) + int(tag.get("count") or 0)
        genre_tags = list(votes.items())
    color = data.get("release_color") or {}
    hex_color = ""
    if all(isinstance(color.get(part), int) for part in ("red", "green", "blue")):
        hex_color = "#{:02x}{:02x}{:02x}".format(
            *(max(0, min(255, color[part])) for part in ("red", "green", "blue"))
        )
    return Album(
        mbid=key,
        title=title,
        artists=credits,
        release_date=str(group.get("date") or ""),
        primary_type=str(group.get("type") or "").casefold(),
        genres=main_genres(genre_tags),
        cover_release=str(group.get("caa_release_mbid") or ""),
        cover_id=int(group.get("caa_id") or 0),
        color=hex_color,
    )

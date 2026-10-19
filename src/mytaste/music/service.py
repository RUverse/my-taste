"""The Music tab: collections, search, ordering, album details, and caching.

Lists (Apple's chart, ListenBrainz's charts and fresh releases, a search) are kept in memory
for a few hours; album facts, popularity, and chart matches are kept in SQLite. Grids are
filled from ListenBrainz, which answers for fifty albums at a time. MusicBrainz, limited to
one request a second, is asked only for searches, details, and in the background for the
facts ListenBrainz lacks (live, compilation, the exact first release date).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime
from itertools import islice
from typing import Any, TypeVar

from mytaste.music.apple import AppleChartClient, ChartEntry
from mytaste.music.coverart import CoverCache
from mytaste.music.grouping import GROUP_ALBUM_LIMIT
from mytaste.music.http import MusicError
from mytaste.music.listenbrainz import ListenBrainzClient, Ranked
from mytaste.music.matching import Matches, match_chart, normalized
from mytaste.music.models import (
    GENRES,
    MBID,
    Album,
    AlbumDetails,
    AlbumPage,
    AlbumQuery,
    album_matches,
    genre_matches,
)
from mytaste.music.musicbrainz import MusicBrainzClient
from mytaste.storage.music import MusicCacheRepository

logger = logging.getLogger(__name__)

T = TypeVar("T")

_MOST_LISTENED_LIMIT = 200
_NEW_LIMIT = 300
_NEW_DAYS = 30
_SEARCH_FLOOR = 60
_ARTIST_SEARCH_LIMIT = 60
_MAX_LISTS = 128
_ENRICH_BATCH = 50

_SPOTIFY = re.compile(r"^https?://open\.spotify\.com/(?:intl-[a-z-]+/)?album/([0-9A-Za-z]{22})")
_APPLE = re.compile(r"^https?://(?:music|itunes)\.apple\.com/([a-z]{2})/album/(?:[^/?#]+/)?(\d+)")
_DEEZER = re.compile(r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?album/(\d+)")
_BANDCAMP = re.compile(r"^https?://([a-z0-9-]+)\.bandcamp\.com/album/([^/?#]+)")
_MUSICBRAINZ = re.compile(
    r"^https?://(?:beta\.)?musicbrainz\.org/release-group/([0-9a-f-]{36})", re.IGNORECASE
)


def candidate_links(text: str) -> list[str]:
    """The URLs MusicBrainz may store for a pasted album link, most likely first."""

    text = text.strip()
    if found := _SPOTIFY.match(text):
        return [f"https://open.spotify.com/album/{found.group(1)}"]
    if found := _APPLE.match(text):
        fronts = dict.fromkeys((found.group(1), "us"))
        return [f"https://music.apple.com/{front}/album/{found.group(2)}" for front in fronts]
    if found := _DEEZER.match(text):
        return [f"https://www.deezer.com/album/{found.group(1)}"]
    if found := _BANDCAMP.match(text):
        return [f"https://{found.group(1)}.bandcamp.com/album/{found.group(2)}"]
    return []


def is_album_link(text: str) -> bool:
    """Whether a search is a pasted link to an album page MusicBrainz may know."""

    return bool(candidate_links(text) or _MUSICBRAINZ.match(text.strip()))


def merged(base: Album, extra: Album) -> Album:
    """``base`` from ListenBrainz with what MusicBrainz's own record adds."""

    return replace(
        base,
        title=base.title or extra.title,
        artists=base.artists if any(part.mbid for part in base.artists) else extra.artists,
        release_date=extra.release_date or base.release_date,
        primary_type=extra.primary_type or base.primary_type,
        secondary_types=extra.secondary_types,
    )


def sort_albums(albums: list[Album], sort: str, descending: bool) -> list[Album]:
    """One order for albums from any source; undated albums go last either way."""

    def popularity(album: Album) -> tuple[Any, ...]:
        return (album.listens or 0, album.listeners or 0, album.title.casefold())

    if sort == "popularity":
        return sorted(albums, key=popularity, reverse=descending)
    if sort == "release":
        dated = sorted(
            (album for album in albums if album.release_date),
            key=lambda album: (album.release_date, album.listens or 0),
            reverse=descending,
        )
        return dated + [album for album in albums if not album.release_date]
    if sort == "artist":
        return sorted(
            albums,
            key=lambda album: (album.artist.casefold(), album.release_date or "9999"),
            reverse=descending,
        )
    if sort == "title":
        return sorted(
            albums,
            key=lambda album: (album.title.casefold(), album.release_date),
            reverse=descending,
        )
    return list(reversed(albums)) if descending else albums


class MusicService:
    def __init__(
        self,
        repository: MusicCacheRepository,
        *,
        covers: CoverCache | None = None,
        musicbrainz: Any | None = None,
        listenbrainz: Any | None = None,
        apple: Any | None = None,
        page_size: int = 48,
        list_ttl: float = 6 * 3600,
        facts_ttl: float = 7 * 86_400,
        enrich_ttl: float = 30 * 86_400,
        popularity_ttl: float = 86_400,
        details_ttl: float = 7 * 86_400,
        retry_unmatched: float = 3 * 86_400,
        match_wait: float = 10.0,
    ) -> None:
        self.repository = repository
        self.covers = covers
        self.musicbrainz = musicbrainz or MusicBrainzClient()
        self.listenbrainz = listenbrainz or ListenBrainzClient()
        self.apple = apple or AppleChartClient()
        self.page_size = page_size
        self.list_ttl = list_ttl
        self.facts_ttl = facts_ttl
        self.enrich_ttl = enrich_ttl
        self.popularity_ttl = popularity_ttl
        self.details_ttl = details_ttl
        self.retry_unmatched = retry_unmatched
        self.match_wait = match_wait
        self._lists: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._matching: dict[str, asyncio.Task[None]] = {}
        self._enrich_queue: dict[str, None] = {}
        self._enriching: asyncio.Task[None] | None = None

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for client in (self.musicbrainz, self.listenbrainz, self.apple, self.covers):
            close = getattr(client, "close", None)
            if callable(close):
                await close()

    def _track(self, task: asyncio.Task[T]) -> asyncio.Task[T]:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _cached(self, key: str, ttl: float, load: Callable[[], Awaitable[T]]) -> T:
        """A list from memory while fresh; an older copy when the service can't answer."""

        hit = self._lists.get(key)
        if hit is not None and time.monotonic() - hit[0] < ttl:
            return hit[1]  # type: ignore[no-any-return]
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._lists.get(key)
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return hit[1]  # type: ignore[no-any-return]
            try:
                value = await load()
            except MusicError:
                if hit is not None:
                    return hit[1]  # type: ignore[no-any-return]
                raise
            self._lists[key] = (time.monotonic(), value)
            if len(self._lists) > _MAX_LISTS:
                for old in sorted(self._lists, key=lambda name: self._lists[name][0])[:16]:
                    self._lists.pop(old, None)
                    self._locks.pop(old, None)
            return value

    # Lists ------------------------------------------------------------------------------

    async def _chart(self, region: str) -> tuple[list[str], int, tuple[str, ...]]:
        """Apple Music's chart for ``region`` as albums, how many entries are still being
        matched, and notices."""

        country = (region or "US").upper()
        notices: list[str] = []
        entries, stored_on = await self._chart_entries(country)
        if entries is None:
            notices.append(f"Apple Music has no chart for {country}, so this is the US chart.")
            country = "US"
            entries, stored_on = await self._chart_entries(country)
        if stored_on:
            notices.append(
                f"Apple Music's chart can't be reached right now, so this is the chart from "
                f"{stored_on}."
            )
        entries = entries or []
        ids = [entry.id for entry in entries]
        known = self.repository.links("apple", ids)
        retry_before = time.time() - self.retry_unmatched
        todo = [
            entry
            for entry in entries
            if entry.id not in known
            or (known[entry.id][0] is None and known[entry.id][1] < retry_before)
        ]
        if todo:
            task = self._match(country, todo)
            if not any(mbid for mbid, _checked in known.values()):
                # Nothing to show yet: the first steps match most of a chart in a few requests.
                await asyncio.wait({task}, timeout=self.match_wait)
            known = self.repository.links("apple", ids)
        order = [mbid for key in ids if key in known and (mbid := known[key][0])]
        pending = sum(1 for key in ids if key not in known)
        return list(dict.fromkeys(order)), pending, tuple(notices)

    async def _chart_entries(self, country: str) -> tuple[list[ChartEntry] | None, str]:
        """Apple's chart for ``country``; when Apple can't be reached, the last one read and
        the day it was read."""

        async def load() -> list[ChartEntry] | None:
            entries = await self.apple.most_played(country)
            if entries is not None:
                self.repository.save_chart("apple", country, [asdict(entry) for entry in entries])
            return entries

        try:
            return await self._cached(f"chart:{country}", self.list_ttl, load), ""
        except MusicError:
            stored = self.repository.chart("apple", country)
            if stored is None:
                raise
        names = {item.name for item in fields(ChartEntry)}
        entries = [
            ChartEntry(**{key: str(value) for key, value in row.items() if key in names})
            for row in stored[0]
            if isinstance(row, dict) and row.get("id") and row.get("title")
        ]
        # Kept for ten minutes, so a service that is down isn't asked on every page.
        self._lists[f"chart:{country}"] = (time.monotonic() - self.list_ttl + 600, entries)
        return entries, datetime.fromtimestamp(stored[1], UTC).strftime("%Y-%m-%d")

    def _match(self, country: str, entries: list[ChartEntry]) -> asyncio.Task[None]:
        running = self._matching.get(country)
        if running is not None and not running.done():
            return running

        def save(rows: Matches) -> None:
            self.repository.save_links(
                "apple", [(key, mbid, how) for key, (mbid, how) in rows.items()]
            )

        async def run() -> None:
            try:
                await match_chart(entries, country, self.musicbrainz, save)
            except MusicError:
                logger.warning("Matching Apple Music's %s chart stopped early", country)
            except Exception:
                logger.exception("Matching Apple Music's %s chart failed", country)

        task = self._track(asyncio.create_task(run()))
        self._matching[country] = task
        return task

    async def _most_listened(self) -> list[Ranked]:
        rows: list[Ranked] = await self._cached(
            "sitewide:month", self.list_ttl, lambda: self.listenbrainz.sitewide("month")
        )
        return rows[:_MOST_LISTENED_LIMIT]

    async def _new(self) -> list[tuple[str, Album]]:
        """The most listened albums and EPs of the last month, then the newest."""

        async def load() -> list[tuple[str, Album]]:
            fresh = [
                row
                for row in await self.listenbrainz.fresh_releases(_NEW_DAYS)
                if row.album is not None and row.album.primary_type in {"album", "ep"}
            ]
            counts = await self._popularity([row.mbid for row in fresh])
            fresh.sort(
                key=lambda row: (
                    counts.get(row.mbid, (0, 0))[0],
                    row.album.release_date if row.album else "",
                ),
                reverse=True,
            )
            return [(row.mbid, row.album) for row in fresh[:_NEW_LIMIT] if row.album]

        return await self._cached("new", self.list_ttl, load)

    async def _pool(self) -> list[str]:
        """Albums the genre collections choose from: the most listened of all time, of the
        last year, and of the last month."""

        async def load() -> list[str]:
            keys: list[str] = []
            for period in ("all_time", "year"):
                keys += [row.mbid for row in await self.listenbrainz.sitewide(period)]
            keys += [row.mbid for row in await self._most_listened()]
            return list(dict.fromkeys(keys))

        return await self._cached("pool", 24 * 3600, load)

    async def _search(self, text: str) -> tuple[list[tuple[Album, int]], list[Ranked]]:
        """Albums whose title or artist matches, and, when ``text`` names an artist, that
        artist's albums and EPs, which a title search alone buries under tributes."""

        async def load() -> tuple[list[tuple[Album, int]], list[Ranked]]:
            found = await self.musicbrainz.search(text)
            wanted = normalized(text)
            artists = await self.musicbrainz.search_artists(text)
            named = next(
                (key for key, name, score in artists if score >= 90 and normalized(name) == wanted),
                "",
            )
            own: list[Ranked] = []
            if named:
                _name, ranked = await self._artist(named)
                own = [
                    row
                    for row in ranked
                    if row.album is not None and row.album.primary_type in {"album", "ep"}
                ][:_ARTIST_SEARCH_LIMIT]
            return found, own

        return await self._cached(f"search:{text.casefold()}", 3600, load)

    async def _artist(self, artist: str) -> tuple[str, list[Ranked]]:
        return await self._cached(
            f"artist:{artist}", self.list_ttl, lambda: self.listenbrainz.artist_albums(artist)
        )

    # Facts ------------------------------------------------------------------------------

    async def _popularity(self, mbids: list[str]) -> dict[str, tuple[int, int]]:
        cached = self.repository.popularity(mbids)
        now = time.time()
        counts = {key: (listens, users) for key, (listens, users, _checked) in cached.items()}
        stale = [
            key for key in mbids if key not in cached or now - cached[key][2] > self.popularity_ttl
        ]
        if stale:
            try:
                fresh = await self.listenbrainz.popularity(stale)
            except MusicError:
                logger.warning("ListenBrainz popularity is unavailable; using stored counts")
            else:
                self.repository.save_popularity(fresh)
                counts.update(fresh)
        return counts

    async def _albums(
        self,
        mbids: list[str],
        fallback: dict[str, Album] | None = None,
        *,
        musicbrainz: dict[str, Album] | None = None,
    ) -> dict[str, Album]:
        """Facts and popularity for each album.

        ``fallback`` stands in for albums ListenBrainz doesn't know yet; ``musicbrainz`` holds
        MusicBrainz's own record of some albums (from a search), which needs no second look.
        """

        mbids = list(dict.fromkeys(mbids))
        musicbrainz = musicbrainz or {}
        cached = self.repository.albums(mbids)
        now = time.time()
        stale = [key for key in mbids if key not in cached or now - cached[key][1] > self.facts_ttl]
        fetched: dict[str, Album] = {}
        if stale:
            try:
                fetched = await self.listenbrainz.metadata(stale)
            except MusicError:
                logger.warning("ListenBrainz metadata is unavailable; using stored albums")
            for key, album in list(fetched.items()):
                previous = cached.get(key)
                if key in musicbrainz:
                    fetched[key] = merged(album, musicbrainz[key])
                elif previous is not None and previous[2] is not None:
                    # Keep what MusicBrainz added the last time.
                    fetched[key] = merged(album, previous[0])
            self.repository.save_albums(
                album for key, album in fetched.items() if key not in musicbrainz
            )
            self.repository.save_albums(
                (album for key, album in fetched.items() if key in musicbrainz), enriched=True
            )
        found: dict[str, Album] = {}
        for key in mbids:
            album = fetched.get(key) or (cached[key][0] if key in cached else None)
            album = album or (fallback or {}).get(key)
            if album is not None:
                found[key] = album
        counts = await self._popularity(list(found))
        for key, album in found.items():
            if key in counts:
                found[key] = replace(album, listens=counts[key][0], listeners=counts[key][1])
        self._enrich(
            key
            for key in found
            if key not in musicbrainz
            and (
                key in fetched or (key in cached and (cached[key][2] or 0) < now - self.enrich_ttl)
            )
        )
        return found

    def _enrich(self, mbids: Iterable[str]) -> None:
        """Ask MusicBrainz, in the background, for what ListenBrainz leaves out."""

        for key in mbids:
            self._enrich_queue[key] = None
        if self._enrich_queue and (self._enriching is None or self._enriching.done()):
            self._enriching = self._track(asyncio.create_task(self._run_enrichment()))

    async def _run_enrichment(self) -> None:
        while self._enrich_queue:
            batch = list(islice(self._enrich_queue, _ENRICH_BATCH))
            for key in batch:
                self._enrich_queue.pop(key, None)
            try:
                found = await self.musicbrainz.release_groups(batch, background=True)
            except MusicError:
                logger.warning("MusicBrainz is unavailable; album types will be added later")
                return
            except Exception:
                logger.exception("Adding MusicBrainz facts to albums failed")
                return
            cached = self.repository.albums(batch)
            # Albums MusicBrainz no longer has (merged or removed) are marked done as they are.
            self.repository.save_albums(
                (
                    merged(cached[key][0], found[key]) if key in found else cached[key][0]
                    for key in batch
                    if key in cached
                ),
                enriched=True,
                fetched=False,
            )

    async def album(self, mbid: str) -> Album:
        """One album's facts and popularity; ``LookupError`` when MusicBrainz has none."""

        found = await self._albums([mbid])
        if mbid in found:
            return found[mbid]
        known = await self.musicbrainz.release_groups([mbid])
        if mbid not in known:
            raise LookupError("Unknown album")
        self.repository.save_albums([known[mbid]], enriched=True)
        counts = await self._popularity([mbid])
        listens, users = counts.get(mbid, (0, 0))
        return replace(known[mbid], listens=listens, listeners=users)

    async def details(self, mbid: str) -> AlbumDetails:
        album = await self.album(mbid)
        cached = self.repository.details(mbid)
        if cached is not None and time.time() - cached[1] < self.details_ttl:
            return replace(cached[0], album=album)
        try:
            details = await self.musicbrainz.details(album)
        except MusicError:
            if cached is not None:
                return replace(cached[0], album=album)
            raise
        self.repository.save_details(details)
        return details

    async def find_link(self, text: str) -> str | None:
        """The album a pasted Spotify, Apple Music, Deezer, Bandcamp, or MusicBrainz link
        opens, through the links MusicBrainz keeps on releases."""

        direct = _MUSICBRAINZ.match(text.strip())
        if direct:
            key = direct.group(1).lower()
            return key if MBID.fullmatch(key) else None
        urls = candidate_links(text)
        if not urls:
            return None
        known = self.repository.links("url", urls)
        for url in urls:
            if url in known and known[url][0]:
                return known[url][0]
        releases = await self.musicbrainz.releases_for_urls(urls)
        first = next((found[0] for url in urls if (found := releases.get(url))), None)
        group = (await self.musicbrainz.groups_for_releases([first])).get(first) if first else None
        self.repository.save_links("url", [(url, group, "url") for url in urls])
        return group

    # Browsing ---------------------------------------------------------------------------

    async def ordered(self, region: str, query: AlbumQuery) -> AlbumPage:
        """Every album of the query in its order, unpaginated."""

        pending = 0
        notices: tuple[str, ...] = ()
        artist_name = ""
        fallback: dict[str, Album] = {}
        authoritative: dict[str, Album] = {}
        scores: dict[str, int] = {}
        if query.search:
            results, own = await self._search(query.search)
            order = [row.mbid for row in own] + [album.mbid for album, _score in results]
            authoritative = {album.mbid: album for album, _score in results}
            fallback = {row.mbid: row.album for row in own if row.album} | authoritative
            # The named artist's own albums are exact matches.
            scores = {album.mbid: score for album, score in results} | {
                row.mbid: 100 for row in own
            }
        elif query.artist:
            artist_name, ranked = await self._artist(query.artist)
            order = [row.mbid for row in ranked]
            fallback = {row.mbid: row.album for row in ranked if row.album}
        elif query.collection == "popular":
            order, pending, notices = await self._chart(region)
        elif query.collection == "most-listened":
            order = [row.mbid for row in await self._most_listened()]
        elif query.collection == "new":
            rows = await self._new()
            order = [key for key, _album in rows]
            fallback = dict(rows)
        else:
            order = await self._pool()
        order = list(dict.fromkeys(order))
        albums = await self._albums(order, fallback, musicbrainz=authoritative)
        listed = [albums[key] for key in order if key in albums]
        if query.search:
            # MusicBrainz's relevance, with the albums people listen to first among good matches.
            listed.sort(
                key=lambda album: (
                    scores.get(album.mbid, 0) < _SEARCH_FLOOR,
                    -(album.listens or 0) if scores.get(album.mbid, 0) >= _SEARCH_FLOOR else 0,
                )
            )
        elif query.collection in GENRES and not query.artist:
            # Genre collections are albums and EPs; singles stay in the charts.
            listed = [
                album
                for album in listed
                if genre_matches(query.collection, album.genres)
                and album.primary_type in {"", "album", "ep"}
            ]
        listed = [album for album in listed if album_matches(album, query)]
        if query.sort != "catalog":
            listed = sort_albums(listed, query.sort, query.descending)
        return AlbumPage(
            items=tuple(listed),
            total=len(listed),
            pending=pending,
            notices=notices,
            artist_name=artist_name,
        )

    async def browse(self, region: str, query: AlbumQuery) -> AlbumPage:
        found = await self.ordered(region, query)
        pages = max((found.total + self.page_size - 1) // self.page_size, 1)
        page = min(max(query.page, 1), pages)
        start = (page - 1) * self.page_size
        return replace(
            found, items=found.items[start : start + self.page_size], pages=pages, page=page
        )

    async def shelves(self, region: str, query: AlbumQuery) -> AlbumPage:
        """The first albums of the query, which Shelves groups into rows."""

        found = await self.ordered(region, query)
        return replace(found, items=found.items[:GROUP_ALBUM_LIMIT])

    def warm(self, regions: Iterable[str]) -> None:
        """Fill the caches in the background so the first visit to Music is quick."""

        async def run() -> None:
            for name, load in (
                ("most listened", lambda: self.ordered("", AlbumQuery("most-listened"))),
                ("new releases", lambda: self.ordered("", AlbumQuery("new"))),
                ("genres", lambda: self.ordered("", AlbumQuery("rock"))),
                *(
                    (f"chart {region}", lambda region=region: self.ordered(region, AlbumQuery()))
                    for region in dict.fromkeys(regions)
                ),
            ):
                try:
                    await load()
                except MusicError:
                    logger.warning("Music could not prepare %s", name)
                except Exception:
                    logger.exception("Music could not prepare %s", name)

        self._track(asyncio.create_task(run()))

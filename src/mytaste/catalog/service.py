from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import ClassVar, Generic, TypeVar

from mytaste.catalog.models import (
    BrowseCategory,
    BrowseMediaType,
    BrowseQuery,
    Catalog,
    CatalogItem,
    CatalogPage,
    Genre,
    MediaDetails,
    MediaType,
    Provider,
    Region,
    Season,
    WatchLink,
    WatchOption,
)
from mytaste.catalog.tmdb import TMDBClient

T = TypeVar("T")

LocalSource = Callable[[int], Awaitable[CatalogPage]]
"""Load the first ``limit`` local titles in category order, with the local total."""

_PAGE_SIZE = 20
_MAX_MERGED_PAGES = 100
_MAX_STREAM_PAGES = 150

_GENRE_NAVIGATION: dict[BrowseMediaType, tuple[str, ...]] = {
    "all": ("Drama", "Comedy", "Documentary", "Animation"),
    "movie": ("Action", "Comedy", "Drama", "Horror"),
    "tv": ("Action & Adventure", "Comedy", "Drama", "Mystery"),
}


@dataclass(slots=True)
class _CacheEntry(Generic[T]):
    value: T
    expires_at: float


class CatalogService:
    def __init__(
        self,
        client: TMDBClient,
        *,
        provider_ttl: float = 86_400,
        catalog_ttl: float = 900,
        enrichment_ttl: float = 86_400,
    ) -> None:
        self.client = client
        self.provider_ttl = provider_ttl
        self.catalog_ttl = catalog_ttl
        self.enrichment_ttl = enrichment_ttl
        self._regions: _CacheEntry[tuple[Region, ...]] | None = None
        self._providers: dict[str, _CacheEntry[tuple[Provider, ...]]] = {}
        self._genres: dict[MediaType, _CacheEntry[tuple[Genre, ...]]] = {}
        self._browse_pages: dict[tuple[str, BrowseQuery], _CacheEntry[CatalogPage]] = {}
        self._stream_pages: dict[tuple[object, ...], _CacheEntry[CatalogPage]] = {}
        self._availability: dict[tuple[str, MediaType, int], _CacheEntry[frozenset[int]]] = {}
        self._people: dict[tuple[MediaType, int], _CacheEntry[tuple[str, tuple[str, ...]]]] = {}
        self._details: dict[tuple[MediaType, int], _CacheEntry[MediaDetails]] = {}
        self._watch_links: dict[tuple[str, MediaType, int], _CacheEntry[tuple[WatchLink, ...]]] = {}
        self._seasons: dict[int, _CacheEntry[tuple[Season, ...]]] = {}
        self._matches: dict[tuple[MediaType, str, int | None], _CacheEntry[CatalogItem | None]] = {}
        self._availability_limit = asyncio.Semaphore(8)

    async def close(self) -> None:
        await self.client.close()

    async def regions(self) -> tuple[Region, ...]:
        now = time.monotonic()
        if self._regions is not None and self._regions.expires_at > now:
            return self._regions.value
        regions = await self.client.regions()
        self._regions = _CacheEntry(regions, now + self.provider_ttl)
        return regions

    async def providers(self, region: str) -> tuple[Provider, ...]:
        now = time.monotonic()
        cached = self._providers.get(region)
        if cached is not None and cached.expires_at > now:
            return cached.value
        providers = await self.client.providers(region)
        self._providers[region] = _CacheEntry(providers, now + self.provider_ttl)
        return providers

    async def genres(self, media_type: MediaType) -> tuple[Genre, ...]:
        now = time.monotonic()
        cached = self._genres.get(media_type)
        if cached is not None and cached.expires_at > now:
            return cached.value
        genres = await self.client.genres(media_type)
        self._genres[media_type] = _CacheEntry(genres, now + self.provider_ttl)
        return genres

    async def categories(self, media_type: BrowseMediaType) -> tuple[BrowseCategory, ...]:
        movie_genres: tuple[Genre, ...] = ()
        tv_genres: tuple[Genre, ...] = ()
        if media_type == "all":
            movie_genres, tv_genres = await asyncio.gather(
                self.genres("movie"),
                self.genres("tv"),
            )
        elif media_type == "movie":
            movie_genres = await self.genres("movie")
        else:
            tv_genres = await self.genres("tv")

        movie_by_name = {genre.name.casefold(): genre.id for genre in movie_genres}
        tv_by_name = {genre.name.casefold(): genre.id for genre in tv_genres}
        categories = [
            BrowseCategory("latest", "Latest"),
            BrowseCategory("popular", "Most Popular"),
        ]
        for name in _GENRE_NAVIGATION[media_type]:
            key = name.casefold()
            movie_id = movie_by_name.get(key)
            tv_id = tv_by_name.get(key)
            if media_type == "all" and (movie_id is None or tv_id is None):
                continue
            if media_type == "movie" and movie_id is None:
                continue
            if media_type == "tv" and tv_id is None:
                continue
            categories.append(
                BrowseCategory(
                    slug=_slugify(name),
                    label=name,
                    movie_genre_id=movie_id,
                    tv_genre_id=tv_id,
                )
            )
        return tuple(categories)

    async def browse(
        self,
        region: str,
        query: BrowseQuery,
        *,
        local: LocalSource | None = None,
    ) -> CatalogPage:
        """Browse streaming titles, optionally merged with local library titles.

        Streaming and local titles form one ordering: page N is the Nth slice of the merged,
        de-duplicated sequence, so a local title appears exactly where it ranks.
        """

        query = replace(query, library_ids=())
        cache_key = (region, query)
        now = time.monotonic()
        cached = self._browse_pages.get(cache_key)
        if local is None and cached is not None and cached.expires_at > now:
            return cached.value

        categories = await self.categories(query.media_type)
        category = next(
            (candidate for candidate in categories if candidate.slug == query.category),
            categories[0],
        )
        if query.search:
            page = await self._search(region, query, local)
        else:
            page = await self._discover(region, query, category, local)
        page = await self._add_genre_names(page)
        if local is None:
            self._browse_pages[cache_key] = _CacheEntry(page, now + self.catalog_ttl)
        return page

    async def _discover(
        self,
        region: str,
        query: BrowseQuery,
        category: BrowseCategory,
        local: LocalSource | None,
    ) -> CatalogPage:
        streams: list[_Stream | _LocalStream] = []
        for media_type in _media_types(query.media_type):
            if category.slug == "latest":
                sort_by = (
                    "primary_release_date.desc" if media_type == "movie" else "first_air_date.desc"
                )
            else:
                sort_by = "popularity.desc"
            genre_id = category.genre_id_for(media_type)
            if category.slug not in {"latest", "popular"} and genre_id is None:
                continue
            streams.append(
                _Stream(self._discover_loader(region, media_type, query, sort_by, genre_id))
            )
        if local is not None:
            streams.append(_LocalStream(local))
        if not streams:
            return CatalogPage(items=(), page=query.page)
        if len(streams) == 1 and isinstance(streams[0], _Stream):
            return await streams[0].load(query.page)
        key = _latest_key if category.slug == "latest" else _popular_key
        return await _merge_streams(streams, key, query.page)

    def _discover_loader(
        self,
        region: str,
        media_type: MediaType,
        query: BrowseQuery,
        sort_by: str,
        genre_id: int | None,
    ) -> Callable[[int], Awaitable[CatalogPage]]:
        async def load(page: int) -> CatalogPage:
            cache_key = (
                region,
                media_type,
                sort_by,
                genre_id,
                query.provider_ids,
                query.year_from,
                query.year_to,
                query.minimum_rating,
                query.include_unrated,
                page,
            )
            now = time.monotonic()
            cached = self._stream_pages.get(cache_key)
            if cached is not None and cached.expires_at > now:
                return cached.value
            result = await self.client.discover(
                media_type,
                region,
                query.provider_ids,
                sort_by=sort_by,
                genre_id=genre_id,
                year_from=query.year_from,
                year_to=query.year_to,
                minimum_rating=query.minimum_rating,
                include_unrated=query.include_unrated,
                page=page,
            )
            self._stream_pages[cache_key] = _CacheEntry(result, now + self.catalog_ttl)
            return result

        return load

    async def _search(
        self,
        region: str,
        query: BrowseQuery,
        local: LocalSource | None,
    ) -> CatalogPage:
        media_types = _media_types(query.media_type)
        pages = await asyncio.gather(
            *(
                self.client.search(
                    media_type,
                    query.search,
                    region,
                    year_from=query.year_from,
                    year_to=query.year_to,
                    minimum_rating=query.minimum_rating,
                    include_unrated=query.include_unrated,
                    page=query.page,
                )
                for media_type in media_types
            )
        )
        candidates = _merge_pages(pages, query, latest=False)
        checks = await asyncio.gather(
            *(
                self._is_on_selected_service(region, item, query.provider_ids)
                for item in candidates.items
            )
        )
        items = tuple(
            item for item, available in zip(candidates.items, checks, strict=True) if available
        )
        streaming = CatalogPage(
            items=items,
            page=candidates.page,
            total_pages=candidates.total_pages,
            total_results=len(items),
        )
        if local is None:
            return streaming
        # TMDB search pages are ordered by relevance rather than by a shared sort key, so
        # local matches are paged alongside each search page and ranked by popularity.
        offset = (query.page - 1) * _PAGE_SIZE
        local_page = await local(offset + _PAGE_SIZE)
        local_items = local_page.items[offset : offset + _PAGE_SIZE]
        merged = _deduplicate(sorted((*items, *local_items), key=_popular_key, reverse=True))
        return CatalogPage(
            items=tuple(merged),
            page=query.page,
            total_pages=max(
                streaming.total_pages, math.ceil(local_page.total_results / _PAGE_SIZE), 1
            ),
            total_results=len(items) + local_page.total_results,
        )

    async def available_provider_ids(
        self,
        region: str,
        media_type: MediaType,
        item_id: int,
    ) -> frozenset[int]:
        """Return the subscription services that carry a title in ``region``."""

        cache_key = (region, media_type, item_id)
        now = time.monotonic()
        cached = self._availability.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.value
        async with self._availability_limit:
            available_ids = await self.client.watch_provider_ids(media_type, item_id, region)
        self._availability[cache_key] = _CacheEntry(available_ids, now + self.enrichment_ttl)
        return available_ids

    async def _is_on_selected_service(
        self,
        region: str,
        item: CatalogItem,
        selected_provider_ids: tuple[int, ...],
    ) -> bool:
        available_ids = await self.available_provider_ids(region, item.media_type, item.id)
        return bool(available_ids.intersection(selected_provider_ids))

    async def people(self, media_type: MediaType, item_id: int) -> tuple[str, tuple[str, ...]]:
        cache_key = (media_type, item_id)
        now = time.monotonic()
        cached = self._people.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.value
        people = await self.client.people(media_type, item_id)
        self._people[cache_key] = _CacheEntry(people, now + self.enrichment_ttl)
        return people

    async def details(self, media_type: MediaType, item_id: int) -> MediaDetails:
        cache_key = (media_type, item_id)
        now = time.monotonic()
        cached = self._details.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.value
        details = await self.client.details(media_type, item_id)
        self._details[cache_key] = _CacheEntry(details, now + self.enrichment_ttl)
        return details

    async def watch_options(
        self,
        region: str,
        media_type: MediaType,
        item_id: int,
        provider_ids: Sequence[int],
    ) -> tuple[WatchOption, ...]:
        """Return the selected services that carry a title, best-ranked first, with links."""

        providers, available_ids = await asyncio.gather(
            self.providers(region),
            self.available_provider_ids(region, media_type, item_id),
        )
        selected = set(provider_ids) & available_ids
        carriers = sorted(
            (provider for provider in providers if provider.id in selected),
            key=lambda provider: provider.priority,
        )
        if not carriers:
            return ()
        links = await self._cached_watch_links(region, media_type, item_id)
        by_id = {link.provider_id: link for link in links}
        by_name = {_normalize_title(link.provider_name): link for link in links}
        fallback = f"https://www.themoviedb.org/{media_type}/{item_id}/watch?locale={region}"
        options: list[WatchOption] = []
        for provider in carriers:
            # TMDB's page uses JustWatch provider IDs, which differ for some services.
            link = by_id.get(provider.id) or by_name.get(_normalize_title(provider.name))
            if link is None:
                options.append(WatchOption(provider, fallback, direct=False))
            else:
                options.append(WatchOption(provider, link.url))
        return tuple(options)

    async def _cached_watch_links(
        self, region: str, media_type: MediaType, item_id: int
    ) -> tuple[WatchLink, ...]:
        cache_key = (region, media_type, item_id)
        now = time.monotonic()
        cached = self._watch_links.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.value
        async with self._availability_limit:
            links = await self.client.watch_links(media_type, item_id, region)
        # A page that yields nothing may be a transient failure, so retry it sooner.
        ttl = self.enrichment_ttl if links else min(self.enrichment_ttl, self.catalog_ttl)
        self._watch_links[cache_key] = _CacheEntry(links, now + ttl)
        return links

    async def seasons(self, item_id: int) -> tuple[Season, ...]:
        now = time.monotonic()
        cached = self._seasons.get(item_id)
        if cached is not None and cached.expires_at > now:
            return cached.value
        seasons = await self.client.seasons(item_id)
        self._seasons[item_id] = _CacheEntry(seasons, now + self.enrichment_ttl)
        return seasons

    async def match_title(
        self,
        media_type: MediaType,
        title: str,
        year: int | None = None,
    ) -> CatalogItem | None:
        """Find the TMDB entry that best matches a locally parsed title."""

        cleaned = " ".join(title.split())
        if not cleaned:
            return None
        cache_key = (media_type, cleaned.casefold(), year)
        now = time.monotonic()
        cached = self._matches.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.value
        results = await self.client.lookup(media_type, cleaned, year=year)
        match = _best_match(results, cleaned, year)
        self._matches[cache_key] = _CacheEntry(match, now + self.enrichment_ttl)
        return match

    async def _add_genre_names(self, page: CatalogPage) -> CatalogPage:
        if not page.items:
            return page
        requested_types = tuple(dict.fromkeys(item.media_type for item in page.items))
        genre_groups = await asyncio.gather(*(self.genres(value) for value in requested_types))
        names_by_type = {
            media_type: {genre.id: genre.name for genre in genres}
            for media_type, genres in zip(requested_types, genre_groups, strict=True)
        }
        items = tuple(
            replace(
                item,
                genres=tuple(
                    names_by_type[item.media_type][genre_id]
                    for genre_id in item.genre_ids
                    if genre_id in names_by_type[item.media_type]
                )[:2],
            )
            for item in page.items
        )
        return replace(page, items=items)

    async def catalog(self, region: str, provider_ids: tuple[int, ...]) -> Catalog:
        movies, shows = await asyncio.gather(
            self.client.latest("movie", region, provider_ids),
            self.client.latest("tv", region, provider_ids),
        )
        return Catalog(movies=movies, shows=shows)


def _media_types(media_type: BrowseMediaType) -> tuple[MediaType, ...]:
    return ("movie", "tv") if media_type == "all" else (media_type,)


def _latest_key(item: CatalogItem) -> tuple[str, float]:
    return (item.release_date, item.popularity)


def _popular_key(item: CatalogItem) -> tuple[float, float]:
    return (item.popularity, item.rating)


def _identity(item: CatalogItem) -> tuple[str, int] | None:
    return (item.media_type, item.id) if item.id > 0 else None


@dataclass(slots=True)
class _Stream:
    """A sorted streaming result sequence, read lazily one TMDB page at a time."""

    load: Callable[[int], Awaitable[CatalogPage]]
    items: list[CatalogItem] = field(default_factory=list)
    next_page: int = 1
    total_pages: int | None = None
    total_results: int = 0
    local: ClassVar[bool] = False

    @property
    def exhausted(self) -> bool:
        return self.total_pages is not None and (
            self.next_page > self.total_pages or self.next_page > _MAX_STREAM_PAGES
        )

    async def fill(self, count: int) -> None:
        """Load pages until at least ``count`` items are buffered or the stream ends."""

        while len(self.items) < count and not self.exhausted:
            if self.total_pages is None:
                # Learn the page count from the (usually cached) first page before fanning out.
                last = self.next_page
            else:
                wanted = max(math.ceil((count - len(self.items)) / _PAGE_SIZE), 1)
                last = min(self.next_page + wanted - 1, _MAX_STREAM_PAGES, self.total_pages)
            numbers = range(self.next_page, last + 1)
            pages = await asyncio.gather(*(self.load(number) for number in numbers))
            self.total_pages = min(page.total_pages for page in pages)
            self.total_results = pages[0].total_results
            for number, page in zip(numbers, pages, strict=True):
                if number <= self.total_pages:
                    self.items.extend(page.items)
            self.next_page = last + 1


@dataclass(slots=True)
class _LocalStream:
    """Local library titles in category order, loaded with a single bounded query."""

    source: LocalSource
    items: list[CatalogItem] = field(default_factory=list)
    requested: int = 0
    total_results: int = 0
    local: ClassVar[bool] = True

    @property
    def total_pages(self) -> int:
        return max(math.ceil(self.total_results / _PAGE_SIZE), 1)

    @property
    def exhausted(self) -> bool:
        return self.requested > 0 and (
            len(self.items) >= self.total_results or len(self.items) < self.requested
        )

    async def fill(self, count: int) -> None:
        if len(self.items) >= count or self.exhausted:
            return
        result = await self.source(count)
        self.items = list(result.items)
        self.requested = count
        self.total_results = result.total_results


async def _merge_streams(
    streams: Sequence[_Stream | _LocalStream],
    key: Callable[[CatalogItem], tuple[object, ...]],
    page: int,
) -> CatalogPage:
    """Return one page of the de-duplicated k-way merge of sorted streams."""

    page = min(max(page, 1), _MAX_MERGED_PAGES)
    wanted = page * _PAGE_SIZE
    await asyncio.gather(*(stream.fill(wanted) for stream in streams))
    local_items = {
        identity: item
        for stream in streams
        if stream.local
        for item in stream.items
        if (identity := _identity(item)) is not None
    }
    positions = [0] * len(streams)
    merged: list[CatalogItem] = []
    seen: set[tuple[str, int]] = set()
    while len(merged) < wanted:
        for index, stream in enumerate(streams):
            if positions[index] >= len(stream.items) and not stream.exhausted:
                await stream.fill(len(stream.items) + wanted - len(merged))
        heads = [
            (key(stream.items[positions[index]]), -index)
            for index, stream in enumerate(streams)
            if positions[index] < len(stream.items)
        ]
        if not heads:
            break
        index = -max(heads)[1]
        item = streams[index].items[positions[index]]
        positions[index] += 1
        identity = _identity(item)
        if identity is not None:
            if identity in seen:
                continue
            seen.add(identity)
            twin = local_items.get(identity)
            if twin is not None and not item.in_library:
                item = replace(item, in_library=True, library_summary=twin.library_summary)
        merged.append(item)

    total_results = sum(
        min(stream.total_results, (stream.total_pages or 1) * _PAGE_SIZE) for stream in streams
    )
    total_pages = min(max(math.ceil(total_results / _PAGE_SIZE), 1), _MAX_MERGED_PAGES)
    if len(merged) < wanted and all(stream.exhausted for stream in streams):
        total_pages = max(math.ceil(len(merged) / _PAGE_SIZE), 1)
    return CatalogPage(
        items=tuple(merged[(page - 1) * _PAGE_SIZE :]),
        page=page,
        total_pages=total_pages,
        total_results=total_results,
    )


def _deduplicate(items: Sequence[CatalogItem]) -> list[CatalogItem]:
    kept: list[CatalogItem] = []
    positions: dict[tuple[str, int], int] = {}
    for item in items:
        identity = _identity(item)
        if identity is None:
            kept.append(item)
            continue
        existing = positions.get(identity)
        if existing is None:
            positions[identity] = len(kept)
            kept.append(item)
        elif item.in_library and not kept[existing].in_library:
            kept[existing] = replace(
                kept[existing], in_library=True, library_summary=item.library_summary
            )
    return kept


def _merge_pages(
    pages: list[CatalogPage] | tuple[CatalogPage, ...],
    query: BrowseQuery,
    *,
    latest: bool,
) -> CatalogPage:
    items = [item for page in pages for item in page.items]
    if latest:
        items.sort(key=lambda item: (item.release_date, item.popularity), reverse=True)
    else:
        items.sort(key=lambda item: (item.popularity, item.rating), reverse=True)
    return CatalogPage(
        items=tuple(items[:20]),
        page=query.page,
        total_pages=max((page.total_pages for page in pages), default=1),
        total_results=sum(page.total_results for page in pages),
    )


def _slugify(value: str) -> str:
    return "-".join(value.casefold().replace("&", "and").split())


_TITLE_NOISE = re.compile(r"[^0-9a-z]+")


def _normalize_title(value: str) -> str:
    return _TITLE_NOISE.sub(" ", value.casefold().replace("&", "and")).strip()


def _best_match(
    results: tuple[CatalogItem, ...],
    title: str,
    year: int | None,
) -> CatalogItem | None:
    if not results:
        return None
    wanted = _normalize_title(title)
    exact = [item for item in results if _normalize_title(item.title) == wanted]
    if year is not None:
        same_year = [item for item in exact if item.year == str(year)]
        if same_year:
            return same_year[0]
    if exact:
        return exact[0]
    if year is not None:
        dated = [item for item in results if item.year == str(year)]
        if dated:
            return dated[0]
    return results[0]

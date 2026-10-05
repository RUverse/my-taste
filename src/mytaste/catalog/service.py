from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, ClassVar, Generic, TypeVar, cast

from mytaste.catalog.filters import (
    PERSON_ROLES,
    GenreChoice,
    PersonRole,
    TitleFacts,
    discover_params,
    genre_choices,
    genre_queries,
)
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
    SortKey,
    WatchLink,
    WatchOption,
    catalog_sort_key,
    natural_descending,
)
from mytaste.catalog.tmdb import TMDBClient
from mytaste.collections.models import smart_categories

T = TypeVar("T")

LocalSource = Callable[[BrowseQuery, int], Awaitable[CatalogPage]]
"""Load the first ``limit`` local titles in the query's sort order, with the local total."""

TitleKey = tuple[str, int]
Refine = Callable[[Sequence[CatalogItem]], Awaitable[frozenset[TitleKey]]]
"""Return the titles, of those given, that pass the filters TMDB could not apply itself."""

_PAGE_SIZE = 20
_MAX_MERGED_PAGES = 100
_MAX_STREAM_PAGES = 150
# Sorting by score alone surfaces obscure titles with a handful of perfect votes.
_RATING_SORT_MIN_VOTES = 200


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
        self._credits: dict[
            int, _CacheEntry[tuple[tuple[CatalogItem, frozenset[PersonRole]], ...]]
        ] = {}
        self._names: dict[tuple[str, int], _CacheEntry[str]] = {}
        self._options: dict[str, _CacheEntry[Any]] = {}
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

    async def genre_choices(self) -> tuple[GenreChoice, ...]:
        movie_genres, tv_genres = await asyncio.gather(self.genres("movie"), self.genres("tv"))
        return genre_choices(movie_genres, tv_genres)

    async def certifications(self, region: str) -> dict[MediaType, tuple[str, ...]]:
        """The content ratings of ``region`` (or the US when it has none), mildest first."""

        movie, tv = await asyncio.gather(
            self._option("certifications:movie", lambda: self.client.certifications("movie")),
            self._option("certifications:tv", lambda: self.client.certifications("tv")),
        )
        return {"movie": movie.get(region, ()), "tv": tv.get(region, ())}

    async def countries(self) -> tuple[tuple[str, str], ...]:
        return await self._option("countries", self.client.countries)

    async def languages(self) -> tuple[tuple[str, str], ...]:
        return await self._option("languages", self.client.languages)

    async def _option(self, key: str, load: Callable[[], Awaitable[T]]) -> T:
        now = time.monotonic()
        cached = self._options.get(key)
        if cached is not None and cached.expires_at > now:
            return cast(T, cached.value)
        value = await load()
        self._options[key] = _CacheEntry(value, now + self.provider_ttl)
        return value

    async def title_facts(self, media_type: MediaType, item_id: int) -> TitleFacts:
        async with self._availability_limit:
            return await self.client.title_facts(media_type, item_id)

    async def person_credits(
        self, person_id: int
    ) -> tuple[tuple[CatalogItem, frozenset[PersonRole]], ...]:
        now = time.monotonic()
        cached = self._credits.get(person_id)
        if cached is not None and cached.expires_at > now:
            return cached.value
        credits = await self.client.person_credits(person_id)
        self._credits[person_id] = _CacheEntry(credits, now + self.enrichment_ttl)
        return credits

    async def search_people(self, query: str) -> tuple[tuple[int, str, str], ...]:
        return await self.client.search_people(query)

    async def search_keywords(self, query: str) -> tuple[tuple[int, str], ...]:
        return await self.client.search_keywords(query)

    async def name_of(self, kind: str, item_id: int) -> str:
        """The name of a person or keyword chosen in a filter."""

        cache_key = (kind, item_id)
        now = time.monotonic()
        cached = self._names.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.value
        if kind == "person":
            name = await self.client.person_name(item_id)
        else:
            name = await self.client.keyword_name(item_id)
        self._names[cache_key] = _CacheEntry(name, now + self.enrichment_ttl)
        return name

    async def categories(self, media_type: BrowseMediaType) -> tuple[BrowseCategory, ...]:
        """Return the predefined collections, with genre names bound to TMDB ids."""

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
        return smart_categories(media_type, movie_genres, tv_genres)

    async def browse(
        self,
        region: str,
        query: BrowseQuery,
        *,
        local: LocalSource | None = None,
        refine: Refine | None = None,
        exclude: frozenset[TitleKey] = frozenset(),
    ) -> CatalogPage:
        """Browse streaming titles, optionally merged with local library titles.

        Streaming and local titles form one ordering: page N is the Nth slice of the merged,
        de-duplicated sequence, so a local title appears exactly where it ranks.

        TMDB applies the query's filters to discovered titles. Titles it cannot filter, such as
        search results and a person's credits, are passed through ``refine``; ``exclude`` drops
        streaming titles the filters rule out by their local copies, such as watched ones.
        """

        query = replace(query, library_ids=(), game_sources=())
        cache_key = (region, query)
        now = time.monotonic()
        cached = self._browse_pages.get(cache_key)
        uncached = local is not None or refine is not None or bool(exclude)
        if not uncached and cached is not None and cached.expires_at > now:
            return cached.value

        categories = await self.categories(query.media_type)
        category = next(
            (candidate for candidate in categories if candidate.slug == query.category),
            categories[0],
        )
        if query.search:
            page = await self._search(region, query, local, refine, exclude)
        else:
            page = await self._discover(region, query, category, local, refine, exclude)
        page = await self._add_genre_names(page)
        if not uncached:
            self._browse_pages[cache_key] = _CacheEntry(page, now + self.catalog_ttl)
        return page

    async def _discover(
        self,
        region: str,
        query: BrowseQuery,
        category: BrowseCategory,
        local: LocalSource | None,
        refine: Refine | None = None,
        exclude: frozenset[TitleKey] = frozenset(),
    ) -> CatalogPage:
        sort, descending = query.sort_for(category.sort)
        if sort == "added":
            # Only library titles have an added date; streaming keeps the collection's order.
            sort, descending = category.sort, natural_descending(category.sort)
        released_after = (
            date.today() - timedelta(days=category.released_within_days)
            if category.released_within_days is not None
            else None
        )

        filters = query.filters

        def streams_for(value: BrowseQuery) -> list[_Stream | _LocalStream | _ListStream]:
            streams: list[_Stream | _LocalStream | _ListStream] = []
            # Filters on files or watch progress only match local titles.
            if filters.has_people and not filters.local_only:
                # TMDB cannot filter series by person, so a person's credits are read instead.
                order = value.sort_for("popularity")
                streams.append(
                    _ListStream(
                        lambda: self._credited(region, value, category, released_after, refine),
                        catalog_sort_key(*order),
                        order[1],
                    )
                )
            elif not filters.local_only:
                for media_type in _media_types(value.media_type):
                    genre_id = category.genre_id_for(media_type)
                    if category.has_genre and genre_id is None:
                        continue
                    for genres in genre_queries(genre_id, filters):
                        streams.append(
                            _Stream(
                                self._discover_loader(
                                    region, media_type, value, genres, released_after, exclude
                                )
                            )
                        )
            if local is not None:
                streams.append(_LocalStream(local, value))
            return streams

        if category.limit and (sort, descending) != ("popularity", True):
            # The collection is its most popular titles; other sorts reorder just those.
            ranked = replace(query, sort="popularity", descending=True)
            streams = streams_for(ranked)
            if not streams:
                return CatalogPage(items=(), page=query.page)
            top = await _merge_prefix(
                streams, catalog_sort_key("popularity", True), True, category.limit
            )
            items = sorted(top, key=catalog_sort_key(sort, descending), reverse=descending)
            total_pages = max(math.ceil(len(items) / _PAGE_SIZE), 1)
            page = min(max(query.page, 1), total_pages)
            return CatalogPage(
                items=tuple(items[(page - 1) * _PAGE_SIZE : page * _PAGE_SIZE]),
                page=page,
                total_pages=total_pages,
                total_results=len(items),
            )

        value = replace(query, sort=sort, descending=descending)
        streams = streams_for(value)
        if not streams:
            return CatalogPage(items=(), page=query.page)
        if len(streams) == 1 and isinstance(streams[0], _Stream):
            if not category.limit:
                return await streams[0].load(query.page)
            last_page = math.ceil(category.limit / _PAGE_SIZE)
            number = min(query.page, last_page)
            result = await streams[0].load(number)
            return replace(
                result,
                items=result.items[: category.limit - (number - 1) * _PAGE_SIZE],
                total_pages=min(result.total_pages, last_page),
                total_results=min(result.total_results, category.limit),
            )
        return await _merge_streams(
            streams, catalog_sort_key(sort, descending), descending, query.page, category.limit
        )

    def _discover_loader(
        self,
        region: str,
        media_type: MediaType,
        query: BrowseQuery,
        genres: str | None,
        released_after: date | None,
        exclude: frozenset[TitleKey] = frozenset(),
    ) -> Callable[[int], Awaitable[CatalogPage]]:
        sort, descending = query.sort_for("popularity")
        sort_by = _tmdb_sort(media_type, sort, descending)
        minimum_votes = _RATING_SORT_MIN_VOTES if sort == "rating" else None
        extra = discover_params(query.filters)

        async def load(page: int) -> CatalogPage:
            result = await load_page(page)
            if not exclude:
                return result
            return replace(
                result, items=tuple(item for item in result.items if _identity(item) not in exclude)
            )

        async def load_page(page: int) -> CatalogPage:
            cache_key = (
                region,
                media_type,
                sort_by,
                genres,
                extra,
                released_after,
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
                genres=genres,
                year_from=query.year_from,
                year_to=query.year_to,
                minimum_rating=query.minimum_rating,
                include_unrated=query.include_unrated,
                released_after=released_after,
                minimum_votes=minimum_votes,
                extra=extra,
                page=page,
            )
            self._stream_pages[cache_key] = _CacheEntry(result, now + self.catalog_ttl)
            return result

        return load

    async def search_everywhere(
        self,
        region: str,
        query: BrowseQuery,
        *,
        refine: Refine | None = None,
        exclude: frozenset[TitleKey] = frozenset(),
    ) -> CatalogPage:
        """One page of search results from any service, or none, ignoring the user's services.

        For a search the user's services carry nothing for; the query's filters still apply.
        """

        candidates = await self._search_candidates(region, query)
        items = tuple(item for item in candidates.items if _identity(item) not in exclude)
        if refine is not None and query.filters.active:
            allowed = await refine(items)
            items = tuple(item for item in items if _identity(item) in allowed)
        return await self._add_genre_names(CatalogPage(items=items, total_results=len(items)))

    async def _search_candidates(self, region: str, query: BrowseQuery) -> CatalogPage:
        media_types = () if query.filters.local_only else _media_types(query.media_type)
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
        return _merge_pages(pages, query, latest=False)

    async def _search(
        self,
        region: str,
        query: BrowseQuery,
        local: LocalSource | None,
        refine: Refine | None = None,
        exclude: frozenset[TitleKey] = frozenset(),
    ) -> CatalogPage:
        candidates = await self._search_candidates(region, query)
        checks = await asyncio.gather(
            *(
                self._is_on_selected_service(region, item, query.provider_ids)
                for item in candidates.items
            )
        )
        items = tuple(
            item
            for item, available in zip(candidates.items, checks, strict=True)
            if available and _identity(item) not in exclude
        )
        if refine is not None and query.filters.active:
            allowed = await refine(items)
            items = tuple(item for item in items if _identity(item) in allowed)
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
        local_page = await local(
            replace(query, sort="popularity", descending=True), offset + _PAGE_SIZE
        )
        local_items = local_page.items[offset : offset + _PAGE_SIZE]
        merged = _deduplicate(
            sorted((*items, *local_items), key=catalog_sort_key("popularity", True), reverse=True)
        )
        return CatalogPage(
            items=tuple(merged),
            page=query.page,
            total_pages=max(
                streaming.total_pages, math.ceil(local_page.total_results / _PAGE_SIZE), 1
            ),
            total_results=len(items) + local_page.total_results,
        )

    async def _credited(
        self,
        region: str,
        query: BrowseQuery,
        category: BrowseCategory,
        released_after: date | None,
        refine: Refine | None,
    ) -> list[CatalogItem]:
        """The titles the chosen people are credited on that match the view and services.

        People in one filter are alternatives; each filter (actor, director, …) must match.
        """

        filters = query.filters
        chosen: list[dict[TitleKey, CatalogItem]] = []
        for role in PERSON_ROLES:
            people = filters.people(role)
            if not people:
                continue
            credits = await asyncio.gather(*map(self.person_credits, people))
            chosen.append(
                {
                    (item.media_type, item.id): item
                    for titles in credits
                    for item, roles in titles
                    if role in roles
                }
            )
        keys = set.intersection(*(set(titles) for titles in chosen)) if chosen else set()
        today = date.today().isoformat()
        candidates = [
            item
            for key, item in chosen[0].items()
            if key in keys
            and query.media_type in {"all", item.media_type}
            and _in_view(item, query, category, released_after, today)
        ]
        checks = await asyncio.gather(
            *(
                self._is_on_selected_service(region, item, query.provider_ids)
                for item in candidates
            ),
            return_exceptions=True,
        )
        available = [item for item, found in zip(candidates, checks, strict=True) if found is True]
        if refine is None:
            return available
        allowed = await refine(available)
        return [item for item in available if _identity(item) in allowed]

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
        async with self._availability_limit:
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


def _in_view(
    item: CatalogItem,
    query: BrowseQuery,
    category: BrowseCategory,
    released_after: date | None,
    today: str,
) -> bool:
    """Apply what TMDB's discover would to a title found another way: dates, rating, genre."""

    if not item.release_date or item.release_date > today:
        return False
    if released_after is not None and item.release_date < released_after.isoformat():
        return False
    year = int(item.year) if item.year.isdigit() else None
    if query.year_from is not None and (year is None or year < query.year_from):
        return False
    if query.year_to is not None and (year is None or year > query.year_to):
        return False
    if (
        query.minimum_rating is not None
        and item.rating < query.minimum_rating
        and not (query.include_unrated and item.rating == 0)
    ):
        return False
    genre_id = category.genre_id_for(item.media_type)
    return not category.has_genre or (genre_id is not None and genre_id in item.genre_ids)


def _media_types(media_type: BrowseMediaType) -> tuple[MediaType, ...]:
    return ("movie", "tv") if media_type == "all" else (media_type,)


def _tmdb_sort(media_type: MediaType, sort: SortKey, descending: bool) -> str:
    fields: dict[SortKey, str] = {
        "release": "primary_release_date" if media_type == "movie" else "first_air_date",
        "rating": "vote_average",
        "title": "title" if media_type == "movie" else "name",
    }
    return f"{fields.get(sort, 'popularity')}.{'desc' if descending else 'asc'}"


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
    query: BrowseQuery
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
        result = await self.source(self.query, count)
        self.items = list(result.items)
        self.requested = count
        self.total_results = result.total_results


@dataclass(slots=True)
class _ListStream:
    """Streaming titles found as a complete list, such as a person's credits, then sorted."""

    load: Callable[[], Awaitable[list[CatalogItem]]]
    key: Callable[[CatalogItem], tuple[Any, ...]]
    descending: bool
    items: list[CatalogItem] = field(default_factory=list)
    loaded: bool = False
    local: ClassVar[bool] = False

    @property
    def total_results(self) -> int:
        return len(self.items)

    @property
    def total_pages(self) -> int:
        return max(math.ceil(len(self.items) / _PAGE_SIZE), 1)

    @property
    def exhausted(self) -> bool:
        return self.loaded

    async def fill(self, count: int) -> None:
        if not self.loaded:
            self.items = sorted(await self.load(), key=self.key, reverse=self.descending)
            self.loaded = True


_AnyStream = _Stream | _LocalStream | _ListStream


async def _merge_prefix(
    streams: Sequence[_AnyStream],
    key: Callable[[CatalogItem], tuple[Any, ...]],
    descending: bool,
    wanted: int,
) -> list[CatalogItem]:
    """Return the first ``wanted`` titles of the de-duplicated k-way merge of sorted streams."""

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
    # Ties go to the earlier stream in both directions.
    direction = -1 if descending else 1
    pick = max if descending else min
    while len(merged) < wanted:
        for index, stream in enumerate(streams):
            if positions[index] >= len(stream.items) and not stream.exhausted:
                await stream.fill(len(stream.items) + wanted - len(merged))
        heads = [
            (key(stream.items[positions[index]]), direction * index)
            for index, stream in enumerate(streams)
            if positions[index] < len(stream.items)
        ]
        if not heads:
            break
        index = direction * pick(heads)[1]
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
    return merged


async def _merge_streams(
    streams: Sequence[_AnyStream],
    key: Callable[[CatalogItem], tuple[Any, ...]],
    descending: bool,
    page: int,
    limit: int | None = None,
) -> CatalogPage:
    """Return one page of the de-duplicated k-way merge of sorted streams.

    ``limit`` caps the merged sequence; without it the merge stops after 100 pages.
    """

    cap = limit or _MAX_MERGED_PAGES * _PAGE_SIZE
    max_pages = math.ceil(cap / _PAGE_SIZE)
    page = min(max(page, 1), max_pages)
    wanted = min(page * _PAGE_SIZE, cap)
    merged = await _merge_prefix(streams, key, descending, wanted)
    total_results = sum(
        min(stream.total_results, (stream.total_pages or 1) * _PAGE_SIZE) for stream in streams
    )
    if limit is not None:
        total_results = min(total_results, limit)
    total_pages = min(max(math.ceil(total_results / _PAGE_SIZE), 1), max_pages)
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
    items.sort(key=catalog_sort_key("release" if latest else "popularity", True), reverse=True)
    return CatalogPage(
        items=tuple(items[:20]),
        page=query.page,
        total_pages=max((page.total_pages for page in pages), default=1),
        total_results=sum(page.total_results for page in pages),
    )


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

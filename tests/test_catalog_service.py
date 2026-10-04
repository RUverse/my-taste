from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, timedelta

from mytaste.catalog.models import (
    BrowseCategory,
    BrowseQuery,
    CatalogItem,
    CatalogPage,
    Genre,
    MediaDetails,
    Provider,
    WatchLink,
)
from mytaste.catalog.service import CatalogService


class FakeTMDBClient:
    def __init__(self) -> None:
        self.detail_calls = 0

    async def close(self) -> None:
        return None

    async def genres(self, media_type: str) -> tuple[Genre, ...]:
        if media_type == "movie":
            names = ((28, "Action"), (35, "Comedy"), (18, "Drama"), (27, "Horror"))
        else:
            names = (
                (10759, "Action & Adventure"),
                (35, "Comedy"),
                (18, "Drama"),
                (9648, "Mystery"),
            )
        return tuple(Genre(identifier, name, media_type) for identifier, name in names)

    async def discover(self, media_type: str, *_args, **_kwargs) -> CatalogPage:
        item_id = 1 if media_type == "movie" else 2
        return CatalogPage(
            items=(
                CatalogItem(
                    id=item_id,
                    media_type=media_type,
                    title=f"{media_type} title",
                    release_date="2026-08-20",
                    overview="",
                    rating=8,
                    genre_ids=(18,),
                    popularity=float(item_id),
                ),
            ),
            total_results=1,
        )

    async def search(self, media_type: str, *_args, **_kwargs) -> CatalogPage:
        return await self.discover(media_type)

    async def lookup(self, media_type: str, query: str, *, year: int | None = None):
        self.lookup_calls = getattr(self, "lookup_calls", 0) + 1
        if query == "Nothing":
            return ()
        return (
            CatalogItem(
                id=1,
                media_type=media_type,
                title="Blade Runner",
                release_date="1982-06-25",
                overview="",
                rating=8.1,
            ),
            CatalogItem(
                id=2,
                media_type=media_type,
                title="Blade Runner 2049",
                release_date="2017-10-06",
                overview="",
                rating=8.0,
            ),
        )

    async def watch_provider_ids(
        self, media_type: str, item_id: int, region: str
    ) -> frozenset[int]:
        assert region == "DE"
        return frozenset({8}) if item_id == 1 else frozenset({337})

    async def providers(self, region: str) -> tuple[Provider, ...]:
        return (
            Provider(337, "Disney Plus", "/disney.jpg", 3),
            Provider(8, "Netflix", "/netflix.jpg", 1),
            Provider(9, "Amazon Prime Video", "/prime.jpg", 2),
        )

    async def watch_links(self, media_type: str, item_id: int, region: str):
        self.watch_link_calls = getattr(self, "watch_link_calls", 0) + 1
        return (
            WatchLink(8, "Netflix", "https://www.netflix.com/title/1"),
            WatchLink(2706, "Disney Plus", "https://www.disneyplus.com/x"),
        )

    async def details(self, media_type: str, item_id: int) -> MediaDetails:
        self.detail_calls += 1
        return MediaDetails(
            id=item_id,
            media_type=media_type,
            title="Detail title",
            release_date="2026-08-20",
            overview="Details",
            rating=8,
        )


def test_categories_follow_media_specific_navigation() -> None:
    service = CatalogService(FakeTMDBClient())

    movie_categories = asyncio.run(service.categories("movie"))
    tv_categories = asyncio.run(service.categories("tv"))

    assert [item.label for item in movie_categories] == [
        "Popular",
        "Latest",
        "Comedy",
        "Drama",
        "Action",
        "Horror",
    ]
    assert [item.label for item in tv_categories] == [
        "Popular",
        "Latest",
        "Comedy",
        "Drama",
        "Action",
        "Mystery",
    ]
    action = next(item for item in tv_categories if item.slug == "action")
    assert (action.movie_genre_id, action.tv_genre_id) == (None, 10759)
    assert movie_categories[0].limit == 200
    assert movie_categories[1].sort == "release"


def test_all_media_browse_merges_results_and_adds_genre_names() -> None:
    service = CatalogService(FakeTMDBClient())
    query = BrowseQuery(media_type="all", category="popular", provider_ids=(8,))

    page = asyncio.run(service.browse("DE", query))

    assert [item.title for item in page.items] == ["tv title", "movie title"]
    assert all(item.genres == ("Drama",) for item in page.items)
    assert page.total_results == 2


def test_search_keeps_only_titles_on_selected_services() -> None:
    service = CatalogService(FakeTMDBClient())
    query = BrowseQuery(media_type="all", search="title", provider_ids=(8,))

    page = asyncio.run(service.browse("DE", query))

    assert [item.id for item in page.items] == [1]


def test_search_everywhere_ignores_the_selected_services() -> None:
    service = CatalogService(FakeTMDBClient())
    query = BrowseQuery(media_type="all", search="title", provider_ids=(8,))

    page = asyncio.run(service.search_everywhere("DE", query))

    assert sorted(item.id for item in page.items) == [1, 2]
    assert all(item.genres == ("Drama",) for item in page.items)


def test_available_provider_ids_are_cached() -> None:
    client = FakeTMDBClient()
    calls: list[int] = []
    original = client.watch_provider_ids

    async def counted(media_type: str, item_id: int, region: str) -> frozenset[int]:
        calls.append(item_id)
        return await original(media_type, item_id, region)

    client.watch_provider_ids = counted  # type: ignore[method-assign]
    service = CatalogService(client)

    first = asyncio.run(service.available_provider_ids("DE", "movie", 1))
    second = asyncio.run(service.available_provider_ids("DE", "movie", 1))

    assert first == second == frozenset({8})
    assert calls == [1]


def test_details_are_cached() -> None:
    client = FakeTMDBClient()
    service = CatalogService(client)

    first = asyncio.run(service.details("movie", 1))
    second = asyncio.run(service.details("movie", 1))

    assert first is second
    assert client.detail_calls == 1


def test_watch_options_rank_selected_carriers_and_match_links() -> None:
    client = FakeTMDBClient()

    async def carriers(media_type: str, item_id: int, region: str) -> frozenset[int]:
        return frozenset({8, 9, 337}) if item_id == 1 else frozenset()

    client.watch_provider_ids = carriers  # type: ignore[method-assign]
    service = CatalogService(client)

    options = asyncio.run(service.watch_options("DE", "movie", 1, (337, 9, 8)))
    again = asyncio.run(service.watch_options("DE", "movie", 1, (8,)))
    unavailable = asyncio.run(service.watch_options("DE", "movie", 2, (8,)))

    assert [(option.provider.id, option.url, option.direct) for option in options] == [
        (8, "https://www.netflix.com/title/1", True),
        (9, "https://www.themoviedb.org/movie/1/watch?locale=DE", False),
        # The page lists Disney Plus under JustWatch's own provider id.
        (337, "https://www.disneyplus.com/x", True),
    ]
    assert [option.provider.id for option in again] == [8]
    assert unavailable == ()
    assert client.watch_link_calls == 1


def test_match_title_prefers_exact_title_and_year() -> None:
    client = FakeTMDBClient()
    service = CatalogService(client)  # type: ignore[arg-type]

    sequel = asyncio.run(service.match_title("movie", "Blade Runner 2049", 2017))
    original = asyncio.run(service.match_title("movie", "blade runner", None))
    by_year = asyncio.run(service.match_title("movie", "Runner", 2017))
    fallback = asyncio.run(service.match_title("movie", "Runner", None))
    missing = asyncio.run(service.match_title("movie", "Nothing", None))
    cached = asyncio.run(service.match_title("movie", "Blade Runner 2049", 2017))

    assert sequel is not None and sequel.id == 2
    assert original is not None and original.id == 1
    assert by_year is not None and by_year.id == 2
    assert fallback is not None and fallback.id == 1
    assert missing is None
    assert cached is not None and cached.id == 2
    assert client.lookup_calls == 5


class PagedTMDBClient(FakeTMDBClient):
    """Three pages per media type, each sorted by descending popularity and date."""

    def __init__(self) -> None:
        super().__init__()
        self.discover_calls: list[tuple[str, int]] = []

    async def discover(self, media_type: str, *_args, page: int = 1, **_kwargs) -> CatalogPage:
        self.discover_calls.append((media_type, page))
        base = 1000 if media_type == "movie" else 2000
        offset = 0 if media_type == "movie" else 1
        items = tuple(
            CatalogItem(
                id=base + index,
                media_type=media_type,
                title=f"{media_type} {index}",
                release_date=f"{2026 - index // 12:04d}-{12 - index % 12:02d}-0{1 + offset}",
                overview="",
                rating=7,
                popularity=float(200 - 2 * index - offset),
            )
            for index in range((page - 1) * 20, page * 20)
        )
        return CatalogPage(items=items, page=page, total_pages=3, total_results=60)


def _local_source(items: list[CatalogItem], calls: list[int]):
    async def load(_query: BrowseQuery, limit: int) -> CatalogPage:
        calls.append(limit)
        return CatalogPage(items=tuple(items[:limit]), total_results=len(items))

    return load


def _local_items() -> list[CatalogItem]:
    return [
        CatalogItem(5000, "movie", "Local favourite", "2025-01-01", "", 8, popularity=500),
        CatalogItem(
            1003,
            "movie",
            "movie 3",
            "2025-12-01",
            "",
            7,
            popularity=150,
            library_summary="2 files",
            in_library=True,
        ),
        CatalogItem(0, "movie", "Unmatched", "", "", 0, in_library=True),
    ]


def test_mixed_pages_follow_one_global_order_without_gaps_or_repeats() -> None:
    client = PagedTMDBClient()
    service = CatalogService(client)
    local_items = _local_items()
    calls: list[int] = []
    query = BrowseQuery(media_type="all", category="popular", provider_ids=(8,))

    async def browse_pages() -> list[CatalogPage]:
        pages = []
        for number in range(1, 8):
            page = await service.browse(
                "DE", replace(query, page=number), local=_local_source(local_items, calls)
            )
            pages.append(page)
        return pages

    pages = asyncio.run(browse_pages())
    merged = [item for page in pages for item in page.items]

    streaming = [
        item
        for media_type in ("movie", "tv")
        for number in (1, 2, 3)
        for item in asyncio.run(PagedTMDBClient().discover(media_type, page=number)).items
    ]
    expected = sorted(
        [*streaming, local_items[0], local_items[2]],
        key=lambda item: item.popularity,
        reverse=True,
    )
    assert [(item.media_type, item.id) for item in merged] == [
        (item.media_type, item.id) for item in expected
    ]
    assert [len(page.items) for page in pages] == [20, 20, 20, 20, 20, 20, 2]
    assert pages[0].items[0].title == "Local favourite"
    duplicate = next(item for item in merged if item.id == 1003)
    assert duplicate.in_library and duplicate.library_summary == "2 files"
    assert merged[-1].title == "Unmatched"
    assert pages[0].total_pages == 7
    assert calls[0] == 20
    assert client.discover_calls[:2] == [("movie", 1), ("tv", 1)]
    assert len(client.discover_calls) == 6, "TMDB pages are cached between merged pages"


def test_single_stream_without_local_titles_passes_the_tmdb_page_through() -> None:
    client = PagedTMDBClient()
    service = CatalogService(client)
    query = BrowseQuery(media_type="movie", category="popular", provider_ids=(8,), page=2)

    page = asyncio.run(service.browse("DE", query))

    assert client.discover_calls == [("movie", 2)]
    assert page.items[0].id == 1020
    assert (page.page, page.total_pages) == (2, 3)


def test_mixed_latest_orders_by_release_date_with_undated_titles_last() -> None:
    service = CatalogService(PagedTMDBClient())
    local_items = [
        CatalogItem(7000, "movie", "Newest on disk", "2026-12-31", "", 6, in_library=True),
        CatalogItem(7001, "movie", "Old classic", "1958-05-01", "", 8, in_library=True),
        CatalogItem(0, "movie", "Unknown", "", "", 0, in_library=True),
    ]
    query = BrowseQuery(media_type="movie", category="latest", provider_ids=(8,), page=4)

    page = asyncio.run(service.browse("DE", query, local=_local_source(local_items, [])))
    first = asyncio.run(
        service.browse("DE", replace(query, page=1), local=_local_source(local_items, []))
    )

    assert first.items[0].title == "Newest on disk"
    assert [item.title for item in page.items] == ["movie 59", "Old classic", "Unknown"]
    assert page.total_pages == 4


def test_search_mixes_local_matches_by_popularity() -> None:
    service = CatalogService(FakeTMDBClient())
    local_items = [
        CatalogItem(70523, "tv", "Dark", "2017-12-01", "", 8.4, popularity=5, in_library=True),
        CatalogItem(
            1,
            "movie",
            "movie title",
            "2026-08-20",
            "",
            8,
            popularity=1,
            library_summary="2 files",
            in_library=True,
        ),
    ]
    query = BrowseQuery(media_type="all", search="title", provider_ids=(8,))

    page = asyncio.run(service.browse("DE", query, local=_local_source(local_items, [])))

    assert [item.title for item in page.items] == ["Dark", "movie title"]
    assert page.items[1].in_library and page.items[1].library_summary == "2 files"


class RecordingTMDBClient(PagedTMDBClient):
    def __init__(self) -> None:
        super().__init__()
        self.discover_kwargs: list[dict[str, object]] = []

    async def discover(self, media_type: str, *args, page: int = 1, **kwargs) -> CatalogPage:
        self.discover_kwargs.append({"media_type": media_type, **kwargs})
        return await super().discover(media_type, *args, page=page, **kwargs)


def test_sorts_and_collection_rules_reach_tmdb() -> None:
    client = RecordingTMDBClient()
    service = CatalogService(client)
    base = BrowseQuery(media_type="all", provider_ids=(8,))

    asyncio.run(service.browse("DE", replace(base, category="latest")))
    asyncio.run(service.browse("DE", replace(base, category="comedy", sort="rating")))
    asyncio.run(
        service.browse("DE", replace(base, category="drama", sort="title", descending=True))
    )

    latest = [
        call for call in client.discover_kwargs if call["sort_by"].startswith(("primary", "first"))
    ]
    assert {call["sort_by"] for call in latest} == {
        "primary_release_date.desc",
        "first_air_date.desc",
    }
    assert all(call["released_after"] == date.today() - timedelta(days=365) for call in latest)
    rated = [call for call in client.discover_kwargs if call["sort_by"] == "vote_average.desc"]
    assert {call["genres"] for call in rated} == {"35"}
    assert all(call["minimum_votes"] == 200 for call in rated)
    titled = {call["sort_by"] for call in client.discover_kwargs if call["genres"] == "18"}
    assert titled == {"title.desc", "name.desc"}


def test_ascending_merge_puts_oldest_first_and_undated_last() -> None:
    service = CatalogService(PagedTMDBClient())
    local_items = [
        CatalogItem(0, "movie", "Unknown", "", "", 0, in_library=True),
        CatalogItem(7001, "movie", "Old classic", "1958-05-01", "", 8, in_library=True),
    ]
    query = BrowseQuery(
        media_type="movie", category="drama", provider_ids=(8,), sort="release", descending=False
    )
    seen: list[tuple[str | None, bool | None]] = []

    async def local(value: BrowseQuery, limit: int) -> CatalogPage:
        seen.append((value.sort, value.descending))
        dated = sorted(
            (item for item in local_items if item.release_date), key=lambda i: i.release_date
        )
        undated = [item for item in local_items if not item.release_date]
        return CatalogPage(items=tuple([*dated, *undated][:limit]), total_results=2)

    class AscendingClient(PagedTMDBClient):
        async def discover(self, media_type: str, *args, page: int = 1, **kwargs) -> CatalogPage:
            result = await super().discover(media_type, *args, page=4 - page, **kwargs)
            return replace(result, items=tuple(reversed(result.items)), page=page)

    service = CatalogService(AscendingClient())
    first = asyncio.run(service.browse("DE", query, local=local))
    last = asyncio.run(service.browse("DE", replace(query, page=4), local=local))

    assert first.items[0].title == "Old classic"
    assert [item.release_date for item in first.items[1:]] == sorted(
        item.release_date for item in first.items[1:]
    )
    assert last.items[-1].title == "Unknown"
    assert seen[0] == ("release", False)


def test_popular_is_its_most_popular_titles_in_any_sort() -> None:
    service = CatalogService(PagedTMDBClient())
    top = BrowseCategory("popular", "Popular", limit=30)

    async def categories(_media_type: str) -> tuple[BrowseCategory, ...]:
        return (top,)

    service.categories = categories  # type: ignore[method-assign]
    query = BrowseQuery(media_type="all", category="popular", provider_ids=(8,))

    by_popularity = asyncio.run(service.browse("DE", query))
    by_release = asyncio.run(service.browse("DE", replace(query, sort="release")))
    second = asyncio.run(service.browse("DE", replace(query, sort="release", page=2)))
    beyond = asyncio.run(service.browse("DE", replace(query, page=9)))

    assert (by_popularity.total_pages, by_popularity.total_results) == (2, 30)
    assert by_release.total_results == 30
    reordered = [*by_release.items, *second.items]
    assert {item.id for item in reordered} == {
        item.id
        for item in [
            *by_popularity.items,
            *asyncio.run(service.browse("DE", replace(query, page=2))).items,
        ][:30]
    }
    dates = [item.release_date for item in reordered]
    assert dates == sorted(dates, reverse=True)
    assert beyond.page == 2

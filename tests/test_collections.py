from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from mytaste.catalog.models import BrowseQuery, MediaDetails
from mytaste.catalog.tmdb import TMDBError
from mytaste.collections.models import (
    SMART_COLLECTIONS,
    CollectionItem,
    smart_categories,
    smart_collection,
)
from mytaste.collections.service import CollectionService
from mytaste.storage.collections import CollectionRepository


def make_repository(tmp_path: Path) -> CollectionRepository:
    repository = CollectionRepository(tmp_path / "mytaste.db")
    repository.initialize()
    return repository


def item(media_type: str, tmdb_id: int, title: str, **values: object) -> CollectionItem:
    values.setdefault("added_at", "")
    return CollectionItem(media_type=media_type, tmdb_id=tmdb_id, title=title, **values)  # type: ignore[arg-type]


class FakeCatalog:
    """Netflix (8) carries movies 1 and 2, Disney Plus (337) the series; 404 cannot be checked."""

    def __init__(self) -> None:
        self.availability_calls: list[tuple[str, int]] = []

    async def details(self, media_type: str, item_id: int) -> MediaDetails:
        if item_id == 500:
            raise TMDBError("TMDB is down")
        return MediaDetails(
            id=item_id,
            media_type=media_type,  # type: ignore[arg-type]
            title=f"Title {item_id}",
            release_date="2020-05-01",
            overview="An overview.",
            rating=7.5,
            poster_path="/poster.jpg",
            genres=("Drama", "Crime", "Mystery"),
            genre_ids=(18, 80, 9648),
            popularity=12.5,
        )

    async def available_provider_ids(
        self, region: str, media_type: str, item_id: int
    ) -> frozenset[int]:
        assert region == "DE"
        self.availability_calls.append((media_type, item_id))
        if item_id == 404:
            raise TMDBError("Availability unavailable")
        if media_type == "tv":
            return frozenset({337})
        return frozenset({8}) if item_id in {1, 2} else frozenset()


class FakeLibrary:
    def matched_keys(
        self, library_ids: tuple[int, ...] | None = None
    ) -> frozenset[tuple[str, int]]:
        keys = {1: {("movie", 3)}, 2: {("tv", 10)}}
        chosen = keys if library_ids is None else {key: keys[key] for key in library_ids}
        return frozenset(key for group in chosen.values() for key in group)


def test_default_collections_are_created_once(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    defaults = repository.list()
    assert [(c.name, c.icon, c.default_sort) for c in defaults] == [
        ("Watchlist", "bookmark", "added"),
        ("My favourites", "heart", "added"),
    ]
    assert repository.delete(defaults[0].id) is True
    repository.initialize()

    assert [c.name for c in repository.list()] == ["My favourites"], "deleting one is final"


def test_collections_are_created_edited_and_validated(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    created = repository.create("  Weekend   picks ", description="Light")
    assert (created.name, created.position, created.key) == ("Weekend picks", 2, str(created.id))
    updated = repository.update(
        created.id, name="Weekend", description="Short ones", icon="clock", default_sort="rating"
    )
    assert updated is not None
    assert (updated.name, updated.description, updated.icon, updated.default_sort) == (
        "Weekend",
        "Short ones",
        "clock",
        "rating",
    )
    assert repository.update(999, name="Missing") is None
    with pytest.raises(ValueError, match="name"):
        repository.create("   ")
    with pytest.raises(ValueError, match="icon"):
        repository.create("Odd", icon="unicorn")
    with pytest.raises(ValueError, match="sort"):
        repository.create("Odd", default_sort="random")
    with pytest.raises(ValueError, match="60"):
        repository.create("x" * 61)
    assert [c.name for c in repository.list()][-1] == "Weekend"


def test_titles_are_saved_once_and_removed_with_their_collection(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    watchlist, favourites = repository.list()

    first = item("movie", 1, "First", added_at="2026-10-01T10:00:00+00:00", genres=("Drama",))
    assert repository.add_item(watchlist.id, first) is True
    assert repository.add_item(watchlist.id, first) is False
    assert repository.add_item(favourites.id, first) is True
    repository.add_item(watchlist.id, item("tv", 10, "Show", added_at=first.added_at))
    with pytest.raises(ValueError, match="TMDB"):
        repository.add_item(watchlist.id, item("movie", 0, "Unmatched"))

    stored = repository.items(watchlist.id)
    assert [entry.title for entry in stored] == ["First", "Show"], "ties keep insertion order"
    assert stored[0].genres == ("Drama",)
    assert repository.get(watchlist.id).item_count == 2  # type: ignore[union-attr]
    assert repository.memberships("movie", 1) == {watchlist.id, favourites.id}

    assert repository.remove_item(watchlist.id, "movie", 1) is True
    assert repository.remove_item(watchlist.id, "movie", 1) is False
    repository.delete(watchlist.id)
    assert repository.memberships("movie", 1) == {favourites.id}
    with sqlite3.connect(tmp_path / "mytaste.db") as connection:
        count = connection.execute("SELECT COUNT(*) FROM collection_items").fetchone()[0]
    assert count == 1, "deleting a collection deletes its titles"


def test_availability_is_stored_per_region(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    repository.save_availability("DE", [(("movie", 1), frozenset({8, 337}))])
    repository.save_availability(
        "DE", [(("movie", 1), frozenset({8}))], checked_at="2026-01-01T00:00:00+00:00"
    )

    assert repository.availability("DE", [("movie", 1), ("movie", 2)]) == {
        ("movie", 1): (frozenset({8}), "2026-01-01T00:00:00+00:00")
    }
    assert repository.availability("US", [("movie", 1)]) == {}


def make_service(tmp_path: Path) -> tuple[CollectionService, FakeCatalog, int]:
    repository = make_repository(tmp_path)
    catalog = FakeCatalog()
    service = CollectionService(repository, catalog, FakeLibrary(), page_size=2)
    watchlist = repository.list()[0].id
    titles = (
        item("movie", 1, "Zodiac", added_at="2026-10-01", release_date="2007-03-02", rating=7.7),
        item("movie", 2, "Arrival", added_at="2026-10-03", release_date="2016-11-11", rating=7.6),
        item("movie", 3, "On disk", added_at="2026-10-02", release_date="1999-01-01", rating=0),
        item("movie", 4, "Nowhere", added_at="2026-10-04", release_date="2020-01-01", rating=6),
        item("tv", 10, "Series", added_at="2026-10-05", release_date="2019-01-01", rating=8.4),
        item("movie", 404, "Unknown", added_at="2026-09-01", release_date="2001-01-01", rating=5),
    )
    for title in titles:
        repository.add_item(watchlist, title)
    return service, catalog, watchlist


def browse(service: CollectionService, collection_id: int, **query: object):
    collection = service.get(collection_id)
    assert collection is not None
    return asyncio.run(
        service.browse(
            collection,
            BrowseQuery(provider_ids=(8, 337), library_ids=(1, 2), **query),  # type: ignore[arg-type]
            region="DE",
            provider_ids=(8, 337),
            library_ids=(1, 2),
        )
    )


def all_titles(service: CollectionService, collection_id: int, **query: object) -> list[str]:
    first = browse(service, collection_id, **query)
    titles = [entry.title for entry in first.page.items]
    for number in range(2, first.page.total_pages + 1):
        titles += [e.title for e in browse(service, collection_id, page=number, **query).page.items]
    return titles


def test_collections_only_show_titles_on_the_users_services(tmp_path: Path) -> None:
    service, catalog, watchlist = make_service(tmp_path)

    result = browse(service, watchlist)

    assert (result.available, result.total) == (5, 6), "Nowhere streams on none of them"
    assert (result.page.total_results, result.page.total_pages) == (5, 3)
    assert [entry.title for entry in result.page.items] == ["Series", "Arrival"], "newest first"
    assert all_titles(service, watchlist) == ["Series", "Arrival", "On disk", "Zodiac", "Unknown"]
    on_disk = browse(service, watchlist, page=2).page.items[0]
    assert on_disk.in_library is True
    assert set(catalog.availability_calls) == {
        ("movie", 1),
        ("movie", 2),
        ("movie", 3),
        ("movie", 4),
        ("tv", 10),
        ("movie", 404),
    }

    catalog.availability_calls.clear()
    browse(service, watchlist)
    assert catalog.availability_calls == [("movie", 404)], "stored results are reused"


def test_collection_filters_sources_and_sorts_apply_on_top(tmp_path: Path) -> None:
    service, _catalog, watchlist = make_service(tmp_path)

    assert all_titles(service, watchlist, media_type="tv") == ["Series"]
    assert browse(service, watchlist, media_type="tv").total == 1
    assert all_titles(service, watchlist, sort="title") == [
        "Arrival",
        "On disk",
        "Series",
        "Unknown",
        "Zodiac",
    ]
    assert all_titles(service, watchlist, sort="rating") == [
        "Series",
        "Zodiac",
        "Arrival",
        "Unknown",
        "On disk",
    ], "unrated titles go last"
    assert all_titles(service, watchlist, sort="added", descending=False) == [
        "Unknown",
        "Zodiac",
        "On disk",
        "Arrival",
        "Series",
    ]
    assert all_titles(service, watchlist, year_from=2010) == ["Series", "Arrival"]
    assert all_titles(service, watchlist, minimum_rating=7.0) == ["Series", "Arrival", "Zodiac"]
    assert all_titles(service, watchlist, minimum_rating=7.0, include_unrated=True) == [
        "Series",
        "Arrival",
        "On disk",
        "Zodiac",
    ]

    collection = service.get(watchlist)
    assert collection is not None
    netflix_only = asyncio.run(
        service.browse(
            collection,
            BrowseQuery(provider_ids=(8,), library_ids=()),
            region="DE",
            provider_ids=(8, 337),
            library_ids=(1, 2),
        )
    )
    assert [entry.title for entry in netflix_only.page.items] == ["Arrival", "Zodiac"]
    assert (netflix_only.available, netflix_only.total) == (5, 6), "counts ignore narrowing"
    library_only = asyncio.run(
        service.browse(
            collection,
            BrowseQuery(provider_ids=(), library_ids=(1, 2)),
            region="",
            provider_ids=(),
            library_ids=(1, 2),
        )
    )
    assert [entry.title for entry in library_only.page.items] == ["Series", "On disk"]
    assert (library_only.available, library_only.total) == (2, 6)


def test_saving_a_title_stores_its_details_and_where_it_streams(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    catalog = FakeCatalog()
    service = CollectionService(repository, catalog, FakeLibrary())
    watchlist = repository.list()[0].id

    added = asyncio.run(service.add_item(watchlist, "movie", 2, region="DE"))
    again = asyncio.run(service.add_item(watchlist, "movie", 2, region="DE"))

    assert (added, again) == (True, False)
    saved = repository.items(watchlist)[0]
    assert (saved.title, saved.rating, saved.popularity, saved.genre_ids) == (
        "Title 2",
        7.5,
        12.5,
        (18, 80, 9648),
    )
    assert saved.to_catalog_item().genres == ("Drama", "Crime")
    assert repository.availability("DE", [("movie", 2)])[("movie", 2)][0] == {8}
    with pytest.raises(LookupError):
        asyncio.run(service.add_item(999, "movie", 2))
    with pytest.raises(TMDBError):
        asyncio.run(service.add_item(watchlist, "movie", 500))


def test_stale_availability_is_refreshed_in_the_background(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    catalog = FakeCatalog()
    service = CollectionService(repository, catalog, FakeLibrary())
    watchlist = repository.list()[0].id
    repository.add_item(watchlist, item("movie", 1, "Zodiac"))
    repository.save_availability(
        "DE", [(("movie", 1), frozenset())], checked_at="2020-01-01T00:00:00+00:00"
    )
    collection = service.get(watchlist)
    assert collection is not None

    async def run() -> tuple[int, int]:
        query = BrowseQuery(provider_ids=(8,))
        first = await service.browse(
            collection, query, region="DE", provider_ids=(8,), library_ids=()
        )
        assert service._refresh is not None
        await service._refresh
        second = await service.browse(
            collection, query, region="DE", provider_ids=(8,), library_ids=()
        )
        await service.stop()
        return first.available, second.available

    assert asyncio.run(run()) == (0, 1), "the stored answer is used until the refresh lands"


def test_smart_collections_resolve_genres_per_media_type() -> None:
    from mytaste.catalog.models import Genre

    movie_genres = (Genre(53, "Thriller", "movie"), Genre(35, "Comedy", "movie"))
    tv_genres = (Genre(35, "Comedy", "tv"),)

    all_media = {c.slug: c for c in smart_categories("all", movie_genres, tv_genres)}
    series = {c.slug for c in smart_categories("tv", movie_genres, tv_genres)}

    assert list(all_media)[:2] == ["popular", "latest"]
    assert (all_media["thriller"].movie_genre_id, all_media["thriller"].tv_genre_id) == (53, None)
    assert "thriller" not in series and "comedy" in series
    assert "drama" not in all_media, "genres TMDB does not list are left out"
    assert smart_collection("thriller").supports("tv") is False  # type: ignore[union-attr]
    assert smart_collection("popular").supports("tv") is True  # type: ignore[union-attr]
    assert len({c.slug for c in SMART_COLLECTIONS}) == len(SMART_COLLECTIONS)
    assert not any(c.slug.isdigit() for c in SMART_COLLECTIONS), "digits are user collections"

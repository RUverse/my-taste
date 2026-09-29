from __future__ import annotations

from pathlib import Path

import pytest

from mytaste.catalog.models import BrowseCategory, BrowseQuery
from mytaste.library.models import ScannedFile
from mytaste.storage.library import ItemDraft, LibraryRepository


def make_file(path: str, season: int | None = None, episode: int | None = None) -> ScannedFile:
    return ScannedFile(
        path=path,
        size=10,
        modified_at="2026-09-01T10:00:00+00:00",
        group_key="unused",
        titles=("Title",),
        season=season,
        episode=episode,
    )


def test_libraries_are_persisted_and_unique(tmp_path: Path) -> None:
    repository = LibraryRepository(tmp_path / "data" / "mytaste.db")
    repository.initialize()

    library = repository.add_library("Movies", "/media/movies", "movie")

    assert library.name == "Movies"
    assert library.last_scanned_at is None
    assert repository.list_libraries() == (library,)
    with pytest.raises(ValueError, match="already"):
        repository.add_library("Again", "/media/movies", "movie")
    with pytest.raises(ValueError, match="name"):
        repository.add_library("   ", "/media/other", "movie")
    with pytest.raises(ValueError, match="movies or TV"):
        repository.add_library("Other", "/media/other", "music")
    assert repository.remove_library(library.id) is True
    assert repository.list_libraries() == ()


def test_replace_items_and_browse(tmp_path: Path) -> None:
    repository = LibraryRepository(tmp_path / "mytaste.db")
    repository.initialize()
    movies = repository.add_library("Movies", "/media/movies", "movie")
    shows = repository.add_library("Shows", "/media/shows", "tv")

    repository.replace_items(
        movies.id,
        [
            ItemDraft(
                group_key="movie:parasite:2019",
                title="Parasite",
                files=(make_file("/media/movies/Parasite.2019.mkv"),),
                year=2019,
                tmdb_id=496243,
                release_date="2019-05-30",
                overview="A poor family schemes.",
                rating=8.5,
                poster_path="/parasite.jpg",
                genre_ids=(35, 53, 18),
                popularity=90.0,
            ),
            ItemDraft(
                group_key="movie:dane anjir maabed:",
                title="Dane Anjir Maabed",
                files=(make_file("/media/movies/Dane-Anjir-Maabed-1080.mp4"),),
            ),
        ],
        scanned_at="2026-09-10T08:00:00+00:00",
    )
    repository.replace_items(
        shows.id,
        [
            ItemDraft(
                group_key="tv:folder:dark",
                title="Dark",
                files=(
                    make_file("/media/shows/Dark/S01/E01.mkv", 1, 1),
                    make_file("/media/shows/Dark/S01/E02.mkv", 1, 2),
                    make_file("/media/shows/Dark/S03/E01.mkv", 3, 1),
                ),
                year=2017,
                tmdb_id=70523,
                release_date="2017-12-01",
                rating=8.4,
                genre_ids=(18, 9648),
                popularity=60.0,
            )
        ],
    )

    stored_movies = repository.get_library(movies.id)
    assert stored_movies is not None
    assert stored_movies.item_count == 2
    assert stored_movies.unmatched_count == 1
    assert stored_movies.last_scanned_at == "2026-09-10T08:00:00+00:00"
    stored_shows = repository.get_library(shows.id)
    assert stored_shows is not None
    assert stored_shows.file_count == 3

    items = repository.items(shows.id)
    assert items[0].season_count == 2
    assert items[0].episode_count == 3
    assert items[0].summary == "2 seasons · 3 episodes"
    assert repository.matched_keys() == {("movie", 496243), ("tv", 70523)}
    assert repository.genre_ids("movie") == {35: 1, 53: 1, 18: 1}

    popular = repository.browse(
        BrowseQuery(category="popular"), BrowseCategory("popular", "Popular")
    )
    assert [item.title for item in popular.items] == ["Parasite", "Dark", "Dane Anjir Maabed"]
    assert popular.items[0].id == 496243
    assert popular.items[2].id == 0
    assert popular.total_results == 3

    drama = repository.browse(
        BrowseQuery(category="drama"),
        BrowseCategory("drama", "Drama", movie_genre_id=18, tv_genre_id=18),
    )
    assert [item.title for item in drama.items] == ["Parasite", "Dark"]

    tv_only = repository.browse(
        BrowseQuery(media_type="tv", category="alphabetical"),
        BrowseCategory("alphabetical", "A–Z"),
    )
    assert [item.title for item in tv_only.items] == ["Dark"]

    searched = repository.browse(
        BrowseQuery(search="para", category="recent"),
        BrowseCategory("recent", "Recently Added"),
    )
    assert [item.title for item in searched.items] == ["Parasite"]

    rated = repository.browse(
        BrowseQuery(minimum_rating=8.0, category="recent"),
        BrowseCategory("recent", "Recently Added"),
    )
    assert {item.title for item in rated.items} == {"Parasite", "Dark"}

    unrated = repository.browse(
        BrowseQuery(minimum_rating=9.0, include_unrated=True),
        BrowseCategory("recent", "Recently Added"),
    )
    assert [item.title for item in unrated.items] == ["Dane Anjir Maabed"]

    scoped = repository.browse(
        BrowseQuery(library_ids=(shows.id,)),
        BrowseCategory("recent", "Recently Added"),
    )
    assert [item.title for item in scoped.items] == ["Dark"]

    paged = repository.browse(
        BrowseQuery(category="alphabetical", page=2),
        BrowseCategory("alphabetical", "A–Z"),
        page_size=2,
    )
    assert paged.page == 2
    assert paged.total_pages == 2
    assert [item.title for item in paged.items] == ["Parasite"]

    repository.replace_items(movies.id, [])
    assert repository.get_library(movies.id).item_count == 0  # type: ignore[union-attr]
    assert repository.matched_keys() == {("tv", 70523)}

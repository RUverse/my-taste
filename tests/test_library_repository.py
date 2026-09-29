from __future__ import annotations

import sqlite3
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

    library = repository.add_library("Media", [("/media/movies", "movie"), ("/media/tv", "tv")])

    assert library.name == "Media"
    assert library.last_scanned_at is None
    assert [(folder.path, folder.media_type) for folder in library.folders] == [
        ("/media/movies", "movie"),
        ("/media/tv", "tv"),
    ]
    assert library.media_label == "Movies & TV Shows"
    assert repository.list_libraries() == (library,)
    with pytest.raises(ValueError, match="already"):
        repository.add_library("Again", [("/media/movies", "movie")])
    assert len(repository.list_libraries()) == 1, "a failed folder insert rolls back the library"
    with pytest.raises(ValueError, match="twice"):
        repository.add_library("Again", [("/media/a", "movie"), ("/media/a", "tv")])
    with pytest.raises(ValueError, match="name"):
        repository.add_library("   ", [("/media/other", "movie")])
    with pytest.raises(ValueError, match="movies or TV"):
        repository.add_library("Other", [("/media/other", "music")])
    with pytest.raises(ValueError, match="folder"):
        repository.add_library("Other", [])

    extended = repository.add_folders(library.id, [("/media/anime", "tv")])
    assert [folder.path for folder in extended.folders][-1] == "/media/anime"
    first, *others = extended.folders
    assert repository.remove_folder(library.id, first.id) is True
    assert repository.remove_folder(library.id, first.id) is False
    assert repository.remove_folder(library.id, others[0].id) is True
    with pytest.raises(ValueError, match="at least one folder"):
        repository.remove_folder(library.id, others[1].id)
    assert repository.get_library(library.id).media_label == "TV Shows"  # type: ignore[union-attr]

    assert repository.rename_library(library.id, "  Home   drive ") is True
    assert repository.get_library(library.id).name == "Home drive"  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="name"):
        repository.rename_library(library.id, "   ")
    assert repository.rename_library(999, "Missing") is False

    assert repository.remove_library(library.id) is True
    assert repository.list_libraries() == ()


def test_replace_items_and_browse(tmp_path: Path) -> None:
    repository = LibraryRepository(tmp_path / "mytaste.db")
    repository.initialize()
    movies = repository.add_library("Movies", [("/media/movies", "movie")])
    shows = repository.add_library("Shows", [("/media/shows", "tv")])

    repository.replace_items(
        movies.id,
        [
            ItemDraft(
                group_key="movie:parasite:2019",
                media_type="movie",
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
                media_type="movie",
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
                media_type="tv",
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


def test_single_folder_libraries_are_migrated(tmp_path: Path) -> None:
    database = tmp_path / "mytaste.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
                created_at TEXT NOT NULL,
                last_scanned_at TEXT,
                last_error TEXT
            );
            CREATE TABLE library_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
                media_type TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
                group_key TEXT NOT NULL,
                title TEXT NOT NULL,
                year INTEGER,
                tmdb_id INTEGER,
                release_date TEXT NOT NULL DEFAULT '',
                overview TEXT NOT NULL DEFAULT '',
                rating REAL NOT NULL DEFAULT 0,
                poster_path TEXT,
                genre_ids TEXT NOT NULL DEFAULT ',',
                popularity REAL NOT NULL DEFAULT 0,
                added_at TEXT NOT NULL DEFAULT '',
                file_count INTEGER NOT NULL DEFAULT 0,
                season_count INTEGER NOT NULL DEFAULT 0,
                episode_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE (library_id, group_key)
            );
            INSERT INTO libraries (id, name, path, media_type, created_at, last_scanned_at)
            VALUES
                (1, 'Movies', '/media/movies', 'movie', '2026-09-01', '2026-09-02'),
                (3, 'Shows', '/media/shows', 'tv', '2026-09-01', NULL);
            INSERT INTO library_items
                (library_id, media_type, group_key, title, tmdb_id, file_count)
            VALUES (1, 'movie', 'movie:parasite:2019', 'Parasite', 496243, 1),
                   (3, 'tv', 'tv:folder:dark', 'Dark', 70523, 26);
            """
        )
    connection.close()

    repository = LibraryRepository(database)
    repository.initialize()
    repository.initialize()

    movies, shows = repository.list_libraries()
    assert (movies.id, movies.name, movies.last_scanned_at) == (1, "Movies", "2026-09-02")
    assert [(folder.path, folder.media_type) for folder in movies.folders] == [
        ("/media/movies", "movie")
    ]
    assert [(folder.path, folder.media_type) for folder in shows.folders] == [
        ("/media/shows", "tv")
    ]
    assert (shows.id, shows.show_count, shows.episode_count) == (3, 1, 26)
    assert repository.matched_keys() == {("movie", 496243), ("tv", 70523)}

    added = repository.add_library("New", [("/media/new", "movie")])
    assert added.id == 4, "ids keep counting after the table rebuild"
    assert repository.remove_library(shows.id) is True
    assert repository.matched_keys() == {("movie", 496243)}, "items still cascade"
    with pytest.raises(ValueError, match="already"):
        repository.add_folders(movies.id, [("/media/movies", "tv")])

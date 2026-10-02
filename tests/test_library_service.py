from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mytaste.catalog.models import BrowseQuery, CatalogItem, Genre
from mytaste.catalog.tmdb import TMDBError
from mytaste.library.service import LibraryService
from mytaste.storage.library import LibraryRepository

KNOWN = {
    ("movie", "parasite", 2019): CatalogItem(
        id=496243,
        media_type="movie",
        title="Parasite",
        release_date="2019-05-30",
        overview="A poor family schemes.",
        rating=8.5,
        poster_path="/parasite.jpg",
        genre_ids=(35, 18),
        popularity=90.0,
    ),
    ("tv", "silicon valley", None): CatalogItem(
        id=60573,
        media_type="tv",
        title="Silicon Valley",
        release_date="2014-04-06",
        overview="Startup life.",
        rating=8.2,
        genre_ids=(35,),
        popularity=40.0,
    ),
}


class FakeCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int | None]] = []
        self.fail = False

    async def genres(self, media_type: str) -> tuple[Genre, ...]:
        return (Genre(35, "Comedy", media_type), Genre(18, "Drama", media_type))  # type: ignore[arg-type]

    async def match_title(
        self, media_type: str, title: str, year: int | None = None
    ) -> CatalogItem | None:
        self.calls.append((media_type, title, year))
        if self.fail:
            raise TMDBError("TMDB is down")
        return KNOWN.get((media_type, title.casefold(), year))


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")


def make_service(tmp_path: Path, catalog: FakeCatalog, **kwargs: object) -> LibraryService:
    repository = LibraryRepository(tmp_path / "mytaste.db")
    repository.initialize()
    return LibraryService(repository, catalog, rescan_interval=0, **kwargs)  # type: ignore[arg-type]


def test_scan_matches_groups_and_browses(tmp_path: Path) -> None:
    movies = tmp_path / "Movies"
    touch(movies / "Parasite.2019.1080p.mkv")
    touch(movies / "Dane-Anjir-Maabed-1080.mp4")
    shows = tmp_path / "TV Shows"
    touch(shows / "Sillicon Valley" / "s06" / "Silicon Valley S06 E01 720p.mkv")
    touch(shows / "Sillicon Valley" / "s06" / "Silicon Valley S06 E02 720p.mkv")
    catalog = FakeCatalog()

    async def run() -> None:
        service = make_service(tmp_path, catalog)
        movie_library = service.add("", [(str(movies), "movie")])
        show_library = service.add("Shows", [(str(shows), "tv")])
        assert movie_library.name == "Movies", "a single folder names the library"
        await asyncio.gather(service.scan(movie_library.id), service.scan(show_library.id))

        status = service.status(movie_library.id)
        assert status.state == "idle"
        assert status.scanned_files == 2
        assert status.matched_items == 1
        assert status.total_items == 2

        stored = service.library(show_library.id)
        assert stored is not None
        assert stored.item_count == 1
        assert stored.file_count == 2
        assert stored.unmatched_count == 0
        assert service.matched_keys() == {("movie", 496243), ("tv", 60573)}

        page = await service.browse(BrowseQuery(category="popular"))
        assert [item.title for item in page.items] == [
            "Parasite",
            "Silicon Valley",
            "Dane Anjir Maabed",
        ]
        assert page.items[0].genres == ("Comedy", "Drama")
        assert page.items[1].library_summary == "1 season · 2 episodes"
        assert page.items[2].id == 0

        categories = await service.categories("all")
        assert [category.slug for category in categories] == [
            "popular",
            "latest",
            "comedy",
            "drama",
        ]

        # A rescan reuses existing matches instead of asking TMDB again.
        calls_before = len(catalog.calls)
        await service.scan(movie_library.id)
        new_calls = catalog.calls[calls_before:]
        assert all(title != "Parasite" for _, title, _ in new_calls)

        await service.stop()

    asyncio.run(run())
    # The misspelled folder name was retried with the filename-derived title.
    assert ("tv", "Sillicon Valley", None) in catalog.calls
    assert ("tv", "Silicon Valley", None) in catalog.calls


def test_scan_survives_tmdb_outage_and_missing_folder(tmp_path: Path) -> None:
    movies = tmp_path / "Movies"
    touch(movies / "Parasite.2019.mkv")
    catalog = FakeCatalog()
    catalog.fail = True

    async def run() -> None:
        service = make_service(tmp_path, catalog)
        library = service.add("Movies", [(str(movies), "movie")])
        status = await service.scan(library.id)
        assert status.state == "idle"
        assert "TMDB" in status.message
        stored = service.library(library.id)
        assert stored is not None
        assert stored.item_count == 1
        assert stored.unmatched_count == 1
        assert "TMDB" in (stored.last_error or "")

        (movies / "Parasite.2019.mkv").unlink()
        movies.rmdir()
        status = await service.scan(library.id)
        assert status.state == "error"
        stored = service.library(library.id)
        assert stored is not None
        assert stored.item_count == 1, "items are kept while the folder is unavailable"
        await service.stop()

    asyncio.run(run())


def test_one_library_scans_movie_and_show_folders(tmp_path: Path) -> None:
    movies = tmp_path / "drive" / "Movies"
    touch(movies / "Parasite.2019.1080p.mkv")
    shows = tmp_path / "drive" / "TV Shows"
    touch(shows / "Silicon Valley" / "Season 1" / "Silicon.Valley.S01E01.mkv")
    touch(shows / "Silicon Valley" / "Season 1" / "Silicon.Valley.S01E02.mkv")
    catalog = FakeCatalog()

    async def run() -> None:
        service = make_service(tmp_path, catalog)
        library = service.add("", [(str(movies), "movie"), (str(shows), "tv")])
        assert library.name == "Local library"
        await service.scan(library.id)

        stored = service.library(library.id)
        assert stored is not None
        assert (stored.movie_count, stored.show_count, stored.episode_count) == (1, 1, 2)
        assert stored.item_count == 2
        assert service.matched_keys() == {("movie", 496243), ("tv", 60573)}
        shows_only = await service.browse(BrowseQuery(media_type="tv", category="popular"))
        assert [item.title for item in shows_only.items] == ["Silicon Valley"]

        with pytest.raises(ValueError, match="already in the library"):
            service.add("Again", [(str(movies), "movie")])
        with pytest.raises(ValueError, match="overlaps"):
            service.add("Drive", [(str(tmp_path / "drive"), "movie")])
        with pytest.raises(ValueError, match="overlaps"):
            service.add_folders(library.id, [(str(shows / "Silicon Valley"), "tv")])

        # Removing a folder rescans the library without that folder's titles.
        movie_folder = next(folder for folder in stored.folders if folder.media_type == "movie")
        assert service.remove_folder(library.id, movie_folder.id) is True
        await asyncio.gather(*service._scan_tasks.values())
        stored = service.library(library.id)
        assert stored is not None
        assert (stored.movie_count, stored.show_count) == (0, 1)
        assert stored.media_label == "TV Shows"

        # Adding it back restores the movie, and existing show matches are reused.
        calls_before = len(catalog.calls)
        service.add_folders(library.id, [(str(movies), "movie")])
        await asyncio.gather(*service._scan_tasks.values())
        stored = service.library(library.id)
        assert stored is not None
        assert (stored.movie_count, stored.show_count) == (1, 1)
        assert all(media == "movie" for media, _, _ in catalog.calls[calls_before:])
        await service.stop()

    asyncio.run(run())


def test_folders_in_one_request_may_not_repeat_or_nest(tmp_path: Path) -> None:
    movies = tmp_path / "drive" / "Movies"
    movies.mkdir(parents=True)

    async def run() -> None:
        service = make_service(tmp_path, FakeCatalog())
        with pytest.raises(ValueError, match="already in this list"):
            service.add("", [(str(movies), "movie"), (str(movies), "tv")])
        with pytest.raises(ValueError, match="overlaps .* in this list"):
            service.add("", [(str(movies), "movie"), (str(tmp_path / "drive"), "tv")])
        assert service.libraries() == ()
        await service.stop()

    asyncio.run(run())


def test_changing_folders_replaces_a_running_scan(tmp_path: Path) -> None:
    movies = tmp_path / "Movies"
    touch(movies / "Parasite.2019.1080p.mkv")
    shows = tmp_path / "TV Shows"
    touch(shows / "Silicon Valley" / "Season 1" / "Silicon.Valley.S01E01.mkv")

    async def run() -> None:
        service = make_service(tmp_path, FakeCatalog())
        library = service.add("", [(str(movies), "movie")])
        first_scan = service._scan_tasks[library.id]
        service.add_folders(library.id, [(str(shows), "tv")])
        second_scan = service._scan_tasks[library.id]
        assert second_scan is not first_scan
        await asyncio.gather(first_scan, return_exceptions=True)
        assert first_scan.cancelled()
        assert service._scan_tasks.get(library.id) is second_scan, "old scan kept the new one"

        await second_scan
        stored = service.library(library.id)
        assert stored is not None
        assert (stored.movie_count, stored.show_count) == (1, 1)
        await service.stop()

    asyncio.run(run())


def test_path_validation_and_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    (allowed / "Movies").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    async def run() -> None:
        service = make_service(tmp_path, FakeCatalog(), roots=(allowed,))
        with pytest.raises(ValueError, match="absolute"):
            service.resolve_path("relative/path")
        with pytest.raises(ValueError, match="allowed folder"):
            service.resolve_path(str(outside))
        with pytest.raises(ValueError, match="does not exist"):
            service.resolve_path(str(allowed / "missing"))
        assert service.resolve_path(str(allowed / "Movies")) == (allowed / "Movies").resolve()

        listing = service.list_folders(None)
        assert [entry.path for entry in listing.entries] == [str(allowed)]
        listing = service.list_folders(str(allowed))
        assert [entry.name for entry in listing.entries] == ["Movies"]
        assert listing.parent == ""
        with pytest.raises(ValueError, match="outside"):
            service.list_folders(str(outside))
        await service.stop()

    asyncio.run(run())

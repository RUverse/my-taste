from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from mytaste.catalog.filters import GenreChoice, TitleFacts, genre_choices
from mytaste.catalog.models import (
    BrowseCategory,
    BrowseQuery,
    CastMember,
    CatalogItem,
    CatalogPage,
    Episode,
    Genre,
    MediaDetails,
    Provider,
    Region,
    Season,
    WatchOption,
)
from mytaste.catalog.service import LocalSource, Refine
from mytaste.catalog.tmdb import TMDBError
from mytaste.config import AppSettings
from mytaste.library.models import Library, LibraryFolder, LibraryStatus
from mytaste.library.service import FolderEntry, FolderListing
from mytaste.storage.library import TitleRef
from mytaste.web.app import create_app


class FakeCatalog:
    def __init__(self) -> None:
        self.browse_queries: list[BrowseQuery] = []
        self.browse_options: list[dict[str, object]] = []
        self.fail_browse = False
        self.everywhere_queries: list[BrowseQuery] = []

    async def genre_choices(self) -> tuple[GenreChoice, ...]:
        return genre_choices(
            (
                Genre(28, "Action", "movie"),
                Genre(18, "Drama", "movie"),
                Genre(53, "Thriller", "movie"),
            ),
            (
                Genre(10759, "Action & Adventure", "tv"),
                Genre(18, "Drama", "tv"),
                Genre(10762, "Kids", "tv"),
            ),
        )

    async def certifications(self, region: str) -> dict[str, tuple[str, ...]]:
        ratings = ("0", "6", "12", "16", "18")
        return {"movie": ratings, "tv": ratings} if region == "DE" else {"movie": (), "tv": ()}

    async def countries(self) -> tuple[tuple[str, str], ...]:
        return (("DE", "Germany"), ("KR", "South Korea"), ("US", "United States of America"))

    async def languages(self) -> tuple[tuple[str, str], ...]:
        return (("de", "German"), ("en", "English"), ("ko", "Korean"))

    async def name_of(self, kind: str, item_id: int) -> str:
        names = {("person", 525): "Christopher Nolan", ("keyword", 4379): "time travel"}
        return names.get((kind, item_id), "")

    async def search_people(self, query: str) -> tuple[tuple[int, str, str], ...]:
        return ((525, "Christopher Nolan", "Directing · Inception"),)

    async def search_keywords(self, query: str) -> tuple[tuple[int, str], ...]:
        return ((4379, "time travel"),)

    async def title_facts(self, media_type: str, item_id: int) -> TitleFacts:
        if item_id == 70523:
            return TitleFacts(original_language="de", countries=("DE",), runtime=55)
        return TitleFacts(original_language="en", countries=("US",), runtime=120, directors=(525,))

    async def regions(self) -> tuple[Region, ...]:
        return (Region("DE", "Germany"), Region("US", "United States"))

    async def providers(self, region: str) -> tuple[Provider, ...]:
        assert region in {"DE", "US"}
        netflix = Provider(8, "Netflix", "/netflix.jpg", 1)
        if region == "US":
            return (netflix,)
        return (netflix, Provider(337, "Disney Plus", "/disney.jpg", 2))

    async def categories(self, media_type: str) -> tuple[BrowseCategory, ...]:
        return (
            BrowseCategory("popular", "Popular", limit=200, icon="flame"),
            BrowseCategory("latest", "Latest", sort="release", released_within_days=365),
            BrowseCategory("drama", "Drama", 18, 18),
        )

    async def browse(
        self,
        region: str,
        query: BrowseQuery,
        *,
        local: LocalSource | None = None,
        refine: Refine | None = None,
        exclude: frozenset[tuple[str, int]] = frozenset(),
    ) -> CatalogPage:
        assert region == "DE"
        assert query.provider_ids and set(query.provider_ids) <= {8, 337}
        self.browse_queries.append(query)
        self.browse_options.append({"refine": refine, "exclude": exclude})
        if self.fail_browse:
            raise TMDBError("TMDB is down")
        if query.search == "elsewhere":
            return CatalogPage(items=(), total_pages=3)
        local_items = (await local(query, 20)).items if local is not None else ()
        return CatalogPage(
            items=(
                CatalogItem(
                    id=12,
                    media_type="movie",
                    title="A New Film",
                    release_date="2026-08-20",
                    overview="A test film.",
                    rating=7.2,
                    poster_path="/poster.jpg",
                    genre_ids=(18,),
                    genres=("Drama",),
                ),
                CatalogItem(
                    id=13,
                    media_type="tv",
                    title="A New Series",
                    release_date="2026-08-21",
                    overview="A test series.",
                    rating=8.1,
                ),
                *local_items,
            ),
            total_results=2 + len(local_items),
        )

    async def search_everywhere(
        self,
        region: str,
        query: BrowseQuery,
        *,
        refine: Refine | None = None,
        exclude: frozenset[tuple[str, int]] = frozenset(),
    ) -> CatalogPage:
        assert region == "DE"
        self.everywhere_queries.append(query)
        item = CatalogItem(
            id=77,
            media_type="tv",
            title="Somewhere Else",
            release_date="2024-01-02",
            overview="On another service.",
            rating=7.7,
        )
        return CatalogPage(items=(item,), total_results=1)

    async def people(self, media_type: str, item_id: int) -> tuple[str, tuple[str, ...]]:
        return ("Director", ("A Director",))

    async def available_provider_ids(
        self, region: str, media_type: str, item_id: int
    ) -> frozenset[int]:
        assert region == "DE"
        if item_id == 404:
            raise TMDBError("Availability unavailable")
        return frozenset({8, 337, 99}) if media_type == "movie" else frozenset({337})

    async def watch_options(
        self, region: str, media_type: str, item_id: int, provider_ids: tuple[int, ...]
    ) -> tuple[WatchOption, ...]:
        assert region == "DE"
        if item_id == 404:
            raise TMDBError("Availability unavailable")
        netflix = Provider(8, "Netflix", "/netflix.jpg", 1)
        return (
            (WatchOption(netflix, "https://www.netflix.com/title/12"),) if 8 in provider_ids else ()
        )

    async def seasons(self, item_id: int) -> tuple[Season, ...]:
        if item_id == 404:
            raise TMDBError("Seasons unavailable")
        return (
            Season(
                1,
                "Season 1",
                (
                    Episode(
                        1,
                        1,
                        "Pilot",
                        air_date="2026-01-01",
                        runtime_minutes=50,
                        still_path="/still.jpg",
                    ),
                    Episode(1, 2, "Second"),
                ),
            ),
        )

    async def details(self, media_type: str, item_id: int) -> MediaDetails:
        return MediaDetails(
            id=item_id,
            media_type=media_type,
            title="A New Film",
            release_date="2026-08-20",
            overview="A test film.",
            rating=7.2,
            runtime_minutes=126,
            poster_path="/poster.jpg",
            backdrop_path="/backdrop.jpg",
            genres=("Drama",),
            cast=(CastMember(4, "Lead Actor", "The Lead", "/actor.jpg"),),
            trailer_key="trailer-key",
            directed_by=("A Director",),
        )


class FakeLibrary:
    def __init__(self) -> None:
        self.items: list[Library] = []
        self.scan_requests: list[int] = []
        self.roots: tuple[Path, ...] = ()
        self.browse_calls: list[tuple[BrowseQuery, BrowseCategory | None, int]] = []
        self.item_id_calls: list[frozenset[int] | None] = []

    def title_refs(self, library_ids: tuple[int, ...]) -> tuple[TitleRef, ...]:
        return (
            TitleRef(1, "tv", 70523, (18, 10765)),
            TitleRef(2, "movie", None),
        )

    def libraries(self) -> tuple[Library, ...]:
        return tuple(self.items)

    def library(self, library_id: int) -> Library | None:
        return next((item for item in self.items if item.id == library_id), None)

    @property
    def has_libraries(self) -> bool:
        return bool(self.items)

    def status(self, library_id: int) -> LibraryStatus:
        return LibraryStatus(library_id=library_id)

    def statuses(self) -> tuple[LibraryStatus, ...]:
        return tuple(self.status(item.id) for item in self.items)

    def add(self, name: str, folders: list[tuple[str, str]]) -> Library:
        if not folders:
            raise ValueError("Choose a folder")
        if any(not path.startswith("/") for path, _ in folders):
            raise ValueError("Use an absolute folder path such as /mnt/media/Movies")
        library = Library(
            id=len(self.items) + 1,
            name=name or "Library",
            created_at="2026-09-10T08:00:00+00:00",
            folders=tuple(
                LibraryFolder(
                    id=len(self.items) * 10 + index + 1,
                    path=path,
                    media_type=media_type,  # type: ignore[arg-type]
                )
                for index, (path, media_type) in enumerate(folders)
            ),
            last_scanned_at="2026-09-10T08:05:00+00:00",
            item_count=2,
            file_count=3,
            movie_count=2 if any(media == "movie" for _, media in folders) else 0,
            show_count=1 if any(media == "tv" for _, media in folders) else 0,
            episode_count=2 if any(media == "tv" for _, media in folders) else 0,
        )
        self.items.append(library)
        self.scan_requests.append(library.id)
        return library

    def add_folders(self, library_id: int, folders: list[tuple[str, str]]) -> Library:
        library = self.library(library_id)
        if library is None:
            raise ValueError("Unknown library")
        if any(path == folder.path for path, _ in folders for folder in library.folders):
            raise ValueError(f"{folders[0][0]} is already in the library “{library.name}”")
        added = tuple(
            LibraryFolder(id=100 + len(library.folders) + index, path=path, media_type=media)  # type: ignore[arg-type]
            for index, (path, media) in enumerate(folders)
        )
        updated = replace(library, folders=library.folders + added)
        self.items = [updated if item.id == library_id else item for item in self.items]
        self.scan_requests.append(library_id)
        return updated

    def remove_folder(self, library_id: int, folder_id: int) -> bool:
        library = self.library(library_id)
        if library is None or all(folder.id != folder_id for folder in library.folders):
            return False
        if len(library.folders) == 1:
            raise ValueError("A library needs at least one folder; remove the library instead")
        updated = replace(
            library, folders=tuple(folder for folder in library.folders if folder.id != folder_id)
        )
        self.items = [updated if item.id == library_id else item for item in self.items]
        self.scan_requests.append(library_id)
        return True

    def rename(self, library_id: int, name: str) -> bool:
        cleaned = " ".join(name.split())
        if not cleaned:
            raise ValueError("Give the library a name")
        library = self.library(library_id)
        if library is None:
            return False
        updated = replace(library, name=cleaned)
        self.items = [updated if item.id == library_id else item for item in self.items]
        return True

    def remove(self, library_id: int) -> bool:
        before = len(self.items)
        self.items = [item for item in self.items if item.id != library_id]
        return len(self.items) != before

    def schedule_scan(self, library_id: int) -> bool:
        self.scan_requests.append(library_id)
        return True

    def list_folders(self, raw: str | None) -> FolderListing:
        if raw == "/nope":
            raise ValueError("That folder does not exist")
        return FolderListing(
            path="/media",
            parent="/",
            entries=(FolderEntry("Movies", "/media/Movies"),),
        )

    def matched_keys(
        self, library_ids: tuple[int, ...] | None = None
    ) -> frozenset[tuple[str, int]]:
        if library_ids is not None and not library_ids:
            return frozenset()
        return frozenset({("movie", 12)})

    def episode_keys(self, tmdb_id: int) -> frozenset[tuple[int, int]]:
        return frozenset({(1, 2)}) if tmdb_id == 13 else frozenset()

    async def categories(self, media_type: str) -> tuple[BrowseCategory, ...]:
        return (
            BrowseCategory("popular", "Popular", limit=200, icon="flame"),
            BrowseCategory("latest", "Latest", sort="release", released_within_days=365),
        )

    async def browse(
        self,
        query: BrowseQuery,
        *,
        category: BrowseCategory | None = None,
        page_size: int = 24,
        item_ids: frozenset[int] | None = None,
    ) -> CatalogPage:
        self.browse_calls.append((query, category, page_size))
        self.item_id_calls.append(item_ids)
        return CatalogPage(
            items=(
                CatalogItem(
                    id=70523,
                    media_type="tv",
                    title="Dark",
                    release_date="2017-12-01",
                    overview="Time travel.",
                    rating=8.4,
                    library_summary="2 seasons · 3 episodes",
                    in_library=True,
                ),
                CatalogItem(
                    id=0,
                    media_type="movie",
                    title="Dane Anjir Maabed",
                    release_date="",
                    overview="",
                    rating=0,
                    in_library=True,
                ),
            ),
            total_results=2,
        )


def make_client(
    tmp_path: Path,
    library: FakeLibrary | None = None,
    catalog: FakeCatalog | None = None,
) -> TestClient:
    settings = AppSettings(
        tmdb_token="test-token",
        database_path=tmp_path / "mytaste.db",
    )
    return TestClient(
        create_app(settings, catalog=catalog or FakeCatalog(), library=library or FakeLibrary())
    )


def test_unconfigured_app_redirects_to_onboarding(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.url.path == "/settings"
    assert "Where do you watch?" in response.text
    assert 'data-open-add="streaming"' in response.text
    assert 'data-open-add="local"' in response.text
    assert 'class="source-grid"' not in response.text


def test_onboarding_guesses_region_from_browser_language(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/settings", headers={"Accept-Language": "de-DE,de;q=0.9"})

    assert '<option value="DE" selected>' in response.text
    assert 'name="region" value="DE"' in response.text


def test_services_are_added_and_render_catalog(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        saved = client.post(
            "/settings/services",
            data={"region": "DE", "provider_ids": "8"},
            follow_redirects=False,
        )
        services = client.get("/settings")
        home = client.get("/")

    assert saved.status_code == 303
    assert saved.headers["location"] == "/settings"
    assert "Your services" in services.text
    assert 'action="/settings/services/8/remove"' in services.text
    assert 'name="provider_ids" value="8"' not in services.text, "enabled services leave the picker"
    assert 'name="provider_ids" value="337"' in services.text
    assert 'data-open-add="choose"' in services.text
    assert home.status_code == 200
    assert "A New Film" in home.text
    assert "A New Series" in home.text
    assert "Netflix" in home.text
    assert 'class="collection-tab collection-current" href="/" aria-current="page"' in home.text
    assert 'data-collection="popular"' not in home.text, "the open collection leads the bar"
    assert '<h1 id="collection-heading">' in home.text
    assert "<span>Popular</span></h1>" in home.text
    assert 'id="browse-sidebar"' in home.text
    assert 'data-sidebar="open"' in home.text
    assert "data-sidebar-toggle" in home.text
    assert 'id="sources-heading">' in home.text
    assert "<span>Services</span>" in home.text
    assert 'class="source-logos" data-disclosure-summary="sources"' in home.text
    assert 'aria-label="Remove Netflix"' not in home.text, "services are only hidden here"
    assert "/settings/services/8/remove" not in home.text
    assert 'href="/settings?next=/">Manage</a>' in home.text
    assert 'aria-label="Show only Netflix"' not in home.text, "the only service needs no Only"
    assert 'class="services-link"' not in home.text, "services moved into the sidebar"
    assert 'data-rail-section="sidebar-sources"' in home.text
    assert "controls-drawer" not in home.text
    assert "<h1>Latest</h1>" not in home.text
    assert 'class="active-services"' not in home.text
    assert "Save display" not in home.text
    assert 'id="media-details"' in home.text
    assert 'data-detail-url="/api/items/movie/12/details"' in home.text
    assert "data-detail-sound" in home.text
    assert "data-detail-save" in home.text
    assert "View on TMDB" not in home.text
    assert 'data-autoplay-trailer="true"' in home.text
    assert 'name="autoplay_trailer" checked' in home.text
    assert 'data-overview="A test film."' in home.text
    assert "data-genres='[\"Drama\"]'" in home.text


def test_adding_services_requires_a_selection(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post("/settings/services", data={"region": "DE"})
        unknown = client.post("/settings/services", data={"region": "XX", "provider_ids": "8"})

    assert response.status_code == 422
    assert "Choose at least one streaming service" in response.text
    assert 'data-open-step="streaming"' in response.text
    assert unknown.status_code == 422
    assert "supported country" in unknown.text


def test_services_are_merged_removed_and_pruned_by_region(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        preferences = client.app.state.preferences
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        client.post("/settings/services", data={"region": "DE", "provider_ids": "337"})
        merged = preferences.get().provider_ids
        client.post("/settings/region", data={"region": "US"})
        pruned = preferences.get()
        removed = client.post("/settings/services/8/remove", follow_redirects=False)
        emptied = preferences.get()
        invalid_region = client.post("/settings/region", data={"region": "XX"})
        home = client.get("/")

    assert merged == (8, 337)
    assert (pruned.region, pruned.provider_ids) == ("US", (8,))
    assert removed.status_code == 303
    assert emptied.provider_ids == ()
    assert invalid_region.status_code == 422
    assert "supported country" in invalid_region.text
    assert home.url.path == "/settings", "no services and no folders returns to onboarding"


def test_display_preferences_are_saved(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/preferences/display",
            json={
                "show_year": False,
                "show_rating": True,
                "show_media_type": False,
                "show_genres": True,
                "show_people": True,
                "card_size": "compact",
                "autoplay_trailer": False,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "saved"}

    with make_client(tmp_path) as client:
        assert client.app.state.preferences.get_display().autoplay_trailer is False


def test_sidebar_state_is_saved_without_touching_other_display_options(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        client.post("/api/preferences/display", json={"card_size": "compact"})
        collapsed = client.post("/api/preferences/display", json={"sidebar_open": False})
        home = client.get("/")
        invalid = client.post("/api/preferences/display", json={"sidebar_open": "no"})
        display = client.app.state.preferences.get_display()

    assert collapsed.status_code == 200
    assert display.sidebar_open is False
    assert display.card_size == "compact"
    assert display.show_year is True
    assert 'data-sidebar="closed"' in home.text
    assert 'aria-labelledby="collection-heading" inert' in home.text
    assert '<nav class="sidebar-rail" aria-label="Sidebar" >' in home.text, "the rail is usable"
    assert 'aria-expanded="false" aria-label="Show the sidebar"' in home.text
    assert invalid.status_code == 422


def test_service_icons_are_an_opt_in_card_option(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": ["8", "337"]})
        before = client.get("/")
        client.post("/api/preferences/display", json={"show_providers": True})
        after = client.get("/")
        providers = client.get(
            "/api/items/providers", params={"items": "movie:12,tv:13,tv:404,x:1"}
        )
        empty = client.get("/api/items/providers")

    assert 'data-show-providers="false"' in before.text
    assert 'name="show_providers" >' in before.text
    assert 'data-show-providers="true"' in after.text
    assert 'name="show_providers" checked' in after.text
    assert 'data-providers-key="movie:12">' in after.text
    assert 'data-providers-key="tv:13">' in after.text
    assert "data-providers-text" in after.text
    assert providers.json() == {
        "providers": {
            "movie:12": [
                {
                    "id": 8,
                    "name": "Netflix",
                    "logo_url": "https://image.tmdb.org/t/p/w92/netflix.jpg",
                },
                {
                    "id": 337,
                    "name": "Disney Plus",
                    "logo_url": "https://image.tmdb.org/t/p/w92/disney.jpg",
                },
            ],
            "tv:13": [
                {
                    "id": 337,
                    "name": "Disney Plus",
                    "logo_url": "https://image.tmdb.org/t/p/w92/disney.jpg",
                }
            ],
        },
        "any": {},
    }, "only enabled services, in priority order; failed lookups are skipped"
    assert empty.json() == {"providers": {}, "any": {}}


def test_a_search_without_matches_shows_results_from_other_services(tmp_path: Path) -> None:
    catalog = FakeCatalog()
    with make_client(tmp_path, catalog=catalog) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        client.post("/api/preferences/display", json={"show_providers": True})
        missing = client.get("/?q=elsewhere")
        found = client.get("/?q=dune")
        carriers = client.get("/api/items/providers", params={"items": "tv:13", "any": "tv:13"})

    assert "No match found" in missing.text
    assert "From other services:" in missing.text
    assert "Somewhere Else" in missing.text
    assert 'data-providers-key="tv:77" data-providers-scope="any"' in missing.text
    assert "data-next-page" not in missing.text, "other services' results are a single page"
    assert [query.search for query in catalog.everywhere_queries] == ["elsewhere"]
    assert "From other services:" not in found.text
    assert carriers.json() == {
        "providers": {"tv:13": []},
        "any": {
            "tv:13": [
                {
                    "id": 337,
                    "name": "Disney Plus",
                    "logo_url": "https://image.tmdb.org/t/p/w92/disney.jpg",
                }
            ]
        },
    }, "titles from elsewhere list every service, not only the enabled ones"


def test_local_titles_show_a_folder_in_the_source_strip(tmp_path: Path) -> None:
    library = FakeLibrary()
    library.add("Shows", [("/media/Shows", "tv")])
    with make_client(tmp_path, library) as client:
        home = client.get("/")
        providers = client.get("/api/items/providers", params={"items": "movie:12"})

    assert 'name="show_providers"' in home.text, "the Sources option also covers folders"
    assert "data-providers-key" not in home.text, "no service lookups without streaming"
    assert home.text.count('<span class="source-local">') == 2
    assert "library-badge" not in home.text
    assert '<p class="sr-only" data-providers-text>In your local library</p>' in home.text
    assert providers.json() == {"providers": {}, "any": {}}


def test_sidebar_filters_show_presets_and_removable_chips(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        home = client.get("/?media=all&year_from=2010&year_to=2019&rating_min=7")
        plain = client.get("/")

    assert 'class="active-filters"' in home.text
    assert "2010–2019" in home.text
    assert "Rated 7+" in home.text
    assert '<span class="filter-count">2</span>' in home.text
    assert 'href="/?rating_min=7.0"' in home.text
    assert 'aria-current="true">2010s</a>' in home.text
    assert 'name="rating_min" value="7" checked' in home.text
    assert "Clear all" in home.text
    assert '<div class="filter-rule" data-filter-rule="year" >' in home.text
    assert '<button type="button" data-add-filter="year" hidden>' in home.text
    assert 'href="/?year_from=2010&amp;year_to=2019" aria-label="Remove the rating filter"' in (
        home.text
    )
    assert home.text.count('<span class="rail-dot"') == 1, "only the filter section changed"
    assert 'class="active-filters"' not in plain.text
    assert '<div class="filter-rule" data-filter-rule="rating" hidden>' in plain.text
    assert 'aria-label="Add a filter" title="Add a filter" >' in plain.text
    assert 'class="rail-dot"' not in plain.text


def test_filters_beyond_year_and_rating_reach_every_source(tmp_path: Path) -> None:
    catalog = FakeCatalog()
    library = FakeLibrary()
    with make_client(tmp_path, library=library, catalog=catalog) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        client.post("/settings/libraries", data={"name": "Media", "path": "/media/Movies"})
        page = client.get(
            "/collections/latest?genre=drama,unknown&country=de&language=de&director=525"
            "&keyword=4379&runtime_min=40&content_rating=16&watch=unwatched"
        )
        plain = client.get("/")
        german = client.get("/?language=de&runtime_min=40")

    filters = catalog.browse_queries[0].filters
    assert filters.genre_ids == (18,)
    assert (filters.countries, filters.languages, filters.directors) == (("DE",), ("de",), (525,))
    assert (filters.certifications, filters.certification_country) == (("16",), "DE")
    assert catalog.browse_options[0]["refine"] is not None
    assert catalog.browse_options[1] == {"refine": None, "exclude": frozenset()}, "unfiltered"
    # Dark is not directed by Nolan; the unmatched file has no TMDB facts at all.
    assert library.item_id_calls[0] == frozenset()
    assert library.item_id_calls[-1] == {1}, "Dark is German and runs 55 minutes"
    assert german.status_code == 200
    text = page.text
    for chip in (
        "Genre: Drama",
        "Country: Germany",
        "Original language: German",
        "Director: Christopher Nolan",
        "Keyword: time travel",
        "Runtime: 40 min or longer",
        "Content rating: 16",
        "Unwatched",
    ):
        assert chip in text, chip
    assert '<span class="filter-count">8</span>' in text
    assert "As rated in Germany" in text
    assert 'href="/collections/latest?content_rating=16&amp;country=DE&amp;language=de' in text, (
        "removing genre keeps the rest"
    )
    assert 'name="genre" value="drama" checked' in text
    assert 'data-suggest="person" data-suggest-param="director"' in text
    assert '<option value="KR">South Korea</option>' in text
    assert '<option value="DE">Germany</option>' not in text, (
        "chosen countries are not offered again"
    )
    assert 'href="/collections/drama?genre=drama' in text, "other collections keep the filters"
    assert '<button type="button" data-add-filter="resolution"' not in text, "no probed files"
    assert '<button type="button" data-add-filter="watch" hidden>' in text
    assert '<div class="filter-rule" data-filter-rule="genre" hidden>' in plain.text
    assert 'name="genre" value="kids"' in plain.text and "Action" in plain.text


def test_file_filters_show_only_local_titles(tmp_path: Path) -> None:
    catalog = FakeCatalog()
    library = FakeLibrary()
    with make_client(tmp_path, library=library, catalog=catalog) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        client.post("/settings/libraries", data={"name": "Media", "path": "/media/Movies"})
        response = client.get("/?resolution=4K")

    assert "Only titles in your local libraries are shown" in response.text
    assert "Resolution: 4K" in response.text
    assert catalog.browse_queries[-1].filters.resolutions == ("4K",)
    assert library.item_id_calls[-1] == frozenset(), "nothing was probed, so nothing matches"


def test_filter_suggestions(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        people = client.get("/api/filters/suggest?kind=person&q=nolan")
        keywords = client.get("/api/filters/suggest?kind=keyword&q=time")
        short = client.get("/api/filters/suggest?kind=person&q=n")
        unknown = client.get("/api/filters/suggest?kind=studio&q=pixar")

    assert people.json() == {
        "results": [{"id": 525, "name": "Christopher Nolan", "detail": "Directing · Inception"}]
    }
    assert keywords.json()["results"][0]["name"] == "time travel"
    assert short.json() == unknown.json() == {"results": []}


def test_people_endpoint_returns_enrichment(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/items/movie/12/people")

    assert response.status_code == 200
    assert response.json() == {"label": "Director", "names": ["A Director"]}


def test_details_endpoint_returns_trailer_and_cast(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/items/movie/12/details")

    assert response.status_code == 200
    payload = response.json()
    assert payload["title"] == "A New Film"
    assert payload["runtime_minutes"] == 126
    assert payload["years"] == "2026"
    assert payload["directed_by"] == ["A Director"]
    assert payload["trailer_key"] == "trailer-key"
    assert payload["trailer_url"] == "https://www.youtube.com/watch?v=trailer-key"
    assert payload["cast"] == [
        {
            "name": "Lead Actor",
            "character": "The Lead",
            "profile_url": "https://image.tmdb.org/t/p/w185/actor.jpg",
        }
    ]


def test_watch_endpoint_lists_selected_services_with_links(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        unconfigured = client.get("/api/items/movie/12/watch")
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        response = client.get("/api/items/movie/12/watch")
        failed = client.get("/api/items/movie/404/watch")
        invalid = client.get("/api/items/person/12/watch")

    assert unconfigured.json() == {"options": []}
    assert response.json() == {
        "options": [
            {
                "provider_id": 8,
                "name": "Netflix",
                "logo_url": "https://image.tmdb.org/t/p/w92/netflix.jpg",
                "url": "https://www.netflix.com/title/12",
                "direct": True,
            }
        ]
    }
    assert failed.status_code == 502
    assert invalid.status_code == 404


def test_episodes_endpoint_marks_episodes_in_the_library(tmp_path: Path) -> None:
    library = FakeLibrary()
    with make_client(tmp_path, library) as client:
        without_library = client.get("/api/items/tv/13/episodes")
        library.add("Shows", [("/media/Shows", "tv")])
        response = client.get("/api/items/tv/13/episodes")
        failed = client.get("/api/items/tv/404/episodes")

    assert [
        episode["in_library"] for episode in without_library.json()["seasons"][0]["episodes"]
    ] == [False, False]
    assert response.json() == {
        "next_up": None,
        "seasons": [
            {
                "season_number": 1,
                "name": "Season 1",
                "episodes": [
                    {
                        "episode_number": 1,
                        "name": "Pilot",
                        "overview": "",
                        "air_date": "2026-01-01",
                        "runtime_minutes": 50,
                        "still_url": "https://image.tmdb.org/t/p/w300/still.jpg",
                        "in_library": False,
                    },
                    {
                        "episode_number": 2,
                        "name": "Second",
                        "overview": "",
                        "air_date": "",
                        "runtime_minutes": None,
                        "still_url": None,
                        "in_library": True,
                        "play_url": "/watch/tv/13/1/2",
                        "watched": False,
                        "progress": 0,
                    },
                ],
            }
        ],
    }
    assert failed.status_code == 502


def test_library_can_be_added_and_is_mixed_into_browse(tmp_path: Path) -> None:
    library = FakeLibrary()
    catalog = FakeCatalog()
    with make_client(tmp_path, library, catalog) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        rejected = client.post(
            "/settings/libraries",
            data={"name": "Movies", "path": "relative", "media_type_0": "movie"},
        )
        added = client.post(
            "/settings/libraries",
            data={"name": "Movies", "path": "/media/Movies", "media_type_0": "movie"},
            follow_redirects=False,
        )
        services = client.get("/settings")
        mixed = client.get("/")
        local_calls = list(library.browse_calls)
        library_only = client.get("/?media=all&providers=none")
        streaming_only = client.get("/?media=all&libraries=none")
        status = client.get("/api/libraries/status")
        folders = client.get("/api/libraries/folders", params={"path": "/media"})
        bad_folder = client.get("/api/libraries/folders", params={"path": "/nope"})
        rescan = client.post("/settings/libraries/1/rescan", follow_redirects=False)
        removed = client.post("/settings/libraries/1/remove", follow_redirects=False)

    assert rejected.status_code == 422
    assert "absolute folder path" in rejected.text
    assert 'data-open-step="local"' in rejected.text
    assert 'value="relative"' in rejected.text
    assert added.status_code == 303
    assert added.headers["location"] == "/settings"
    assert "/media/Movies" in services.text
    assert "2 movies" in services.text
    assert 'data-library-id="1"' in services.text
    assert 'Local · <span class="source-status" data-library-status' in services.text

    assert mixed.status_code == 200
    assert 'aria-label="Catalog source"' not in mixed.text
    assert "data-source" not in mixed.text
    assert "A New Film" in mixed.text
    assert "Dark" in mixed.text, "library titles are mixed into the streaming categories"
    assert "2 seasons · 3 episodes" in mixed.text
    assert "Not matched on TMDB yet" in mixed.text
    assert mixed.text.count("is-in-library") == 3
    assert 'data-providers-key="movie:12" title="In your local library"' in mixed.text
    assert mixed.text.count('<span class="source-local">') == 3
    assert '<option value="added" ' not in mixed.text, "streaming has no date added"
    assert 'name="providers" value="8" checked' in mixed.text
    assert 'name="libraries" value="1" checked' in mixed.text
    assert 'title="Local · Movies">Local</span>' in mixed.text
    assert "/settings/libraries/1/remove" not in mixed.text
    assert 'href="/?libraries=none" aria-label="Show only Netflix"' in mixed.text
    assert 'href="/?providers=none" aria-label="Show only Movies"' in mixed.text
    assert "mini-logo-library" in mixed.text, "the collapsed rail shows the folder too"
    local_query, local_category, local_page_size = local_calls[0]
    assert local_category is not None and local_category.slug == "popular"
    assert (local_query.page, local_page_size) == (1, 20)
    assert local_query.library_ids == (1,), "the library selection reaches local browsing"

    assert "Dark" in library_only.text
    assert "A New Film" not in library_only.text
    assert '<option value="added" ' in library_only.text
    assert 'href="/collections/latest?providers=none"' in library_only.text, (
        "tabs keep the source selection"
    )
    assert 'name="providers" value="8" >' in library_only.text
    assert library.browse_calls[-1][1] is not None
    assert library.browse_calls[-1][1].slug == "popular"
    assert len(catalog.browse_queries) == 2

    assert "A New Film" in streaming_only.text
    assert "Dark" not in streaming_only.text
    assert '<span class="filter-count">1</span>' in streaming_only.text
    assert '<a href="/">Show all</a>' in streaming_only.text
    assert 'aria-label="Show only Netflix"' not in streaming_only.text
    assert streaming_only.text.count('<span class="rail-dot"') == 1, "sources are narrowed"

    assert status.json()["libraries"][0]["text"].startswith("2 movies")
    assert folders.json()["entries"] == [{"name": "Movies", "path": "/media/Movies"}]
    assert bad_folder.status_code == 400
    assert rescan.status_code == 303
    assert rescan.headers["location"] == "/settings"
    assert library.scan_requests == [1, 1]
    assert removed.status_code == 303
    assert library.items == []


def test_library_holds_several_folders_of_both_media_types(tmp_path: Path) -> None:
    library = FakeLibrary()
    with make_client(tmp_path, library) as client:
        created = client.post(
            "/settings/libraries",
            data={
                "name": "Drive",
                "path": ["/media/Movies", "/media/TV Shows", ""],
                "media_type_0": "movie",
                "media_type_1": "tv",
                "media_type_2": "movie",
            },
            follow_redirects=False,
        )
        services = client.get("/settings")
        status = client.get("/api/libraries/status")
        duplicate = client.post(
            "/settings/libraries/1/folders",
            data={"path": "/media/Movies", "media_type_0": "movie"},
        )
        extra = client.post(
            "/settings/libraries/1/folders",
            data={"path": "/media/Anime", "media_type_0": "tv"},
            follow_redirects=False,
        )
        removed = client.post("/settings/libraries/1/folders/1/remove", follow_redirects=False)
        home = client.get("/")

    assert created.status_code == 303
    assert library.items[0].name == "Drive"
    assert [(folder.path, folder.media_type) for folder in library.items[0].folders] == [
        ("/media/TV Shows", "tv"),
        ("/media/Anime", "tv"),
    ], "the blank row is ignored; later folders are appended and removed individually"
    assert "2 movies · 1 show · 2 episodes" in services.text
    assert services.text.count('class="source-folder"') == 2
    assert 'action="/settings/libraries/1/folders/2/remove"' in services.text
    assert 'data-library-target="1"' in services.text
    assert status.json()["libraries"][0]["folders"] == [
        {"id": 1, "path": "/media/Movies", "media_type": "movie", "media_label": "Movies"},
        {"id": 2, "path": "/media/TV Shows", "media_type": "tv", "media_label": "TV Shows"},
    ]
    assert status.json()["libraries"][0]["media_label"] == "Movies & TV Shows"
    assert duplicate.status_code == 422
    assert "already in the library" in duplicate.text
    assert 'action="/settings/libraries/1/folders"' in duplicate.text
    assert "Add folders to Drive" in duplicate.text
    assert extra.status_code == 303
    assert removed.status_code == 303
    assert library.scan_requests == [1, 1, 1]
    assert 'title="Local · TV Shows"' in home.text


def test_library_can_be_renamed(tmp_path: Path) -> None:
    library = FakeLibrary()
    library.add("Shows", [("/media/Shows", "tv")])
    with make_client(tmp_path, library) as client:
        services = client.get("/settings")
        renamed = client.post(
            "/settings/libraries/1/rename", data={"name": "Home Drive"}, follow_redirects=False
        )
        blank = client.post("/settings/libraries/1/rename", data={"name": "  "})

    assert 'action="/settings/libraries/1/rename" data-library-rename-form hidden' in services.text
    assert 'aria-label="Rename Shows"' in services.text
    assert renamed.status_code == 303
    assert blank.status_code == 422
    assert "Give the library a name" in blank.text
    assert library.items[0].name == "Home Drive"


def test_last_folder_of_a_library_cannot_be_removed(tmp_path: Path) -> None:
    library = FakeLibrary()
    library.add("Shows", [("/media/Shows", "tv")])
    with make_client(tmp_path, library) as client:
        services = client.get("/settings")
        response = client.post("/settings/libraries/1/folders/1/remove")

    assert "/folders/1/remove" not in services.text
    assert response.status_code == 422
    assert "remove the library instead" in response.text
    assert len(library.items[0].folders) == 1


def test_library_only_setup_skips_streaming_onboarding(tmp_path: Path) -> None:
    library = FakeLibrary()
    catalog = FakeCatalog()
    library.add("Shows", [("/media/Shows", "tv")])
    with make_client(tmp_path, library, catalog) as client:
        home = client.get("/")
        services = client.get("/settings")

    assert home.status_code == 200
    assert home.url.path == "/"
    assert "Dark" in home.text
    assert '<option value="added" ' in home.text, "local-only views sort by date added"
    assert catalog.browse_queries == []
    assert "Your services" in services.text
    assert "mini-logo-library" in home.text


def test_streaming_outage_falls_back_to_library(tmp_path: Path) -> None:
    library = FakeLibrary()
    catalog = FakeCatalog()
    catalog.fail_browse = True
    library.add("Shows", [("/media/Shows", "tv")])
    with make_client(tmp_path, library, catalog) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        home = client.get("/")
        streaming_only = client.get("/?libraries=none")

    assert home.status_code == 200
    assert "Streaming results are unavailable right now" in home.text
    assert "Dark" in home.text
    assert "Catalog unavailable." in streaming_only.text


def test_user_collections_list_saved_titles_on_the_users_services(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        empty = client.get("/collections/1")
        saved_movie = client.put("/api/collections/1/items/movie/12")
        saved_again = client.put("/api/collections/1/items/movie/12")
        client.put("/api/collections/1/items/tv/13")
        unknown_collection = client.put("/api/collections/99/items/movie/12")
        bad_title = client.put("/api/collections/1/items/person/12")
        listed = client.get("/collections/1")
        movies = client.get("/collections/1?media=movie&sort=title")
        memberships = client.get("/api/items/movie/12/collections")
        removed = client.delete("/api/collections/1/items/movie/12")
        after = client.get("/collections/1")
        client.delete("/api/collections/1/items/tv/13")
        home = client.get("/")

    assert empty.status_code == 200
    assert "Nothing in Watchlist yet" in empty.text
    assert 'href="/collections/1" aria-current="page"' in empty.text
    assert 'data-collection="1"' not in empty.text
    assert 'data-collection="popular"' in empty.text
    assert "Titles to watch next." in empty.text
    assert '<option value="added" selected>' in empty.text
    assert saved_movie.json() == {"saved": True, "added": True}
    assert saved_again.json() == {"saved": True, "added": False}
    assert unknown_collection.status_code == 404
    assert bad_title.status_code == 404
    assert "A New Film" in listed.text
    assert "1 of 2 titles are on your services" in listed.text, "the series is not on Netflix"
    assert 'data-detail-url="/api/items/movie/12/details"' in listed.text
    assert 'action="/collections/1"' in listed.text
    assert 'href="/collections/1?sort=title"' in movies.text, "media links keep the sort"
    assert memberships.json()["collections"][:2] == [
        {
            "id": 1,
            "name": "Watchlist",
            "description": "Titles to watch next.",
            "icon": "bookmark",
            "default_sort": "added",
            "item_count": 2,
            "url": "/collections/1",
            "saved": True,
        },
        {
            "id": 2,
            "name": "My favourites",
            "description": "Titles you love.",
            "icon": "heart",
            "default_sort": "added",
            "item_count": 0,
            "url": "/collections/2",
            "saved": False,
        },
    ]
    assert removed.json() == {"saved": False, "removed": True}
    assert "None of these titles are on your services" in after.text
    assert 'class="card-save is-saved"' in listed.text
    assert (
        '<span class="saved-icons" data-saved-icons><svg class="collection-icon" aria-hidden="true"'
        ' viewBox="0 0 24 24"><path d="M6.5 4.5h11' in listed.text
    ), "saved cards show the Watchlist's bookmark"
    assert 'id="card-save-popover"' in listed.text
    assert '<span data-icon="heart"><svg class="collection-icon"' in listed.text, "menus copy icons"
    assert 'class="card-save" type="button" data-card-save' in home.text
    assert "card-save is-saved" not in home.text


def test_media_switch_keeps_the_search(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        searched = client.get("/?q=dune&media=movie")

    assert 'href="/?q=dune"' in searched.text
    assert 'href="/?q=dune&amp;media=tv"' in searched.text
    assert 'class="search-form is-open"' in searched.text
    assert 'class="search-clear" type="button" aria-label="Clear search"' in searched.text


def test_collection_links_redirects_and_sorts(tmp_path: Path) -> None:
    catalog = FakeCatalog()
    with make_client(tmp_path, catalog=catalog) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        home = client.get("/")
        legacy = client.get("/?category=latest&rating_min=7", follow_redirects=False)
        legacy_recent = client.get("/?media=movie&category=recent", follow_redirects=False)
        legacy_home = client.get("/?category=popular", follow_redirects=False)
        movie_only = client.get("/collections/thriller?media=tv", follow_redirects=False)
        missing = client.get("/collections/999?year_from=2020", follow_redirects=False)
        unknown = client.get("/collections/nope", follow_redirects=False)
        latest = client.get("/collections/latest?sort=release&order=desc")
        rated = client.get("/collections/latest?sort=rating&order=asc")
        added = client.get("/?sort=added")

    assert 'data-collection="1"' in home.text and 'data-collection="2"' in home.text
    assert home.text.index('data-collection="1"') < home.text.index('data-collection="latest"')
    assert 'class="collection-divider"' in home.text
    assert 'href="/collections/latest"' in home.text
    assert "The most popular" not in home.text, "fake categories carry no description"
    assert 'name="sort" data-default="popularity"' in home.text
    assert legacy.headers["location"] == "/collections/latest?rating_min=7"
    assert legacy_recent.headers["location"] == "/?media=movie&sort=added"
    assert legacy_home.headers["location"] == "/"
    assert movie_only.headers["location"] == "/?media=tv"
    assert missing.headers["location"] == "/?year_from=2020"
    assert unknown.headers["location"] == "/"
    latest_query = catalog.browse_queries[1]
    assert (latest_query.category, latest_query.sort, latest_query.descending) == (
        "latest",
        None,
        None,
    ), "the default sort is not repeated"
    assert '<option value="release" selected>' in latest.text
    assert 'href="/collections/latest?media=movie"' in latest.text
    rated_query = catalog.browse_queries[2]
    assert (rated_query.sort, rated_query.descending) == ("rating", False)
    assert 'href="/collections/latest?sort=rating&amp;order=asc&amp;page=2"' not in rated.text
    assert 'name="order" value="asc" data-default="desc"' in rated.text
    assert 'href="/collections/latest?sort=rating" aria-label="Rating, ascending' in rated.text
    assert 'href="/collections/latest" aria-label="Back to this collection’s order"' in rated.text
    assert 'href="/collections/drama"' in rated.text, "other collections reset the sort"
    assert catalog.browse_queries[3].sort is None, "streaming has no date added"
    assert '<option value="added" ' not in added.text


def test_collections_are_created_edited_and_deleted(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        created = client.post(
            "/api/collections", json={"name": " Weekend ", "icon": "clock", "description": "Fun"}
        )
        nameless = client.post("/api/collections", json={"icon": "clock"})
        bad_icon = client.post("/api/collections", json={"name": "Odd", "icon": "unicorn"})
        not_text = client.post("/api/collections", json={"name": 4})
        collection_id = created.json()["collection"]["id"]
        renamed = client.patch(f"/api/collections/{collection_id}", json={"name": "Weekends"})
        resorted = client.patch(f"/api/collections/{collection_id}", json={"default_sort": "x"})
        missing = client.patch("/api/collections/999", json={"name": "Missing"})
        deleted = client.delete(f"/api/collections/{collection_id}")
        deleted_again = client.delete(f"/api/collections/{collection_id}")
        listed = client.get("/api/collections")

    assert created.status_code == 201
    assert created.json()["collection"] == {
        "id": 3,
        "name": "Weekend",
        "description": "Fun",
        "icon": "clock",
        "default_sort": "added",
        "item_count": 0,
        "url": "/collections/3",
    }
    assert nameless.status_code == 422
    assert bad_icon.status_code == 422
    assert not_text.status_code == 422
    assert renamed.json()["collection"]["name"] == "Weekends"
    assert renamed.json()["collection"]["icon"] == "clock", "unchanged fields are kept"
    assert resorted.status_code == 422
    assert missing.status_code == 404
    assert deleted.json() == {"status": "deleted"}
    assert deleted_again.status_code == 404
    assert [item["name"] for item in listed.json()["collections"]] == [
        "Watchlist",
        "My favourites",
    ]


def test_site_title_is_renamed_and_kept_by_display_changes(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        default = client.get("/")
        renamed = client.post("/api/preferences/site-title", json={"title": " Sarah's  Taste "})
        client.post("/api/preferences/display", json={"show_year": False})
        home = client.get("/")
        blank = client.post("/api/preferences/site-title", json={"title": "  "})
        wrong = client.post("/api/preferences/site-title", json={"name": "x"})

    assert '<span class="brand-name" data-site-title>MyTaste</span>' in default.text
    assert renamed.json() == {"site_title": "Sarah's Taste"}
    assert '<span class="brand-name" data-site-title>Sarah&#39;s Taste</span>' in home.text
    assert 'value="Sarah&#39;s Taste" maxlength="40"' in home.text
    assert "<title>Popular · Sarah&#39;s Taste</title>" in home.text
    assert 'data-show-year="false"' in home.text
    assert blank.status_code == 422
    assert wrong.status_code == 422


def test_sidebar_source_changes_return_to_the_page(tmp_path: Path) -> None:
    library = FakeLibrary()
    library.add("Shows", [("/media/Shows", "tv")])
    with make_client(tmp_path, library) as client:
        add_page = client.get("/settings?add=streaming&next=/collections/latest%3Fsort%3Dtitle")
        added = client.post(
            "/settings/services",
            data={"region": "DE", "provider_ids": "8", "next": "/collections/latest?sort=title"},
            follow_redirects=False,
        )
        rejected = client.post(
            "/settings/services",
            data={"region": "DE", "next": "/collections/2"},
        )
        removed = client.post(
            "/settings/services/8/remove", data={"next": "/?media=tv"}, follow_redirects=False
        )
        offsite = client.post(
            "/settings/libraries/1/remove",
            data={"next": "//evil.example/"},
            follow_redirects=False,
        )

    assert 'data-open-step="streaming"' in add_page.text
    assert '<input type="hidden" name="next" value="/collections/latest?sort=title">' in (
        add_page.text
    )
    assert 'href="/collections/latest?sort=title">Done</a>' in add_page.text
    assert added.headers["location"] == "/collections/latest?sort=title"
    assert rejected.status_code == 422
    assert '<input type="hidden" name="next" value="/collections/2">' in rejected.text
    assert removed.headers["location"] == "/?media=tv"
    assert offsite.headers["location"] == "/settings", "only paths on this site are followed"
    assert library.items == []


def test_titles_can_be_saved_and_collections_edited_from_the_page(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        home = client.get("/")
        watchlist = client.get("/collections/1")

    assert 'aria-controls="save-popover"' in home.text
    assert "data-save-new" in home.text
    assert "data-detail-add" not in home.text
    assert 'id="collection-editor"' in home.text
    assert home.text.count('name="icon"') == 17, "no icon plus the sixteen choices"
    assert '<option value="added">Date added</option>' in home.text
    assert "data-edit-collection" not in home.text, "predefined collections cannot be edited"
    assert home.text.count("data-new-collection") == 2, "in Show all and beside the tabs"
    assert "data-collection-id" not in home.text
    assert 'data-collection-id="1"' in watchlist.text
    assert 'aria-label="Edit Watchlist"' in watchlist.text
    assert 'data-edit-collection="{&#34;default_sort&#34;: &#34;added&#34;' in watchlist.text
    assert "&#34;name&#34;: &#34;Watchlist&#34;" in watchlist.text
    assert "choose <strong>Save</strong> to add it here" in watchlist.text


def test_titles_are_grouped_into_rows(tmp_path: Path) -> None:
    library = FakeLibrary()
    library.add("Shows", [("/media/Shows", "tv")])
    with make_client(tmp_path, library) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        by_director = client.get("/?group=director")
        local_only = client.get("/collections/latest?providers=none&group=decade")
        local_page_size = library.browse_calls[-1][2]
        by_type = client.get("/collections/1?group=type")
        unknown = client.get("/?group=colour")

    assert '<h2 id="group-row-1">A Director</h2>' in by_director.text
    assert 'class="media-grid"' not in by_director.text
    assert 'href="/collections/latest?group=director"' in by_director.text, "tabs keep grouping"
    assert '<option value="director" selected>Director</option>' in by_director.text
    assert 'href="/" aria-label="Stop grouping"' in by_director.text
    assert '<span class="rail-dot"' in by_director.text
    assert '<h2 id="group-row-1">2010s</h2>' in local_only.text
    assert '<h2 id="group-row-2">Unknown year</h2>' in local_only.text
    assert local_page_size == 100, "grouping reads the top 100 titles"
    assert "Nothing in Watchlist yet" in by_type.text
    assert 'class="group-rows"' not in unknown.text
    assert 'aria-controls="group-popover"' in unknown.text
    assert 'href="/?group=genre">By genre</a>' in unknown.text


class PagedCatalog(FakeCatalog):
    async def browse(self, region: str, query: BrowseQuery, **options: object) -> CatalogPage:
        page = await super().browse(region, query, **options)  # type: ignore[arg-type]
        return replace(page, page=query.page, total_pages=3)


def test_infinite_scroll_fetches_batches_of_cards(tmp_path: Path) -> None:
    with make_client(tmp_path, catalog=PagedCatalog()) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        first = client.get("/collections/latest?sort=title")
        second = client.get(
            "/collections/latest?sort=title&page=2", headers={"X-MyTaste-Fragment": "results"}
        )
        last = client.get(
            "/collections/latest?sort=title&page=3", headers={"X-MyTaste-Fragment": "results"}
        )
        grouped = client.get("/?group=decade")

    assert "data-results" in first.text
    assert "data-load-more" in first.text
    assert 'href="/collections/latest?sort=title&amp;page=2" data-next-page>Next</a>' in first.text
    assert second.status_code == 200
    assert "<html" not in second.text and "browse-sidebar" not in second.text
    assert second.text.count("<article\n") + second.text.count("<article ") == 2
    assert 'data-detail-url="/api/items/movie/12/details"' in second.text
    assert 'loading="lazy"' in second.text
    assert second.headers["X-Next-Page"] == "/collections/latest?sort=title&page=3"
    assert last.headers["X-Next-Page"] == ""
    assert '<div class="group-rows" data-results>' in grouped.text
    assert 'aria-labelledby="group-row-1"' in grouped.text

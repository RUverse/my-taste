from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mytaste.catalog.models import (
    BrowseCategory,
    BrowseQuery,
    CastMember,
    CatalogItem,
    CatalogPage,
    MediaDetails,
    Provider,
    Region,
)
from mytaste.catalog.service import LocalSource
from mytaste.catalog.tmdb import TMDBError
from mytaste.config import AppSettings
from mytaste.library.models import Library, LibraryStatus
from mytaste.library.service import FolderEntry, FolderListing
from mytaste.web.app import create_app


class FakeCatalog:
    def __init__(self) -> None:
        self.browse_queries: list[BrowseQuery] = []
        self.fail_browse = False

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
            BrowseCategory("latest", "Latest"),
            BrowseCategory("popular", "Most Popular"),
            BrowseCategory("drama", "Drama", 18, 18),
        )

    async def browse(
        self, region: str, query: BrowseQuery, *, local: LocalSource | None = None
    ) -> CatalogPage:
        assert region == "DE"
        assert query.provider_ids and set(query.provider_ids) <= {8, 337}
        self.browse_queries.append(query)
        if self.fail_browse:
            raise TMDBError("TMDB is down")
        local_items = (await local(20)).items if local is not None else ()
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

    async def people(self, media_type: str, item_id: int) -> tuple[str, tuple[str, ...]]:
        return ("Director", ("A Director",))

    async def available_provider_ids(
        self, region: str, media_type: str, item_id: int
    ) -> frozenset[int]:
        assert region == "DE"
        if item_id == 404:
            raise TMDBError("Availability unavailable")
        return frozenset({8, 337, 99}) if media_type == "movie" else frozenset({337})

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
        )


class FakeLibrary:
    def __init__(self) -> None:
        self.items: list[Library] = []
        self.scan_requests: list[int] = []
        self.roots: tuple[Path, ...] = ()
        self.browse_calls: list[tuple[BrowseQuery, BrowseCategory | None, int]] = []

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

    def add(self, name: str, path: str, media_type: str) -> Library:
        if not path.startswith("/"):
            raise ValueError("Use an absolute folder path such as /mnt/media/Movies")
        library = Library(
            id=len(self.items) + 1,
            name=name or "Library",
            path=path,
            media_type=media_type,  # type: ignore[arg-type]
            created_at="2026-09-10T08:00:00+00:00",
            last_scanned_at="2026-09-10T08:05:00+00:00",
            item_count=2,
            file_count=3,
        )
        self.items.append(library)
        self.scan_requests.append(library.id)
        return library

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

    def matched_keys(self) -> frozenset[tuple[str, int]]:
        return frozenset({("movie", 12)})

    async def categories(self, media_type: str) -> tuple[BrowseCategory, ...]:
        return (
            BrowseCategory("recent", "Recently Added"),
            BrowseCategory("alphabetical", "A–Z"),
        )

    async def browse(
        self,
        query: BrowseQuery,
        *,
        category: BrowseCategory | None = None,
        page_size: int = 24,
    ) -> CatalogPage:
        self.browse_calls.append((query, category, page_size))
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
    assert "Most Popular" in home.text
    assert 'id="browse-sidebar"' in home.text
    assert 'data-sidebar="open"' in home.text
    assert "data-sidebar-toggle" in home.text
    assert "Filters &amp; display" not in home.text, "sections carry their own labels"
    assert '<h3 id="sources-heading">Sources</h3>' in home.text
    assert "controls-drawer" not in home.text
    assert "<h1>Latest</h1>" not in home.text
    assert 'class="active-services"' not in home.text
    assert "Save display" not in home.text
    assert 'id="media-details"' in home.text
    assert 'data-detail-url="/api/items/movie/12/details"' in home.text
    assert "data-detail-sound" in home.text
    assert "data-detail-add" in home.text
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
    assert 'aria-label="Filters and display options" inert' in home.text
    assert 'aria-expanded="false" aria-label="Show filters and display options"' in home.text
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
        }
    }, "only enabled services, in priority order; failed lookups are skipped"
    assert empty.json() == {"providers": {}}


def test_local_titles_show_a_folder_in_the_source_strip(tmp_path: Path) -> None:
    library = FakeLibrary()
    library.add("Shows", "/media/Shows", "tv")
    with make_client(tmp_path, library) as client:
        home = client.get("/")
        providers = client.get("/api/items/providers", params={"items": "movie:12"})

    assert 'name="show_providers"' in home.text, "the Sources option also covers folders"
    assert "data-providers-key" not in home.text, "no service lookups without streaming"
    assert home.text.count('<span class="source-local">') == 2
    assert "library-badge" not in home.text
    assert '<p class="sr-only" data-providers-text>In your local library</p>' in home.text
    assert providers.json() == {"providers": {}}


def test_sidebar_filters_show_presets_and_removable_chips(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        home = client.get("/?media=all&year_from=2010&year_to=2019&rating_min=7")
        plain = client.get("/")

    assert 'class="active-filters"' in home.text
    assert "2010–2019" in home.text
    assert "Rated 7+" in home.text
    assert '<span class="filter-count">2</span>' in home.text
    assert 'href="/?media=all&amp;category=latest&amp;rating_min=7.0"' in home.text
    assert 'aria-current="true">2010s</a>' in home.text
    assert 'name="rating_min" value="7" checked' in home.text
    assert "Clear all" in home.text
    assert 'class="active-filters"' not in plain.text
    assert 'name="rating_min" value="" checked' in plain.text


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
    assert payload["trailer_key"] == "trailer-key"
    assert payload["trailer_url"] == "https://www.youtube.com/watch?v=trailer-key"
    assert payload["cast"] == [
        {
            "name": "Lead Actor",
            "character": "The Lead",
            "profile_url": "https://image.tmdb.org/t/p/w185/actor.jpg",
        }
    ]


def test_library_can_be_added_and_is_mixed_into_browse(tmp_path: Path) -> None:
    library = FakeLibrary()
    catalog = FakeCatalog()
    with make_client(tmp_path, library, catalog) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        rejected = client.post(
            "/settings/libraries",
            data={"name": "Movies", "path": "relative", "media_type": "movie"},
        )
        added = client.post(
            "/settings/libraries",
            data={"name": "Movies", "path": "/media/Movies", "media_type": "movie"},
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
    assert "Recently Added" not in mixed.text
    assert 'name="providers" value="8" checked' in mixed.text
    assert 'name="libraries" value="1" checked' in mixed.text
    assert 'category=latest&amp;libraries=none">Only' in mixed.text, "Only link for a service"
    assert 'category=latest&amp;providers=none">Only' in mixed.text, "Only link for a folder"
    assert "Local · Movies" in mixed.text
    local_query, local_category, local_page_size = local_calls[0]
    assert local_category is not None and local_category.slug == "latest"
    assert (local_query.page, local_page_size) == (1, 20)

    assert "Dark" in library_only.text
    assert "A New Film" not in library_only.text
    assert "Recently Added" in library_only.text
    assert "providers=none" in library_only.text, "tabs keep the source selection"
    assert 'name="providers" value="8" >' in library_only.text
    assert library.browse_calls[-1][1] is not None
    assert library.browse_calls[-1][1].slug == "recent"
    assert len(catalog.browse_queries) == 2

    assert "A New Film" in streaming_only.text
    assert "Dark" not in streaming_only.text
    assert '<span class="filter-count">1</span>' in streaming_only.text
    assert 'class="section-action" href="/?media=all&amp;category=latest">Select all' in (
        streaming_only.text
    )

    assert status.json()["libraries"][0]["text"].startswith("2 movies")
    assert folders.json()["entries"] == [{"name": "Movies", "path": "/media/Movies"}]
    assert bad_folder.status_code == 400
    assert rescan.status_code == 303
    assert rescan.headers["location"] == "/settings"
    assert library.scan_requests == [1, 1]
    assert removed.status_code == 303
    assert library.items == []


def test_library_only_setup_skips_streaming_onboarding(tmp_path: Path) -> None:
    library = FakeLibrary()
    catalog = FakeCatalog()
    library.add("Shows", "/media/Shows", "tv")
    with make_client(tmp_path, library, catalog) as client:
        home = client.get("/")
        services = client.get("/settings")

    assert home.status_code == 200
    assert home.url.path == "/"
    assert "Dark" in home.text
    assert "Recently Added" in home.text
    assert catalog.browse_queries == []
    assert "Your services" in services.text
    assert "mini-logo-library" in services.text


def test_streaming_outage_falls_back_to_library(tmp_path: Path) -> None:
    library = FakeLibrary()
    catalog = FakeCatalog()
    catalog.fail_browse = True
    library.add("Shows", "/media/Shows", "tv")
    with make_client(tmp_path, library, catalog) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        home = client.get("/")
        streaming_only = client.get("/?libraries=none")

    assert home.status_code == 200
    assert "Streaming results are unavailable right now" in home.text
    assert "Dark" in home.text
    assert "Catalog unavailable." in streaming_only.text

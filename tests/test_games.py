from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from mytaste.catalog.models import CatalogItem
from mytaste.collections.models import CollectionItem
from mytaste.collections.portable import game_snapshot, title_snapshot
from mytaste.config import AppSettings, ConfigurationError, load_app_settings
from mytaste.games.gamepass import GamePassClient, GamePassError, normalize_game
from mytaste.games.models import Game, GamePage, GameQuery
from mytaste.games.service import GamesService
from mytaste.storage.collections import CollectionRepository
from mytaste.storage.gamepass_cache import GamePassCache
from mytaste.web.app import create_app


@pytest.fixture
def anyio_backend():
    return "asyncio"


A, B, C = "9NPDN9R45JX4", "9N0000000002", "9N0000000003"


def product(key=A):
    return {
        "ProductId": key,
        "ProductType": "Game",
        "Properties": {"Categories": ["Action"]},
        "LocalizedProperties": [
            {
                "ProductTitle": "A Game",
                "ProductDescription": "<script>provider text</script>",
                "DeveloperName": "A Studio",
                "PublisherName": "A Publisher",
                "Images": [
                    {"ImagePurpose": "Poster", "Uri": "//example.com/cover.jpg"},
                    {"ImagePurpose": "Screenshot", "Uri": "javascript:alert(1)"},
                    {"ImagePurpose": "Screenshot", "Uri": "https://example.com/shot.jpg"},
                ],
            }
        ],
        "MarketProperties": [
            {
                "OriginalReleaseDate": "2026-01-02T00:00:00Z",
                "UsageData": [
                    {"AggregateTimeSpan": "AllTime", "AverageRating": 4.5, "RatingCount": 12}
                ],
            }
        ],
    }


def test_metadata_normalization_and_portable_snapshot():
    game = normalize_game(product())
    assert game.rating == 4.5 and game.rating_count == 12
    assert game.release_date == "2026-01-02"
    assert game.poster_url == "https://example.com/cover.jpg"
    assert game.screenshots == ("https://example.com/shot.jpg",)
    snapshot = game_snapshot(game)
    assert snapshot["kind"] == "game"
    assert snapshot["ids"] == {"microsoft_store": A}
    assert snapshot["meta"]["ratings"]["microsoft_store"]["scale"] == 5
    assert "rating" not in snapshot  # Store ratings are metadata, not a user's rating.
    assert game.portable_id == f"game-xbox-{A}"
    assert game.store_url == f"https://www.xbox.com/games/store/-/{A}"
    demo = product()
    demo["Properties"]["IsDemo"] = True
    assert normalize_game(demo) is None
    trial = product()
    trial["DisplaySkuAvailabilities"] = [{"Sku": {"Properties": {"IsTrial": True}}}]
    assert normalize_game(trial) is None
    invalid = product()
    invalid["MarketProperties"][0]["OriginalReleaseDate"] = "unknown"
    invalid["MarketProperties"][0]["UsageData"][0]["AverageRating"] = 10
    game = normalize_game(invalid)
    assert game.release_date == "" and game.rating is None


def test_non_game_free_benefits_and_unknown_release_dates():
    data = product()
    data["ProductType"] = "Durable"
    assert normalize_game(data) is None
    data = product()
    data["DisplaySkuAvailabilities"] = [
        {
            "Sku": {"Properties": {"IsTrial": False}},
            "Availabilities": [
                {
                    "Actions": ["Purchase"],
                    "OrderManagementData": {"Price": {"MSRP": 0, "ListPrice": 0}},
                }
            ],
        }
    ]
    assert normalize_game(data) is None
    # A paid game's discounted/subscriber offer must not exclude the full game.
    data["DisplaySkuAvailabilities"][0]["Availabilities"][0]["RemediationRequired"] = True
    data["MarketProperties"][0]["OriginalReleaseDate"] = "9998-12-31T00:00:00Z"
    assert normalize_game(data).release_date == ""


@pytest.mark.anyio
async def test_client_public_requests_context_and_retry():
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if request.url.host == "catalog.gamepass.com":
            return httpx.Response(200, json=[{"siglId": "list"}, {"id": A}, {"id": A}])
        return httpx.Response(200, json={"Products": [product()]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = GamePassClient(client=http)
        assert await client.ids(
            GameQuery(plan="premium", platform="console"), "DE", "de-DE", "all"
        ) == [A]
        params = requests[-1].url.params
        assert params["market"] == "DE" and params["language"] == "de-DE"
        assert params["platformContext"] == "ConsoleGen8;ConsoleGen9"
        assert params["subscriptionContext"] == "cfq7ttc0p85b"
        assert (await client.products([A], "DE", "de-DE"))[A].title == "A Game"
        await client.close()
        assert not http.is_closed  # Borrowed clients remain caller-owned.


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"wrong": []}),
        httpx.Response(200, text="not json"),
        httpx.Response(400),
        httpx.Response(429, headers={"Retry-After": "600"}),
    ],
)
async def test_client_rejects_unreliable_catalog(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
        with pytest.raises(GamePassError):
            await GamePassClient(client=http).ids(GameQuery(), "DE", "en-US", "all")


class Source:
    def __init__(self):
        self.calls = []
        self.lists = {
            "all": [A, B],
            "popular": [C, B, A],
            "recent": [B],
            "coming": [C],
            "leaving": [A],
            "cloud": [B],
        }
        self.games = {
            A: Game(A, "Zulu", release_date="2024-01-01", genres=("Action",), rating=4),
            B: Game(B, "Alpha", release_date="2026-01-01", genres=("RPG",), rating=4.5),
            C: Game(C, "Future", genres=("RPG",)),
        }

    async def ids(self, query, region, language, collection):
        self.calls.append((collection, region, language, query.plan, query.platform))
        return self.lists[collection]

    async def products(self, ids, region, language):
        self.calls.append(("metadata", region, language, tuple(ids)))
        return {key: self.games[key] for key in ids if key in self.games}

    async def close(self):
        pass


@pytest.mark.anyio
async def test_global_filter_sort_pagination_and_live_mode(tmp_path):
    source = Source()
    service = GamesService(source, GamePassCache(tmp_path / "cache.db"), page_size=1)
    query = GameQuery(sort="title")
    page = await service.browse("DE", "en-US", query)
    assert page.items[0].id == B and page.total == 2 and page.pages == 2
    page = await service.browse("DE", "en-US", replace(query, page=2))
    assert page.items[0].id == A
    page = await service.browse("DE", "en-US", replace(query, search="ALPHA", genre="RPG"))
    assert page.total == 1 and page.items[0].id == B
    assert sum(call[0] == "metadata" for call in source.calls) == 3
    assert not (tmp_path / "cache.db").exists()
    await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sort", "order", "first", "second"),
    [
        ("catalog", "desc", B, A),
        ("title", "desc", A, B),
        ("release", "asc", A, B),
        ("rating", "asc", A, B),
    ],
)
async def test_reverse_order_applies_before_pagination(tmp_path, sort, order, first, second):
    service = GamesService(Source(), GamePassCache(tmp_path / "cache.db"), page_size=1)
    query = GameQuery(sort=sort, order=order)
    assert (await service.browse("DE", "en-US", query)).items[0].id == first
    assert (await service.browse("DE", "en-US", replace(query, page=2))).items[0].id == second
    await service.close()


@pytest.mark.anyio
async def test_plan_cloud_and_coming_membership(tmp_path):
    service = GamesService(Source(), GamePassCache(tmp_path / "cache.db"))
    popular = await service.browse("DE", "en-US", GameQuery(collection="popular"))
    assert [game.id for game in popular.items] == [B, A]
    cloud = await service.browse("DE", "en-US", GameQuery(platform="cloud"))
    assert [game.id for game in cloud.items] == [B]
    coming = await service.browse("DE", "en-US", GameQuery(collection="coming"))
    assert [game.id for game in coming.items] == [C]  # Not yet in current membership.
    with pytest.raises(ValueError):
        await service.browse("DE", "en-US", GameQuery(plan="pc", platform="cloud"))


@pytest.mark.anyio
async def test_missing_metadata_never_silently_changes_global_filters(tmp_path):
    source = Source()
    del source.games[B]
    service = GamesService(source, GamePassCache(tmp_path / "cache.db"))
    page = await service.browse("DE", "en-US", GameQuery())
    assert page.total == 2 and page.incomplete
    assert not page.items[1].metadata_complete
    for query in [GameQuery(search="Alpha"), GameQuery(genre="RPG"), GameQuery(sort="rating")]:
        with pytest.raises(GamePassError):
            await service.browse("DE", "en-US", query)


@pytest.mark.anyio
async def test_cache_context_isolation_and_restart(tmp_path):
    path = tmp_path / "cache.db"
    source = Source()
    service = GamesService(source, GamePassCache(path, enabled=True))
    await service.browse("DE", "en-US", GameQuery())
    count = len(source.calls)
    await service.browse("DE", "en-US", GameQuery())
    assert len(source.calls) == count
    await service.browse("US", "en-US", GameQuery())
    await service.browse("DE", "de-DE", GameQuery())
    await service.browse("DE", "en-US", GameQuery(plan="premium"))
    assert ("all", "DE", "en-US", "premium", "pc") in source.calls
    assert ("all", "US", "en-US", "ultimate", "pc") in source.calls
    await service.close()
    source = Source()
    service = GamesService(source, GamePassCache(path, enabled=True))
    assert (await service.browse("DE", "en-US", GameQuery())).total == 2
    assert not source.calls
    await service.close()


@pytest.mark.anyio
async def test_stale_cache_coalesces_refresh_and_limits_outage(tmp_path):
    now = [100.0]
    cache = GamePassCache(tmp_path / "cache.db", enabled=True, clock=lambda: now[0], stale_ttl=20)
    calls = 0
    started, release = asyncio.Event(), asyncio.Event()

    async def load(keys):
        nonlocal calls
        calls += 1
        if calls > 1:
            started.set()
            await release.wait()
        return {key: calls for key in keys}

    assert (await cache.get_many([A], 10, load)).values[A] == 1
    now[0] = 111
    assert (await cache.get_many([A], 10, load)).stale
    await started.wait()
    assert (await cache.get_many([A], 10, load)).values[A] == 1
    assert calls == 2
    pending = tuple(cache._inflight.values())
    release.set()
    await asyncio.gather(*pending)
    assert (await cache.get_many([A], 10, load)).values[A] == 2

    async def fail(_keys):
        raise GamePassError("outage")

    now[0] = 125
    assert (await cache.get_many([A], 10, fail)).stale
    await asyncio.gather(*tuple(cache._inflight.values()), return_exceptions=True)
    assert cache._read([A])[A][0] == 2
    now[0] = 142
    with pytest.raises(GamePassError):
        await cache.get_many([A], 10, fail)
    await cache.close()


@pytest.mark.anyio
async def test_disabled_corrupt_and_bounded_cache(tmp_path):
    async def load(keys):
        return {key: "value" for key in keys}

    path = tmp_path / "cache.db"
    path.write_text("broken SQLite")
    cache = GamePassCache(path, enabled=True)
    assert (await cache.get_many([A], 10, load)).values[A] == "value"
    assert not cache.enabled
    cache = GamePassCache(tmp_path / "bounded.db", enabled=True, max_entries=2)
    await cache.get_many([A, B, C], 10, load)
    with sqlite3.connect(cache.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 2
    await cache.close()


@pytest.mark.anyio
async def test_shutdown_cancels_refresh_after_caller_disconnects(tmp_path):
    cache = GamePassCache(tmp_path / "cache.db", enabled=True)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def load(keys):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {key: "result" for key in keys}

    first = asyncio.create_task(cache.get_many([A], 10, load))
    await started.wait()
    # Shared refresh survives a canceled caller and is stopped by service shutdown.
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    await cache.close()
    assert calls == 1 and not cache._inflight


def test_portable_collection_migration_survives_rename_and_restart(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE collections (id INTEGER PRIMARY KEY, name TEXT, "
            "description TEXT, icon TEXT, default_sort TEXT, position INTEGER, "
            "created_at TEXT)"
        )
        connection.execute("INSERT INTO collections VALUES (42, 'Old', '', '', 'added', 0, 'now')")
    repository = CollectionRepository(path)
    repository.initialize()
    portable = repository.get(42).portable_id
    assert portable.startswith("collection-")
    repository.update(42, name="Renamed")
    repository.initialize()
    assert repository.get(42).portable_id == portable
    assert repository.create("New").portable_id != portable
    assert len(repository.list()) == 2  # Existing databases never get default lists recreated.


def test_tmdb_snapshot_identity_includes_media_kind():
    movie = CollectionItem("movie", 12, "now", "Movie")
    series = CollectionItem("tv", 12, "now", "Series")
    assert movie.to_catalog_item() == CatalogItem(12, "movie", "Movie", "", "", 0)
    assert movie.portable_id != series.portable_id
    assert title_snapshot(movie)["ids"] == {"tmdb": 12}
    assert title_snapshot(series)["kind"] == "tv"


def test_gamepass_configuration(monkeypatch):
    monkeypatch.delenv("MYTASTE_GAMEPASS_CACHE_ENABLED", raising=False)
    assert not load_app_settings().gamepass_cache_enabled
    monkeypatch.setenv("MYTASTE_GAMEPASS_CACHE_ENABLED", "true")
    assert load_app_settings().gamepass_cache_enabled
    monkeypatch.setenv("MYTASTE_GAMEPASS_CATALOG_TTL_SECONDS", "nan")
    with pytest.raises(ConfigurationError):
        load_app_settings()


class WebGames:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def browse(self, region, language, query):
        self.calls.append(query)
        assert region == "DE"
        if self.fail:
            raise GamePassError("Xbox outage")
        return GamePage((normalize_game(product()),), 25, 2, ("Action",), 100)

    async def details(self, key, region, language):
        if self.fail:
            raise GamePassError("Xbox outage")
        return normalize_game(product(key))


def test_games_share_browse_controls_and_keep_movie_series_routes_working(tmp_path):
    from test_web import FakeCatalog

    catalog, games = FakeCatalog(), WebGames()
    app = create_app(AppSettings(None, tmp_path / "app.db"), catalog=catalog, games=games)
    with TestClient(app) as client:
        app.state.preferences.save("DE", (8, 337))
        game_page = client.get("/collections/games?q=A+Game&sort=title&order=desc&render=1")
        assert game_page.status_code == 200
        assert games.calls[-1].search == "A Game" and games.calls[-1].order == "desc"
        for markup in (
            'class="browse-layout"',
            'id="browse-sidebar"',
            'class="browse-toolbar"',
            "data-collection-bar",
            "data-collection-more",
            'id="display-form"',
            "data-filter-form",
            "data-sidebar-toggle",
            'data-rail-section="sidebar-sort"',
            'name="show_people"',
            'name="show_media_type"',
            'aria-label="Media type"',
            'href="/?media=movie"',
            'href="/?media=tv"',
        ):
            assert markup in game_page.text
        assert 'action="/collections/games" role="search"' in game_page.text
        assert 'name="autoplay_trailer"' not in game_page.text
        assert 'class="games-shell"' not in game_page.text
        assert not catalog.browse_queries
        for media in ("movie", "tv"):
            response = client.get(f"/?media={media}")
            assert response.status_code == 200 and "A New" in response.text
            assert 'id="browse-sidebar"' in response.text
            assert "data-collection-bar" in response.text
            assert 'name="autoplay_trailer"' in response.text
            assert catalog.browse_queries[-1].media_type == media
        assert len(games.calls) == 1
        assert app.state.preferences.get().provider_ids == (8, 337)


def test_games_routes_settings_fragments_and_details(tmp_path):
    games = WebGames()
    app = create_app(AppSettings(None, tmp_path / "app.db"), catalog=object(), games=games)
    with TestClient(app) as client:
        assert "Set up Game Pass" in client.get("/collections/games").text
        assert not games.calls
        app.state.preferences.save("DE", (8, 337))
        response = client.get("/collections/games?q=A+Game&sort=title&render=1")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert "A Game" in response.text and "Microsoft Store rating 4.5 out of 5" in response.text
        assert "data-game-open" in response.text and "games.js" in response.text
        assert "&lt;script&gt;" not in response.text  # Description appears only in details.
        fragment = client.get(
            "/collections/games?page=2", headers={"X-MyTaste-Fragment": "results"}
        )
        assert "<html" not in fragment.text and "A Game" in fragment.text
        assert fragment.headers["x-next-page"] == ""
        assert client.get("/collections/games?plan=pc&platform=cloud").status_code == 422
        detail = client.get(f"/api/games/{A}/details")
        assert detail.json()["portable_id"] == f"game-xbox-{A}"
        assert "&lt;script&gt;" in client.get(f"/games/{A}").text
        assert client.get("/api/games/not-an-id/details").status_code == 404
        invalid = client.post(
            "/games/settings", data={"region": "DE", "plan": "pc", "platform": "cloud"}
        )
        assert invalid.status_code == 422
        assert app.state.game_preferences.get().plan == "ultimate"
        response = client.post(
            "/games/settings",
            data={"region": "de", "plan": "premium", "platform": "console"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert app.state.preferences.get().provider_ids == (8, 337)
        assert app.state.game_preferences.get().plan == "premium"
        games.fail = True
        assert client.get("/collections/games?render=1").status_code == 503
        assert client.get(f"/api/games/{A}/details").status_code == 503
        assert "Back to games" in client.get(f"/games/{A}").text


def test_games_layout_does_not_wait_for_catalog_and_deferred_results_can_retry(tmp_path):
    games = WebGames()
    games.fail = True
    app = create_app(AppSettings(None, tmp_path / "app.db"), catalog=object(), games=games)
    url = "/collections/games/recent?q=A+Game&genre=Action&sort=title&order=desc&page=2"
    with TestClient(app) as client:
        app.state.preferences.save("DE", (8, 337))
        shell = client.get(url)
        assert shell.status_code == 200 and not games.calls
        assert 'id="browse-sidebar"' in shell.text and "data-collection-bar" in shell.text
        assert "Loading Game Pass games" in shell.text and 'aria-busy="true"' in shell.text
        assert 'http-equiv="refresh"' in shell.text and "render=1" in shell.text
        assert 'value="Action" selected' in shell.text
        headers = {"X-MyTaste-Fragment": "games-page"}
        failure = client.get(url, headers=headers)
        assert failure.status_code == 503
        assert "Xbox outage" in failure.json()["html"]
        assert "data-game-retry" in failure.json()["html"]
        assert client.get(url).status_code == 200
        assert len(games.calls) == 1  # Layouts do not retry the catalog themselves.
        games.fail = False
        results = client.get(url, headers=headers)
        assert results.status_code == 200 and results.headers["cache-control"] == "no-store"
        assert "A Game" in results.json()["html"] and "<html" not in results.json()["html"]
        assert results.json()["genres"] == ["Action"]
        query = games.calls[-1]
        assert (
            query.collection,
            query.search,
            query.genre,
            query.sort,
            query.order,
            query.page,
        ) == (
            "recent",
            "A Game",
            "Action",
            "title",
            "desc",
            2,
        )
        assert 'aria-busy="true"' not in client.get(url + "&render=1").text


@pytest.mark.anyio
async def test_game_layout_is_available_while_results_are_blocked(tmp_path):
    started, release = asyncio.Event(), asyncio.Event()

    class SlowGames(WebGames):
        async def browse(self, region, language, query):
            started.set()
            await release.wait()
            return await super().browse(region, language, query)

    app = create_app(AppSettings(None, tmp_path / "app.db"), catalog=object(), games=SlowGames())
    app.state.preferences.save("DE", (8,))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(
            client.get("/collections/games", headers={"X-MyTaste-Fragment": "games-page"})
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            shell = await asyncio.wait_for(client.get("/collections/games"), timeout=1)
            assert shell.status_code == 200 and "Loading Game Pass games" in shell.text
            assert not request.done()
        finally:
            release.set()
            await request

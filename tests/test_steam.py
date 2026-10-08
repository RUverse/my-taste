from __future__ import annotations

import json
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import owner_client

from mytaste.games.gamepass import GamePassError
from mytaste.games.matching import GameLinker, normalize_title
from mytaste.games.models import Game, GameQuery, OwnedGame, parse_game_key
from mytaste.games.service import GamesService, allowed_sorts, default_sort
from mytaste.games.steam import (
    OPENID,
    SORT_MOST_PLAYED,
    SORT_NEWEST,
    SORT_TITLE,
    SteamClient,
    SteamError,
    SteamProfileError,
    normalize_item,
    openid_url,
    parse_profile,
)
from mytaste.storage.game_links import GameLink, GameLinkRepository
from mytaste.storage.gamepass_cache import GamePassCache
from mytaste.storage.steam import SteamAccountRepository

STEAM_ID = "76561197960435530"
A, B, C = "9NPDN9R45JX4", "9N0000000002", "9N0000000003"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def store_item(appid, name, *, released=1_700_000_000, tags=(19,), score=90, reviews=100):
    return {
        "id": appid,
        "appid": appid,
        "success": 1,
        "visible": True,
        "type": 0,
        "name": name,
        "tagids": list(tags),
        "tags": [{"tagid": tag, "weight": 100 - index} for index, tag in enumerate(tags)],
        "reviews": {
            "summary_filtered": {
                "review_count": reviews,
                "percent_positive": score,
                "review_score_label": "Very Positive",
            }
        },
        "basic_info": {
            "short_description": "A <b>game</b>",
            "developers": [{"name": "Studio"}],
            "publishers": [{"name": "Publisher"}],
        },
        "release": {"steam_release_date": released},
        "assets": {
            "asset_url_format": f"steam/apps/{appid}/${{FILENAME}}?t=1",
            "library_capsule": "library_600x900.jpg",
            "header": "header.jpg",
        },
    }


# Profiles, store items, and the client ------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (STEAM_ID, ("id", STEAM_ID)),
        (f"https://steamcommunity.com/profiles/{STEAM_ID}/games", ("id", STEAM_ID)),
        ("steamcommunity.com/id/robinwalker/", ("vanity", "robinwalker")),
        ("robin_walker-2", ("vanity", "robin_walker-2")),
    ],
)
def test_profile_links_resolve_to_an_id_or_custom_name(value, expected):
    assert parse_profile(value) == expected


@pytest.mark.parametrize(
    "value", ["https://example.com/id/robin", "steamcommunity.com/groups/x", "a", "not a name!"]
)
def test_other_links_are_rejected(value):
    with pytest.raises(SteamProfileError):
        parse_profile(value)


def test_store_items_become_games_with_shared_genres_and_store_assets():
    game = normalize_item(store_item(620, "Portal 2", tags=(19, 1664, 4182)), {19: "Action"})
    assert game.id == "steam-620" and game.steam_appid == 620
    assert game.poster_url == (
        "https://shared.akamai.steamstatic.com/store_item_assets/"
        "steam/apps/620/library_600x900.jpg?t=1"
    )
    assert game.genres == ("Action & adventure", "Puzzle & trivia")
    assert game.tags == ("Action",)
    assert (game.steam_score, game.steam_reviews, game.score) == (90, 100, 9.0)
    assert game.release_date == "2023-11-14" and game.steam_url.endswith("/app/620/")
    assert game.portable_id == "game-steam-620"
    coming = store_item(1, "Soon")
    coming["release"] = {"is_coming_soon": True, "coming_soon_display": "date_year"}
    assert normalize_item(coming, {}).coming_soon == "Coming soon"
    assert normalize_item(coming, {}).release_date == ""
    assert normalize_item({**store_item(2, "DLC"), "type": 4}, {}) is None
    assert normalize_item({**store_item(3, "Gone"), "success": 15}, {}) is None
    unrated = normalize_item(store_item(4, "New", reviews=0, score=0), {})
    assert unrated.steam_score is None and unrated.score is None


def test_game_keys_accept_older_xbox_links():
    assert parse_game_key(A) == ("xbox", A)
    assert parse_game_key(f"xbox-{A.lower()}") == ("xbox", A)
    assert parse_game_key("steam-730") == ("steam", "730")
    for value in ("steam-0", "steam-x", "epic-1", "xbox-short"):
        with pytest.raises(ValueError):
            parse_game_key(value)


def steam_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.anyio
async def test_client_pages_rankings_and_keeps_its_key_server_side():
    requests = []

    def respond(request):
        requests.append(request)
        if "GetTagList" in request.url.path:
            return httpx.Response(
                200, json={"response": {"tags": [{"tagid": 19, "name": "Action"}]}}
            )
        if "Query" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "response": {
                        "metadata": {"total_matching_records": 3},
                        "ids": [{"appid": 1}, {"appid": 2}],
                        "store_items": [store_item(1, "One"), {**store_item(2, "Two"), "type": 4}],
                    }
                },
            )
        if "GetOwnedGames" in request.url.path:
            steam_id = request.url.params["steamid"]
            if steam_id == STEAM_ID:
                return httpx.Response(
                    200,
                    json={
                        "response": {
                            "game_count": 1,
                            "games": [
                                {
                                    "appid": 1,
                                    "name": "One",
                                    "playtime_forever": 90,
                                    "rtime_last_played": 1_700_000_000,
                                }
                            ],
                        }
                    },
                )
            return httpx.Response(200, json={"response": {}})
        return httpx.Response(404)

    async with steam_transport(respond) as http:
        client = SteamClient(api_key="secret", client=http)
        games, total, covered = await client.query(
            "DE", "de-DE", sort=SORT_NEWEST, start=0, count=2, tag_ids=(19,)
        )
        assert [game.title for game in games] == ["One"] and (total, covered) == (3, 2)
        query = next(request for request in requests if "Query" in request.url.path)
        body = json.loads(query.url.params["input_json"])
        assert body["context"] == {"language": "german", "country_code": "DE"}
        assert body["query"]["filters"]["tagids_must_match"] == [{"tagids": [19]}]
        assert body["query"]["filters"]["released_only"] is True
        owned = await client.owned(STEAM_ID)
        assert owned == (OwnedGame(1, "One", 90, 1_700_000_000),)
        assert await client.owned("76561197960000000") is None  # Private game details.
    keyless = SteamClient()
    with pytest.raises(SteamProfileError, match="MYTASTE_STEAM_API_KEY"):
        await keyless.owned(STEAM_ID)
    await keyless.close()


@pytest.mark.anyio
async def test_custom_names_resolve_with_or_without_a_key():
    def respond(request):
        if request.url.host == "api.steampowered.com":
            found = request.url.params["vanityurl"] == "robin"
            return httpx.Response(
                200,
                json={
                    "response": {"steamid": STEAM_ID, "success": 1} if found else {"success": 42}
                },
            )
        if request.url.path == "/id/robin/":
            return httpx.Response(200, text=f"<profile><steamID64>{STEAM_ID}</steamID64></profile>")
        return httpx.Response(200, text="<response><error>Not found</error></response>")

    async with steam_transport(respond) as http:
        for client in (SteamClient(api_key="secret", client=http), SteamClient(client=http)):
            assert await client.resolve("steamcommunity.com/id/robin") == STEAM_ID
            with pytest.raises(SteamProfileError, match="nobody"):
                await client.resolve("nobody")


@pytest.mark.anyio
async def test_steam_sign_in_is_checked_with_steam():
    checks = []

    def respond(request):
        checks.append(dict(httpx.QueryParams(request.content.decode())))
        valid = checks[-1].get("openid.sig") == "good"
        return httpx.Response(
            200, text=f"ns:http://specs.openid.net/auth/2.0\nis_valid:{str(valid).lower()}\n"
        )

    return_to = "https://pi.example:8000/games/steam/callback?state=abc"
    reply = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "id_res",
        "openid.op_endpoint": OPENID,
        "openid.claimed_id": f"https://steamcommunity.com/openid/id/{STEAM_ID}",
        "openid.identity": f"https://steamcommunity.com/openid/id/{STEAM_ID}",
        "openid.return_to": return_to,
        "openid.sig": "good",
        "state": "abc",
    }
    async with steam_transport(respond) as http:
        client = SteamClient(client=http)
        assert await client.verify_openid(reply, return_to) == STEAM_ID
        assert checks[-1]["openid.mode"] == "check_authentication" and "state" not in checks[-1]
        for change in (
            {"openid.sig": "forged"},
            {"openid.return_to": "https://evil.example/callback"},
            {"openid.op_endpoint": "https://evil.example/openid"},
            {"openid.claimed_id": "https://steamcommunity.com/openid/id/1"},
            {"openid.mode": "cancel"},
        ):
            with pytest.raises(SteamProfileError):
                await client.verify_openid({**reply, **change}, return_to)
    url = urlparse(openid_url(return_to, "https://pi.example:8000/"))
    params = parse_qs(url.query)
    assert url.geturl().startswith(OPENID)
    assert params["openid.return_to"] == [return_to]
    assert params["openid.realm"] == ["https://pi.example:8000/"]


# Matching -----------------------------------------------------------------------------------


def test_titles_normalize_store_decorations_away():
    assert normalize_title("Starfield™ Standard Edition") == normalize_title("Starfield")
    assert normalize_title("Forza Horizon 5 (PC)") == "forza horizon 5"
    assert normalize_title("Pokémon-ish: Game Preview") == "pokemon ish"
    assert normalize_title("Halo 2") != normalize_title("Halo 3")


class SearchSteam:
    def __init__(self, results):
        self.results = results
        self.searches = []

    async def search(self, term, region, language, limit=50):
        self.searches.append(term)
        return self.results.get(term, [])


@pytest.mark.anyio
async def test_linker_uses_itad_then_wikidata_then_titles_and_keeps_corrections(tmp_path):
    repository = GameLinkRepository(tmp_path / "app.db")
    repository.initialize()
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/lookup/id/shop/48/v1":
            asked = json.loads(request.content)
            # Both spellings, as ITAD stores both.
            assert {key.upper() for key in asked} == {key for key in asked if key.isupper()}
            return httpx.Response(200, json={A.lower(): "itad-a", B: None, C: None})
        if request.url.path == "/lookup/shop/61/id/v1":
            return httpx.Response(200, json={"itad-a": ["sub/5", "app/100", "app/101"]})
        if request.url.host == "query.wikidata.org":
            return httpx.Response(
                200,
                json={
                    "results": {
                        "bindings": [{"ms": {"value": B.lower()}, "steam": {"value": "200"}}]
                    }
                },
            )
        return httpx.Response(404)

    now = [1000.0]
    steam = SearchSteam(
        {"Gamma™": [Game("steam-300", "Gamma", steam_appid=300, release_date="2021-03-01")]}
    )
    games = [
        Game(f"xbox-{A}", "Alpha", xbox_id=A),
        Game(f"xbox-{B}", "Beta", xbox_id=B),
        Game(f"xbox-{C}", "Gamma™", xbox_id=C, release_date="2020-11-01"),
        Game("xbox-9N0000000004", "Only on Xbox", xbox_id="9N0000000004"),
    ]
    async with steam_transport(respond) as http:
        linker = GameLinker(repository, steam, client=http, clock=lambda: now[0])
        links = await linker.links(games, "DE", "en-US")
        assert links == {A: 100, B: 200, C: 300, "9N0000000004": 0}
        assert {link.source for link in repository.get([A, B, C]).values()} == {
            "itad",
            "wikidata",
            "title",
        }
        count = len(calls)
        assert await linker.links(games, "DE", "en-US") == links and len(calls) == count
        linker.unlink([A])
        now[0] += 8 * 86_400  # Automatic links are rechecked weekly; corrections are kept.
        assert (await linker.links(games, "DE", "en-US"))[A] == 0
        assert linker.xbox_for(200) == [B]


@pytest.mark.anyio
async def test_failed_sources_never_record_that_a_game_is_not_on_steam(tmp_path):
    repository = GameLinkRepository(tmp_path / "app.db")
    repository.initialize()
    async with steam_transport(lambda request: httpx.Response(503)) as http:
        linker = GameLinker(repository, SearchSteam({}), client=http)
        assert await linker.links([Game(f"xbox-{A}", "Alpha", xbox_id=A)], "DE", "en") == {}
    assert repository.get([A]) == {}


# The games service ----------------------------------------------------------------------------


def steam_game(appid, title, release, tags=(19,), score=80):
    return Game(
        f"steam-{appid}",
        title,
        steam_appid=appid,
        release_date=release,
        tag_ids=tags,
        genres=tuple(
            name
            for name, ids in {"Action & adventure": (19,), "Role playing": (122,)}.items()
            if set(ids) & set(tags)
        ),
        steam_score=score,
        steam_reviews=10,
    )


class FakeSteam:
    api_key = "secret"

    def __init__(self):
        self.store = [
            steam_game(10, "Alpha", "2024-05-01"),
            steam_game(20, "Beta", "2025-01-01", tags=(122,)),
            steam_game(30, "Gamma", "2023-01-01"),
            steam_game(40, "Delta", "2022-01-01", tags=(122,)),
            # Steam matches a tag anywhere on a game, beyond the tags it lists.
            replace(
                steam_game(50, "Echo", "2021-01-01", tags=(19,)), genres=("Action & adventure",)
            ),
        ]
        self.owned_games = (OwnedGame(30, "Gamma", 600, 1_700_000_000), OwnedGame(99, "Tool"))
        self.queries = []

    async def query(self, region, language, *, sort, start, count, tag_ids=(), coming_soon=False):
        self.queries.append((sort, start, tag_ids))
        games = [
            game
            for game in self.store
            if not tag_ids or set(game.tag_ids) & set(tag_ids) or game.steam_appid == 50
        ]
        if sort == SORT_NEWEST:
            games.sort(key=lambda game: game.release_date, reverse=True)
        elif sort == SORT_TITLE:
            games.sort(key=lambda game: game.title.casefold())
        chunk = games[start : start + count]
        return chunk, len(games), len(chunk)

    async def items(self, appids, region, language, screenshots=False):
        return {game.steam_appid: game for game in self.store if game.steam_appid in appids}

    async def search(self, term, region, language, limit=50):
        return [game for game in self.store if term.casefold() in game.title.casefold()]

    async def owned(self, steam_id):
        return self.owned_games

    async def summary(self, steam_id):
        return {"persona": "Rez", "avatar_url": "", "profile_url": ""}

    async def resolve(self, value):
        return STEAM_ID

    async def close(self):
        pass


class FakeLinker:
    def __init__(self, links):
        self.mapping = links
        self.unlinked = []

    async def links(self, games, region, language):
        return {game.xbox_id: self.mapping.get(game.xbox_id, 0) for game in games}

    def xbox_for(self, appid):
        return [key for key, value in self.mapping.items() if value == appid]

    def unlink(self, keys):
        self.unlinked.extend(keys)
        for key in keys:
            self.mapping[key] = 0

    async def close(self):
        pass


class GamePass:
    def __init__(self):
        self.games = {
            A: Game(f"xbox-{A}", "Zulu", xbox_id=A, release_date="2024-01-01", rating=4.0),
            B: Game(
                f"xbox-{B}", "Delta for Xbox", xbox_id=B, release_date="2022-01-01", rating=4.5
            ),
            C: Game(f"xbox-{C}", "Delta Deluxe", xbox_id=C, release_date="2022-01-01"),
        }
        self.lists = {"all": [A, B, C], "popular": [B, A]}
        self.fail = False

    async def ids(self, query, region, language, collection):
        if self.fail:
            raise GamePassError("Xbox outage")
        return self.lists.get(collection, [])

    async def products(self, ids, region, language):
        return {key: self.games[key] for key in ids}

    async def close(self):
        pass


def make_service(tmp_path, *, connected=True, page_size=2):
    accounts = SteamAccountRepository(tmp_path / "app.db")
    accounts.initialize()
    steam = FakeSteam()
    service = GamesService(
        GamePass(),
        GamePassCache(tmp_path / "cache.db"),
        steam=steam,
        linker=FakeLinker({B: 40, C: 40}),  # Two editions of one Steam game.
        accounts=accounts,
        page_size=page_size,
    )
    if connected:
        accounts.connect(replace_account(STEAM_ID))
    return service, steam


def replace_account(steam_id):
    from mytaste.games.models import SteamAccount

    return SteamAccount(steam_id, "Rez")


async def every_page(service, query):
    games, page = [], 1
    while True:
        result = await service.browse("DE", "en-US", replace(query, page=page))
        games.extend(result.items)
        if page >= result.pages or not result.items:
            return games
        page += 1


@pytest.mark.anyio
async def test_game_pass_games_on_steam_merge_into_one_game(tmp_path):
    service, _steam = make_service(tmp_path, page_size=10)
    page = await service.browse("DE", "en-US", GameQuery(collection="popular"))
    delta, zulu = page.items
    assert delta.id == "steam-40" and delta.xbox_id == B and delta.title == "Delta"
    assert delta.game_pass and delta.rating == 4.5 and delta.steam_score == 80
    assert zulu.id == f"xbox-{A}" and zulu.game_pass
    every = await service.browse("DE", "en-US", GameQuery(collection="all", sources=("gamepass",)))
    assert [game.id for game in every.items] == [f"xbox-{A}", "steam-40"]  # One per edition set.


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("release", ["Beta", "Alpha", "Zulu", "Gamma", "Delta", "Echo"]),
        ("title", ["Alpha", "Beta", "Delta", "Echo", "Gamma", "Zulu"]),
    ],
)
async def test_all_games_merge_xbox_only_games_into_steams_order(tmp_path, sort, expected):
    service, _steam = make_service(tmp_path)
    query = GameQuery(collection="all", sort=sort)
    games = await every_page(service, query)
    assert [game.title for game in games] == expected
    by_title = {game.title: game for game in games}
    assert by_title["Delta"].game_pass and by_title["Delta"].xbox_id == B
    assert by_title["Gamma"].owned and by_title["Gamma"].playtime == 600
    assert not by_title["Alpha"].owned and not by_title["Alpha"].game_pass
    assert allowed_sorts(query, True) == ("release", "title")


@pytest.mark.anyio
async def test_steam_rankings_list_steam_games_and_filter_by_their_listed_tags(tmp_path):
    service, steam = make_service(tmp_path, page_size=10)
    page = await service.browse("DE", "en-US", GameQuery(collection="steam-popular"))
    assert [game.title for game in page.items] == ["Alpha", "Beta", "Gamma", "Delta", "Echo"]
    assert steam.queries[-1][0] == SORT_MOST_PLAYED
    action = await service.browse(
        "DE", "en-US", GameQuery(collection="steam-popular", genre="Action & adventure")
    )
    assert [game.title for game in action.items] == ["Alpha", "Gamma", "Echo"]
    rpg = await service.browse(
        "DE", "en-US", GameQuery(collection="steam-popular", genre="Role playing")
    )
    assert [game.title for game in rpg.items] == ["Beta", "Delta"]  # Echo is only matched by Steam.
    assert rpg.items[1].game_pass
    classics = await service.browse(
        "DE", "en-US", GameQuery(collection="steam-popular", genre="Classics")
    )
    assert classics.items == ()
    assert default_sort(GameQuery(collection="steam-popular"), True) == "catalog"


@pytest.mark.anyio
async def test_my_games_combine_game_pass_and_the_steam_library(tmp_path):
    service, _steam = make_service(tmp_path, page_size=10)
    mine = await service.browse("DE", "en-US", GameQuery(collection="mine", sort="played"))
    assert [game.title for game in mine.items] == ["Gamma", "Delta", "Zulu"]
    only_steam = await service.browse(
        "DE", "en-US", GameQuery(collection="mine", sort="title", sources=("steam",))
    )
    assert [game.title for game in only_steam.items] == ["Gamma"]
    played = await service.browse(
        "DE", "en-US", GameQuery(collection="most-played", sort="playtime")
    )
    assert [(game.title, game.hours_played) for game in played.items] == [("Gamma", "10 h")]
    search = await service.browse("DE", "en-US", GameQuery(collection="all", search="ta"))
    assert {game.title for game in search.items} == {"Beta", "Delta"}


@pytest.mark.anyio
async def test_owned_games_refresh_privately_and_keep_the_last_list_on_failure(tmp_path):
    service, steam = make_service(tmp_path)
    account = await service.refresh_owned()
    assert account.owned_status == "ok" and account.persona == "Rez"
    steam.owned_games = None  # The user made their game details private.
    account = await service.refresh_owned(force=True)
    assert account.owned_status == "private" and account.owned  # Last list is kept.
    page = await service.browse("DE", "en-US", GameQuery(collection="mine", sort="title"))
    assert "steam-private" in page.notices

    async def broken(steam_id):
        raise SteamError("Steam is unavailable.")

    steam.owned = broken
    assert (await service.refresh_owned(force=False)).owned_status == "private"  # Still fresh.
    with pytest.raises(SteamError):
        await service.refresh_owned(force=True)
    service.disconnect()
    assert service.account() is None


@pytest.mark.anyio
async def test_steam_pages_survive_a_game_pass_outage(tmp_path):
    service, _steam = make_service(tmp_path, page_size=10)
    service.client.fail = True
    page = await service.browse("DE", "en-US", GameQuery(collection="steam-popular"))
    assert len(page.items) == 5 and "gamepass-unavailable" in page.notices
    with pytest.raises(GamePassError):
        await service.browse("DE", "en-US", GameQuery(collection="popular"))


@pytest.mark.anyio
async def test_details_join_both_stores_and_games_can_be_separated(tmp_path):
    service, _steam = make_service(tmp_path)
    game = await service.details("steam-40", "DE", "en-US")
    assert game.xbox_id == B and game.game_pass and game.rating == 4.5
    assert (await service.details(f"xbox-{B}", "DE", "en-US")).id == "steam-40"
    assert (await service.details(A, "DE", "en-US")).id == f"xbox-{A}"
    owned = await service.details("steam-30", "DE", "en-US")
    assert owned.owned and owned.hours_played == "10 h"
    assert set(await service.unlink("steam-40")) == {B, C}
    assert (await service.details(f"xbox-{B}", "DE", "en-US")).id == f"xbox-{B}"


@pytest.mark.anyio
async def test_access_marks_saved_games(tmp_path):
    service, _steam = make_service(tmp_path)
    saved = [
        Game(f"xbox-{B}", "Delta for Xbox", xbox_id=B),
        Game("steam-30", "Gamma", steam_appid=30, owned=False),
        Game("steam-10", "Alpha", steam_appid=10, game_pass=True),
    ]
    marked = await service.access(saved, "DE", "en-US", GameQuery())
    assert [(game.game_pass, game.owned) for game in marked] == [
        (True, False),
        (False, True),
        (False, False),
    ]


def test_link_corrections_are_never_overwritten_by_automatic_findings(tmp_path):
    repository = GameLinkRepository(tmp_path / "app.db")
    repository.initialize()
    repository.save([GameLink(A, 0, "user", 1)])
    repository.save([GameLink(A, 100, "itad", 2)])
    assert repository.get([A])[A].source == "user"


# Web --------------------------------------------------------------------------------------------


def test_steam_sign_in_saving_and_pages(tmp_path):
    from test_web import FakeCatalog

    from mytaste.config import AppSettings
    from mytaste.web.app import create_app

    service, steam = make_service(tmp_path, connected=False)
    checked = []

    async def verify(params, return_to):
        checked.append(return_to)
        if params.get("openid.sig") != "good":
            raise SteamProfileError("That Steam sign-in could not be verified. Try again.")
        return STEAM_ID

    steam.verify_openid = verify
    app = create_app(AppSettings(None, tmp_path / "app.db"), catalog=FakeCatalog(), games=service)
    with owner_client(app) as client:
        app.state.preferences.save("DE", (8, 337))
        assert "Connect Steam" in client.get("/settings?add=steam").text
        assert 'data-open-step="steam"' in client.get("/settings?add=steam").text
        start = client.get(
            "/games/steam/connect?next=/collections/games/mine", follow_redirects=False
        )
        assert start.status_code == 303
        issued = client.cookies.get("mytaste_steam_state")
        params = parse_qs(urlparse(start.headers["location"]).query)
        return_to = params["openid.return_to"][0]
        state = parse_qs(urlparse(return_to).query)["state"][0]
        assert return_to.startswith("http://testserver/games/steam/callback?state=")
        assert params["openid.realm"] == ["http://testserver/"]
        cookie = start.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/games/steam" in cookie

        forged = client.get(
            "/games/steam/callback",
            params={"state": "guess", "openid.sig": "good"},
            follow_redirects=False,
        )
        assert "steam_error=" in forged.headers["location"] and service.account() is None
        rejected = client.get(
            "/games/steam/callback",
            params={"state": state, "openid.sig": "bad"},
            follow_redirects=False,
        )
        assert "steam_error=" in rejected.headers["location"] and service.account() is None
        client.cookies.set("mytaste_steam_state", issued, path="/games/steam")
        signed_in = client.get(
            "/games/steam/callback",
            params={"state": state, "openid.sig": "good"},
            follow_redirects=False,
        )
        # Back on Services, Done still returns to the games page that opened it.
        assert signed_in.headers["location"] == (
            "/settings?steam=connected&next=%2Fcollections%2Fgames%2Fmine#steam"
        )
        assert checked[-1] == return_to
        assert service.account().steam_id == STEAM_ID and service.account().owned

        settings = client.get("/settings?steam=connected").text
        assert "Steam is connected." in settings and "Rez" in settings
        assert "/games/steam/disconnect" in settings and "1 game" not in settings
        assert "2 games" in settings  # Steam's list, before tools are left out of browsing.

        mine = client.get("/collections/games/mine?render=1").text
        assert "In your Steam library" in mine and "10 h played" in mine
        assert "Recently played" in mine and 'name="source" value="steam"' in mine
        only_gamepass = client.get("/collections/games/mine?source=gamepass&render=1").text
        assert "In your Steam library" not in only_gamepass.split("data-game-results")[1]
        # The sidebar script sends one comma-separated list, with "none" from the hidden field.
        for value in ("none,gamepass", "gamepass"):
            page = client.get(f"/collections/games/mine?source={value}&render=1").text
            assert 'id="game-source-gamepass" name="source" value="gamepass" checked' in page
            assert 'id="game-source-steam" name="source" value="steam" >' in page
        both = client.get("/collections/games/mine?source=none,gamepass,steam&render=1").text
        assert 'value="steam" checked' in both and 'value="gamepass" checked' in both

        assert client.get(f"/games/xbox-{B}", follow_redirects=False).headers["location"] == (
            "/games/steam-40"
        )
        assert client.get(f"/games/{A}", follow_redirects=False).headers["location"] == (
            f"/games/xbox-{A}"
        )
        details = client.get("/api/items/game/steam-40/details").json()
        assert details["xbox_url"] and details["steam_url"] and details["game_pass"]

        watchlist = app.state.collections.collections()[0]
        saved = client.put(f"/api/collections/{watchlist.id}/items/game/steam-30")
        assert saved.json() == {"saved": True, "added": True}
        listing = client.get("/api/items/game/steam-30/collections").json()["collections"]
        assert listing[0]["saved"] and not listing[1]["saved"]
        assert "is-saved" in client.get("/collections/games/mine?render=1").text
        page = client.get(f"/collections/{watchlist.id}").text
        assert 'data-detail-url="/api/items/game/steam-30/details"' in page
        assert 'id="game-dialog"' in page and "games.js" in page
        assert client.delete(f"/api/collections/{watchlist.id}/items/game/steam-30").json() == {
            "saved": False,
            "removed": True,
        }
        assert client.get("/api/items/game/bogus/collections").status_code == 404

        assert client.post("/api/games/steam-40/unlink").json()["separated"] == [B, C]
        assert client.post("/games/steam/refresh", follow_redirects=False).status_code == 303
        client.post("/games/steam/disconnect")
        assert service.account() is None
        reconnect = client.post(
            "/games/steam/profile",
            data={"profile": "robin", "next": "//evil.example"},
            follow_redirects=False,
        )
        assert reconnect.headers["location"] == "/settings?steam=connected#steam"


# All: movies, series, and games -----------------------------------------------------------------


@pytest.mark.anyio
async def test_playable_games_alternate_game_pass_ranking_and_the_steam_library(tmp_path):
    service, _steam = make_service(tmp_path)
    query = GameQuery()
    games = await service.playable("DE", "en-US", query, game_pass=True, steam=True)
    # Game Pass's popular list (Delta, then Zulu) alternates with owned games not on it.
    assert [game.title for game in games] == ["Delta", "Gamma", "Zulu"]
    assert games[0].game_pass and games[1].owned and games[1].playtime == 600
    only_steam = await service.playable("DE", "en-US", query, game_pass=False, steam=True)
    assert [game.title for game in only_steam] == ["Gamma"]
    assert await service.playable("DE", "en-US", query, game_pass=False, steam=False) == []

    # Past Microsoft's popular list, the catalog (A–Z) is ranked by its number of reviews.
    (tmp_path / "unranked").mkdir()
    unranked, _steam = make_service(tmp_path / "unranked")
    unranked.client.lists["popular"] = []
    unranked.client.games[A] = replace(unranked.client.games[A], rating_count=5)
    games = await unranked.playable("DE", "en-US", query, game_pass=True, steam=False)
    assert [game.title for game in games] == ["Delta", "Zulu"]  # 10 Steam reviews, then 5.


def test_all_mixes_the_games_you_can_play_with_movies_and_series(tmp_path):
    from test_web import FakeCatalog

    from mytaste.config import AppSettings
    from mytaste.web.app import create_app

    service, _steam = make_service(tmp_path)
    app = create_app(AppSettings(None, tmp_path / "app.db"), catalog=FakeCatalog(), games=service)
    with owner_client(app) as client:
        app.state.preferences.save("DE", (8, 337))
        app.state.game_preferences.save("ultimate", "pc")

        def titles(url):
            page = client.get(url).text
            return page, [
                name
                for name in ("A New Film", "A New Series", "Delta", "Gamma", "Zulu")
                if f">{name}<" in page
            ]

        page, found = titles("/?q=a")
        assert found == ["A New Film", "A New Series", "Delta", "Gamma"]
        assert 'name="games" value="gamepass" checked' in page
        assert 'name="games" value="steam" checked' in page
        assert 'id="game-dialog"' in page and "games.js" in page
        assert titles("/?q=a&games=steam")[1] == ["A New Film", "A New Series", "Gamma"]
        assert titles("/?q=a&games=none")[1] == ["A New Film", "A New Series"]
        movies, found = titles("/?q=a&media=movie")
        assert found == ["A New Film", "A New Series"] and 'name="games"' not in movies
        # A filter only TMDB titles can answer leaves games out.
        assert "Delta" not in titles("/?q=a&genre=drama")[1]
        # Without a Game Pass plan, only the Steam library counts as yours.
        app.state.game_preferences.clear()
        page, found = titles("/?q=a")
        assert found == ["A New Film", "A New Series", "Gamma"]
        assert 'value="gamepass"' not in page

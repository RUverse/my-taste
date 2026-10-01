from __future__ import annotations

import asyncio
import base64
import json
from datetime import date
from urllib.parse import quote

import httpx

from mytaste.catalog.tmdb import TMDBClient


def test_latest_movies_use_region_and_subscription_filters() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 12,
                        "title": "A New Film",
                        "release_date": "2026-08-20",
                        "overview": "A test film.",
                        "vote_average": 7.25,
                        "poster_path": "/poster.jpg",
                    }
                ]
            },
        )

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        items = asyncio.run(client.latest("movie", "DE", (337, 8, 8), today=date(2026, 8, 27)))
    finally:
        asyncio.run(client.close())

    assert captured["watch_region"] == "DE"
    assert captured["with_watch_providers"] == "8|337"
    assert captured["with_watch_monetization_types"] == "flatrate"
    assert captured["primary_release_date.lte"] == "2026-08-27"
    assert items[0].title == "A New Film"
    assert items[0].rating == 7.2


def test_providers_merge_movie_and_tv_lists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/movie"):
            results = [
                {
                    "provider_id": 8,
                    "provider_name": "Netflix",
                    "logo_path": "/netflix.jpg",
                    "display_priority": 2,
                }
            ]
        else:
            results = [
                {
                    "provider_id": 8,
                    "provider_name": "Netflix",
                    "logo_path": "/netflix.jpg",
                    "display_priority": 2,
                },
                {
                    "provider_id": 337,
                    "provider_name": "Disney Plus",
                    "logo_path": "/disney.jpg",
                    "display_priority": 1,
                },
            ]
        return httpx.Response(200, json={"results": results})

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        providers = asyncio.run(client.providers("DE"))
    finally:
        asyncio.run(client.close())

    assert [provider.name for provider in providers] == ["Disney Plus", "Netflix"]


def test_v3_api_key_uses_query_authentication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == "0123456789abcdef0123456789abcdef"
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"results": []})

    client = TMDBClient(
        "0123456789abcdef0123456789abcdef",
        transport=httpx.MockTransport(handler),
    )
    try:
        regions = asyncio.run(client.regions())
    finally:
        asyncio.run(client.close())

    assert regions == ()


def test_discover_applies_genre_year_and_rating_filters() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        return httpx.Response(200, json={"page": 2, "total_pages": 4, "results": []})

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        page = asyncio.run(
            client.discover(
                "tv",
                "DE",
                (8,),
                sort_by="popularity.desc",
                genre_id=18,
                year_from=2020,
                year_to=2024,
                minimum_rating=7,
                page=2,
            )
        )
    finally:
        asyncio.run(client.close())

    assert captured["with_genres"] == "18"
    assert captured["first_air_date.gte"] == "2020-01-01"
    assert captured["first_air_date.lte"] == "2024-12-31"
    assert captured["vote_average.gte"] == "7"
    assert page.page == 2


def test_movie_people_returns_directors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "crew": [
                    {"job": "Director", "name": "First Director"},
                    {"job": "Writer", "name": "A Writer"},
                ]
            },
        )

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        people = asyncio.run(client.people("movie", 12))
    finally:
        asyncio.run(client.close())

    assert people == ("Director", ("First Director",))


def test_details_returns_youtube_trailer_and_pictured_cast() -> None:
    requested_paths: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.add(request.url.path)
        if request.url.path.endswith("/credits"):
            return httpx.Response(
                200,
                json={
                    "cast": [
                        {
                            "id": 4,
                            "name": "Lead Actor",
                            "character": "The Lead",
                            "profile_path": "/actor.jpg",
                        }
                    ]
                },
            )
        if request.url.path.endswith("/videos"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "site": "YouTube",
                            "type": "Teaser",
                            "key": "teaser-key",
                            "official": True,
                        },
                        {
                            "site": "YouTube",
                            "type": "Trailer",
                            "key": "trailer-key",
                            "official": True,
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 12,
                "title": "A New Film",
                "release_date": "2026-08-20",
                "overview": "A test film.",
                "vote_average": 7.25,
                "runtime": 126,
                "poster_path": "/poster.jpg",
                "backdrop_path": "/backdrop.jpg",
                "genres": [{"id": 18, "name": "Drama"}],
            },
        )

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        details = asyncio.run(client.details("movie", 12))
    finally:
        asyncio.run(client.close())

    assert requested_paths == {
        "/3/movie/12",
        "/3/movie/12/credits",
        "/3/movie/12/videos",
    }
    assert details.title == "A New Film"
    assert details.runtime_minutes == 126
    assert details.backdrop_url == "https://image.tmdb.org/t/p/original/backdrop.jpg"
    assert details.trailer_key == "trailer-key"
    assert details.cast[0].name == "Lead Actor"
    assert details.cast[0].profile_url == "https://image.tmdb.org/t/p/w185/actor.jpg"


def test_video_failure_keeps_base_details_and_cast() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/videos"):
            return httpx.Response(503)
        if request.url.path.endswith("/credits"):
            return httpx.Response(
                200,
                json={"cast": [{"id": 4, "name": "Lead Actor"}]},
            )
        return httpx.Response(
            200,
            json={
                "id": 12,
                "title": "A New Film",
                "release_date": "2026-08-20",
                "overview": "A test film.",
                "vote_average": 7.2,
                "genres": [{"id": 18, "name": "Drama"}],
            },
        )

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        details = asyncio.run(client.details("movie", 12))
    finally:
        asyncio.run(client.close())

    assert details.title == "A New Film"
    assert details.overview == "A test film."
    assert details.genres == ("Drama",)
    assert details.cast[0].name == "Lead Actor"
    assert details.trailer_key is None


def test_lookup_passes_year_hint_per_media_type() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 496243,
                        "title": "Parasite",
                        "name": "Parasite",
                        "release_date": "2019-05-30",
                        "first_air_date": "2019-05-30",
                        "vote_average": 8.5,
                    }
                ]
            },
        )

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        movies = asyncio.run(client.lookup("movie", "Parasite", year=2019))
        shows = asyncio.run(client.lookup("tv", "Parasite"))
    finally:
        asyncio.run(client.close())

    assert captured[0]["primary_release_year"] == "2019"
    assert "first_air_date_year" not in captured[1]
    assert movies[0].id == 496243
    assert shows[0].media_type == "tv"


def _justwatch_href(provider_id: int, provider: str, monetization: str, target: str) -> str:
    context = {
        "data": [
            {
                "schema": "clickout",
                "data": {
                    "provider": provider,
                    "providerId": provider_id,
                    "monetizationType": monetization,
                },
            },
            {"schema": "title", "data": {"titleId": 2, "objectType": "show"}},
        ]
    }
    encoded = base64.urlsafe_b64encode(json.dumps(context).encode()).decode().rstrip("=")
    return (
        f'<a href="https://click.justwatch.com/a?cx={encoded}&amp;r={quote(target, safe="")}'
        f'&amp;uct_country=de" title="Watch on {provider}">'
    )


def test_watch_links_read_provider_pages_without_the_api_credential() -> None:
    requests: list[httpx.Request] = []
    markup = "".join(
        (
            _justwatch_href(10, "Amazon Video", "rent", "https://watch.amazon.de/rent"),
            _justwatch_href(8, "Netflix", "flatrate", "https://www.netflix.com/title/80057281"),
            _justwatch_href(8, "Netflix", "flatrate", "https://www.netflix.com/title/other"),
            _justwatch_href(10, "Amazon Video", "flatrate", "https://watch.amazon.de/prime"),
            _justwatch_href(
                2706,
                "Disney Plus",
                "flatrate",
                "https://disneyplus.bn5x.net/c/1?u="
                + quote("https://www.disneyplus.com/x", safe=""),
            ),
            _justwatch_href(66, "Unsafe", "flatrate", "javascript:alert(1)"),
            '<a href="https://click.justwatch.com/a?cx=not-base64!&amp;r=https%3A%2F%2Fx.test">',
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=f"<html><body>{markup}</body></html>")

    client = TMDBClient("read-access-token", transport=httpx.MockTransport(handler))
    try:
        links = asyncio.run(client.watch_links("tv", 66732, "DE"))
        invalid_region = asyncio.run(client.watch_links("tv", 66732, "de&x=1"))
    finally:
        asyncio.run(client.close())

    assert str(requests[0].url) == "https://www.themoviedb.org/tv/66732/watch?locale=DE"
    assert "authorization" not in requests[0].headers
    assert len(requests) == 1
    assert invalid_region == ()
    assert [(link.provider_id, link.provider_name, link.url) for link in links] == [
        (10, "Amazon Video", "https://watch.amazon.de/prime"),
        (8, "Netflix", "https://www.netflix.com/title/80057281"),
        (2706, "Disney Plus", "https://www.disneyplus.com/x"),
    ]


def test_watch_links_are_empty_when_the_page_fails() -> None:
    client = TMDBClient(
        "token", transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    )
    try:
        assert asyncio.run(client.watch_links("movie", 1, "DE")) == ()
    finally:
        asyncio.run(client.close())


def test_seasons_batch_requests_and_list_specials_last() -> None:
    appended: list[str] = []

    def episode(number: int, **extra: object) -> dict[str, object]:
        return {"episode_number": number, "name": f"Episode {number}", **extra}

    def handler(request: httpx.Request) -> httpx.Response:
        append = request.url.params.get("append_to_response")
        if append is None:
            return httpx.Response(
                200,
                json={
                    "seasons": [
                        {"season_number": 0, "episode_count": 1},
                        {"season_number": 1, "episode_count": 2},
                        {"season_number": 2, "episode_count": 0},
                    ]
                },
            )
        appended.append(append)
        return httpx.Response(
            200,
            json={
                "season/0": {"name": "Specials", "episodes": [episode(1)]},
                "season/1": {
                    "name": "Season 1",
                    "episodes": [
                        episode(2, runtime=0, air_date="2026-02-01"),
                        episode(
                            1,
                            name="Pilot",
                            runtime=58,
                            still_path="/still.jpg",
                            air_date="2026-01-01",
                        ),
                        {"name": "No number"},
                    ],
                },
            },
        )

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        seasons = asyncio.run(client.seasons(1399))
    finally:
        asyncio.run(client.close())

    assert appended == ["season/0,season/1"]
    assert [(season.season_number, season.name) for season in seasons] == [
        (1, "Season 1"),
        (0, "Specials"),
    ]
    pilot, second = seasons[0].episodes
    assert (pilot.episode_number, pilot.name, pilot.runtime_minutes) == (1, "Pilot", 58)
    assert pilot.still_url == "https://image.tmdb.org/t/p/w300/still.jpg"
    assert second.runtime_minutes is None

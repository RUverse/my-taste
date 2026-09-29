from __future__ import annotations

import asyncio
from datetime import date

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

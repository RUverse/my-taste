from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
from starlette.datastructures import QueryParams

from mytaste.catalog.facts import FactsService
from mytaste.catalog.filtering import FilterResolver
from mytaste.catalog.filters import (
    LocalFacets,
    TitleFacts,
    TitleFilters,
    credit_roles,
    discover_params,
    genre_choices,
    genre_queries,
    title_matches,
)
from mytaste.catalog.models import (
    BrowseCategory,
    BrowseQuery,
    CatalogItem,
    CatalogPage,
    Genre,
)
from mytaste.catalog.service import CatalogService
from mytaste.catalog.tmdb import TMDBClient, TMDBError
from mytaste.library.models import ScannedFile
from mytaste.playback.service import PlaybackService, file_facets
from mytaste.storage.facts import FactsRepository
from mytaste.storage.library import ItemDraft, LibraryRepository
from mytaste.storage.playback import PlaybackRepository, PlayState
from mytaste.web.filter_options import filter_params, parse_filters

MOVIE_GENRES = (
    Genre(28, "Action", "movie"),
    Genre(12, "Adventure", "movie"),
    Genre(18, "Drama", "movie"),
    Genre(53, "Thriller", "movie"),
)
TV_GENRES = (
    Genre(10759, "Action & Adventure", "tv"),
    Genre(18, "Drama", "tv"),
    Genre(10762, "Kids", "tv"),
)


def film(item_id: int, title: str, **values: object) -> CatalogItem:
    values.setdefault("release_date", "2020-01-01")
    values.setdefault("overview", "")
    values.setdefault("rating", 7.0)
    return CatalogItem(id=item_id, media_type="movie", title=title, **values)  # type: ignore[arg-type]


def test_genre_choices_join_movie_and_series_genres() -> None:
    choices = {choice.slug: choice for choice in genre_choices(MOVIE_GENRES, TV_GENRES)}

    assert (choices["action"].movie_id, choices["action"].tv_id) == (28, 10759)
    assert (choices["adventure"].movie_id, choices["adventure"].tv_id) == (12, 10759)
    assert choices["action"].label_for("tv") == "Action & Adventure"
    assert choices["drama"].ids == (18, 18)
    assert (choices["thriller"].tv_id, choices["kids"].movie_id) == (None, None)
    assert "action-and-adventure" not in choices, "series genres covered by movie ones merge"
    assert not choices["kids"].supports("movie") and choices["kids"].supports("all")


def test_filter_params_round_trip_through_the_url() -> None:
    genres = genre_choices(MOVIE_GENRES, TV_GENRES)

    params = QueryParams(
        "genre=action,unknown&exclude_genre=kids&country=kr&country=JP,toolong&language=ko"
        "&content_rating=16,%3Cscript%3E&runtime_min=120&runtime_max=60&actor=287,abc,0"
        "&watch=sometimes&resolution=4K"
    )
    filters = parse_filters(params, genres, "DE")

    assert filters.genre_ids == (28, 10759)
    assert filters.exclude_genre_ids == (10762,)
    assert filters.countries == ("KR", "JP")
    assert filters.certifications == ("16",) and filters.certification_country == "DE"
    assert (filters.runtime_min, filters.runtime_max) == (60, 120), "a reversed range is swapped"
    assert filters.cast == (287,)
    assert filters.watch == ""
    assert filter_params(filters, genres) == [
        ("genre", "action"),
        ("exclude_genre", "kids"),
        ("content_rating", "16"),
        ("country", "KR,JP"),
        ("language", "ko"),
        ("actor", "287"),
        ("resolution", "4K"),
        ("runtime_min", "60"),
        ("runtime_max", "120"),
    ]
    assert not parse_filters(QueryParams(""), genres, "DE").active


def test_discover_params_and_genre_requests() -> None:
    filters = TitleFilters(
        genre_ids=(35, 80),
        exclude_genre_ids=(16, 10762),
        certifications=("12", "16"),
        certification_country="DE",
        countries=("KR",),
        languages=("ko", "ja"),
        runtime_max=30,
        keyword_ids=(4379,),
    )

    assert dict(discover_params(filters)) == {
        "without_genres": "16,10762",
        "certification_country": "DE",
        "certification": "12|16",
        "with_origin_country": "KR",
        "with_original_language": "ko|ja",
        "with_runtime.gte": "1",
        "with_runtime.lte": "30",
        "with_keywords": "4379",
    }
    assert genre_queries(None, filters) == ("35|80",)
    assert genre_queries(18, filters) == ("18,35", "18,80"), "TMDB cannot mix and/or"
    assert genre_queries(35, filters) == ("35",)
    assert genre_queries(18, TitleFilters()) == ("18",)
    assert genre_queries(None, TitleFilters()) == (None,)


def test_title_matches_every_kind_of_filter() -> None:
    facts = TitleFacts(
        original_language="ko",
        countries=("KR",),
        runtime=95,
        certifications=(("DE", "16"), ("US", "R")),
        keyword_ids=(1, 2),
        cast=(10,),
        directors=(20,),
    )
    local = LocalFacets(
        resolutions=frozenset({"1080p"}),
        audio_languages=frozenset({"fa", "en"}),
        watch="in_progress",
    )

    def matches(filters: TitleFilters, **values: object) -> bool:
        options: dict[str, object] = {"genre_ids": (18, 80), "facts": facts, "local": local}
        options.update(values)
        return title_matches(filters, **options)  # type: ignore[arg-type]

    assert matches(TitleFilters())
    assert matches(TitleFilters(genre_ids=(35, 80)))
    assert not matches(TitleFilters(genre_ids=(35,)))
    assert not matches(TitleFilters(exclude_genre_ids=(80,)))
    assert matches(TitleFilters(certifications=("16",), certification_country="DE"))
    assert not matches(TitleFilters(certifications=("R",), certification_country="DE"))
    assert matches(TitleFilters(countries=("JP", "KR"), languages=("ko",)))
    assert matches(TitleFilters(runtime_min=90, runtime_max=120))
    assert not matches(TitleFilters(runtime_max=90))
    assert not matches(TitleFilters(runtime_max=90), facts=replace(facts, runtime=None))
    assert matches(TitleFilters(keyword_ids=(2, 3), cast=(10, 11), directors=(20,)))
    assert not matches(TitleFilters(cast=(10,), writers=(30,))), "roles must all match"
    assert not matches(TitleFilters(languages=("ko",)), facts=None), "unknown titles fail"
    assert matches(TitleFilters(resolutions=("4K", "1080p"), audio_languages=("fa",)))
    assert not matches(TitleFilters(resolutions=("1080p",)), local=None)
    assert matches(TitleFilters(watch="in_progress"))
    assert matches(TitleFilters(watch="unwatched"), local=None), "not played here: unwatched"
    assert not matches(TitleFilters(watch="watched"))
    assert TitleFilters(watch="unwatched").needs_local
    assert not TitleFilters(watch="unwatched").local_only
    assert TitleFilters(watch="watched").local_only and TitleFilters(resolutions=("4K",)).local_only


def test_credit_roles_follow_tmdb_jobs() -> None:
    assert credit_roles({"character": "Cobb"}, "movie", cast=True) == {"cast"}
    assert credit_roles({"character": "Self - Host"}, "tv", cast=True) == frozenset()
    assert credit_roles({"roles": [{"character": "Himself"}]}, "tv", cast=True) == frozenset()
    assert credit_roles({"job": "Director", "department": "Directing"}, "movie", cast=False) == {
        "director"
    }
    assert credit_roles({"job": "Screenplay", "department": "Writing"}, "movie", cast=False) == {
        "writer"
    }
    assert credit_roles({"job": "Creator", "department": "Creator"}, "tv", cast=False) == {
        "director",
        "writer",
    }
    assert credit_roles(
        {"jobs": [{"job": "Executive Producer"}], "department": "Production"}, "tv", cast=False
    ) == {"producer"}
    assert credit_roles({"job": "Editor", "department": "Editing"}, "movie", cast=False) == set()


def test_tmdb_reads_title_facts_and_person_credits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/movie/27205"):
            assert request.url.params["append_to_response"] == "credits,release_dates,keywords"
            return httpx.Response(
                200,
                json={
                    "id": 27205,
                    "original_language": "EN",
                    "origin_country": ["US", "GB"],
                    "runtime": 148,
                    "release_dates": {
                        "results": [
                            {"iso_3166_1": "DE", "release_dates": [{"certification": "12"}]},
                            {"iso_3166_1": "US", "release_dates": [{"certification": ""}]},
                        ]
                    },
                    "keywords": {"keywords": [{"id": 4379, "name": "time travel"}]},
                    "credits": {
                        "cast": [
                            {"id": 6193, "character": "Cobb"},
                            {"id": 1, "character": "Himself"},
                        ],
                        "crew": [
                            {"id": 525, "job": "Director", "department": "Directing"},
                            {"id": 525, "job": "Writer", "department": "Writing"},
                            {"id": 556, "job": "Producer", "department": "Production"},
                        ],
                    },
                },
            )
        if path.endswith("/tv/1396"):
            return httpx.Response(
                200,
                json={
                    "id": 1396,
                    "original_language": "en",
                    "origin_country": ["US"],
                    "episode_run_time": [],
                    "last_episode_to_air": {"runtime": 56},
                    "content_ratings": {"results": [{"iso_3166_1": "DE", "rating": "16"}]},
                    "keywords": {"results": [{"id": 1, "name": "drug"}]},
                    "created_by": [{"id": 66633, "name": "Vince Gilligan"}],
                    "aggregate_credits": {
                        "cast": [{"id": 17419, "roles": [{"character": "Walter White"}]}],
                        "crew": [],
                    },
                },
            )
        if path.endswith("/person/525/combined_credits"):
            return httpx.Response(
                200,
                json={
                    "cast": [
                        {
                            "id": 9,
                            "media_type": "tv",
                            "name": "A Talk Show",
                            "first_air_date": "2000-01-01",
                            "character": "Self",
                        }
                    ],
                    "crew": [
                        {
                            "id": 27205,
                            "media_type": "movie",
                            "title": "Inception",
                            "release_date": "2010-07-15",
                            "job": "Director",
                            "department": "Directing",
                            "popularity": 80,
                        },
                        {
                            "id": 27205,
                            "media_type": "movie",
                            "title": "Inception",
                            "release_date": "2010-07-15",
                            "job": "Producer",
                            "department": "Production",
                        },
                        {
                            "id": 1,
                            "media_type": "movie",
                            "title": "Adult",
                            "release_date": "2010-01-01",
                            "job": "Director",
                            "adult": True,
                        },
                    ],
                },
            )
        return httpx.Response(404)

    client = TMDBClient("token", transport=httpx.MockTransport(handler))
    try:
        movie = asyncio.run(client.title_facts("movie", 27205))
        series = asyncio.run(client.title_facts("tv", 1396))
        credits = asyncio.run(client.person_credits(525))
    finally:
        asyncio.run(client.close())

    assert movie == TitleFacts(
        original_language="en",
        countries=("US", "GB"),
        runtime=148,
        certifications=(("DE", "12"),),
        keyword_ids=(4379,),
        cast=(6193,),
        directors=(525,),
        writers=(525,),
        producers=(556,),
    )
    assert TitleFacts.from_dict(json.loads(json.dumps(movie.to_dict()))) == movie
    assert series.runtime == 56, "series without episode lengths use the latest episode"
    assert series.certifications == (("DE", "16"),)
    assert (series.cast, series.directors, series.writers) == ((17419,), (66633,), (66633,))
    assert [(item.title, sorted(roles)) for item, roles in credits] == [
        ("Inception", ["director", "producer"])
    ]


class FactsCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def title_facts(self, media_type: str, item_id: int) -> TitleFacts:
        self.calls.append((media_type, item_id))
        if item_id == 404:
            raise TMDBError("Not found")
        return TitleFacts(original_language="en", runtime=item_id)


def test_facts_are_fetched_once_and_kept_in_sqlite(tmp_path: Path) -> None:
    repository = FactsRepository(tmp_path / "mytaste.db")
    repository.initialize()
    catalog = FactsCatalog()

    async def run() -> tuple[dict, dict]:
        service = FactsService(repository, catalog)
        first = await service.facts([("movie", 90), ("tv", 404), ("movie", 0)])
        second = await FactsService(repository, catalog).facts([("movie", 90)])
        return first, second

    first, second = asyncio.run(run())
    assert first == {("movie", 90): TitleFacts(original_language="en", runtime=90)}
    assert second == first
    assert catalog.calls == [("movie", 90), ("tv", 404)], "stored facts are not fetched again"


class CreditsClient:
    """A person credited on two movies and a series; TMDB discover must not be used."""

    async def close(self) -> None:
        return None

    async def genres(self, media_type: str) -> tuple[Genre, ...]:
        return MOVIE_GENRES if media_type == "movie" else TV_GENRES

    async def person_credits(self, person_id: int):
        series = CatalogItem(13, "tv", "A Series", "2015-01-01", "", 8.0, popularity=50)
        return (
            (film(11, "Popular Film", popularity=90, genre_ids=(18,)), frozenset({"director"})),
            (film(12, "Old Film", popularity=10, release_date="1990-01-01"), frozenset({"cast"})),
            (film(14, "Unreleased", release_date="2999-01-01"), frozenset({"director"})),
            (film(15, "Elsewhere", popularity=70), frozenset({"director"})),
            (series, frozenset({"director", "writer"})),
        )

    async def watch_provider_ids(self, media_type: str, item_id: int, region: str):
        return frozenset() if item_id == 15 else frozenset({8})

    async def discover(self, *args: object, **kwargs: object) -> CatalogPage:
        raise AssertionError("people filters do not use discover")


def test_people_filters_read_credits_and_check_services() -> None:
    service = CatalogService(CreditsClient())
    query = BrowseQuery(
        category="popular", provider_ids=(8,), filters=TitleFilters(directors=(525,))
    )

    async def refine(items):
        return frozenset((item.media_type, item.id) for item in items if item.id != 13)

    page = asyncio.run(service.browse("DE", query))
    refined = asyncio.run(service.browse("DE", query, refine=refine))
    dramas = asyncio.run(service.browse("DE", replace(query, category="drama")))

    assert [item.title for item in page.items] == ["Popular Film", "A Series"]
    assert page.total_results == 2
    assert [item.title for item in refined.items] == ["Popular Film"]
    assert [item.title for item in dramas.items] == ["Popular Film"]


class DiscoverClient(CreditsClient):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def discover(self, media_type: str, *args: object, **kwargs: object) -> CatalogPage:
        self.calls.append({"media_type": media_type, **kwargs})
        items = (film(21, "Watched Film"), film(22, "New Film"))
        return CatalogPage(items=items if media_type == "movie" else (), total_results=2)


def test_discover_gets_filter_params_and_drops_excluded_titles() -> None:
    client = DiscoverClient()
    service = CatalogService(client)
    filters = TitleFilters(genre_ids=(28, 10759), languages=("ko",), watch="unwatched")
    query = BrowseQuery(category="drama", provider_ids=(8,), filters=filters)

    page = asyncio.run(service.browse("DE", query, exclude=frozenset({("movie", 21)})))
    local_only = asyncio.run(
        service.browse("DE", replace(query, filters=TitleFilters(resolutions=("4K",))))
    )

    assert [item.title for item in page.items] == ["New Film"]
    assert {(call["media_type"], call["genres"]) for call in client.calls} == {
        ("movie", "18,28"),
        ("movie", "18,10759"),
        ("tv", "18,28"),
        ("tv", "18,10759"),
    }
    assert all(dict(call["extra"])["with_original_language"] == "ko" for call in client.calls)
    assert local_only.items == ()
    assert len(client.calls) == 4, "file filters never ask TMDB for streaming titles"


def make_library(tmp_path: Path) -> tuple[LibraryRepository, PlaybackRepository, list[int]]:
    library = LibraryRepository(tmp_path / "mytaste.db")
    library.initialize()
    playback = PlaybackRepository(library.database_path)
    playback.initialize()
    created = library.add_library("Media", [(str(tmp_path / "media"), "movie")])

    def scanned(name: str, season: int | None = None, episode: int | None = None) -> ScannedFile:
        return ScannedFile(
            path=str(tmp_path / "media" / name),
            size=1,
            modified_at="2026-09-01T10:00:00+00:00",
            group_key="unused",
            titles=(name,),
            season=season,
            episode=episode,
        )

    library.replace_items(
        created.id,
        [
            ItemDraft("a", "movie", "Korean Film", (scanned("a.mkv"),), tmdb_id=1, genre_ids=(18,)),
            ItemDraft("b", "movie", "Unmatched", (scanned("b.mkv"),)),
            ItemDraft(
                "c",
                "tv",
                "Show",
                (scanned("c1.mkv", 1, 1), scanned("c2.mkv", 1, 2)),
                tmdb_id=2,
                genre_ids=(35,),
            ),
        ],
    )
    files = {
        Path(file.path).name: file.id
        for item in library.items(created.id)
        for file in library.files_for_item(item.id)
    }
    probe = {
        "video": {"codec": "hevc", "width": 3840, "height": 2160, "hdr": True},
        "audio": [{"codec": "eac3", "channels": 6, "language": "kor"}],
        "subtitles": [{"codec": "subrip", "language": "per"}],
        "external_subtitles": [{"path": "/x.srt", "format": "srt", "language": "en"}],
    }
    with playback._connect() as connection:
        connection.execute(
            "INSERT INTO media_info VALUES (?, 1, '', 'now', ?, NULL)",
            (files["a.mkv"], json.dumps(probe)),
        )
    playback.save_state(PlayState("tv:2:1:1", watched=True))
    return library, playback, [created.id]


def test_local_facets_describe_files_and_watch_progress(tmp_path: Path) -> None:
    library, repository, _ids = make_library(tmp_path)
    service = PlaybackService(
        library, repository, cache_dir=tmp_path / "cache", background_probe=False
    )

    facets = {
        item.title: facets
        for item in library.items(1)
        for key, facets in service.local_facets().items()
        if key == item.id
    }

    film_facets = facets["Korean Film"]
    assert film_facets.resolutions == {"4K"} and film_facets.dynamic_ranges == {"hdr"}
    assert (film_facets.video_codecs, film_facets.audio_codecs) == ({"hevc"}, {"eac3"})
    assert film_facets.audio_channels == {"5.1"} and film_facets.audio_languages == {"ko"}
    assert film_facets.subtitle_languages == {"fa", "en"}
    assert film_facets.watch == "unwatched"
    assert facets["Unmatched"].resolutions == frozenset(), "not probed yet"
    assert facets["Show"].watch == "in_progress", "one of two episodes watched"
    assert file_facets("not json") == LocalFacets()


def test_resolver_limits_library_titles_and_collections(tmp_path: Path) -> None:
    library_repository, repository, library_ids = make_library(tmp_path)
    playback = PlaybackService(
        library_repository, repository, cache_dir=tmp_path / "cache", background_probe=False
    )
    facts_repository = FactsRepository(tmp_path / "mytaste.db")
    facts_repository.initialize()
    facts_repository.save(
        [
            (("movie", 1), TitleFacts(original_language="ko")),
            (("tv", 2), TitleFacts(original_language="en")),
        ]
    )

    class Library:
        def title_refs(self, ids):
            return library_repository.title_refs(ids)

    def resolver(filters: TitleFilters) -> FilterResolver:
        return FilterResolver(
            filters,
            facts=FactsService(facts_repository, FactsCatalog()),
            library=Library(),
            playback=playback,
            library_ids=library_ids,
        )

    ids = {item.title: item.id for item in library_repository.items(library_ids[0])}
    show = CatalogItem(2, "tv", "Show", "2020-01-01", "", 8.0, genre_ids=(35,))
    other = film(3, "Streaming only", genre_ids=(35,))

    assert asyncio.run(resolver(TitleFilters()).local_item_ids()) is None
    assert asyncio.run(resolver(TitleFilters(languages=("ko",))).local_item_ids()) == {
        ids["Korean Film"]
    }
    assert asyncio.run(resolver(TitleFilters(resolutions=("4K",))).local_item_ids()) == {
        ids["Korean Film"]
    }
    assert asyncio.run(resolver(TitleFilters(genre_ids=(35,))).allowed([show, other])) == {
        ("tv", 2),
        ("movie", 3),
    }
    unwatched = resolver(TitleFilters(watch="unwatched"))
    assert asyncio.run(unwatched.allowed([show, other])) == {("movie", 3)}
    assert unwatched.streaming_exclude() == {("tv", 2)}
    assert asyncio.run(unwatched.local_item_ids()) == {ids["Korean Film"], ids["Unmatched"]}

    category = BrowseCategory("popular", "Popular")
    page = library_repository.browse(
        BrowseQuery(library_ids=tuple(library_ids)), category, item_ids={ids["Show"]}
    )
    assert [item.title for item in page.items] == ["Show"]
    assert library_repository.browse(BrowseQuery(), category, item_ids=set()).items == ()

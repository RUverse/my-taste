from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import replace

import httpx
import pytest

from mytaste.catalog.models import BrowseQuery
from mytaste.collections.models import CollectionItem
from mytaste.collections.service import CollectionService
from mytaste.games.models import Game
from mytaste.music.apple import AppleChartClient, ChartEntry
from mytaste.music.coverart import CoverCache
from mytaste.music.grouping import group_albums
from mytaste.music.http import USER_AGENT, MusicError, MusicHTTP
from mytaste.music.listenbrainz import ListenBrainzClient, Ranked
from mytaste.music.matching import match_chart, same_album
from mytaste.music.models import (
    Album,
    AlbumDetails,
    AlbumQuery,
    ArtistCredit,
    Medium,
    Track,
    album_matches,
    genre_matches,
    main_genres,
)
from mytaste.music.musicbrainz import MusicBrainzClient, listen_links, pick_release
from mytaste.music.service import MusicService, candidate_links, sort_albums
from mytaste.storage.collections import CollectionRepository
from mytaste.storage.music import MusicCacheRepository
from mytaste.storage.preferences import DisplayPreferences, PreferenceRepository


@pytest.fixture
def anyio_backend():
    return "asyncio"


def key(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def album(number: int, title: str = "", **values) -> Album:
    defaults = {
        "artists": (ArtistCredit("Artist", key(900)),),
        "release_date": "2020-01-01",
        "primary_type": "album",
    }
    return Album(key(number), title or f"Album {number}", **(defaults | values))


def http(handler, **values) -> MusicHTTP:
    return MusicHTTP(
        "Test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        max_wait=0,
        **values,
    )


# Models ----------------------------------------------------------------------------------


def test_album_types_genres_and_filters():
    live = album(1, secondary_types=("live",))
    assert live.type_label == "Live album" and live.has_type("live") and live.has_type("album")
    assert album(2, primary_type="ep").type_label == "EP"
    assert album(3, secondary_types=("compilation",)).type_label == "Compilation"
    assert album(4, release_date="1957").decade == "earlier"
    assert album(5, release_date="1994-03-01").decade == "1990"
    # Votes decide: a stray tag with a single vote doesn't make an album jazz.
    assert main_genres([("rock", 9), ("jazz", 1), ("art rock", 5)]) == ("rock", "art rock")
    assert genre_matches("rock", ("alternative rock",))
    assert genre_matches("hip-hop", ("pop rap",)) and genre_matches("pop", ("pop rap",))
    assert not genre_matches("pop", ("popular",))
    assert not genre_matches("electronic", ("electropop",))
    query = AlbumQuery(genre="rock", type="live", decade="2020")
    assert album_matches(album(6, genres=("rock",), secondary_types=("live",)), query)
    assert not album_matches(album(7, genres=("rock",)), query)
    with pytest.raises(ValueError):
        AlbumQuery(collection="unknown").validate()
    with pytest.raises(ValueError):
        AlbumQuery(artist="not-an-mbid").validate()
    payload = Album.from_payload(json.loads(json.dumps(live.payload())))
    assert payload == live and payload.portable_id == f"album-mb-{key(1)}"
    assert live.cover_url() == ""
    covered = replace(live, cover_release=key(2), cover_id=12)
    assert covered.cover_url(500) == f"/music/covers/{key(2)}/12-500.jpg"


def test_sort_orders_are_global_and_undated_albums_go_last():
    albums = [
        album(1, "b", release_date="2001", listens=5),
        album(2, "A", release_date="", listens=50),
        album(3, "c", release_date="1999", listens=None),
    ]
    assert [item.title for item in sort_albums(albums, "popularity", True)] == ["A", "b", "c"]
    assert [item.title for item in sort_albums(albums, "release", True)] == ["b", "c", "A"]
    assert [item.title for item in sort_albums(albums, "release", False)] == ["c", "b", "A"]
    assert [item.title for item in sort_albums(albums, "title", False)] == ["A", "b", "c"]


def test_shelves_group_by_artist_decade_genre_and_type():
    albums = [
        album(1, artists=(ArtistCredit("One"),), genres=("rock",), release_date="1995"),
        album(2, artists=(ArtistCredit("Two"),), genres=("pop rock",), release_date="2005"),
        album(3, artists=(ArtistCredit("One"),), primary_type="ep", release_date="1950"),
        album(4, artists=(), secondary_types=("live",), release_date=""),
    ]
    assert [(row.label, len(row.items)) for row in group_albums(albums, "artist")] == [
        ("One", 2),
        ("Two", 1),
        ("Unknown artist", 1),
    ]
    assert [row.label for row in group_albums(albums, "decade")] == [
        "2000s",
        "1990s",
        "Before 1960",
        "Unknown year",
    ]
    genres = {row.label: len(row.items) for row in group_albums(albums, "genre")}
    assert genres["Rock"] == 2 and genres["Pop"] == 1 and genres["No genre"] == 2
    assert [row.label for row in group_albums(albums, "type")] == ["Albums", "EPs", "Live"]


def test_pasted_links_become_the_urls_musicbrainz_keeps():
    assert candidate_links(
        "https://open.spotify.com/intl-de/album/6dVIqQ8qmQ5GBnJ9shOYGE?si=x"
    ) == ["https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"]
    assert candidate_links("https://music.apple.com/gb/album/ok-computer/1097861387?i=1") == [
        "https://music.apple.com/gb/album/1097861387",
        "https://music.apple.com/us/album/1097861387",
    ]
    assert candidate_links("https://radiohead.bandcamp.com/album/ok-computer?from=x") == [
        "https://radiohead.bandcamp.com/album/ok-computer"
    ]
    assert candidate_links("radiohead") == []


# Clients ---------------------------------------------------------------------------------


def metadata(number: int, **values) -> dict:
    data = {
        "release_group": {
            "name": f"Album {number}",
            "date": "2007-10-10",
            "type": "Album",
            "caa_id": 77,
            "caa_release_mbid": key(500 + number),
        },
        "artist": {"artists": [{"name": "Radiohead", "artist_mbid": key(900), "join_phrase": ""}]},
        "tag": {
            "release_group": [
                {"tag": "art rock", "count": 4, "genre_mbid": "g"},
                {"tag": "2000s", "count": 9},
                {"tag": "rock", "count": 1, "genre_mbid": "g"},
            ]
        },
        "release_color": {"red": 113, "green": 78, "blue": 52},
    }
    data.update(values)
    return data


@pytest.mark.anyio
async def test_listenbrainz_lists_metadata_and_split_batches():
    requests = []

    def respond(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/stats/sitewide/release-groups"):
            rows = [
                {"release_group_mbid": key(1), "listen_count": 30},
                {"release_group_mbid": key(1), "listen_count": 29},
                {"release_group_mbid": None, "listen_count": 28},
                {"release_group_mbid": key(2), "listen_count": 20},
            ]
            return httpx.Response(200, json={"payload": {"release_groups": rows}})
        if path.endswith("/popularity/release-group"):
            wanted = json.loads(request.content)["release_group_mbids"]
            return httpx.Response(
                200,
                json=[
                    {"release_group_mbid": key(1), "total_listen_count": 7, "total_user_count": 2}
                ]
                + [{"release_group_mbid": item, "total_listen_count": None} for item in wanted[1:]],
            )
        ids = request.url.params["release_group_mbids"].split(",")
        if len(ids) > 1:
            # Too large: ListenBrainz answers 502 until the batch is smaller.
            return httpx.Response(502)
        if ids == [key(4)]:
            return httpx.Response(502)
        return httpx.Response(200, json={ids[0]: metadata(int(ids[0][-3:]))})

    client = ListenBrainzClient(http(respond, attempts=1))
    ranked = await client.sitewide("month")
    assert [(row.mbid, row.count) for row in ranked] == [(key(1), 30), (key(2), 20)]
    counts = await client.popularity([key(1), key(2)])
    assert counts == {key(1): (7, 2), key(2): (0, 0)}
    found = await client.metadata([key(1), key(3), key(4)])
    assert set(found) == {key(1), key(3)}  # A batch that keeps failing loses only its bad half.
    first = found[key(1)]
    assert first.title == "Album 1" and first.artist == "Radiohead"
    assert first.genres == ("art rock",) and first.color == "#714e34"
    assert first.cover_url() == f"/music/covers/{key(501)}/77-250.jpg"
    assert first.primary_type == "album" and first.release_date == "2007-10-10"
    assert USER_AGENT.startswith("MyTaste/") and "https://mytaste.cc" in USER_AGENT


@pytest.mark.anyio
async def test_requests_retry_busy_answers_then_fail_cleanly():
    answers = iter([503, 503, 200])

    def respond(request):
        status = next(answers)
        return httpx.Response(status, json={"ok": True}, headers={"Retry-After": "0"})

    assert await http(respond, attempts=3).json("GET", "https://example.test/") == {"ok": True}
    with pytest.raises(MusicError, match="busy"):
        await http(lambda request: httpx.Response(503), attempts=2).json("GET", "https://x.test")
    with pytest.raises(MusicError, match="could not answer"):
        await http(lambda request: httpx.Response(400)).json("GET", "https://x.test")


@pytest.mark.anyio
async def test_background_requests_wait_for_foreground_ones():
    order = []
    release = asyncio.Event()

    async def respond(request):
        if request.url.path == "/slow":
            await release.wait()
        order.append(request.url.path)
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(respond)
    client = MusicHTTP("Test", client=httpx.AsyncClient(transport=transport))
    foreground = asyncio.create_task(client.json("GET", "https://x.test/slow"))
    await asyncio.sleep(0)
    background = asyncio.create_task(client.json("GET", "https://x.test/later", background=True))
    await asyncio.sleep(0.05)
    assert order == []
    release.set()
    await asyncio.gather(foreground, background)
    assert order == ["/slow", "/later"]


def release(release_id: str, title: str, tracks: int, date: str, *urls: str) -> dict:
    return {
        "id": release_id,
        "title": title,
        "date": date,
        "media": [{"track-count": tracks}],
        "relations": [{"url": {"resource": url}} for url in urls],
        "label-info": [{"label": {"name": "XL Recordings"}}],
    }


def test_release_choice_and_listen_links():
    releases = [
        release("a", "OK Computer", 12, "1997-05-21", "https://www.deezer.com/album/1"),
        release(
            "b",
            "OK Computer OKNOTOK",
            23,
            "2017-06-23",
            "https://open.spotify.com/album/0tzfI6NFJqcJkWb23R3lRZ",
        ),
        release(
            "c",
            "OK Computer",
            12,
            "2016-04-01",
            "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE",
            "https://music.apple.com/gb/album/1097861387",
            "javascript:alert(1)",
        ),
        release("d", "OK Computer", 12, "", "https://radiohead.bandcamp.com/album/ok-computer"),
    ]
    chosen = pick_release(releases, "OK Computer")
    assert chosen["id"] == "c"  # The usual edition that plays on Spotify.
    links = listen_links(chosen, releases)
    assert links == {
        "spotify": "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE",
        "apple-music": "https://music.apple.com/gb/album/1097861387",
        "deezer": "https://www.deezer.com/album/1",
        "bandcamp": "https://radiohead.bandcamp.com/album/ok-computer",
    }
    details = AlbumDetails(album(1), links=links)
    assert details.spotify_id == "6dVIqQ8qmQ5GBnJ9shOYGE"


@pytest.mark.anyio
async def test_musicbrainz_search_details_and_matching_lookups():
    requests = []

    def respond(request):
        requests.append(request)
        path = request.url.path
        params = request.url.params
        if path == "/ws/2/release-group":
            return httpx.Response(
                200,
                json={
                    "release-groups": [
                        {
                            "id": key(1),
                            "title": "OK Computer",
                            "score": 100,
                            "first-release-date": "1997-05-21",
                            "primary-type": "Album",
                            "secondary-types": ["Live"],
                            "artist-credit": [
                                {"name": "Radiohead", "artist": {"id": key(900)}, "joinphrase": ""}
                            ],
                        },
                        {"id": "bad", "title": "Skipped"},
                    ]
                },
            )
        if path == "/ws/2/url":
            assert params.get_list("resource")[0].startswith("https://music.apple.com/")
            return httpx.Response(
                200,
                json={
                    "urls": [
                        {
                            "resource": "https://music.apple.com/us/album/123",
                            "relations": [{"release": {"id": key(50)}}],
                        }
                    ]
                },
            )
        if path == "/ws/2/release" and "release-group" in params:
            return httpx.Response(
                200,
                json={
                    "releases": [
                        release(key(60), "OK Computer", 2, "1997", "https://www.deezer.com/album/9")
                    ]
                },
            )
        if path == f"/ws/2/release/{key(60)}":
            return httpx.Response(
                200,
                json={
                    "title": "OK Computer",
                    "label-info": [{"label": {"name": "Parlophone"}}],
                    "media": [
                        {
                            "position": 1,
                            "format": "CD",
                            "tracks": [
                                {"number": "1", "title": "Airbag", "length": 287_000},
                                {
                                    "number": "2",
                                    "title": "Guest",
                                    "length": 61_000,
                                    "artist-credit": [{"name": "Someone Else"}],
                                },
                            ],
                        }
                    ],
                },
            )
        if path == "/ws/2/release":
            return httpx.Response(
                200,
                json={
                    "releases": [
                        {"id": key(50), "release-group": {"id": key(1)}, "barcode": "0123"}
                    ]
                },
            )
        return httpx.Response(404)

    client = MusicBrainzClient(http(respond))
    results = await client.search("ok computer")
    assert [(item.title, score) for item, score in results] == [("OK Computer", 100)]
    found = results[0][0]
    assert found.secondary_types == ("live",) and found.artist == "Radiohead"
    assert requests[-1].url.params["dismax"] == "true"
    assert await client.releases_for_urls(["https://music.apple.com/us/album/123"]) == {
        "https://music.apple.com/us/album/123": [key(50)]
    }
    assert await client.groups_for_releases([key(50)]) == {key(50): key(1)}
    details = await client.details(found)
    assert details.release_mbid == key(60) and details.label == "Parlophone"
    assert [(track.title, track.duration, track.artist) for track in details.media[0].tracks] == [
        ("Airbag", "4:47", ""),
        ("Guest", "1:01", "Someone Else"),
    ]
    assert details.links == {"deezer": "https://www.deezer.com/album/9"}


@pytest.mark.anyio
async def test_apple_chart_and_barcodes_in_cover_names():
    def respond(request):
        if "/zz/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={
                "feed": {
                    "results": [
                        {
                            "id": "6822944288",
                            "name": "Young Manhood",
                            "artistName": "Zach Bryan",
                            "releaseDate": "2026-10-16",
                            "url": "https://music.apple.com/us/album/young-manhood/6822944288",
                            "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/a/b/093624812647.jpg/100x100bb.jpg",
                        },
                        {"id": "x", "name": "Invalid"},
                    ]
                }
            },
        )

    client = AppleChartClient(http(respond))
    chart = await client.most_played("US")
    assert [(entry.id, entry.barcode) for entry in chart] == [("6822944288", "093624812647")]
    assert await client.most_played("ZZ") is None
    assert ChartEntry("1", "A", "B", artwork="https://x/y/26UMGIM63616.rgb.jpg/1.jpg").barcode == ""


class MatchingCatalog:
    def __init__(self):
        self.searched = []

    async def releases_for_urls(self, urls):
        return {"https://music.apple.com/us/album/1": [key(101)]}

    async def groups_for_releases(self, releases):
        return {key(101): key(1)}

    async def releases_by_barcode(self, barcodes):
        return {
            "0093624812647": [album(2, artists=(ArtistCredit("Zach Bryan"),))],
            "111111111111": [album(9, artists=(ArtistCredit("Somebody Else"),))],
        }

    async def find(self, title, artist, limit=5):
        self.searched.append((title, artist))
        return [
            album(3, "Mud", artists=(ArtistCredit("Remi Wolf"),), release_date="2026-10-09"),
            album(4, "Same Title", artists=(ArtistCredit("Other"),), release_date="2026"),
        ]


@pytest.mark.anyio
async def test_chart_matching_by_link_barcode_then_artist_title_and_year():
    art = "https://is1/a/{}.jpg/100x100bb.jpg"
    entries = [
        ChartEntry("1", "Linked", "Anyone"),
        ChartEntry(
            "2",
            "Young Manhood (Deluxe Edition)",
            "Zach Bryan",
            "2026-10-16",
            artwork=art.format("093624812647"),
        ),
        ChartEntry("5", "Wrong Artist", "Real Artist", "2026", artwork=art.format("111111111111")),
        ChartEntry("3", "Mud", "Remi Wolf & Friend", "2026-10-09"),
        ChartEntry("6", "Same Title", "Someone", "2026"),
    ]
    steps = []
    catalog = MatchingCatalog()
    found = await match_chart(entries, "GB", catalog, steps.append)
    assert found == {
        "1": (key(1), "url"),
        "2": (key(2), "barcode"),
        "5": (None, ""),  # A barcode with someone else's name on it is not a match.
        "3": (key(3), "search"),
        "6": (None, ""),  # The same title by another artist is never a match.
    }
    assert len(steps) == 5 and steps[0] == {"1": (key(1), "url")}
    assert ("Mud", "Remi Wolf") in catalog.searched
    assert not same_album(
        ChartEntry("7", "Mud", "Remi Wolf", "2020"),
        album(3, "Mud", artists=(ArtistCredit("Remi Wolf"),), release_date="2026"),
    )


# Service ---------------------------------------------------------------------------------


class FakeListenBrainz:
    def __init__(self, albums):
        self.albums = {item.mbid: item for item in albums}
        self.metadata_calls = []
        self.popularity_calls = 0
        self.sitewide_rows = {"month": [], "all_time": [], "year": []}
        self.fresh = []
        self.artist = ("", [])

    async def sitewide(self, period="month", count=1000):
        return self.sitewide_rows[period]

    async def fresh_releases(self, days=30):
        return self.fresh

    async def popularity(self, mbids):
        self.popularity_calls += 1
        return {
            mbid: ((self.albums[mbid].listens or 0), 1) if mbid in self.albums else (0, 0)
            for mbid in mbids
        }

    async def metadata(self, mbids):
        self.metadata_calls.append(list(mbids))
        return {
            mbid: replace(self.albums[mbid], listens=None, secondary_types=())
            for mbid in mbids
            if mbid in self.albums
        }

    async def artist_albums(self, artist):
        return self.artist


class FakeMusicBrainz:
    def __init__(self):
        self.searches = []
        self.enriched = []
        self.details_calls = 0
        self.results = []
        self.artists = []
        self.secondary = {}

    async def search(self, text, limit=100):
        self.searches.append(text)
        return self.results

    async def search_artists(self, text, limit=3):
        return self.artists

    async def release_groups(self, mbids, *, background=False):
        self.enriched.append((list(mbids), background))
        return {
            mbid: replace(album(int(mbid[-3:])), secondary_types=self.secondary.get(mbid, ()))
            for mbid in mbids
        }

    async def details(self, item):
        self.details_calls += 1
        return AlbumDetails(
            item,
            release_mbid=key(700),
            media=(Medium(1, "CD", "", (Track("1", "Song", 60_000),)),),
            links={"spotify": "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE"},
        )

    async def releases_for_urls(self, urls):
        return {urls[0]: [key(100)]} if "6dVIqQ8qmQ5GBnJ9shOYGE" in urls[0] else {}

    async def groups_for_releases(self, releases):
        return {key(100): key(1)}

    async def releases_by_barcode(self, barcodes):
        return {}

    async def find(self, title, artist, limit=5):
        return []


class FakeApple:
    def __init__(self, entries):
        self.entries = entries
        self.calls = []
        self.fail = False

    async def most_played(self, country, limit=100):
        self.calls.append(country)
        if self.fail:
            raise MusicError("Apple Music is unavailable.")
        return None if country == "ZZ" else self.entries


def music_service(tmp_path, albums, **values):
    repository = MusicCacheRepository(tmp_path / "music.db")
    repository.initialize()
    listenbrainz = FakeListenBrainz(albums)
    musicbrainz = FakeMusicBrainz()
    apple = FakeApple([])
    service = MusicService(
        repository,
        listenbrainz=listenbrainz,
        musicbrainz=musicbrainz,
        apple=apple,
        page_size=2,
        match_wait=1,
        **values,
    )
    return service, listenbrainz, musicbrainz, apple


@pytest.mark.anyio
async def test_most_listened_order_filters_sorts_and_pages(tmp_path):
    albums = [
        album(1, "Rock One", genres=("rock",), listens=10, release_date="2001"),
        album(2, "Pop Two", genres=("pop",), listens=30, release_date="2011"),
        album(3, "Rock Three", genres=("art rock",), listens=20, primary_type="single"),
    ]
    service, listenbrainz, musicbrainz, _apple = music_service(tmp_path, albums)
    listenbrainz.sitewide_rows["month"] = [Ranked(key(3), 9), Ranked(key(1), 8), Ranked(key(2), 1)]
    page = await service.browse("GB", AlbumQuery("most-listened"))
    assert [item.title for item in page.items] == ["Rock Three", "Rock One"]
    assert (page.total, page.pages, page.items[0].listens) == (3, 2, 20)
    second = await service.browse("GB", AlbumQuery("most-listened", page=2))
    assert [item.title for item in second.items] == ["Pop Two"]
    by_popularity = await service.ordered("GB", AlbumQuery("most-listened", sort="popularity"))
    assert [item.title for item in by_popularity.items] == ["Pop Two", "Rock Three", "Rock One"]
    rock = await service.ordered("GB", AlbumQuery("most-listened", genre="rock"))
    assert [item.title for item in rock.items] == ["Rock Three", "Rock One"]
    # Facts are kept: the second look asks ListenBrainz for nothing new.
    assert listenbrainz.metadata_calls == [[key(3), key(1), key(2)]]
    await asyncio.sleep(0)
    await service._enriching
    assert musicbrainz.enriched == [([key(3), key(1), key(2)], True)]
    await service.close()


@pytest.mark.anyio
async def test_genre_collections_hold_albums_and_eps_from_the_pool(tmp_path):
    albums = [
        album(1, "Rock Album", genres=("rock",), listens=5),
        album(2, "Rock Single", genres=("rock",), primary_type="single", listens=50),
        album(3, "Rock EP", genres=("hard rock",), primary_type="ep", listens=9),
        album(4, "Jazz", genres=("jazz",), listens=99),
    ]
    service, listenbrainz, _musicbrainz, _apple = music_service(tmp_path, albums)
    listenbrainz.sitewide_rows["all_time"] = [Ranked(key(1)), Ranked(key(2))]
    listenbrainz.sitewide_rows["year"] = [Ranked(key(3)), Ranked(key(4))]
    page = await service.ordered("", AlbumQuery("rock", sort="popularity"))
    assert [item.title for item in page.items] == ["Rock EP", "Rock Album"]
    await service.close()


@pytest.mark.anyio
async def test_new_releases_rank_listened_albums_first(tmp_path):
    albums = [album(1, listens=0), album(2, listens=40), album(3, listens=0)]
    service, listenbrainz, _musicbrainz, _apple = music_service(tmp_path, albums)
    listenbrainz.fresh = [
        Ranked(key(1), 0, album(1, release_date="2026-10-10")),
        Ranked(key(2), 0, album(2, release_date="2026-10-01")),
        Ranked(key(3), 0, album(3, release_date="2026-10-12")),
        Ranked(key(4), 0, album(4, "Single", primary_type="single")),
        # ListenBrainz doesn't know this one yet; the fresh-release entry stands in.
        Ranked(key(5), 0, album(5, "Brand new", release_date="2026-10-15")),
    ]
    page = await service.ordered("", AlbumQuery("new"))
    assert [item.mbid for item in page.items] == [key(2), key(5), key(3), key(1)]
    await service.close()


@pytest.mark.anyio
async def test_search_puts_the_named_artists_albums_first(tmp_path):
    albums = [
        album(1, "Tribute", listens=500),
        album(2, "Own", listens=100),
        album(3, "Weak", listens=900),
    ]
    service, listenbrainz, musicbrainz, _apple = music_service(tmp_path, albums)
    musicbrainz.results = [
        (album(1, "Tribute"), 100),
        (album(3, "Weak"), 40),
        (album(2, "Own"), 70),
    ]
    musicbrainz.artists = [(key(900), "Radiohead", 100)]
    listenbrainz.artist = ("Radiohead", [Ranked(key(2), 100, album(2, "Own"))])
    page = await service.ordered("", AlbumQuery(search="radiohead"))
    assert [item.title for item in page.items] == ["Tribute", "Own", "Weak"]
    assert len(page.items) == 3  # The artist's album that also matched the title appears once.
    await service.close()


@pytest.mark.anyio
async def test_popular_chart_matches_in_the_background_and_survives_apple_outages(tmp_path):
    albums = [album(1, "Linked", listens=3)]
    service, _listenbrainz, _musicbrainz, apple = music_service(tmp_path, albums)
    apple.entries = [
        ChartEntry("1", "Linked", "Anyone"),
        ChartEntry("2", "Unknown", "Nobody", "2026"),
    ]
    service.musicbrainz.releases_for_urls = _apple_link
    page = await service.ordered("GB", AlbumQuery())
    assert [item.title for item in page.items] == ["Linked"] and page.pending == 0
    stored = service.repository.links("apple", ["1", "2"])
    assert stored["1"][0] == key(1) and stored["2"][0] is None
    # Apple can't be reached: the last chart read is shown, with its date.
    service._lists.clear()
    apple.fail = True
    page = await service.ordered("GB", AlbumQuery())
    assert [item.title for item in page.items] == ["Linked"]
    assert page.notices and "can't be reached" in page.notices[0]
    apple.fail = False
    page = await service.ordered("ZZ", AlbumQuery())
    assert "no chart for ZZ" in page.notices[0]
    await service.close()


async def _apple_link(urls):
    return {url: [key(100)] for url in urls if url.endswith("/album/1")}


@pytest.mark.anyio
async def test_details_are_cached_and_pasted_links_resolve(tmp_path):
    service, _listenbrainz, musicbrainz, _apple = music_service(tmp_path, [album(1, listens=7)])
    first = await service.details(key(1))
    second = await service.details(key(1))
    assert musicbrainz.details_calls == 1
    assert first.spotify_id == second.spotify_id == "6dVIqQ8qmQ5GBnJ9shOYGE"
    assert second.album.listens == 7 and second.media[0].tracks[0].title == "Song"
    link = "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE?si=1"
    assert await service.find_link(link) == key(1)
    assert await service.find_link(f"https://musicbrainz.org/release-group/{key(9)}") == key(9)
    assert await service.find_link("https://open.spotify.com/album/0000000000000000000000") is None
    with pytest.raises(LookupError):
        musicbrainz.release_groups = _nothing
        await service.album(key(42))
    await service.close()


async def _nothing(mbids, *, background=False):
    return {}


@pytest.mark.anyio
async def test_musicbrainz_facts_are_added_and_kept_across_refreshes(tmp_path):
    service, listenbrainz, musicbrainz, _apple = music_service(
        tmp_path, [album(1, listens=1)], facts_ttl=0
    )
    listenbrainz.sitewide_rows["month"] = [Ranked(key(1))]
    musicbrainz.secondary[key(1)] = ("live",)
    await service.ordered("", AlbumQuery("most-listened"))
    await asyncio.sleep(0)
    await service._enriching
    page = await service.ordered("", AlbumQuery("most-listened", type="live"))
    assert [item.mbid for item in page.items] == [key(1)]
    assert len(listenbrainz.metadata_calls) == 2  # Refreshed, yet still live.
    await service.close()


# Covers ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_covers_are_fetched_once_missing_ones_remembered_and_cache_pruned(tmp_path):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if "404" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=b"x" * 600, headers={"content-type": "image/jpeg"})

    cache = CoverCache(tmp_path / "covers", max_bytes=1000, http=http(respond))
    first, again = await asyncio.gather(
        cache.release_image(key(1), 5, 250), cache.release_image(key(1), 5, 250)
    )
    assert first == again and first.read_bytes() == b"x" * 600
    assert requests == [f"/release/{key(1)}/5-250.jpg"]
    assert await cache.release_image(key(1), 404, 250) is None
    assert await cache.release_image(key(1), 404, 250) is None
    assert len(requests) == 2
    assert await cache.release_image(key(1), 5, 333) is None
    assert await cache.release_image("../etc", 5, 250) is None
    await cache._pruning  # The first pass measures the cache: 600 bytes fit.
    assert first.exists()
    old = time.time() - 100
    os.utime(first, (old, old))
    second = await cache.release_image(key(2), 6, 250)
    await cache._pruning
    assert second.exists() and not first.exists()  # The least recently used went first.
    await cache.close()


# Storage ---------------------------------------------------------------------------------


_OLD_SAVED_ITEMS = """
    CREATE TABLE saved_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        portable_id TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('movie', 'tv', 'game')),
        tmdb_id INTEGER CHECK (tmdb_id > 0),
        steam_appid INTEGER CHECK (steam_appid > 0),
        xbox_id TEXT,
        title TEXT NOT NULL,
        release_date TEXT NOT NULL DEFAULT '',
        overview TEXT NOT NULL DEFAULT '',
        rating REAL NOT NULL DEFAULT 0,
        popularity REAL NOT NULL DEFAULT 0,
        poster_path TEXT,
        genre_ids TEXT NOT NULL DEFAULT ',',
        genres TEXT NOT NULL DEFAULT '[]',
        snapshot TEXT NOT NULL DEFAULT '{}',
        CHECK ((kind = 'game') = (tmdb_id IS NULL)),
        CHECK (kind != 'game' OR steam_appid IS NOT NULL OR xbox_id IS NOT NULL)
    )
"""


def test_saved_items_are_rebuilt_once_for_music_keeping_every_collection(tmp_path):
    path = tmp_path / "data" / "mytaste.db"
    repository = CollectionRepository(path)
    repository.initialize()
    watchlist = repository.list()[0]
    repository.add_item(watchlist.id, CollectionItem("movie", 603, "2026-01-01", "The Matrix"))
    repository.add_game(watchlist.id, Game("steam-1", "A Game", steam_appid=1))
    repository.add_item(watchlist.id, CollectionItem("tv", 1399, "2026-01-03", "A Series"))
    repository.remove_item(watchlist.id, "tv", 1399)
    # Make the table look like it did before music.
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        # Keep collection_entries pointing at "saved_items" while the old shape is put back.
        connection.execute("PRAGMA legacy_alter_table = ON")
        connection.execute("ALTER TABLE saved_items RENAME TO saved_items_new")
        connection.execute(_OLD_SAVED_ITEMS)
        connection.execute(
            "INSERT INTO saved_items SELECT id, portable_id, kind, tmdb_id, steam_appid, xbox_id, "
            "title, release_date, overview, rating, popularity, poster_path, genre_ids, genres, "
            "snapshot FROM saved_items_new"
        )
        connection.execute("DROP TABLE saved_items_new")
        connection.execute("UPDATE sqlite_sequence SET seq = 3 WHERE name = 'saved_items'")
        for statement in (
            "CREATE UNIQUE INDEX saved_items_tmdb ON saved_items (kind, tmdb_id) "
            "WHERE tmdb_id IS NOT NULL",
            "CREATE UNIQUE INDEX saved_items_steam ON saved_items (steam_appid) "
            "WHERE steam_appid IS NOT NULL",
        ):
            connection.execute(statement)
        before = (
            connection.execute("SELECT * FROM saved_items ORDER BY id").fetchall(),
            connection.execute(
                "SELECT * FROM collection_entries ORDER BY collection_id, item_id"
            ).fetchall(),
        )
    with pytest.raises(sqlite3.IntegrityError), sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO saved_items (portable_id, kind, steam_appid, title) "
            "VALUES ('x', 'album', 5, 'z')"
        )

    CollectionRepository(path).initialize()
    with sqlite3.connect(path) as connection:
        after = (
            connection.execute(
                "SELECT id, portable_id, kind, tmdb_id, steam_appid, xbox_id, title, release_date, "
                "overview, rating, popularity, poster_path, genre_ids, genres, snapshot "
                "FROM saved_items ORDER BY id"
            ).fetchall(),
            connection.execute(
                "SELECT * FROM collection_entries ORDER BY collection_id, item_id"
            ).fetchall(),
        )
        sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'saved_items'"
        ).fetchone()[0]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert after == before and sequence == 3
    backups = list((path.parent / "backups").iterdir())
    assert len(backups) == 1 and backups[0].name.endswith("-before-music.db")
    with sqlite3.connect(backups[0]) as copy:
        # Taking a title out of a collection keeps its saved item.
        assert copy.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0] == 3

    repository = CollectionRepository(path)
    repository.initialize()  # Already rebuilt: nothing happens, and no second backup.
    assert len(list((path.parent / "backups").iterdir())) == 1
    saved = album(1, "OK Computer", genres=("art rock",), listens=9)
    assert repository.add_album(watchlist.id, saved)
    assert not repository.add_album(watchlist.id, replace(saved, title="OK Computer (renamed)"))
    entries = repository.albums(watchlist.id)
    assert [(entry.album.title, entry.sequence) for entry in entries] == [
        ("OK Computer (renamed)", 3)
    ]
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT MAX(id) FROM saved_items").fetchone()[0] == 4
    assert repository.album_memberships(key(1)) == {watchlist.id}
    assert repository.saved_album_memberships() == {key(1): frozenset({watchlist.id})}
    assert repository.remove_album(watchlist.id, key(1))
    assert repository.albums(watchlist.id) == ()


def test_a_new_database_allows_music_without_a_rebuild(tmp_path):
    path = tmp_path / "mytaste.db"
    CollectionRepository(path).initialize()
    assert not (tmp_path / "backups").exists()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO saved_items (portable_id, kind, soundcloud_urn, title) "
            "VALUES ('track-soundcloud-1', 'track', 'soundcloud:tracks:1', 'A track')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO saved_items (portable_id, kind, title) VALUES ('x', 'album', 'z')"
            )


@pytest.mark.anyio
async def test_saved_albums_join_collections_beside_titles(tmp_path):
    repository = CollectionRepository(tmp_path / "mytaste.db")
    repository.initialize()

    class Library:
        def matched_keys(self, ids=()):
            return frozenset()

    service = CollectionService(repository, catalog=None, library=Library())
    collection = repository.list()[0]
    repository.add_item(
        collection.id, CollectionItem("movie", 603, "2026-01-01", "Matrix", release_date="1999")
    )
    service.add_album(collection.id, album(1, "Kid A", release_date="2000-10-02"))
    assert service.saved_album_icons() == {key(1): ("bookmark",)}
    with pytest.raises(LookupError):
        service.add_album(999, album(2))
    page = await service.browse(
        collection,
        BrowseQuery(sort="release", descending=True),
        region="",
        provider_ids=(),
        library_ids=(),
    )
    # Albums count as available: they play through their links.
    assert [item.title for item in page.page.items] == ["Kid A"]
    assert (page.available, page.total) == (1, 2)
    movies = await service.browse(
        collection, BrowseQuery(media_type="movie"), region="", provider_ids=(), library_ids=()
    )
    assert movies.total == 1


def test_music_cache_round_trip(tmp_path):
    repository = MusicCacheRepository(tmp_path / "music.db")
    repository.initialize()
    repository.save_albums([album(1, listens=5)])
    stored, fetched, enriched = repository.albums([key(1)])[key(1)]
    assert stored.listens is None and enriched is None
    repository.save_albums(
        [replace(album(1), secondary_types=("live",))], enriched=True, fetched=False
    )
    stored, again, enriched = repository.albums([key(1)])[key(1)]
    assert stored.secondary_types == ("live",) and again == fetched and enriched
    repository.save_popularity({key(1): (10, 2)})
    assert repository.popularity([key(1)])[key(1)][:2] == (10, 2)
    repository.save_links("apple", [("1", key(1), "url"), ("2", None, "")])
    links = repository.links("apple", ["1", "2", "3"])
    assert links["1"][0] == key(1) and links["2"][0] is None and "3" not in links
    details = AlbumDetails(album(1), links={"spotify": "https://open.spotify.com/album/x"})
    repository.save_details(details)
    assert repository.details(key(1))[0].links == details.links
    repository.save_chart("apple", "GB", [{"id": "1", "title": "A", "artist": "B"}])
    assert repository.chart("apple", "GB")[0] == [{"id": "1", "title": "A", "artist": "B"}]


def test_music_view_preference_is_saved_and_added_to_existing_tables(tmp_path):
    path = tmp_path / "mytaste.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE display_preferences (user_id INTEGER PRIMARY KEY, "
            "show_year INTEGER NOT NULL DEFAULT 1, show_rating INTEGER NOT NULL DEFAULT 1, "
            "show_media_type INTEGER NOT NULL DEFAULT 1, show_genres INTEGER NOT NULL DEFAULT 0, "
            "show_people INTEGER NOT NULL DEFAULT 0, "
            "card_size TEXT NOT NULL DEFAULT 'comfortable', "
            "autoplay_trailer INTEGER NOT NULL DEFAULT 1, sidebar_open INTEGER NOT NULL DEFAULT 1, "
            "show_providers INTEGER NOT NULL DEFAULT 0)"
        )
        connection.execute("INSERT INTO display_preferences (user_id) VALUES (1)")
    preferences = PreferenceRepository(path)
    preferences.initialize()
    assert preferences.get_display().music_view == "grid"
    preferences.save_display(DisplayPreferences(music_view="wall"))
    assert preferences.get_display().music_view == "wall"
    with pytest.raises(ValueError):
        preferences.save_display(DisplayPreferences(music_view="crate"))
    preferences.save("GB", ())
    assert preferences.regions() == ("GB",)

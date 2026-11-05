from __future__ import annotations

from dataclasses import replace

from conftest import owner_client
from test_games import ServicesCatalog

from mytaste.config import AppSettings
from mytaste.music.http import MusicError
from mytaste.music.models import (
    Album,
    AlbumDetails,
    AlbumPage,
    ArtistCredit,
    Medium,
    Track,
)
from mytaste.web.app import create_app
from mytaste.web.music import compact_count


def key(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


ALBUMS = (
    Album(
        key(1),
        "OK Computer",
        (ArtistCredit("Radiohead", key(900)),),
        "1997-05-21",
        "album",
        genres=("alternative rock", "art rock"),
        cover_release=key(500),
        cover_id=12,
        listens=1_781_830,
    ),
    Album(key(2), "Kid A", (ArtistCredit("Radiohead", key(900)),), "2000-10-02", "album"),
    Album(key(3), "Live", (ArtistCredit("Someone"),), "", "album", ("live",)),
)


class FakeCovers:
    def __init__(self, path):
        self.path = path
        self.calls = []

    async def release_image(self, release, image, size):
        self.calls.append((release, image, size))
        return self.path if image == 12 else None


class FakeMusic:
    def __init__(self, covers=None):
        self.calls = []
        self.covers = covers
        self.fail = False
        self.pending = 0

    async def browse(self, region, query):
        self.calls.append((region, query))
        if self.fail:
            raise MusicError("ListenBrainz is busy. Try again shortly.")
        items = ALBUMS if not query.search else ALBUMS[:1]
        pages = 2 if not query.search else 1
        page = min(query.page, pages)
        return AlbumPage(
            items=items[page - 1 :: 2],
            total=len(items),
            pages=pages,
            page=page,
            pending=self.pending,
        )

    async def shelves(self, region, query):
        self.calls.append((region, query))
        return AlbumPage(items=ALBUMS, total=len(ALBUMS))

    async def album(self, mbid):
        found = next((item for item in ALBUMS if item.mbid == mbid), None)
        if found is None:
            raise LookupError("Unknown album")
        return found

    async def details(self, mbid):
        found = await self.album(mbid)
        return AlbumDetails(
            found,
            release_mbid=key(600),
            label="Parlophone",
            media=(Medium(1, "CD", "", (Track("1", "Airbag", 287_000),)),),
            links={
                "spotify": "https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE",
                "bandcamp": "https://radiohead.bandcamp.com/album/ok-computer",
            },
        )

    async def find_link(self, text):
        return key(1) if "6dVIqQ8qmQ5GBnJ9shOYGE" in text else None

    def warm(self, regions):
        raise AssertionError("Tests never fill the music caches")

    async def close(self):
        pass


def music_app(tmp_path, music=None):
    music = music or FakeMusic()
    app = create_app(AppSettings(None, tmp_path / "app.db"), catalog=ServicesCatalog(), music=music)
    return app, music


def test_music_tab_layout_loads_results_after_the_page(tmp_path):
    app, music = music_app(tmp_path)
    with owner_client(app) as client:
        page = client.get("/collections/music")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert not music.calls  # The layout doesn't wait for the catalog.
        text = page.text
        assert 'href="/collections/music" aria-current="page">Music</a>' in text
        assert 'action="/collections/music" role="search"' in text
        assert "data-music-load-url" in text and "music.js" in text
        assert 'id="album-dialog"' in text and 'id="card-save-popover"' in text
        assert 'name="music_view" value="grid" checked' in text
        assert 'name="autoplay_trailer"' not in text and "data-collection-add" not in text
        assert 'data-rail-section="sidebar-sources"' not in text
        assert "MusicBrainz" in text and "Cover Art Archive" in text
        for collection in ("Popular", "Most listened", "New releases", "Rock", "Jazz and blues"):
            assert f"{collection}</a>" in text

        fragment = client.get("/collections/music", headers={"X-MyTaste-Fragment": "music-page"})
        payload = fragment.json()
        assert music.calls[-1][0] == "US"  # No country chosen yet.
        assert payload["pending"] == 0
        assert "OK Computer" in payload["html"] and "Chart order" not in payload["html"]
        assert f'data-detail-url="/api/items/album/{key(1)}/details"' in payload["html"]
        assert f'src="/music/covers/{key(500)}/12-250.jpg"' in payload["html"]
        assert "1.8M" in payload["html"]
        assert "data-next-page" in payload["html"]

        more = client.get("/collections/music?page=2", headers={"X-MyTaste-Fragment": "results"})
        assert "<html" not in more.text and "Kid A" in more.text
        assert more.headers["x-next-page"] == ""

        app.state.preferences.save("GB", ())
        rendered = client.get("/collections/music/rock?sort=release&order=asc&type=live&render=1")
        region, query = music.calls[-1]
        assert region == "GB"
        assert (query.collection, query.sort, query.order, query.type) == (
            "rock",
            "release",
            "asc",
            "live",
        )
        assert "Live</a>" in rendered.text or "Live<svg" in rendered.text  # The filter chip.
        assert "The most played Rock albums on ListenBrainz" in rendered.text

        # A sort a collection can't give falls back to its own order.
        client.get("/collections/music/rock?sort=catalog&render=1")
        assert music.calls[-1][1].sort == "popularity"
        client.get("/collections/music/most-listened?render=1")
        assert music.calls[-1][1].sort == "catalog"

        assert client.get("/collections/music/unknown?render=1").status_code == 422
        assert client.get("/collections/music?genre=kpop&render=1").status_code == 422


def test_music_errors_pending_matches_shelves_and_pasted_links(tmp_path):
    app, music = music_app(tmp_path)
    with owner_client(app) as client:
        music.pending = 4
        pending = client.get("/collections/music", headers={"X-MyTaste-Fragment": "music-page"})
        assert pending.json()["pending"] == 4
        assert "Finding 4 more albums from the chart on MusicBrainz" in pending.json()["html"]
        music.pending = 0

        shelves = client.get("/collections/music/rock?group=artist&render=1")
        assert "By artist" in shelves.text
        assert "Radiohead</h2>" in shelves.text and "2 albums" in shelves.text

        link = client.get(
            "/collections/music?q=https://open.spotify.com/album/6dVIqQ8qmQ5GBnJ9shOYGE",
            follow_redirects=False,
        )
        assert link.status_code == 303 and link.headers["location"] == f"/music/albums/{key(1)}"
        unknown = client.get(
            "/collections/music?q=https://open.spotify.com/album/0000000000000000000000&render=1"
        )
        assert "No album on MusicBrainz links to that page yet." in unknown.text

        search = client.get("/collections/music?q=radiohead&render=1")
        assert "Search results for “radiohead”" in search.text and "Relevance" in search.text

        music.fail = True
        failed = client.get("/collections/music", headers={"X-MyTaste-Fragment": "music-page"})
        assert failed.status_code == 503 and "ListenBrainz is busy" in failed.json()["html"]


def test_album_details_page_and_covers(tmp_path):
    image = tmp_path / "cover.jpg"
    image.write_bytes(b"jpeg")
    app, _music = music_app(tmp_path, FakeMusic(FakeCovers(image)))
    with owner_client(app) as client:
        details = client.get(f"/api/items/album/{key(1)}/details").json()
        assert (
            details["title"] == "OK Computer" and details["spotify_id"] == "6dVIqQ8qmQ5GBnJ9shOYGE"
        )
        assert details["artist_links"][0]["url"] == f"/collections/music?artist={key(900)}"
        assert details["cover"] == f"/music/covers/{key(500)}/12-500.jpg"
        assert [link["service"] for link in details["links"]] == ["spotify", "bandcamp"]
        assert details["media"][0]["label"] == "" and details["track_count"] == 1
        assert details["media"][0]["tracks"][0] == {
            "position": "1",
            "title": "Airbag",
            "duration": "4:47",
            "artist": "",
        }
        assert client.get(f"/api/items/album/{key(9)}/details").status_code == 404
        assert client.get("/api/items/album/nope/details").status_code == 404

        page = client.get(f"/music/albums/{key(1)}")
        assert page.status_code == 200 and "<h1" in page.text and "OK Computer" in page.text
        assert 'data-album-spotify="6dVIqQ8qmQ5GBnJ9shOYGE"' in page.text
        assert "Listen on Bandcamp" in page.text and "Airbag" in page.text
        # Spotify's player is never in the page until Play asks for it.
        assert "open.spotify.com/embed" not in page.text
        missing = client.get(f"/music/albums/{key(9)}")
        assert missing.status_code == 404 and "Album unavailable" in missing.text

        cover = client.get(f"/music/covers/{key(500)}/12-250.jpg")
        assert cover.status_code == 200 and cover.content == b"jpeg"
        assert "immutable" in cover.headers["cache-control"]
        assert client.get(f"/music/covers/{key(500)}/13-250.jpg").status_code == 404
        assert client.get(f"/music/covers/{key(500)}/12-300.jpg").status_code == 404
        assert client.get("/music/covers/nope/12-250.jpg").status_code == 404


def test_albums_are_saved_to_collections_and_shown_beside_titles(tmp_path):
    from test_web import FakeCatalog

    app = create_app(
        AppSettings(None, tmp_path / "app.db"), catalog=FakeCatalog(), music=FakeMusic()
    )
    with owner_client(app) as client:
        app.state.preferences.save("DE", (8,))
        collections = client.get(f"/api/items/album/{key(1)}/collections").json()["collections"]
        watchlist = collections[0]["id"]
        assert not any(item["saved"] for item in collections)
        saved = client.put(f"/api/collections/{watchlist}/items/album/{key(1)}")
        assert saved.json() == {"saved": True, "added": True}
        again = client.put(f"/api/collections/{watchlist}/items/album/{key(1)}")
        assert again.json()["added"] is False
        assert client.put(f"/api/collections/999/items/album/{key(1)}").status_code == 404
        assert client.put(f"/api/collections/{watchlist}/items/album/{key(9)}").status_code == 404
        listed = client.get(f"/api/items/album/{key(1)}/collections").json()["collections"]
        assert listed[0]["saved"] is True

        page = client.get(f"/collections/{watchlist}")
        assert 'class="media-card album-card album-card-mixed"' in page.text
        assert 'id="album-dialog"' in page.text and "music.js" in page.text
        assert "is-saved" in page.text
        music_page = client.get(
            "/collections/music", headers={"X-MyTaste-Fragment": "music-page"}
        ).json()["html"]
        assert 'class="card-save is-saved"' in music_page
        # Music stays out of All's smart collections.
        assert "album-card" not in client.get("/").text

        removed = client.delete(f"/api/collections/{watchlist}/items/album/{key(1)}")
        assert removed.json() == {"saved": False, "removed": True}
        assert "album-card" not in client.get(f"/collections/{watchlist}").text


def test_music_view_is_a_display_preference(tmp_path):
    app, _music = music_app(tmp_path)
    with owner_client(app) as client:
        assert (
            client.post("/api/preferences/display", json={"music_view": "list"}).status_code == 200
        )
        assert app.state.preferences.get_display().music_view == "list"
        assert 'data-music-view="list"' in client.get("/collections/music").text
        assert (
            client.post("/api/preferences/display", json={"music_view": "crate"}).status_code == 422
        )


def test_listen_counts_read_compactly():
    assert [
        compact_count(value) for value in (None, 0, 999, 1_234, 12_345, 1_781_830, 2_000_000)
    ] == [
        "",
        "",
        "999",
        "1.2K",
        "12K",
        "1.8M",
        "2M",
    ]


def test_album_payload_keeps_snapshot_fields(tmp_path):
    from mytaste.web.music import album_payload

    payload = album_payload(replace(ALBUMS[0], color="#112233"))
    assert payload["color"] == "#112233" and payload["listens_label"] == "1.8M"
    assert payload["musicbrainz_url"].endswith(key(1))

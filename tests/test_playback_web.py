from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_web import FakeCatalog, FakeLibrary

from mytaste.config import AppSettings
from mytaste.library.models import ScannedFile
from mytaste.playback import service as service_module
from mytaste.playback.models import AudioStream, ExternalSubtitle, MediaInfo, VideoStream
from mytaste.storage.library import ItemDraft, LibraryRepository
from mytaste.web.app import create_app

CHROME = {
    "hls": True,
    "direct_containers": ["mp4"],
    "direct_video": ["h264"],
    "direct_audio": ["aac"],
    "hls_video": ["h264"],
    "hls_audio": ["aac"],
}


def scanned(path: Path, season: int | None = None, episode: int | None = None) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        size=path.stat().st_size,
        modified_at="2026-09-01T10:00:00+00:00",
        group_key="unused",
        titles=("Title",),
        season=season,
        episode=episode,
    )


@pytest.fixture
def library_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    folder = tmp_path / "media"
    (folder / "Show").mkdir(parents=True)
    (folder / "Film.mp4").write_bytes(b"0123456789" * 100)
    (folder / "Film.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
    (folder / "Home.mp4").write_bytes(b"home video")
    for episode in (1, 2):
        (folder / "Show" / f"Show.S01E0{episode}.mp4").write_bytes(b"episode")
    repository = LibraryRepository(tmp_path / "mytaste.db")
    repository.initialize()
    library = repository.add_library("Media", [(str(folder), "movie")])
    repository.replace_items(
        library.id,
        [
            ItemDraft(
                "movie:film", "movie", "A New Film", (scanned(folder / "Film.mp4"),), tmdb_id=12
            ),
            ItemDraft("movie:home", "movie", "Home", (scanned(folder / "Home.mp4"),)),
            ItemDraft(
                "tv:show",
                "tv",
                "Dark",
                tuple(
                    scanned(folder / "Show" / f"Show.S01E0{episode}.mp4", 1, episode)
                    for episode in (1, 2)
                ),
                tmdb_id=13,
            ),
        ],
    )

    def fake_probe(_ffprobe: str, path: Path, **_kwargs: object) -> MediaInfo:
        subtitles = ()
        if path.name == "Film.mp4":
            subtitles = (ExternalSubtitle(str(folder / "Film.en.srt"), "srt", "en"),)
        return MediaInfo(
            container="mov",
            duration=6000.0,
            size=path.stat().st_size,
            video=VideoStream(0, "h264", "High", 41, width=1920, height=1080, codec_tag="avc1"),
            audio=(AudioStream(1, "aac", channels=2, language="eng", default=True),),
            external_subtitles=subtitles,
            keyframes=(0.0, 5.0, 10.0),
        )

    monkeypatch.setattr(service_module, "probe_file", fake_probe)
    files = {Path(file.path).name: file.id for file in repository.show_files(13)}
    files.update({Path(file.path).name: file.id for file in repository.movie_files(12)})
    files["Home.mp4"] = next(
        file.id for file in repository.files_for_item(2) if file.path.endswith("Home.mp4")
    )
    return files


def client_for(tmp_path: Path) -> TestClient:
    settings = AppSettings(tmdb_token="test-token", database_path=tmp_path / "mytaste.db")
    app = create_app(settings, catalog=FakeCatalog(), library=FakeLibrary())
    app.state.playback.ffprobe = "ffprobe"
    return TestClient(app)


def player_config(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="player-config">(.*?)</script>', html)
    assert match, "the page carries the player configuration"
    return json.loads(match.group(1))


def test_watch_pages_use_stable_links(tmp_path: Path, library_files: dict[str, int]) -> None:
    with client_for(tmp_path) as client:
        movie = client.get("/watch/movie/12")
        episode = client.get("/watch/tv/13/1/2")
        series = client.get("/watch/tv/13", follow_redirects=False)
        canonical = client.get(f"/watch/local/{library_files['Film.mp4']}", follow_redirects=False)
        home = client.get(f"/watch/local/{library_files['Home.mp4']}")
        missing = client.get("/watch/movie/999")

    assert movie.status_code == 200
    config = player_config(movie.text)
    assert config["file_id"] == library_files["Film.mp4"]
    assert config["title"] == "A New Film"
    assert config["url"] == "/watch/movie/12"
    assert config["backdrop"] == "https://image.tmdb.org/t/p/original/backdrop.jpg"
    assert config["subtitles"] == [
        {
            "id": "x0",
            "label": "English",
            "detail": "Film.en.srt",
            "language": "en",
            "forced": False,
            "kind": "text",
            "url": f"/api/playback/files/{library_files['Film.mp4']}/subtitles/x0.vtt",
        }
    ]
    assert config["qualities"] == [720, 480]
    assert "/mnt" not in movie.text and str(tmp_path) not in movie.text, "paths stay private"

    config = player_config(episode.text)
    assert config["subtitle"] == "S1 · E2 · Second"
    assert config["previous"]["url"] == "/watch/tv/13/1/1"
    assert config["previous"]["label"] == "S1 · E1 · Pilot"
    assert config["next"] is None
    assert series.status_code == 303 and series.headers["location"] == "/watch/tv/13/1/1"
    assert canonical.status_code == 307
    assert canonical.headers["location"] == f"/watch/movie/12?file={library_files['Film.mp4']}"
    assert player_config(home.text)["url"] == f"/watch/local/{library_files['Home.mp4']}"
    assert missing.status_code == 404
    assert "not in your library" in missing.text


def test_sessions_direct_streams_and_subtitles(
    tmp_path: Path, library_files: dict[str, int]
) -> None:
    film = library_files["Film.mp4"]
    with client_for(tmp_path) as client:
        no_player = client.post("/api/playback/sessions", json={"file_id": film})
        unknown = client.post(
            "/api/playback/sessions", json={"file_id": 999, "player": "abcdefgh12"}
        )
        bad_quality = client.post(
            "/api/playback/sessions",
            json={"file_id": film, "player": "abcdefgh12", "max_height": 333},
        )
        started = client.post(
            "/api/playback/sessions",
            json={"file_id": film, "player": "abcdefgh12", "capabilities": CHROME},
        )
        partial = client.get(started.json()["url"], headers={"Range": "bytes=10-19"})
        subtitles = client.get(f"/api/playback/files/{film}/subtitles/x0.vtt")
        no_track = client.get(f"/api/playback/files/{film}/subtitles/x5.vtt")
        gone = client.get("/api/playback/sessions/nope/main.m3u8")

    assert no_player.status_code == 400
    assert unknown.status_code == 404
    assert bad_quality.status_code == 400
    assert started.json() == {
        "mode": "direct",
        "label": "Direct play",
        "reasons": [],
        "url": f"/api/playback/files/{film}/stream",
        "session": None,
        "duration": 6000.0,
        "height": 1080,
    }
    assert partial.status_code == 206
    assert partial.content == b"0123456789"
    assert partial.headers["content-type"] == "video/mp4"
    assert subtitles.text == "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n"
    assert subtitles.headers["content-type"].startswith("text/vtt")
    assert no_track.status_code == 404
    assert gone.status_code == 404


def test_progress_feeds_continue_watching_and_cards(
    tmp_path: Path, library_files: dict[str, int]
) -> None:
    library = FakeLibrary()
    library.add("Media", [("/media", "movie")])
    settings = AppSettings(tmdb_token="test-token", database_path=tmp_path / "mytaste.db")
    app = create_app(settings, catalog=FakeCatalog(), library=library)
    app.state.playback.ffprobe = "ffprobe"
    with TestClient(app) as client:
        client.post("/settings/services", data={"region": "DE", "provider_ids": "8"})
        saved = client.post(
            "/api/playback/progress",
            json={"file_id": library_files["Film.mp4"], "position": 1800, "duration": 6000},
        )
        client.post(
            "/api/playback/progress",
            json={"file_id": library_files["Show.S01E01.mp4"], "position": 10, "ended": True},
        )
        invalid = client.post("/api/playback/progress", json={"file_id": "x", "position": 1})
        page = client.get("/")
        episodes = client.get("/api/items/tv/13/episodes")
        unwatched = client.post(
            "/api/playback/watched", json={"url": "/watch/tv/13/1/1", "watched": False}
        )
        unknown = client.post(
            "/api/playback/watched", json={"url": "/watch/movie/1", "watched": True}
        )

    assert saved.json() == {"watched": False, "position": 1800.0}
    assert invalid.status_code == 404
    assert "Continue watching" in page.text
    assert 'href="/watch/movie/12"' in page.text
    assert "70 min left" in page.text
    assert "Up next · S1 · E2" in page.text
    assert 'data-play-url="/watch/movie/12"' in page.text
    assert 'data-resume="1800"' in page.text
    assert 'class="card-progress" title="30% watched"' in page.text
    payload = episodes.json()
    assert payload["next_up"] == {"url": "/watch/tv/13/1/2", "label": "S1 · E2", "resume": False}
    first, second = payload["seasons"][0]["episodes"]
    assert "play_url" not in first, "the fake library only has the second episode"
    assert (second["play_url"], second["watched"], second["progress"]) == (
        "/watch/tv/13/1/2",
        False,
        0,
    )
    assert unwatched.json() == {"watched": False}
    assert unknown.status_code == 404

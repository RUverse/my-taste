from __future__ import annotations

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import requires_ffmpeg

from mytaste.library.models import ScannedFile
from mytaste.playback import service as service_module
from mytaste.playback.decision import Capabilities, PlaybackOptions
from mytaste.playback.models import MediaInfo, VideoStream
from mytaste.playback.service import PlaybackService, PlaybackUnavailableError, state_key
from mytaste.storage.library import ItemDraft, LibraryRepository
from mytaste.storage.playback import PlaybackRepository


def scanned(path: Path, season: int | None = None, episode: int | None = None) -> ScannedFile:
    return ScannedFile(
        path=str(path),
        size=path.stat().st_size if path.exists() else 1,
        modified_at="2026-09-01T10:00:00+00:00",
        group_key="unused",
        titles=("Title",),
        season=season,
        episode=episode,
    )


def build(tmp_path: Path, **kwargs) -> tuple[PlaybackService, LibraryRepository, Path]:
    folder = tmp_path / "media"
    (folder / "Show").mkdir(parents=True)
    for name in ("Film.mkv", "Film.1080p.mkv", "Other.mkv"):
        (folder / name).write_bytes(b"x" * (len(name) * 10))
    for episode in range(1, 4):
        (folder / "Show" / f"Show.S01E0{episode}.mkv").write_bytes(b"e")
    (folder / "Show" / "Show.S00E01.mkv").write_bytes(b"s")
    library = LibraryRepository(tmp_path / "data" / "mytaste.db")
    library.initialize()
    playback_repository = PlaybackRepository(library.database_path)
    playback_repository.initialize()
    created = library.add_library("Media", [(str(folder), "movie")])
    show_files = tuple(
        scanned(folder / "Show" / f"Show.S01E0{episode}.mkv", 1, episode) for episode in (3, 1, 2)
    ) + (scanned(folder / "Show" / "Show.S00E01.mkv", 0, 1),)
    library.replace_items(
        created.id,
        [
            ItemDraft(
                "movie:film",
                "movie",
                "Film",
                (scanned(folder / "Film.mkv"), scanned(folder / "Film.1080p.mkv")),
                tmdb_id=10,
                poster_path="/film.jpg",
            ),
            ItemDraft("movie:other", "movie", "Other", (scanned(folder / "Other.mkv"),)),
            ItemDraft("tv:show", "tv", "Show", show_files, tmdb_id=20),
        ],
    )
    service = PlaybackService(
        library,
        playback_repository,
        cache_dir=tmp_path / "cache",
        roots=(tmp_path,),
        background_probe=False,
        **kwargs,
    )
    return service, library, folder


def test_rescans_keep_file_and_title_ids(tmp_path: Path) -> None:
    service, library, folder = build(tmp_path)
    before = {file.path: file.id for file in library.show_files(20)}
    movie = service.movie(10)
    assert movie is not None
    film = library.get_library(1)
    assert film is not None

    library.replace_items(
        film.id,
        [
            ItemDraft(
                "tv:show",
                "tv",
                "Show",
                tuple(
                    scanned(Path(path), 1, int(Path(path).stem[-1])) for path in sorted(before)[1:]
                ),
                tmdb_id=20,
            ),
            ItemDraft("movie:film", "movie", "Film", (scanned(folder / "Film.mkv"),), tmdb_id=10),
        ],
    )

    after = {file.path: file.id for file in library.show_files(20)}
    assert after == {path: before[path] for path in sorted(before)[1:]}
    remaining = service.movie(10)
    assert remaining is not None
    assert [file.id for file in remaining.files] == [
        file.id for file in movie.files if file.path.endswith("Film.mkv")
    ]
    removed = next(file for file in movie.files if file.path.endswith("Film.1080p.mkv"))
    assert service.local(removed.id) is None, "a file gone from the drive loses its link"


def test_targets_and_urls(tmp_path: Path) -> None:
    service, library, _ = build(tmp_path)

    movie = service.movie(10)
    episode = service.episode(20, 1, 2)
    other = library.movie_files(10)

    assert movie is not None and episode is not None
    assert movie.url == "/watch/movie/10" and movie.key == "movie:10"
    assert [file.name for file in movie.files] == ["Film.1080p.mkv", "Film.mkv"], "largest first"
    assert episode.url == "/watch/tv/20/1/2" and episode.episode_label == "S1 · E2"
    assert [item.url for item in service.episodes(episode)] == [
        "/watch/tv/20/1/1",
        "/watch/tv/20/1/2",
        "/watch/tv/20/1/3",
        "/watch/tv/20/0/1",
    ]
    unmatched = service.movie(99)
    assert unmatched is None
    local = next(file for file in library.files_for_item(2) if file.title == "Other")
    target = service.local(local.id)
    assert target is not None and target.url == f"/watch/local/{local.id}"
    assert state_key(local) == f"file:{local.id}"
    assert len(other) == 2


def test_progress_marks_titles_watched_once(tmp_path: Path) -> None:
    service, library, _ = build(tmp_path)
    file = library.movie_files(10)[0]

    early = service.record_progress(file, 12, 6000)
    middle = service.record_progress(file, 2400, 6000)
    done = service.record_progress(file, 5500, 6000)
    again = service.record_progress(file, 5990, 0, ended=True)
    rewatch = service.record_progress(file, 600, 6000)

    assert (early.resumable, middle.resumable) == (False, True)
    assert middle.progress == pytest.approx(0.4)
    assert (done.watched, done.position, done.play_count) == (True, 0, 1)
    assert (again.watched, again.play_count) == (True, 1), "the end is only counted once"
    assert (rewatch.watched, rewatch.position, rewatch.play_count) == (True, 600, 1)
    target = service.movie(10)
    assert target is not None
    cleared = service.set_watched(target, False)
    assert (cleared.watched, cleared.position) == (False, 0)
    assert service.set_watched(target, True).play_count == 2


def test_next_up_and_continue_watching(tmp_path: Path) -> None:
    service, library, _ = build(tmp_path)
    episodes = {(file.season, file.episode): file for file in library.show_files(20)}

    assert service.next_up(20).url == "/watch/tv/20/1/1"  # type: ignore[union-attr]
    service.record_progress(episodes[(1, 1)], 1800, 1800, ended=True)
    assert service.next_up(20).url == "/watch/tv/20/1/2"  # type: ignore[union-attr]
    service.record_progress(episodes[(1, 3)], 300, 1800)
    assert service.next_up(20).url == "/watch/tv/20/1/3", "an episode in progress comes first"  # type: ignore[union-attr]

    movie = library.movie_files(10)[0]
    service.record_progress(movie, 1200, 6000)
    entries = service.continue_watching()
    assert [(entry.target.url, entry.up_next) for entry in entries] == [
        ("/watch/movie/10", False),
        ("/watch/tv/20/1/3", False),
    ]

    service.record_progress(episodes[(1, 3)], 1800, 1800, ended=True)
    service.record_progress(movie, 6000, 6000, ended=True)
    entries = service.continue_watching()
    assert [(entry.target.url, entry.up_next) for entry in entries] == [("/watch/tv/20/1/2", True)]
    service.record_progress(episodes[(1, 2)], 1800, 1800, ended=True)
    assert service.continue_watching() == (), "nothing is left once every episode is watched"
    assert service.next_up(20).url == "/watch/tv/20/1/1"  # type: ignore[union-attr]
    assert service.next_up(404) is None


def test_files_outside_the_library_are_refused(tmp_path: Path) -> None:
    service, library, folder = build(tmp_path)
    file = library.movie_files(10)[0]
    outside = tmp_path / "secret.mkv"
    outside.write_bytes(b"secret")

    assert asyncio.run(service.resolve_path(file)) == Path(file.path).resolve()
    Path(file.path).unlink()
    Path(file.path).symlink_to(outside)
    with pytest.raises(PlaybackUnavailableError):
        asyncio.run(service.resolve_path(file))
    Path(file.path).unlink()
    with pytest.raises(PlaybackUnavailableError):
        asyncio.run(service.resolve_path(file))

    other = library.show_files(20)[0]
    service.roots = (tmp_path / "elsewhere",)
    with pytest.raises(PlaybackUnavailableError):
        asyncio.run(service.resolve_path(other))


def test_probe_results_are_cached_until_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, library, _ = build(tmp_path)
    service.ffprobe = "ffprobe"
    calls: list[Path] = []

    def fake_probe(_ffprobe: str, path: Path, **_kwargs: object) -> MediaInfo:
        calls.append(path)
        return MediaInfo("matroska", 60.0, video=VideoStream(0, "h264", height=720))

    monkeypatch.setattr(service_module, "probe_file", fake_probe)
    file = library.movie_files(10)[0]

    first = asyncio.run(service.media_info(file))
    second = asyncio.run(service.media_info(file))
    assert first == second and len(calls) == 1
    asyncio.run(service.media_info(replace(file, size=file.size + 1)))
    assert len(calls) == 2
    assert service.probe_summary() == {"files": 7, "probed": 0}


@requires_ffmpeg
def test_real_files_play_directly_or_through_a_session(media_dir: Path, tmp_path: Path) -> None:
    service, library, folder = build(tmp_path)
    shutil.copy(media_dir / "Clip.mkv", folder / "Film.mkv")
    (folder / "Film.fa.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nسلام\n")
    film = library.get_library(1)
    assert film is not None
    library.replace_items(
        film.id,
        [ItemDraft("movie:film", "movie", "Film", (scanned(folder / "Film.mkv"),), tmdb_id=10)],
    )
    file = library.movie_files(10)[0]
    mkv = Capabilities(
        hls=True,
        direct_containers=frozenset({"mp4", "mkv"}),
        direct_video=frozenset({"h264"}),
        direct_audio=frozenset({"aac"}),
        hls_video=frozenset({"h264"}),
        hls_audio=frozenset({"aac"}),
    )

    async def scenario() -> None:
        await service.start()
        try:
            direct = await service.start_playback(file, mkv, PlaybackOptions(), owner="viewer-1")
            assert (direct.decision.mode, direct.url) == (
                "direct",
                f"/api/playback/files/{file.id}/stream",
            )
            remux = await service.start_playback(
                file, mkv, PlaybackOptions(excluded=frozenset({"direct"})), owner="viewer-1"
            )
            assert remux.session is not None
            assert remux.url == f"/api/playback/sessions/{remux.session.id}/master.m3u8"
            assert 'CODECS="avc1.42e00c,mp4a.40.2"' in service.master_playlist(remux.session)
            assert len(await remux.session.init_segment()) > 100
            external = await service.subtitle(file, "x0")
            embedded = await service.subtitle(file, "s2")
            assert external == "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nسلام\n"
            assert embedded.startswith("WEBVTT")
            assert "Hello <b>there</b>" in embedded and "Second line" in embedded
            with pytest.raises(KeyError):
                await service.subtitle(file, "x9")
            with pytest.raises(KeyError):
                await service.subtitle(file, "../etc")
        finally:
            await service.stop()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))

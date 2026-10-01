"""Probing, keyframe indexes, and HLS sessions against small synthetic clips."""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path

import pytest
from conftest import FFMPEG, FFPROBE, requires_ffmpeg

from mytaste.playback.decision import Capabilities, PlaybackOptions, decide
from mytaste.playback.fmp4 import _children, fragment_start, video_timing
from mytaste.playback.keyframes import read_keyframes
from mytaste.playback.models import MediaInfo
from mytaste.playback.probe import ProbeError, probe_file
from mytaste.playback.sessions import (
    TIMESTAMP_OFFSET,
    CommandFactory,
    SegmentPlan,
    SessionManager,
)

pytestmark = requires_ffmpeg

H264_ONLY = Capabilities(
    hls=True,
    direct_containers=frozenset({"mp4"}),
    direct_video=frozenset({"h264"}),
    direct_audio=frozenset({"aac"}),
    hls_video=frozenset({"h264"}),
    hls_audio=frozenset({"aac"}),
)


def probe(path: Path) -> MediaInfo:
    assert FFPROBE is not None
    return probe_file(FFPROBE, path)


@pytest.mark.parametrize("name", ["Clip.mkv", "Clip.mp4"])
def test_keyframes_come_from_the_container_index(media_dir: Path, name: str) -> None:
    keyframes = read_keyframes(media_dir / name)

    assert keyframes is not None
    assert [round(value, 2) for value in keyframes] == [float(value) for value in range(0, 20, 2)]


def test_unknown_or_broken_files_have_no_keyframes(tmp_path: Path, media_dir: Path) -> None:
    text = tmp_path / "notes.mkv"
    text.write_text("not a video")
    truncated = tmp_path / "cut.mkv"
    truncated.write_bytes((media_dir / "Clip.mkv").read_bytes()[:400])

    assert read_keyframes(text) is None
    assert read_keyframes(truncated) is None
    assert read_keyframes(tmp_path / "missing.mkv") is None


def test_probe_describes_streams_and_external_subtitles(media_dir: Path, tmp_path: Path) -> None:
    folder = tmp_path / "Clip (2020)"
    folder.mkdir()
    shutil.copy(media_dir / "Clip.mkv", folder / "Clip.mkv")
    (folder / "Clip.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")

    info = probe(folder / "Clip.mkv")

    assert info.container == "matroska"
    assert info.duration == pytest.approx(20, abs=0.2)
    assert info.video is not None and info.video.codec == "h264"
    assert (info.video.width, info.video.height, info.video.bit_depth) == (320, 180, 8)
    assert [(audio.codec, audio.channels) for audio in info.audio] == [("aac", 1)]
    assert [(item.codec, item.language, item.text) for item in info.subtitles] == [
        ("subrip", "per", True)
    ]
    assert info.subtitles[0].label == "Persian"
    assert [(item.name, item.language) for item in info.external_subtitles] == [
        ("Clip.en.srt", "en")
    ]
    assert len(info.keyframes) == 10
    assert MediaInfo.from_dict(info.to_dict()) == info

    broken = tmp_path / "broken.mkv"
    broken.write_text("nope")
    with pytest.raises(ProbeError):
        probe(broken)


def read_fragment_starts(init: bytes, segment: bytes) -> list[float]:
    timing = video_timing(init[init.index(b"moov") - 4 :])
    starts = []
    for kind, start, end in _children(segment, 0, len(segment)):
        if kind == b"moof":
            seconds = fragment_start(segment[start - 8 : end], timing)
            assert seconds is not None
            starts.append(round(seconds - TIMESTAMP_OFFSET, 2))
    return starts


async def open_session(
    manager: SessionManager, path: Path, *, start_index: int = 0, transcode: bool = False
):
    info = probe(path)
    if transcode:
        decision = decide(info, Capabilities(hls=True))
    else:
        decision = decide(info, H264_ONLY, PlaybackOptions(excluded=frozenset({"direct"})))
    plan = (
        SegmentPlan.from_keyframes(info.keyframes, info.duration)
        if decision.copy_video
        else SegmentPlan.fixed(info.duration)
    )
    assert FFMPEG is not None
    command = CommandFactory(FFMPEG, path, info, decision, hwaccel=None, burn_subtitle_index=None)
    session = await manager.create(
        owner=f"owner-{time.monotonic_ns()}",
        file_id=1,
        command=command,
        info=info,
        decision=decision,
        plan=plan,
        start_index=start_index,
    )
    return session, plan


def test_remuxed_segments_line_up_with_keyframes_across_restarts(
    media_dir: Path, tmp_path: Path
) -> None:
    async def scenario() -> None:
        manager = SessionManager(tmp_path / "streams", max_transcodes=1)
        await manager.start()
        session, plan = await open_session(manager, media_dir / "Clip.mkv")
        assert session.decision.mode == "remux"
        assert plan.starts == (0.0, 6.0, 12.0, 18.0)
        init = await session.init_segment()
        first = (await session.segment(0)).read_bytes()
        assert read_fragment_starts(init, first) == [0.0, 2.0, 4.0]
        # Jump back and forth: each restart produces exactly the planned segment.
        last = (await session.segment(3)).read_bytes()
        assert read_fragment_starts(init, last) == [18.0]
        second = (await session.segment(1)).read_bytes()
        assert read_fragment_starts(init, second) == [6.0, 8.0, 10.0]
        playlist = plan.media_playlist()
        assert playlist.count("#EXTINF:") == 4
        assert "#EXTINF:6.000000,\n0.m4s" in playlist
        assert playlist.rstrip().endswith("#EXT-X-ENDLIST")
        await manager.close()
        assert not (tmp_path / "streams").exists()

    asyncio.run(scenario())


def test_transcoded_segments_have_forced_keyframes(media_dir: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = SessionManager(tmp_path / "streams", max_transcodes=1)
        await manager.start()
        session, plan = await open_session(manager, media_dir / "Clip.mp4", transcode=True)
        assert session.decision.mode == "transcode"
        assert plan.starts == (0.0, 4.0, 8.0, 12.0, 16.0)
        init = await session.init_segment()
        for index in (2, 0, 4):
            data = (await session.segment(index)).read_bytes()
            starts = read_fragment_starts(init, data)
            assert len(starts) == 1
            assert starts[0] == pytest.approx(plan.starts[index], abs=0.05)
        await manager.close()

    asyncio.run(scenario())


def test_a_throttled_session_stops_promptly(media_dir: Path, tmp_path: Path) -> None:
    """ffmpeg blocked on a full pipe must not keep a session from closing."""

    async def throttled(manager: SessionManager):
        session, _ = await open_session(manager, media_dir / "Clip.mp4", transcode=True)
        session.lookahead = 0.5
        await session.segment(0)
        await asyncio.sleep(1.5)
        return session

    async def scenario() -> None:
        manager = SessionManager(tmp_path / "streams", max_transcodes=1)
        await manager.start()
        session = await throttled(manager)
        started = time.monotonic()
        replacement, _ = await open_session(manager, media_dir / "Clip.mp4", transcode=True)
        assert time.monotonic() - started < 5, "another viewer's transcode replaces it"
        assert set(manager.sessions) == {replacement.id}
        assert session.id != replacement.id

        await throttled(manager)
        started = time.monotonic()
        await asyncio.wait_for(manager.close(), timeout=10)
        assert time.monotonic() - started < 5, "shutting down does not wait for ffmpeg"
        assert manager.sessions == {}

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_only_one_transcode_runs_at_a_time(media_dir: Path, tmp_path: Path) -> None:
    async def scenario() -> None:
        manager = SessionManager(tmp_path / "streams", max_transcodes=1)
        await manager.start()
        first, _ = await open_session(manager, media_dir / "Clip.mp4", transcode=True)
        remux, _ = await open_session(manager, media_dir / "Clip.mkv")
        second, _ = await open_session(manager, media_dir / "Clip.mp4", transcode=True)
        assert set(manager.sessions) == {remux.id, second.id}
        assert first.id not in manager.sessions
        await manager.close()

    asyncio.run(scenario())


def test_an_abandoned_request_does_not_pull_ffmpeg_back(media_dir: Path, tmp_path: Path) -> None:
    """After a seek, the old segment request must not restart ffmpeg at its own position."""

    async def scenario() -> None:
        manager = SessionManager(tmp_path / "streams", max_transcodes=1)
        await manager.start()
        session, _ = await open_session(manager, media_dir / "Clip.mp4", transcode=True)
        await session.init_segment()
        old = asyncio.create_task(session.segment(1))
        await asyncio.sleep(0)
        new = await session.segment(4)
        assert new.exists()
        result = await asyncio.gather(old, return_exceptions=True)
        assert isinstance(result[0], Path) or "Superseded" in str(result[0])
        assert session._producer_start == 4, "ffmpeg stayed where the viewer went"
        await manager.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))

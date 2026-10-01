from __future__ import annotations

import pytest

from mytaste.playback.decision import (
    Capabilities,
    PlaybackOptions,
    UnplayableError,
    codec_string,
    decide,
)
from mytaste.playback.models import AudioStream, MediaInfo, SubtitleStream, VideoStream
from mytaste.playback.sessions import SegmentPlan, build_command

KEYFRAMES = tuple(float(value) for value in range(0, 600, 5))

SAFARI = Capabilities.from_payload(
    {
        "hls": True,
        "direct_containers": ["mp4"],
        "direct_video": ["h264", "hevc", "hevc10"],
        "direct_audio": ["aac", "ac3", "eac3", "mp3"],
        "hls_video": ["h264", "hevc", "hevc10"],
        "hls_audio": ["aac", "ac3", "eac3"],
    }
)
CHROME_LINUX = Capabilities.from_payload(
    {
        "hls": True,
        "direct_containers": ["mp4", "webm", "mkv"],
        "direct_video": ["h264", "vp9", "av1"],
        "direct_audio": ["aac", "mp3", "opus", "flac"],
        "hls_video": ["h264", "vp9", "av1"],
        "hls_audio": ["aac", "opus", "flac"],
    }
)


def media(
    container: str = "matroska",
    codec: str = "h264",
    *,
    profile: str = "High",
    bit_depth: int = 8,
    height: int = 1080,
    audio: tuple[str, ...] = ("aac",),
    tag: str = "",
    keyframes: tuple[float, ...] = KEYFRAMES,
) -> MediaInfo:
    return MediaInfo(
        container=container,
        duration=600,
        bit_rate=4_000_000,
        video=VideoStream(
            index=0,
            codec=codec,
            profile=profile,
            level=41 if codec == "h264" else 120,
            bit_depth=bit_depth,
            width=height * 16 // 9,
            height=height,
            codec_tag=tag,
        ),
        audio=tuple(
            AudioStream(index=index + 1, codec=name, channels=6, default=index == 0)
            for index, name in enumerate(audio)
        ),
        subtitles=(SubtitleStream(index=9, codec="hdmv_pgs_subtitle", language="eng"),),
        keyframes=keyframes,
    )


@pytest.mark.parametrize(
    ("info", "capabilities", "mode", "copy_audio"),
    [
        (media("mov", tag="avc1"), SAFARI, "direct", True),
        (media("mov", "hevc", profile="Main", tag="hev1"), SAFARI, "remux", True),
        (media("mov", "hevc", profile="Main", tag="hvc1"), SAFARI, "direct", True),
        (media("matroska", "hevc", profile="Main 10", bit_depth=10), SAFARI, "remux", True),
        (media("matroska", "h264", audio=("dts",)), SAFARI, "remux", False),
        (media("matroska", "h264"), CHROME_LINUX, "direct", True),
        (media("matroska", "h264", audio=("ac3",)), CHROME_LINUX, "remux", False),
        (
            media("matroska", "hevc", profile="Main 10", bit_depth=10),
            CHROME_LINUX,
            "transcode",
            True,
        ),
        (media("avi", "mpeg4", profile="Advanced Simple Profile"), SAFARI, "transcode", True),
        (media("matroska", "h264", profile="High 10", bit_depth=10), SAFARI, "transcode", True),
        (media("matroska", "h264", keyframes=()), SAFARI, "transcode", True),
    ],
)
def test_the_cheapest_working_delivery_is_chosen(
    info: MediaInfo, capabilities: Capabilities, mode: str, copy_audio: bool
) -> None:
    decision = decide(info, capabilities)

    assert decision.mode == mode
    assert decision.copy_audio is copy_audio
    assert decision.copy_video is (mode != "transcode")


def test_options_force_conversion() -> None:
    info = media("mov", tag="avc1", audio=("aac", "ac3"))

    limited = decide(info, SAFARI, PlaybackOptions(max_height=720))
    second_audio = decide(info, CHROME_LINUX, PlaybackOptions(audio_index=2))
    burned = decide(info, SAFARI, PlaybackOptions(burn_subtitle_index=9))
    no_direct = decide(info, SAFARI, PlaybackOptions(excluded=frozenset({"direct"})))

    assert (limited.mode, limited.target_height) == ("transcode", 720)
    assert limited.reasons == ("Quality limited to 720p",)
    assert (second_audio.mode, second_audio.copy_audio, second_audio.audio.index) == (
        "remux",
        False,
        2,
    )
    assert burned.mode == "transcode"
    assert no_direct.mode == "remux"
    assert (
        decide(media(height=480), SAFARI, PlaybackOptions(burn_subtitle_index=9)).target_height
        == 480
    )


def test_browsers_without_hls_or_transcoding_get_clear_errors() -> None:
    with pytest.raises(UnplayableError, match="cannot play"):
        decide(media(), Capabilities(direct_containers=frozenset({"mp4"})))
    with pytest.raises(UnplayableError, match="turned off"):
        decide(media(codec="mpeg4"), SAFARI, can_transcode=False)
    with pytest.raises(UnplayableError, match="no video"):
        decide(MediaInfo(container="mp3", duration=10), SAFARI)


def test_capabilities_ignore_unknown_values() -> None:
    parsed = Capabilities.from_payload(
        {"hls": "yes", "direct_video": ["h264", "realvideo", 3], "hls_audio": "aac"}
    )

    assert parsed == Capabilities(direct_video=frozenset({"h264"}))
    assert Capabilities.from_payload(None) == Capabilities()


def test_codec_strings_describe_what_is_sent() -> None:
    hevc = media("matroska", "hevc", profile="Main 10", bit_depth=10, audio=("eac3",))

    assert codec_string(hevc, decide(hevc, SAFARI)) == "hvc1.2.4.L120.B0,ec-3"
    transcoded = decide(hevc, CHROME_LINUX)
    assert codec_string(hevc, transcoded) == "avc1.64001f,mp4a.40.2"
    assert codec_string(media(), decide(media(), SAFARI)) == "avc1.640029,mp4a.40.2"


def test_segment_plans_follow_keyframes_or_a_fixed_grid() -> None:
    plan = SegmentPlan.from_keyframes((0.0, 2.5, 5.0, 7.5, 10.0, 12.5, 14.6), 15.0)

    assert plan.starts == (0.0, 7.5)
    assert plan.end(1) == 15.0
    assert plan.index_for(7.45) == 1, "a fragment a hair early still starts its segment"
    assert plan.index_for(7.3) == 0
    assert SegmentPlan.fixed(9.0).starts == (0.0, 4.0)
    assert SegmentPlan.fixed(9.5, 4.0).starts == (0.0, 4.0, 8.0)


def test_ffmpeg_commands_copy_or_convert_only_what_is_needed(tmp_path) -> None:
    hevc = media("matroska", "hevc", profile="Main 10", bit_depth=10, audio=("dts",))
    remux = build_command("ffmpeg", tmp_path / "a.mkv", hevc, decide(hevc, SAFARI), 12.5)
    transcode_decision = decide(hevc, CHROME_LINUX, PlaybackOptions(burn_subtitle_index=9))
    transcode = build_command(
        "ffmpeg",
        tmp_path / "a.mkv",
        hevc,
        transcode_decision,
        0,
        hwaccel="drm",
        burn_subtitle_index=9,
    )

    joined = " ".join(remux)
    assert "-ss 12.500 -copyts -i" in joined
    assert "-c:v copy -tag:v hvc1" in joined
    assert "-c:a aac -ac 2" in joined
    assert joined.endswith("-use_editlist 0 pipe:1")
    joined = " ".join(transcode)
    assert "-ss" not in transcode
    assert joined.startswith("ffmpeg -hide_banner -nostdin -loglevel error -hwaccel drm")
    assert "[0:9][0:0]scale2ref[sub][base];[base][sub]overlay" in joined
    assert "scale=-2:720" in joined
    assert "-c:v libx264" in joined
    assert "expr:gte(t,n_forced*4)" in joined

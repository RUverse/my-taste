"""Choose how a browser plays a file: the original bytes, a remux, or a transcode.

The browser reports which codecs it can decode (like Jellyfin's device profiles); nothing is
guessed from file extensions. Remuxing copies the video into fragmented MP4 for HLS and is
nearly free; transcoding re-encodes it with x264 and is the last resort on small servers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from mytaste.playback.models import AudioStream, MediaInfo, VideoStream, codec_label

Mode = Literal["direct", "remux", "transcode"]

_DIRECT_CONTAINERS = {"mov": "mp4", "mp4": "mp4", "matroska": "mkv", "webm": "webm"}
_FMP4_AUDIO = frozenset({"aac", "ac3", "eac3", "opus", "flac"})
_KNOWN_VIDEO = frozenset({"h264", "hevc", "av1", "vp9"})
_KNOWN_AUDIO = frozenset({"aac", "mp3", "ac3", "eac3", "opus", "flac", "vorbis"})
_H264_UNSUPPORTED_PROFILES = ("high 10", "high 4:2:2", "high 4:4:4")
TRANSCODE_HEIGHTS = (1080, 720, 480)
DEFAULT_TRANSCODE_HEIGHT = 720


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What a browser can decode, split by delivery method."""

    hls: bool = False
    direct_containers: frozenset[str] = frozenset()
    direct_video: frozenset[str] = frozenset()
    direct_audio: frozenset[str] = frozenset()
    hls_video: frozenset[str] = frozenset()
    hls_audio: frozenset[str] = frozenset()

    @classmethod
    def from_payload(cls, payload: object) -> Capabilities:
        if not isinstance(payload, Mapping):
            return cls()

        def values(key: str, allowed: Iterable[str]) -> frozenset[str]:
            raw = payload.get(key)
            if not isinstance(raw, list):
                return frozenset()
            return frozenset(value for value in raw if isinstance(value, str) and value in allowed)

        video_keys = {"h264", "hevc", "hevc10", "av1", "vp9"}
        return cls(
            hls=payload.get("hls") is True,
            direct_containers=values("direct_containers", {"mp4", "mkv", "webm"}),
            direct_video=values("direct_video", video_keys),
            direct_audio=values("direct_audio", _KNOWN_AUDIO),
            hls_video=values("hls_video", video_keys),
            hls_audio=values("hls_audio", _KNOWN_AUDIO),
        )


@dataclass(frozen=True, slots=True)
class PlaybackOptions:
    audio_index: int | None = None
    burn_subtitle_index: int | None = None
    max_height: int | None = None
    excluded: frozenset[Mode] = frozenset()


@dataclass(frozen=True, slots=True)
class Decision:
    mode: Mode
    copy_video: bool
    copy_audio: bool
    audio: AudioStream | None
    target_height: int | None = None
    reasons: tuple[str, ...] = field(default=())

    @property
    def label(self) -> str:
        if self.mode == "direct":
            return "Direct play"
        if self.mode == "remux":
            return "Direct stream" if self.copy_audio else "Direct stream · audio converted"
        return f"Converting to {self.target_height}p" if self.target_height else "Converting"


class UnplayableError(ValueError):
    """Raised when no delivery method can play a file in this browser."""


def decide(
    info: MediaInfo,
    capabilities: Capabilities,
    options: PlaybackOptions | None = None,
    *,
    can_transcode: bool = True,
) -> Decision:
    options = options or PlaybackOptions()
    video = info.video
    if video is None:
        raise UnplayableError("This file has no video track")
    audio = info.audio_stream(options.audio_index) or info.default_audio
    reasons: list[str] = []
    too_tall = bool(options.max_height and video.height > options.max_height * 1.1)
    if too_tall:
        reasons.append(f"Quality limited to {options.max_height}p")
    if options.burn_subtitle_index is not None:
        reasons.append("Image subtitles are drawn into the picture")

    direct_ok = (
        "direct" not in options.excluded
        and not too_tall
        and options.burn_subtitle_index is None
        and _direct_container(info, video) in capabilities.direct_containers
        and _video_key(video, direct=True) in capabilities.direct_video
        and (audio is None or audio.codec in capabilities.direct_audio)
        and (audio is None or audio == info.default_audio or len(info.audio) == 1)
    )
    if direct_ok:
        return Decision("direct", True, True, audio, reasons=tuple(reasons))
    if not capabilities.hls:
        raise UnplayableError("This browser cannot play this file")

    copy_audio = audio is not None and audio.codec in (capabilities.hls_audio & _FMP4_AUDIO)
    if audio is not None and not copy_audio:
        reasons.append(f"Audio {codec_label(audio.codec)} converted to AAC")

    video_key = _video_key(video, direct=False)
    video_ok = video_key in capabilities.hls_video
    if (
        "remux" not in options.excluded
        and video_ok
        and not too_tall
        and options.burn_subtitle_index is None
        and len(info.keyframes) >= 2
    ):
        return Decision("remux", True, copy_audio, audio, reasons=tuple(reasons))

    if not can_transcode:
        raise UnplayableError("This file needs converting, which is turned off on this server")
    if not video_ok:
        reasons.append(f"Video {_describe_video(video)} is not supported by this browser")
    elif len(info.keyframes) < 2 and not too_tall and options.burn_subtitle_index is None:
        reasons.append("The file has no keyframe index")
    height = options.max_height or DEFAULT_TRANSCODE_HEIGHT
    target = min(height, _even(video.height) or height)
    return Decision(
        "transcode", False, copy_audio, audio, target_height=target, reasons=tuple(reasons)
    )


def _direct_container(info: MediaInfo, video: VideoStream) -> str:
    container = _DIRECT_CONTAINERS.get(info.container, "")
    # Safari only plays HEVC in MP4 when its parameter sets are out of band ("hvc1").
    if container == "mp4" and video.codec == "hevc" and video.codec_tag != "hvc1":
        return ""
    return container


def _video_key(video: VideoStream, *, direct: bool) -> str:
    if video.codec not in _KNOWN_VIDEO:
        return ""
    if video.codec == "h264":
        profile = video.profile.casefold()
        if video.bit_depth > 8 or any(name in profile for name in _H264_UNSUPPORTED_PROFILES):
            return ""
        return "h264"
    if video.codec == "hevc":
        if "rext" in video.profile.casefold() or "4:4:4" in video.profile:
            return ""
        return "hevc10" if video.bit_depth > 8 else "hevc"
    return video.codec


def _describe_video(video: VideoStream) -> str:
    label = codec_label(video.codec)
    if video.bit_depth > 8:
        label += f" {video.bit_depth}-bit"
    return label


def _even(value: int) -> int:
    return value - value % 2


def codec_string(info: MediaInfo, decision: Decision) -> str:
    """Return the RFC 6381 ``CODECS`` value for the HLS master playlist."""

    video = info.video
    parts: list[str] = []
    if video is not None:
        if not decision.copy_video:
            height = decision.target_height or DEFAULT_TRANSCODE_HEIGHT
            parts.append("avc1.640029" if height > 720 else "avc1.64001f")
        elif video.codec == "h264":
            profile = video.profile.casefold()
            idc = "4d00" if profile == "main" else "42e0" if "baseline" in profile else "6400"
            parts.append(f"avc1.{idc}{(video.level or 40):02x}")
        elif video.codec == "hevc":
            ten_bit = video.bit_depth > 8
            parts.append(
                f"hvc1.{2 if ten_bit else 1}.{4 if ten_bit else 6}.L{video.level or 120}.B0"
            )
        elif video.codec == "av1":
            parts.append(f"av01.0.08M.{10 if video.bit_depth > 8 else 8:02d}")
        elif video.codec == "vp9":
            parts.append("vp09.00.40.08")
    audio = decision.audio
    if audio is not None:
        if not decision.copy_audio:
            parts.append("mp4a.40.2")
        else:
            parts.append(
                {
                    "aac": "mp4a.40.5" if "he" in audio.profile.casefold() else "mp4a.40.2",
                    "ac3": "ac-3",
                    "eac3": "ec-3",
                    "opus": "opus",
                    "flac": "fLaC",
                }.get(audio.codec, "mp4a.40.2")
            )
    return ",".join(parts)

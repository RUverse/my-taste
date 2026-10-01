"""Run ffprobe on a video file and describe its streams, keyframes, and subtitle files."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from mytaste.playback.keyframes import read_keyframes
from mytaste.playback.models import (
    AudioStream,
    Chapter,
    MediaInfo,
    SubtitleStream,
    VideoStream,
)
from mytaste.playback.subtitles import find_external_subtitles

_PROBE_TIMEOUT = 60
_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
_IGNORED_VIDEO = frozenset({"mjpeg", "png", "bmp", "gif"})


class ProbeError(RuntimeError):
    """Raised when ffprobe cannot read a file."""


def probe_file(
    ffprobe: str,
    path: Path,
    *,
    season: int | None = None,
    episode: int | None = None,
) -> MediaInfo:
    """Synchronously probe ``path``; call from a worker thread."""

    command = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("ffprobe could not read the file") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ProbeError(message[-1] if message else "ffprobe could not read the file")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe returned unreadable output") from exc
    info = parse_probe(payload)
    keyframes = read_keyframes(path) if info.video is not None else None
    external = find_external_subtitles(path, season=season, episode=episode)
    return MediaInfo(
        container=info.container,
        duration=info.duration,
        start_time=info.start_time,
        size=info.size,
        bit_rate=info.bit_rate,
        video=info.video,
        audio=info.audio,
        subtitles=info.subtitles,
        external_subtitles=external,
        chapters=info.chapters,
        keyframes=keyframes or (),
    )


def parse_probe(payload: dict[str, Any]) -> MediaInfo:
    """Turn ffprobe's JSON into a :class:`MediaInfo` (without keyframes or external files)."""

    format_info = payload.get("format") or {}
    streams = payload.get("streams") or []
    video: VideoStream | None = None
    audio: list[AudioStream] = []
    subtitles: list[SubtitleStream] = []
    for stream in streams:
        kind = stream.get("codec_type")
        codec = str(stream.get("codec_name") or "")
        tags = {
            str(key).casefold(): str(value) for key, value in (stream.get("tags") or {}).items()
        }
        disposition = stream.get("disposition") or {}
        index = int(stream.get("index", 0))
        if kind == "video" and video is None and codec and codec not in _IGNORED_VIDEO:
            if disposition.get("attached_pic"):
                continue
            pix_fmt = str(stream.get("pix_fmt") or "")
            video = VideoStream(
                index=index,
                codec=codec,
                profile=str(stream.get("profile") or ""),
                level=_int(stream.get("level")),
                pix_fmt=pix_fmt,
                bit_depth=_bit_depth(stream, pix_fmt),
                width=_int(stream.get("width")),
                height=_int(stream.get("height")),
                frame_rate=_rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate")),
                codec_tag=str(stream.get("codec_tag_string") or "").strip("[]0 "),
                hdr=str(stream.get("color_transfer") or "") in _HDR_TRANSFERS,
                duration=_float(stream.get("duration")),
            )
        elif kind == "audio" and codec:
            audio.append(
                AudioStream(
                    index=index,
                    codec=codec,
                    profile=str(stream.get("profile") or ""),
                    channels=_int(stream.get("channels")) or 2,
                    language=_language(tags),
                    title=tags.get("title", ""),
                    default=bool(disposition.get("default")),
                )
            )
        elif kind == "subtitle" and codec:
            title = tags.get("title", "")
            lowered = title.casefold()
            subtitles.append(
                SubtitleStream(
                    index=index,
                    codec=codec,
                    language=_language(tags),
                    title=title,
                    default=bool(disposition.get("default")),
                    forced=bool(disposition.get("forced")) or "forced" in lowered,
                    hearing_impaired=bool(disposition.get("hearing_impaired"))
                    or "sdh" in lowered.split(),
                )
            )
    chapters = tuple(
        Chapter(
            start=_float(chapter.get("start_time")),
            end=_float(chapter.get("end_time")),
            title=str((chapter.get("tags") or {}).get("title") or ""),
        )
        for chapter in payload.get("chapters") or []
    )
    return MediaInfo(
        container=str(format_info.get("format_name") or "").split(",")[0],
        duration=_float(format_info.get("duration")),
        start_time=_float(format_info.get("start_time")),
        size=_int(format_info.get("size")),
        bit_rate=_int(format_info.get("bit_rate")),
        video=video,
        audio=tuple(audio),
        subtitles=tuple(subtitles),
        chapters=chapters,
    )


def _language(tags: dict[str, str]) -> str:
    language = tags.get("language", "").strip().casefold()
    return "" if language in {"und", "unk", "mis", "zxx"} else language


def _bit_depth(stream: dict[str, Any], pix_fmt: str) -> int:
    raw = _int(stream.get("bits_per_raw_sample"))
    if raw:
        return raw
    for depth in (16, 12, 10):
        if f"p{depth}" in pix_fmt:
            return depth
    return 8


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _rate(value: object) -> float:
    try:
        numerator, _, denominator = str(value).partition("/")
        return float(numerator) / float(denominator or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0

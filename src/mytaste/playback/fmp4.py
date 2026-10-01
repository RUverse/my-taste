"""Minimal fragmented-MP4 parsing for ffmpeg's piped output.

ffmpeg writes ``ftyp`` and ``moov`` (the HLS init segment), then one ``moof``/``mdat`` pair per
video keyframe. The sessions read those boxes from the pipe and group the fragments into
playlist segments by the presentation time of each fragment's first video sample.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

_HEADER = struct.Struct(">I4s")
_MAX_BOX_BYTES = 512 * 1024 * 1024


class BoxError(ValueError):
    """Raised for a malformed or oversized box."""


@dataclass(frozen=True, slots=True)
class TrackTiming:
    track_id: int
    timescale: int


async def read_box(stream: asyncio.StreamReader) -> tuple[bytes, bytes] | None:
    """Return the next ``(type, whole box bytes)`` from ``stream``, or ``None`` at its end."""

    try:
        header = await stream.readexactly(8)
    except asyncio.IncompleteReadError as exc:
        if exc.partial:
            raise BoxError("truncated box header") from exc
        return None
    size, kind = _HEADER.unpack(header)
    if size == 1:
        extended = await stream.readexactly(8)
        size = struct.unpack(">Q", extended)[0]
        header += extended
    if size < len(header) or size > _MAX_BOX_BYTES:
        raise BoxError(f"unsupported {kind!r} box size {size}")
    body = await stream.readexactly(size - len(header))
    return kind, header + body


def _children(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """Return ``(type, payload_start, box_end)`` for child boxes inside ``data[start:end]``."""

    boxes: list[tuple[bytes, int, int]] = []
    offset = start
    while offset + 8 <= end:
        size, kind = _HEADER.unpack_from(data, offset)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        if size < header or offset + size > end:
            raise BoxError("malformed child box")
        boxes.append((kind, offset + header, offset + size))
        offset += size
    return boxes


def _child(data: bytes, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    for name, payload, box_end in _children(data, start, end):
        if name == kind:
            return payload, box_end
    return None


def video_timing(moov: bytes) -> TrackTiming:
    """Find the video track id and its timescale in a ``moov`` box."""

    for kind, start, end in _children(moov, 8, len(moov)):
        if kind != b"trak":
            continue
        tkhd = _child(moov, start, end, b"tkhd")
        mdia = _child(moov, start, end, b"mdia")
        if tkhd is None or mdia is None:
            continue
        hdlr = _child(moov, *mdia, b"hdlr")
        mdhd = _child(moov, *mdia, b"mdhd")
        if hdlr is None or mdhd is None or moov[hdlr[0] + 8 : hdlr[0] + 12] != b"vide":
            continue
        version = moov[tkhd[0]]
        track_id = struct.unpack_from(">I", moov, tkhd[0] + (20 if version == 1 else 12))[0]
        version = moov[mdhd[0]]
        timescale = struct.unpack_from(">I", moov, mdhd[0] + (20 if version == 1 else 12))[0]
        return TrackTiming(track_id, timescale)
    raise BoxError("no video track in init segment")


def fragment_start(moof: bytes, video: TrackTiming) -> float | None:
    """Presentation time (seconds) of the first video sample in a ``moof`` box."""

    for kind, start, end in _children(moof, 8, len(moof)):
        if kind != b"traf":
            continue
        tfhd = _child(moof, start, end, b"tfhd")
        if tfhd is None or struct.unpack_from(">I", moof, tfhd[0] + 4)[0] != video.track_id:
            continue
        tfdt = _child(moof, start, end, b"tfdt")
        if tfdt is None:
            return None
        version = moof[tfdt[0]]
        decode_time = (
            struct.unpack_from(">Q", moof, tfdt[0] + 4)[0]
            if version == 1
            else struct.unpack_from(">I", moof, tfdt[0] + 4)[0]
        )
        offset = 0
        trun = _child(moof, start, end, b"trun")
        if trun is not None:
            offset = _first_composition_offset(moof, trun[0])
        return (decode_time + offset) / video.timescale
    return None


def _first_composition_offset(data: bytes, start: int) -> int:
    version = data[start]
    flags = int.from_bytes(data[start + 1 : start + 4], "big")
    sample_count = struct.unpack_from(">I", data, start + 4)[0]
    if not sample_count or not flags & 0x800:
        return 0
    position = start + 8
    if flags & 0x1:
        position += 4
    if flags & 0x4:
        position += 4
    for flag in (0x100, 0x200, 0x400):
        if flags & flag:
            position += 4
    fmt = ">i" if version == 1 else ">I"
    value = struct.unpack_from(fmt, data, position)[0]
    return value - (1 << 32) if version == 0 and value >= 1 << 31 else value

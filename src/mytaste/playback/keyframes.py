"""Read video keyframe times from a container index without reading the media itself.

Remuxed HLS segments must start on keyframes, so the playlist is built from this list before
ffmpeg runs (the approach Jellyfin uses). Matroska files carry it in their Cues element and MP4
files in the sync-sample table of the video track. Both are a few kilobytes near the start or end
of the file, which keeps this cheap even on slow USB drives.
"""

from __future__ import annotations

import struct
from bisect import bisect_left
from pathlib import Path
from typing import BinaryIO

_MAX_INDEX_BYTES = 64 * 1024 * 1024

# Matroska element ids (with their length marker bits).
_EBML = 0x1A45DFA3
_SEGMENT = 0x18538067
_SEEK_HEAD = 0x114D9B74
_SEEK = 0x4DBB
_SEEK_ID = 0x53AB
_SEEK_POSITION = 0x53AC
_INFO = 0x1549A966
_TIMESTAMP_SCALE = 0x2AD7B1
_TRACKS = 0x1654AE6B
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_CUES = 0x1C53BB6B
_CUE_POINT = 0xBB
_CUE_TIME = 0xB3
_CUE_TRACK_POSITIONS = 0xB7
_CUE_TRACK = 0xF7
_CLUSTER = 0x1F43B675


class KeyframeError(ValueError):
    """Raised when a container index is missing or malformed."""


def read_keyframes(path: Path | str) -> tuple[float, ...] | None:
    """Return sorted keyframe presentation times in seconds, or ``None`` when unknown."""

    try:
        with open(path, "rb") as handle:
            head = handle.read(12)
            handle.seek(0)
            if head[:4] == struct.pack(">I", _EBML):
                times = _matroska_keyframes(handle)
            elif head[4:8] in (b"ftyp", b"moov", b"free", b"wide", b"mdat", b"skip"):
                times = _mp4_keyframes(handle)
            else:
                return None
    except (OSError, KeyframeError, struct.error, IndexError, ValueError):
        return None
    if len(times) < 2:
        return None
    return times


# Matroska ------------------------------------------------------------------------------------


def _read_vint(data: bytes, offset: int, *, keep_marker: bool) -> tuple[int, int]:
    first = data[offset]
    if first == 0:
        raise KeyframeError("invalid EBML variable-length integer")
    length = 8 - first.bit_length() + 1
    value = first if keep_marker else first & ((1 << (8 - length)) - 1)
    for byte in data[offset + 1 : offset + length]:
        value = (value << 8) | byte
    if len(data) < offset + length:
        raise KeyframeError("truncated EBML element")
    return value, offset + length


def _element_header(data: bytes, offset: int) -> tuple[int, int, int]:
    """Return ``(id, size, data_offset)``; an unknown size is reported as ``-1``."""

    element_id, offset = _read_vint(data, offset, keep_marker=True)
    length = 8 - data[offset].bit_length() + 1
    size, data_offset = _read_vint(data, offset, keep_marker=False)
    if size == (1 << (7 * length)) - 1:
        size = -1
    return element_id, size, data_offset


def _children(data: bytes) -> list[tuple[int, bytes]]:
    children: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(data):
        element_id, size, start = _element_header(data, offset)
        if size < 0:
            raise KeyframeError("unknown-size child element")
        children.append((element_id, data[start : start + size]))
        offset = start + size
    return children


def _uint(data: bytes) -> int:
    return int.from_bytes(data, "big") if data else 0


def _read_element(handle: BinaryIO, position: int) -> tuple[int, bytes]:
    handle.seek(position)
    header = handle.read(16)
    element_id, size, start = _element_header(header, 0)
    if size < 0 or size > _MAX_INDEX_BYTES:
        raise KeyframeError("index element is too large")
    handle.seek(position + start)
    payload = handle.read(size)
    if len(payload) != size:
        raise KeyframeError("truncated index element")
    return element_id, payload


def _matroska_keyframes(handle: BinaryIO) -> tuple[float, ...]:
    header = handle.read(64)
    _, ebml_size, ebml_start = _element_header(header, 0)
    segment_position = ebml_start + ebml_size
    handle.seek(segment_position)
    segment_id, _, segment_start = _element_header(handle.read(16), 0)
    if segment_id != _SEGMENT:
        raise KeyframeError("missing Matroska segment")
    segment_data = segment_position + segment_start

    positions: dict[int, int] = {}
    pending_seek_heads: list[int] = []

    def read_seek_head(payload: bytes) -> None:
        for child_id, child in _children(payload):
            if child_id != _SEEK:
                continue
            fields = dict(_children(child))
            target = _uint(fields.get(_SEEK_ID, b""))
            position = _uint(fields.get(_SEEK_POSITION, b""))
            if target == _SEEK_HEAD:
                pending_seek_heads.append(segment_data + position)
            elif target and target not in positions:
                positions[target] = segment_data + position

    # Top-level elements before the first cluster: usually SeekHead, Info and Tracks.
    position = segment_data
    for _ in range(64):
        handle.seek(position)
        chunk = handle.read(16)
        if len(chunk) < 2:
            break
        element_id, size, start = _element_header(chunk, 0)
        if element_id == _CLUSTER or size < 0:
            break
        if element_id in (_SEEK_HEAD, _INFO, _TRACKS, _CUES):
            positions.setdefault(element_id, position)
            if element_id == _SEEK_HEAD:
                read_seek_head(_read_element(handle, position)[1])
        position += start + size
    for seek_head in pending_seek_heads[:4]:
        element_id, payload = _read_element(handle, seek_head)
        if element_id == _SEEK_HEAD:
            read_seek_head(payload)

    if _CUES not in positions or _TRACKS not in positions:
        raise KeyframeError("no Matroska cues")
    scale = 1_000_000
    if _INFO in positions:
        info = dict(_children(_read_element(handle, positions[_INFO])[1]))
        scale = _uint(info.get(_TIMESTAMP_SCALE, b"")) or scale
    video_track = None
    for child_id, entry in _children(_read_element(handle, positions[_TRACKS])[1]):
        if child_id == _TRACK_ENTRY:
            fields = dict(_children(entry))
            if _uint(fields.get(_TRACK_TYPE, b"")) == 1:
                video_track = _uint(fields.get(_TRACK_NUMBER, b""))
                break
    if video_track is None:
        raise KeyframeError("no video track")
    cues_id, cues = _read_element(handle, positions[_CUES])
    if cues_id != _CUES:
        raise KeyframeError("cue position does not point at cues")

    times: set[int] = set()
    for child_id, point in _children(cues):
        if child_id != _CUE_POINT:
            continue
        cue_time = None
        tracks: list[int] = []
        for field_id, value in _children(point):
            if field_id == _CUE_TIME:
                cue_time = _uint(value)
            elif field_id == _CUE_TRACK_POSITIONS:
                track = dict(_children(value)).get(_CUE_TRACK)
                if track is not None:
                    tracks.append(_uint(track))
        if cue_time is not None and video_track in tracks:
            times.add(cue_time)
    return tuple(value * scale / 1_000_000_000 for value in sorted(times))


# MP4 -----------------------------------------------------------------------------------------


def _boxes(data: bytes, offset: int = 0, end: int | None = None) -> list[tuple[bytes, bytes]]:
    end = len(data) if end is None else end
    boxes: list[tuple[bytes, bytes]] = []
    while offset + 8 <= end:
        size, kind = struct.unpack_from(">I4s", data, offset)
        header = 8
        if size == 1:
            size = struct.unpack_from(">Q", data, offset + 8)[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise KeyframeError("malformed MP4 box")
        boxes.append((kind, data[offset + header : offset + size]))
        offset += size
    return boxes


def _child(data: bytes, kind: bytes) -> bytes | None:
    return next((payload for name, payload in _boxes(data) if name == kind), None)


def _find_moov(handle: BinaryIO) -> bytes:
    handle.seek(0, 2)
    file_size = handle.tell()
    position = 0
    while position + 8 <= file_size:
        handle.seek(position)
        header = handle.read(16)
        size, kind = struct.unpack_from(">I4s", header, 0)
        header_size = 8
        if size == 1:
            size = struct.unpack_from(">Q", header, 8)[0]
            header_size = 16
        elif size == 0:
            size = file_size - position
        if size < header_size:
            raise KeyframeError("malformed MP4 box")
        if kind == b"moov":
            if size > _MAX_INDEX_BYTES:
                raise KeyframeError("MP4 index is too large")
            handle.seek(position + header_size)
            return handle.read(size - header_size)
        position += size
    raise KeyframeError("no moov box")


def _mp4_keyframes(handle: BinaryIO) -> tuple[float, ...]:
    moov = _find_moov(handle)
    mvhd = _child(moov, b"mvhd")
    movie_scale = 1000
    if mvhd:
        movie_scale = struct.unpack_from(">I", mvhd, 20 if mvhd[0] == 1 else 12)[0] or 1000
    best: tuple[float, ...] = ()
    best_samples = -1
    for kind, trak in _boxes(moov):
        if kind != b"trak":
            continue
        mdia = _child(trak, b"mdia")
        if mdia is None:
            continue
        hdlr = _child(mdia, b"hdlr")
        if hdlr is None or hdlr[8:12] != b"vide":
            continue
        result = _track_keyframes(trak, mdia, movie_scale)
        if result is not None and result[1] > best_samples:
            best, best_samples = result
    if best_samples < 0:
        raise KeyframeError("no MP4 video track")
    return best


def _track_keyframes(
    trak: bytes, mdia: bytes, movie_scale: int
) -> tuple[tuple[float, ...], int] | None:
    mdhd = _child(mdia, b"mdhd")
    minf = _child(mdia, b"minf")
    stbl = _child(minf, b"stbl") if minf else None
    if mdhd is None or stbl is None:
        return None
    timescale = struct.unpack_from(">I", mdhd, 20 if mdhd[0] == 1 else 12)[0]
    stts = _child(stbl, b"stts")
    if not timescale or stts is None:
        return None

    decode_times: list[int] = []
    current = 0
    (entries,) = struct.unpack_from(">I", stts, 4)
    for entry in range(entries):
        count, delta = struct.unpack_from(">II", stts, 8 + entry * 8)
        for _ in range(count):
            decode_times.append(current)
            current += delta
    sample_count = len(decode_times)

    offsets = [0] * sample_count
    ctts = _child(stbl, b"ctts")
    if ctts is not None:
        (entries,) = struct.unpack_from(">I", ctts, 4)
        sample = 0
        for entry in range(entries):
            count, offset = struct.unpack_from(">Ii", ctts, 8 + entry * 8)
            for _ in range(count):
                if sample < sample_count:
                    offsets[sample] = offset
                sample += 1

    stss = _child(stbl, b"stss")
    if stss is None:
        sync = range(sample_count)
    else:
        (entries,) = struct.unpack_from(">I", stss, 4)
        sync = [struct.unpack_from(">I", stss, 8 + entry * 4)[0] - 1 for entry in range(entries)]

    # ffmpeg applies the first edit: an empty edit delays the track, a media edit trims it.
    shift = 0.0
    edts = _child(trak, b"edts")
    elst = _child(edts, b"elst") if edts else None
    if elst is not None:
        version = elst[0]
        (entries,) = struct.unpack_from(">I", elst, 4)
        offset = 8
        for _ in range(entries):
            if version == 1:
                duration, media_time = struct.unpack_from(">Qq", elst, offset)
                offset += 20
            else:
                duration, media_time = struct.unpack_from(">Ii", elst, offset)
                offset += 12
            if media_time == -1:
                shift += duration / movie_scale
                continue
            shift -= media_time / timescale
            break

    times = sorted(
        (decode_times[index] + offsets[index]) / timescale + shift
        for index in sync
        if 0 <= index < sample_count
    )
    if times and times[0] < 0:
        times = [max(value, 0.0) for value in times]
    return tuple(dict.fromkeys(round(value, 6) for value in times)), sample_count


def nearest_keyframe(keyframes: tuple[float, ...], time: float) -> float:
    """Return the last keyframe at or before ``time``."""

    index = bisect_left(keyframes, time + 1e-6)
    return keyframes[max(index - 1, 0)] if keyframes else 0.0

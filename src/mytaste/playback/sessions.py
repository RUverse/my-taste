"""HLS sessions: one ffmpeg process per viewer, cut into keyframe-aligned segments.

The playlist is known before ffmpeg starts: remuxed segments start on the source keyframes,
transcoded ones every six seconds with forced keyframes. ffmpeg writes fragmented MP4 to a
pipe with source timestamps (``-copyts``), and each fragment is filed under the segment its
first video sample belongs to. Seeking far ahead or back restarts ffmpeg at that segment; the
timestamps make the new fragments line up exactly with the old ones.

ffmpeg only runs as far ahead of the viewer as needed: when it is far enough ahead the pipe is
no longer read, so ffmpeg blocks without using CPU until the viewer catches up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import secrets
import shutil
import signal
import time
from bisect import bisect_right
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from mytaste.playback.decision import Decision
from mytaste.playback.fmp4 import BoxError, TrackTiming, fragment_start, read_box, video_timing
from mytaste.playback.models import MediaInfo

logger = logging.getLogger(__name__)

TIMESTAMP_OFFSET = 10.0
SEGMENT_SECONDS = 6.0
# Shorter transcoded segments start and seek faster; their keyframes are forced anyway.
TRANSCODE_SEGMENT_SECONDS = 4.0
BOUNDARY_TOLERANCE = 0.15
SEGMENT_TIMEOUT = 75.0
_HEVC_HWACCEL_CODECS = frozenset({"hevc"})


class SessionError(RuntimeError):
    """Raised when a session cannot produce a requested segment."""


@dataclass(frozen=True, slots=True)
class SegmentPlan:
    starts: tuple[float, ...]
    duration: float

    @classmethod
    def from_keyframes(
        cls, keyframes: Sequence[float], duration: float, target: float = SEGMENT_SECONDS
    ) -> SegmentPlan:
        starts = [0.0]
        for keyframe in keyframes:
            if keyframe - starts[-1] >= target and duration - keyframe >= 1.0:
                starts.append(keyframe)
        return cls(tuple(starts), duration)

    @classmethod
    def fixed(cls, duration: float, target: float = TRANSCODE_SEGMENT_SECONDS) -> SegmentPlan:
        count = max(1, math.ceil((duration - 1.0) / target))
        return cls(tuple(index * target for index in range(count)), duration)

    def __len__(self) -> int:
        return len(self.starts)

    def end(self, index: int) -> float:
        return self.starts[index + 1] if index + 1 < len(self.starts) else self.duration

    def index_for(self, seconds: float) -> int:
        return max(0, bisect_right(self.starts, seconds + BOUNDARY_TOLERANCE) - 1)

    def media_playlist(self) -> str:
        durations = [self.end(index) - start for index, start in enumerate(self.starts)]
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{max(1, math.ceil(max(durations)))}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            '#EXT-X-MAP:URI="init.mp4"',
        ]
        for index, duration in enumerate(durations):
            lines.append(f"#EXTINF:{max(duration, 0.001):.6f},")
            lines.append(f"{index}.m4s")
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


def build_command(
    ffmpeg: str,
    path: Path,
    info: MediaInfo,
    decision: Decision,
    start: float,
    *,
    hwaccel: str | None = None,
    burn_subtitle_index: int | None = None,
) -> list[str]:
    """Return the ffmpeg arguments that write fragmented MP4 from ``start`` to stdout."""

    video = info.video
    if video is None:
        raise SessionError("This file has no video track")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if not decision.copy_video and hwaccel and video.codec in _HEVC_HWACCEL_CODECS:
        command += ["-hwaccel", hwaccel]
    if start > 0:
        command += ["-ss", f"{start:.3f}"]
    command += ["-copyts", "-i", str(path)]

    if decision.copy_video:
        command += ["-map", f"0:{video.index}", "-c:v", "copy"]
        if video.codec == "hevc":
            command += ["-tag:v", "hvc1"]
    else:
        height = decision.target_height or 720
        filters = []
        if video.height > height:
            filters.append(f"scale=-2:{height}:flags=bicubic")
        if video.hdr:
            filters.append(
                "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv"
            )
        filters.append("format=yuv420p")
        chain = ",".join(filters)
        if burn_subtitle_index is not None:
            command += [
                "-filter_complex",
                f"[0:{burn_subtitle_index}][0:{video.index}]scale2ref[sub][base];"
                f"[base][sub]overlay=eof_action=pass,{chain}[video]",
                "-map",
                "[video]",
            ]
        else:
            command += ["-map", f"0:{video.index}", "-vf", chain]
        rate = {1080: "8M", 720: "4M"}.get(height, "1800k")
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-maxrate",
            rate,
            "-bufsize",
            rate.replace("M", "6M") if rate.endswith("M") else "3600k",
            "-profile:v",
            "high",
            "-level:v",
            "4.1" if height > 720 else "3.1",
            "-g",
            "999999",
            "-sc_threshold",
            "0",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{TRANSCODE_SEGMENT_SECONDS:g})",
        ]

    audio = decision.audio
    if audio is not None:
        command += ["-map", f"0:{audio.index}"]
        if decision.copy_audio:
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-ac", "2", "-b:a", "192k"]
    command += [
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-avoid_negative_ts",
        "disabled",
        "-output_ts_offset",
        f"{TIMESTAMP_OFFSET:g}",
        "-max_muxing_queue_size",
        "4096",
        "-f",
        "mp4",
        "-movflags",
        "frag_keyframe+empty_moov+default_base_moof+frag_discont",
        "-use_editlist",
        "0",
        "pipe:1",
    ]
    return command


class Session:
    """One viewer's HLS stream of one file."""

    def __init__(
        self,
        *,
        session_id: str,
        owner: str,
        file_id: int,
        command: CommandFactory,
        info: MediaInfo,
        decision: Decision,
        plan: SegmentPlan,
        workdir: Path,
        start_index: int = 0,
    ) -> None:
        self.id = session_id
        self.owner = owner
        self.file_id = file_id
        self.info = info
        self.decision = decision
        self.plan = plan
        self.workdir = workdir
        self.start_index = start_index
        self.last_active = time.monotonic()
        self._command = command
        self._init: bytes | None = None
        self._ready: dict[int, Path] = {}
        self._generation = 0
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._producer_start = start_index
        self._current: int | None = None
        self._produced = plan.starts[start_index] if plan.starts else 0.0
        self._demand = plan.end(start_index) if plan.starts else 0.0
        self._complete = False
        self._error: str | None = None
        self._request_serial = 0
        self._changed = asyncio.Event()
        self._demand_changed = asyncio.Event()
        self._closed = False
        transcoding = not decision.copy_video
        self.lookahead = 45.0 if transcoding else 150.0
        self.restart_gap = 15.0 if transcoding else 90.0

    @property
    def transcoding(self) -> bool:
        return not self.decision.copy_video

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def start(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self._restart(self.start_index)

    async def init_segment(self) -> bytes:
        self.touch()
        deadline = time.monotonic() + SEGMENT_TIMEOUT
        while self._init is None:
            if self._error:
                raise SessionError(self._error)
            if self._task is None or self._task.done():
                self._restart(self._producer_start)
            await self._wait(deadline)
        return self._init

    async def segment(self, index: int) -> Path:
        if not 0 <= index < len(self.plan):
            raise KeyError(index)
        self.touch()
        # ffmpeg may run ``lookahead`` seconds past the end of the latest requested segment.
        self._demand = self.plan.end(index)
        self._demand_changed.set()
        self._request_serial += 1
        serial = self._request_serial
        deadline = time.monotonic() + SEGMENT_TIMEOUT
        while True:
            path = self._ready.get(index)
            if path is not None:
                return path
            if self._error and self._producer_start <= index:
                raise SessionError(self._error)
            if not self._covers(index):
                if serial != self._request_serial:
                    # A newer request (the viewer seeked) owns ffmpeg now; the player has
                    # abandoned this one, so do not drag ffmpeg back to it.
                    raise SessionError("Superseded by a newer request")
                self._restart(index)
            await self._wait(deadline)

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await asyncio.to_thread(shutil.rmtree, self.workdir, True)
        self._changed.set()

    def _covers(self, index: int) -> bool:
        if self._task is None or self._task.done() or self._complete:
            return False
        current = self._current if self._current is not None else self._producer_start
        if index < max(current, self._producer_start):
            return False
        return self.plan.starts[index] - max(self._produced, self.plan.starts[current]) <= (
            self.restart_gap
        )

    def _restart(self, index: int) -> None:
        if self._closed:
            raise SessionError("This playback session has ended")
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
        self._producer_start = index
        self._current = None
        self._produced = self.plan.starts[index]
        self._complete = False
        self._error = None
        self._task = asyncio.create_task(self._produce(index, self._generation))

    async def _wait(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SessionError("Timed out preparing the video")
        event = self._changed
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=min(remaining, 1.0))

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def _throttle(self, generation: int) -> None:
        while generation == self._generation and self._produced - self._demand > self.lookahead:
            self._demand_changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._demand_changed.wait(), timeout=1.0)

    async def _produce(self, start_index: int, generation: int) -> None:
        start = self.plan.starts[start_index]
        process = await asyncio.create_subprocess_exec(
            *self._command(start),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._process = process
        with contextlib.suppress(OSError, AttributeError):
            os.setpriority(os.PRIO_PROCESS, process.pid, 10)
        errors: deque[str] = deque(maxlen=6)
        stderr_task = asyncio.create_task(_collect(process.stderr, errors))
        assert process.stdout is not None
        timing: TrackTiming | None = None
        file_type = b""
        moof: bytes | None = None
        buffer = bytearray()
        complete_from = start_index
        try:
            while generation == self._generation:
                await self._throttle(generation)
                if generation != self._generation:
                    break
                box = await read_box(process.stdout)
                if box is None:
                    break
                kind, data = box
                if kind == b"ftyp":
                    file_type = data
                elif kind == b"moov":
                    timing = video_timing(data)
                    if self._init is None:
                        self._init = file_type + data
                        self._notify()
                elif kind == b"moof":
                    moof = data
                elif kind == b"mdat" and moof is not None and timing is not None:
                    seconds = fragment_start(moof, timing)
                    if seconds is None:
                        raise BoxError("fragment without a video timestamp")
                    seconds -= TIMESTAMP_OFFSET
                    index = self.plan.index_for(seconds)
                    if self._current is None:
                        self._current = index
                        at_start = seconds <= self.plan.starts[index] + BOUNDARY_TOLERANCE
                        complete_from = index if at_start or index == 0 else index + 1
                    elif index > self._current:
                        await self._finish(self._current, buffer, complete_from)
                        buffer = bytearray()
                        self._current = index
                    if index >= self._current:
                        buffer += moof
                        buffer += data
                    moof = None
                    self._produced = max(self._produced, seconds)
                    self._notify()
            if generation != self._generation:
                return
            code = await process.wait()
            await stderr_task
            if generation != self._generation:
                return
            if code != 0:
                self._error = _describe_failure(errors)
                logger.warning(
                    "ffmpeg exited with %s for file %s: %s", code, self.file_id, self._error
                )
            elif self._current is not None:
                await self._finish(self._current, buffer, complete_from)
                self._complete = True
            self._notify()
        except asyncio.CancelledError:
            raise
        except (BoxError, OSError, asyncio.IncompleteReadError) as exc:
            if generation == self._generation:
                self._error = _describe_failure(errors) or f"Unreadable stream: {exc}"
                logger.warning("Playback stream failed for file %s: %s", self.file_id, exc)
                self._notify()
        finally:
            await _terminate(process, stderr_task)

    async def _finish(self, index: int, data: bytearray, complete_from: int) -> None:
        if index < complete_from or index in self._ready or not data:
            return
        path = self.workdir / f"{index}.m4s"
        partial = path.with_suffix(".part")
        payload = bytes(data)

        def write() -> None:
            partial.write_bytes(payload)
            partial.replace(path)

        await asyncio.to_thread(write)
        self._ready[index] = path
        self._prune()
        self._notify()

    def _prune(self) -> None:
        horizon = self._demand - 180.0
        for index in [key for key in self._ready if self.plan.end(key) < horizon]:
            path = self._ready.pop(index)
            with contextlib.suppress(OSError):
                path.unlink()


class CommandFactory:
    """Build the ffmpeg command for a session at a given start time."""

    def __init__(
        self,
        ffmpeg: str,
        path: Path,
        info: MediaInfo,
        decision: Decision,
        *,
        hwaccel: str | None,
        burn_subtitle_index: int | None,
    ) -> None:
        self.ffmpeg = ffmpeg
        self.path = path
        self.info = info
        self.decision = decision
        self.hwaccel = hwaccel
        self.burn_subtitle_index = burn_subtitle_index

    def __call__(self, start: float) -> list[str]:
        return build_command(
            self.ffmpeg,
            self.path,
            self.info,
            self.decision,
            start,
            hwaccel=self.hwaccel,
            burn_subtitle_index=self.burn_subtitle_index,
        )


class SessionManager:
    """Own every running session, enforce limits, and stop sessions nobody is watching."""

    def __init__(
        self,
        workdir: Path,
        *,
        max_transcodes: int = 1,
        max_sessions: int = 4,
        idle_timeout: float = 120.0,
    ) -> None:
        self.workdir = workdir
        self.max_transcodes = max_transcodes
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self.sessions: dict[str, Session] = {}
        self._reaper: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(_remove_stale_folders, self.workdir)
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap())

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for session in list(self.sessions.values()):
            await self.stop(session.id)
        await asyncio.to_thread(shutil.rmtree, self.workdir, True)

    def get(self, session_id: str) -> Session | None:
        session = self.sessions.get(session_id)
        if session is not None:
            session.touch()
        return session

    async def create(
        self,
        *,
        owner: str,
        file_id: int,
        command: CommandFactory,
        info: MediaInfo,
        decision: Decision,
        plan: SegmentPlan,
        start_index: int,
    ) -> Session:
        for session in [item for item in self.sessions.values() if item.owner == owner]:
            await self.stop(session.id)
        transcoding = not decision.copy_video
        while True:
            running = list(self.sessions.values())
            busy = [item for item in running if item.transcoding] if transcoding else running
            limit = self.max_transcodes if transcoding else self.max_sessions
            if len(busy) < max(limit, 1):
                break
            await self.stop(min(busy, key=lambda item: item.last_active).id)
        session_id = secrets.token_urlsafe(12)
        session = Session(
            session_id=session_id,
            owner=owner,
            file_id=file_id,
            command=command,
            info=info,
            decision=decision,
            plan=plan,
            workdir=self.workdir / session_id,
            start_index=start_index,
        )
        self.sessions[session_id] = session
        session.start()
        return session

    async def stop(self, session_id: str) -> bool:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(10)
            now = time.monotonic()
            for session in list(self.sessions.values()):
                if now - session.last_active > self.idle_timeout:
                    logger.info("Stopping idle playback session for file %s", session.file_id)
                    await self.stop(session.id)


def _remove_stale_folders(workdir: Path) -> None:
    """Delete this process's folder and those left behind by servers that are gone."""

    shutil.rmtree(workdir, ignore_errors=True)
    parent = workdir.parent
    if not parent.is_dir():
        return
    for folder in parent.iterdir():
        if not folder.name.isdigit():
            continue
        try:
            os.kill(int(folder.name), 0)
        except ProcessLookupError:
            shutil.rmtree(folder, ignore_errors=True)
        except (PermissionError, OverflowError, ValueError):
            continue


async def _terminate(process: asyncio.subprocess.Process, stderr_task: asyncio.Task[None]) -> None:
    """Kill ffmpeg and drain its pipes.

    A throttled session has stopped reading stdout, and asyncio only reports the exit once
    every pipe reaches end-of-file, so the remaining output must be read and discarded.
    """

    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)

    async def drain() -> None:
        if process.stdout is not None:
            while await process.stdout.read(1 << 16):
                pass
        with contextlib.suppress(asyncio.CancelledError):
            await stderr_task
        await process.wait()

    try:
        await asyncio.wait_for(asyncio.shield(asyncio.ensure_future(drain())), timeout=10)
    except (TimeoutError, OSError, ValueError):
        stderr_task.cancel()
        logger.warning("ffmpeg (pid %s) did not exit cleanly", process.pid)


async def _collect(stream: asyncio.StreamReader | None, lines: deque[str]) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            lines.append(text)


def _describe_failure(lines: deque[str]) -> str:
    return lines[-1][:300] if lines else "ffmpeg stopped unexpectedly"

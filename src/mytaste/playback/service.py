"""Local playback: watch targets, media probing, streaming sessions, subtitles, progress."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import signal
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from mytaste.catalog.filters import LocalFacets, WatchStatus
from mytaste.library.models import LibraryFile
from mytaste.playback.decision import (
    Capabilities,
    Decision,
    PlaybackOptions,
    codec_string,
    decide,
)
from mytaste.playback.models import MediaInfo, VideoStream, channel_label, language_tag
from mytaste.playback.probe import ProbeError, probe_file
from mytaste.playback.sessions import CommandFactory, SegmentPlan, Session, SessionManager
from mytaste.playback.subtitles import (
    normalize_webvtt,
    read_subtitle_file,
    repair_mojibake,
    to_webvtt,
)
from mytaste.storage.library import LibraryRepository
from mytaste.storage.playback import FileRow, PlaybackRepository, PlayState, utc_now

logger = logging.getLogger(__name__)

UNAVAILABLE = "This video is no longer available"
_WATCHED_FRACTION = 0.9
_PROBE_START_DELAY = 20.0
_SUBTITLE_TIMEOUT = 900


class PlaybackUnavailableError(LookupError):
    """Raised when a file is missing, moved, or outside the library folders."""


@dataclass(frozen=True, slots=True)
class WatchTarget:
    """Something playable: a movie, an episode, or an unmatched file, with its versions."""

    key: str
    url: str
    media_type: str
    title: str
    files: tuple[LibraryFile, ...]
    tmdb_id: int | None = None
    season: int | None = None
    episode: int | None = None
    year: int | None = None
    poster_path: str | None = None

    @property
    def episode_label(self) -> str:
        if self.season is None or self.episode is None:
            return ""
        if self.season == 0:
            return f"Special {self.episode}"
        return f"S{self.season} · E{self.episode}"


@dataclass(frozen=True, slots=True)
class PlaybackStart:
    decision: Decision
    url: str
    session: Session | None = None


@dataclass(frozen=True, slots=True)
class ContinueEntry:
    target: WatchTarget
    state: PlayState | None
    up_next: bool = False


def state_key(file: LibraryFile) -> str:
    if file.tmdb_id is not None:
        if file.media_type == "movie":
            return f"movie:{file.tmdb_id}"
        if file.season is not None and file.episode is not None:
            return f"tv:{file.tmdb_id}:{file.season}:{file.episode}"
    return f"file:{file.id}"


def watch_url(file: LibraryFile) -> str:
    key = state_key(file)
    if key.startswith("movie:"):
        return f"/watch/movie/{file.tmdb_id}"
    if key.startswith("tv:"):
        return f"/watch/tv/{file.tmdb_id}/{file.season}/{file.episode}"
    return f"/watch/local/{file.id}"


class PlaybackService:
    def __init__(
        self,
        library: LibraryRepository,
        repository: PlaybackRepository,
        *,
        cache_dir: Path,
        roots: Iterable[Path] = (),
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        max_transcodes: int = 1,
        hwaccel: str = "auto",
        background_probe: bool = True,
    ) -> None:
        self.library = library
        self.repository = repository
        self.cache_dir = cache_dir
        self.roots = tuple(Path(root) for root in roots)
        self.ffmpeg = shutil.which(ffmpeg)
        self.ffprobe = shutil.which(ffprobe)
        self.max_transcodes = max_transcodes
        self.hwaccel = _hwaccel(hwaccel)
        self.background_probe = background_probe
        self.sessions = SessionManager(
            cache_dir / "streams" / str(os.getpid()), max_transcodes=max_transcodes
        )
        self._probe_locks: dict[int, asyncio.Lock] = {}
        self._file_facets: dict[tuple[int, str], LocalFacets] = {}
        self._probe_wanted = asyncio.Event()
        self._prober: asyncio.Task[None] | None = None
        self._extractions: dict[str, asyncio.Task[Path]] = {}

    @property
    def available(self) -> bool:
        return self.ffprobe is not None

    @property
    def can_stream(self) -> bool:
        return self.ffmpeg is not None

    @property
    def can_transcode(self) -> bool:
        return self.ffmpeg is not None and self.max_transcodes > 0

    # Lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        await self.sessions.start()
        if self.background_probe and self.available and self._prober is None:
            self._prober = asyncio.create_task(self._probe_loop())
            self._probe_wanted.set()

    async def stop(self) -> None:
        if self._prober is not None:
            self._prober.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._prober
            self._prober = None
        for task in self._extractions.values():
            task.cancel()
        await self.sessions.close()

    def library_changed(self, _library_id: int | None = None) -> None:
        """Called after a scan so new or changed files get probed in the background."""

        self._probe_wanted.set()

    async def _probe_loop(self) -> None:
        await asyncio.sleep(_PROBE_START_DELAY)
        while True:
            await self._probe_wanted.wait()
            self._probe_wanted.clear()
            await asyncio.to_thread(self.repository.forget_missing_files)
            tried: set[int] = set()
            while ids := await asyncio.to_thread(self.repository.files_needing_probe, 20):
                fresh = [file_id for file_id in ids if file_id not in tried]
                if not fresh:
                    break
                for file_id in fresh:
                    tried.add(file_id)
                    file = self.library.get_file(file_id)
                    if file is None:
                        continue
                    try:
                        await self.media_info(file)
                    except PlaybackUnavailableError:
                        # Remember it so the pass moves on; playing it later tries again.
                        self.repository.save_probe(
                            file.id, file.size, file.modified_at, None, UNAVAILABLE
                        )
                    except ProbeError:
                        pass
                    await asyncio.sleep(0.1)

    # Targets ---------------------------------------------------------------------------

    def file(self, file_id: int) -> LibraryFile | None:
        return self.library.get_file(file_id)

    def movie(self, tmdb_id: int) -> WatchTarget | None:
        files = _best_first(self.library.movie_files(tmdb_id))
        if not files:
            return None
        return _target(files)

    def episode(self, tmdb_id: int, season: int, episode: int) -> WatchTarget | None:
        files = tuple(
            file
            for file in self.library.show_files(tmdb_id)
            if file.season == season and file.episode == episode
        )
        return _target(_best_first(files)) if files else None

    def local(self, file_id: int) -> WatchTarget | None:
        file = self.library.get_file(file_id)
        return _target((file,)) if file is not None else None

    def episodes(self, target: WatchTarget) -> tuple[WatchTarget, ...]:
        """All episodes of the target's series in viewing order (specials last)."""

        if target.media_type != "tv":
            return ()
        if target.key.startswith("tv:") and target.tmdb_id is not None:
            return _episodes(self.library.show_files(target.tmdb_id))
        return _episodes(self.library.files_for_item(target.files[0].item_id))

    def next_up(self, tmdb_id: int) -> WatchTarget | None:
        """The episode to play next: one in progress, else the one after the last watched."""

        episodes = tuple(
            episode
            for episode in _episodes(self.library.show_files(tmdb_id))
            if episode.key.startswith("tv:")
        )
        states = self.repository.states(episode.key for episode in episodes)
        progress = [states[episode.key] for episode in episodes if episode.key in states]
        if not episodes:
            return None
        in_progress = [state for state in progress if state.resumable and not state.watched]
        if in_progress:
            latest = max(in_progress, key=lambda state: state.updated_at)
            return next(episode for episode in episodes if episode.key == latest.key)
        regular = [episode for episode in episodes if episode.season != 0] or list(episodes)
        watched = [state for state in progress if state.watched]
        start = 0
        if watched:
            last = max(watched, key=lambda state: state.updated_at)
            keys = [episode.key for episode in regular]
            if last.key in keys:
                start = keys.index(last.key) + 1
        for episode in regular[start:] + regular[:start]:
            state = states.get(episode.key)
            if state is None or not state.watched:
                return episode
        return regular[0]

    # Media information -----------------------------------------------------------------

    async def resolve_path(self, file: LibraryFile) -> Path:
        library = self.library.get_library(file.library_id)
        if library is None:
            raise PlaybackUnavailableError(UNAVAILABLE)
        folders = tuple(Path(folder.path) for folder in library.folders)
        return await asyncio.to_thread(_validated_path, Path(file.path), folders, self.roots)

    async def media_info(self, file: LibraryFile) -> MediaInfo:
        record = self.repository.probe_record(file.id)
        if record and (record.size, record.modified_at) == (file.size, file.modified_at):
            if record.info is not None:
                return record.info
            if record.error != UNAVAILABLE:
                raise ProbeError(record.error or "This file could not be read")
        if self.ffprobe is None:
            raise ProbeError("ffprobe is not installed, so this file cannot be inspected")
        lock = self._probe_locks.setdefault(file.id, asyncio.Lock())
        async with lock:
            record = self.repository.probe_record(file.id)
            if (
                record
                and record.info
                and (record.size, record.modified_at)
                == (
                    file.size,
                    file.modified_at,
                )
            ):
                return record.info
            path = await self.resolve_path(file)
            try:
                info = await asyncio.to_thread(
                    probe_file,
                    self.ffprobe,
                    path,
                    season=file.season,
                    episode=file.episode,
                )
            except ProbeError as exc:
                self.repository.save_probe(file.id, file.size, file.modified_at, None, str(exc))
                raise
            self.repository.save_probe(file.id, file.size, file.modified_at, info)
        self._probe_locks.pop(file.id, None)
        return info

    def probe_summary(self) -> dict[str, int]:
        return self.repository.probe_summary()

    # Streaming -------------------------------------------------------------------------

    async def start_playback(
        self,
        file: LibraryFile,
        capabilities: Capabilities,
        options: PlaybackOptions,
        *,
        owner: str,
        start: float = 0.0,
    ) -> PlaybackStart:
        path = await self.resolve_path(file)
        info = await self.media_info(file)
        if not self.can_stream:
            capabilities = replace(capabilities, hls=False)
        decision = decide(info, capabilities, options, can_transcode=self.can_transcode)
        if info.subtitles and self.can_stream:
            self._extract_subtitles(file, path, info)
        if decision.mode == "direct":
            return PlaybackStart(decision, f"/api/playback/files/{file.id}/stream")
        assert self.ffmpeg is not None
        plan = segment_plan(info, decision)
        command = CommandFactory(
            self.ffmpeg,
            path,
            info,
            decision,
            hwaccel=self.hwaccel,
            burn_subtitle_index=options.burn_subtitle_index,
        )
        session = await self.sessions.create(
            owner=owner,
            file_id=file.id,
            command=command,
            info=info,
            decision=decision,
            plan=plan,
            start_index=plan.index_for(max(start, 0.0)),
        )
        return PlaybackStart(decision, f"/api/playback/sessions/{session.id}/master.m3u8", session)

    def master_playlist(self, session: Session) -> str:
        info = session.info
        decision = session.decision
        video = info.video
        if decision.copy_video:
            bandwidth = max(int(info.bit_rate * 1.3), 1_000_000)
            width, height = (video.width, video.height) if video else (0, 0)
        else:
            height = decision.target_height or 720
            width = round((video.width * height / video.height) / 2) * 2 if video else height
            bandwidth = {1080: 8_000_000, 720: 4_000_000}.get(height, 1_800_000)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-INDEPENDENT-SEGMENTS",
            f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},CODECS="{codec_string(info, decision)}"'
            + (f",RESOLUTION={width}x{height}" if width and height else ""),
            "main.m3u8",
        ]
        return "\n".join(lines) + "\n"

    # Subtitles -------------------------------------------------------------------------

    async def subtitle(self, file: LibraryFile, track: str) -> str:
        """Return a subtitle track as WebVTT. ``track`` is ``x<n>`` (file) or ``s<index>``."""

        info = await self.media_info(file)
        if track.startswith("x") and track[1:].isdigit():
            index = int(track[1:])
            if index >= len(info.external_subtitles):
                raise KeyError(track)
            subtitle = info.external_subtitles[index]
            video_path = await self.resolve_path(file)
            try:
                subtitle_path = Path(subtitle.path).resolve(strict=True)
                if video_path.parent not in subtitle_path.parents:
                    raise KeyError(track)
                text = await asyncio.to_thread(read_subtitle_file, subtitle_path)
            except (OSError, ValueError) as exc:
                raise PlaybackUnavailableError("This subtitle file is no longer available") from exc
            return to_webvtt(text, subtitle.format)
        if track.startswith("s") and track[1:].isdigit():
            index = int(track[1:])
            stream = next((item for item in info.subtitles if item.index == index), None)
            if stream is None or not stream.text:
                raise KeyError(track)
            path = await self.resolve_path(file)
            folder = await self._extract_subtitles(file, path, info)
            text = await asyncio.to_thread((folder / f"{index}.vtt").read_text, "utf-8", "replace")
            return normalize_webvtt(repair_mojibake(text))
        raise KeyError(track)

    def _extract_subtitles(
        self, file: LibraryFile, path: Path, info: MediaInfo
    ) -> asyncio.Task[Path]:
        """Extract every embedded text subtitle in one read of the file, once."""

        digest = hashlib.sha256(f"{file.path}|{file.size}|{file.modified_at}".encode()).hexdigest()
        folder = self.cache_dir / "subtitles" / digest[:24]
        task = self._extractions.get(digest)
        if task is None or (task.done() and (task.cancelled() or task.exception())):
            task = asyncio.create_task(self._run_extraction(path, info, folder))
            task.add_done_callback(_log_extraction)
            self._extractions[digest] = task
        return task

    async def _run_extraction(self, path: Path, info: MediaInfo, folder: Path) -> Path:
        streams = [stream for stream in info.subtitles if stream.text]
        if all((folder / f"{stream.index}.vtt").exists() for stream in streams):
            return folder
        if self.ffmpeg is None:
            raise ProbeError("ffmpeg is not installed")
        folder.mkdir(parents=True, exist_ok=True)
        command = [self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
        ionice = shutil.which("ionice")
        if ionice:
            # Reading the whole file must not slow down the video being watched.
            command = [ionice, "-c", "3", *command]
        command += ["-i", str(path)]
        for stream in streams:
            command += [
                "-map",
                f"0:{stream.index}",
                "-c:s",
                "webvtt",
                "-f",
                "webvtt",
                str(folder / f"{stream.index}.vtt.part"),
            ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, process.pid, 15)
        try:
            _, error = await asyncio.wait_for(process.communicate(), timeout=_SUBTITLE_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            raise
        if process.returncode != 0:
            message = error.decode("utf-8", errors="replace").strip().splitlines()
            raise ProbeError(message[-1] if message else "Subtitles could not be extracted")
        for stream in streams:
            partial = folder / f"{stream.index}.vtt.part"
            if partial.exists():
                partial.replace(folder / f"{stream.index}.vtt")
        return folder

    # Watch state -----------------------------------------------------------------------

    def state(self, key: str) -> PlayState | None:
        return self.repository.state(key)

    def states(self, keys: Iterable[str]) -> dict[str, PlayState]:
        return self.repository.states(keys)

    def record_progress(
        self, file: LibraryFile, position: float, duration: float, *, ended: bool = False
    ) -> PlayState:
        key = state_key(file)
        previous = self.repository.state(key)
        duration = duration if duration > 0 else (previous.duration if previous else 0.0)
        position = max(0.0, min(position, duration or position))
        finished = ended or (duration > 0 and position / duration >= _WATCHED_FRACTION)
        if finished:
            counted = previous is not None and previous.watched and previous.position == 0
            state = PlayState(
                key=key,
                file_id=file.id,
                position=0.0,
                duration=duration,
                watched=True,
                play_count=(previous.play_count if previous else 0) + (0 if counted else 1),
                updated_at=utc_now(),
            )
        else:
            state = PlayState(
                key=key,
                file_id=file.id,
                position=position,
                duration=duration,
                watched=previous.watched if previous else False,
                play_count=previous.play_count if previous else 0,
                updated_at=utc_now(),
            )
        self.repository.save_state(state)
        return state

    def set_watched(self, target: WatchTarget, watched: bool) -> PlayState:
        previous = self.repository.state(target.key)
        state = PlayState(
            key=target.key,
            file_id=target.files[0].id if target.files else None,
            position=0.0,
            duration=previous.duration if previous else 0.0,
            watched=watched,
            play_count=(previous.play_count if previous else 0)
            + (1 if watched and not (previous and previous.watched) else 0),
            updated_at=utc_now(),
        )
        self.repository.save_state(state)
        return state

    def local_facets(self) -> dict[int, LocalFacets]:
        """What each library title's files contain and how far it was watched, by item id.

        A series is watched when every local episode is, and in progress when some are.
        """

        states = {state.key: state for state in self.repository.all_states()}
        parsed: dict[tuple[int, str], LocalFacets] = {}
        files: dict[int, list[LocalFacets]] = {}
        progress: dict[int, list[PlayState | None]] = {}
        for row in self.repository.library_files():
            cache_key = (row.file_id, row.probed_at)
            facets = self._file_facets.get(cache_key) or file_facets(row.data)
            parsed[cache_key] = facets
            files.setdefault(row.item_id, []).append(facets)
            progress.setdefault(row.item_id, []).append(states.get(_row_state_key(row)))
        # Only files that are still indexed stay cached.
        self._file_facets = parsed
        result: dict[int, LocalFacets] = {}
        for item_id, found in files.items():
            item_states = progress[item_id]
            if all(state is not None and state.watched for state in item_states):
                status: WatchStatus = "watched"
            elif any(
                state is not None and (state.watched or state.resumable) for state in item_states
            ):
                status = "in_progress"
            else:
                status = "unwatched"
            combined = found[0]
            for facets in found[1:]:
                combined = combined.merge(facets)
            result[item_id] = replace(combined, watch=status)
        return result

    def continue_watching(self, limit: int = 12) -> tuple[ContinueEntry, ...]:
        entries: list[ContinueEntry] = []
        seen_shows: set[int] = set()
        for state in self.repository.recent_states(80):
            if len(entries) >= limit:
                break
            if state.key.startswith("tv:"):
                tmdb_id = int(state.key.split(":")[1])
                if tmdb_id in seen_shows:
                    continue
                seen_shows.add(tmdb_id)
                target = self.next_up(tmdb_id)
                if target is None:
                    continue
                current = self.repository.state(target.key)
                if current is not None and current.resumable and not current.watched:
                    entries.append(ContinueEntry(target, current))
                elif current is None or not current.watched:
                    entries.append(ContinueEntry(target, None, up_next=True))
                continue
            if not state.resumable or state.watched:
                continue
            target = self._target_for_key(state)
            if target is not None:
                entries.append(ContinueEntry(target, state))
        return tuple(entries)

    def _target_for_key(self, state: PlayState) -> WatchTarget | None:
        kind, _, rest = state.key.partition(":")
        if kind == "movie":
            return self.movie(int(rest))
        if kind == "file" and rest.isdigit():
            return self.local(int(rest))
        return None


def file_facets(data: str | None) -> LocalFacets:
    """Read what a filter can ask of one file from its stored probe JSON."""

    if not data:
        return LocalFacets()
    try:
        payload = json.loads(data)
    except ValueError:
        return LocalFacets()
    if not isinstance(payload, dict):
        return LocalFacets()
    video = payload.get("video") if isinstance(payload.get("video"), dict) else None
    audio = [item for item in payload.get("audio") or () if isinstance(item, dict)]
    subtitles = [
        item
        for key in ("subtitles", "external_subtitles")
        for item in payload.get(key) or ()
        if isinstance(item, dict)
    ]
    resolutions: frozenset[str] = frozenset()
    dynamic_ranges: frozenset[str] = frozenset()
    video_codecs: frozenset[str] = frozenset()
    if video is not None:
        stream = VideoStream(
            index=0,
            codec=str(video.get("codec") or ""),
            width=int(video.get("width") or 0),
            height=int(video.get("height") or 0),
        )
        resolutions = frozenset({stream.resolution_label})
        dynamic_ranges = frozenset({"hdr" if video.get("hdr") else "sdr"})
        video_codecs = frozenset({stream.codec}) if stream.codec else frozenset()
    return LocalFacets(
        resolutions=resolutions,
        video_codecs=video_codecs,
        dynamic_ranges=dynamic_ranges,
        audio_codecs=frozenset(str(item["codec"]) for item in audio if item.get("codec")),
        audio_channels=frozenset(
            channel_label(int(item.get("channels") or 0)) for item in audio if item.get("channels")
        ),
        audio_languages=frozenset(
            tag for item in audio if (tag := language_tag(str(item.get("language") or "")))
        ),
        subtitle_languages=frozenset(
            tag for item in subtitles if (tag := language_tag(str(item.get("language") or "")))
        ),
    )


def _row_state_key(row: FileRow) -> str:
    """The watch-progress key of a file, as ``state_key`` gives it for a ``LibraryFile``."""

    if row.tmdb_id is not None:
        if row.media_type == "movie":
            return f"movie:{row.tmdb_id}"
        if row.season is not None and row.episode is not None:
            return f"tv:{row.tmdb_id}:{row.season}:{row.episode}"
    return f"file:{row.file_id}"


def segment_plan(info: MediaInfo, decision: Decision) -> SegmentPlan:
    """Remuxes cut on the source keyframes, conversions on a fixed grid of forced ones.

    ffmpeg runs with ``-start_at_zero``, so times are measured from the file's start.
    """

    if decision.copy_video:
        keyframes = [time - info.start_time for time in info.keyframes]
        return SegmentPlan.from_keyframes(keyframes, info.duration)
    return SegmentPlan.fixed(_picture_duration(info))


def _picture_duration(info: MediaInfo) -> float:
    """Converted streams end with the picture; trailing audio joins the last segment."""

    video = info.video
    if video is not None and 1.0 < video.duration < info.duration - 1.0:
        return video.duration
    return info.duration


def _episodes(files: Sequence[LibraryFile]) -> tuple[WatchTarget, ...]:
    grouped: dict[str, list[LibraryFile]] = {}
    for file in files:
        grouped.setdefault(state_key(file), []).append(file)
    return tuple(_target(_best_first(group)) for group in grouped.values())


def _target(files: Sequence[LibraryFile]) -> WatchTarget:
    first = files[0]
    return WatchTarget(
        key=state_key(first),
        url=watch_url(first),
        media_type=first.media_type,
        title=first.title,
        files=tuple(files),
        tmdb_id=first.tmdb_id,
        season=first.season,
        episode=first.episode,
        year=first.year,
        poster_path=first.poster_path,
    )


def _best_first(files: Sequence[LibraryFile]) -> tuple[LibraryFile, ...]:
    return tuple(sorted(files, key=lambda file: (-file.size, file.path)))


def _validated_path(path: Path, folders: Sequence[Path], roots: Sequence[Path]) -> Path:
    try:
        resolved = path.resolve(strict=True)
        allowed = [folder.resolve() for folder in folders]
        if not any(resolved == folder or folder in resolved.parents for folder in allowed):
            raise PlaybackUnavailableError(UNAVAILABLE)
        if roots and not any(
            resolved == root or root.resolve() in resolved.parents for root in roots
        ):
            raise PlaybackUnavailableError(UNAVAILABLE)
        if not resolved.is_file() or not os.access(resolved, os.R_OK):
            raise PlaybackUnavailableError(UNAVAILABLE)
    except (OSError, RuntimeError) as exc:
        raise PlaybackUnavailableError(UNAVAILABLE) from exc
    return resolved


def _log_extraction(task: asyncio.Task[Path]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.warning("Subtitle extraction failed: %s", task.exception())


def _hwaccel(setting: str) -> str | None:
    if setting == "none":
        return None
    if setting == "drm":
        return "drm"
    try:
        names = [
            (entry / "name").read_text().strip()
            for entry in Path("/sys/class/video4linux").iterdir()
        ]
    except OSError:
        return None
    return "drm" if any("hevc-dec" in name for name in names) else None

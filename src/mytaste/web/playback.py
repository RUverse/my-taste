"""Routes for the local player: watch pages, stream sessions, subtitles, and progress."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from mytaste.catalog.tmdb import TMDBError
from mytaste.library.models import LibraryFile
from mytaste.playback.decision import (
    TRANSCODE_HEIGHTS,
    Capabilities,
    PlaybackOptions,
    UnplayableError,
)
from mytaste.playback.models import MediaInfo, language_tag
from mytaste.playback.probe import ProbeError
from mytaste.playback.service import (
    PlaybackService,
    PlaybackUnavailableError,
    WatchTarget,
    watch_url,
)
from mytaste.playback.sessions import SessionError

_OWNER = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_TARGET = re.compile(r"^/watch/(?:(movie)/(\d+)|(tv)/(\d+)/(\d+)/(\d+)|(local)/(\d+))$")
_MEDIA_TYPES = {"mov": "video/mp4", "mp4": "video/mp4", "matroska": "video/x-matroska"}
_PLAYLIST = "application/vnd.apple.mpegurl"
_NO_STORE = {"Cache-Control": "no-store"}
_TMDB_TIMEOUT = 4.0


def create_playback_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def service(request: Request) -> PlaybackService:
        return request.app.state.playback

    # Pages -------------------------------------------------------------------------------

    @router.get("/watch/movie/{tmdb_id}", response_class=HTMLResponse)
    async def watch_movie(tmdb_id: int, request: Request) -> Response:
        return await render_watch(request, service(request).movie(tmdb_id))

    @router.get("/watch/tv/{tmdb_id}", response_class=HTMLResponse)
    async def watch_series(tmdb_id: int, request: Request) -> Response:
        target = service(request).next_up(tmdb_id)
        if target is None:
            return await render_watch(request, None)
        return RedirectResponse(target.url, status_code=303)

    @router.get("/watch/tv/{tmdb_id}/{season}/{episode}", response_class=HTMLResponse)
    async def watch_episode(tmdb_id: int, season: int, episode: int, request: Request) -> Response:
        return await render_watch(request, service(request).episode(tmdb_id, season, episode))

    @router.get("/watch/local/{file_id}", response_class=HTMLResponse)
    async def watch_local(file_id: int, request: Request) -> Response:
        target = service(request).local(file_id)
        if target is not None and target.url != f"/watch/local/{file_id}":
            # Matched titles have a stable link by TMDB id; keep the chosen version.
            query = request.url.query
            location = (
                f"{target.url}?{query}&file={file_id}"
                if query
                else (f"{target.url}?file={file_id}")
            )
            return RedirectResponse(location, status_code=307)
        return await render_watch(request, target)

    async def render_watch(request: Request, target: WatchTarget | None) -> HTMLResponse:
        playback = service(request)
        display = request.app.state.preferences.get_display()
        if target is None:
            return templates.TemplateResponse(
                request=request,
                name="watch.html",
                context={
                    "request": request,
                    "display": display,
                    "config": None,
                    "page_title": "Not found",
                    "error": "This video is not in your library.",
                },
                status_code=404,
            )
        chosen = _chosen_file(target, request.query_params.get("file"))
        error: str | None = None
        info: MediaInfo | None = None
        try:
            await playback.resolve_path(chosen)
            info = await playback.media_info(chosen)
        except PlaybackUnavailableError:
            error = "This video is no longer available. The drive may be disconnected."
        except ProbeError:
            error = "This file could not be read, so it cannot be played."
        extras = await _tmdb_extras(request, target)
        episodes = playback.episodes(target)
        keys = [episode.key for episode in episodes]
        position = keys.index(target.key) if target.key in keys else -1
        previous = episodes[position - 1] if position > 0 else None
        following = episodes[position + 1] if 0 <= position < len(episodes) - 1 else None
        state = playback.state(target.key)
        start = _float(request.query_params.get("t"))
        names = extras.get("episode_names", {})
        subtitle_line = " · ".join(
            part for part in (target.episode_label, extras.get("episode_name", "")) if part
        )
        config: dict[str, Any] | None = None
        if info is not None:
            config = {
                "file_id": chosen.id,
                "key": target.key,
                "url": target.url,
                "title": target.title,
                "subtitle": subtitle_line,
                "duration": info.duration,
                "resume": state.position if state and state.resumable else 0,
                "watched": bool(state and state.watched),
                "start": start,
                "autoplay": request.query_params.get("autoplay") == "1",
                "media": _media_summary(info),
                "format": _format_key(info),
                "versions": [
                    {
                        "id": file.id,
                        "label": _version_label(playback, file, chosen, info),
                        "current": file.id == chosen.id,
                        "url": f"{target.url}?file={file.id}",
                    }
                    for file in target.files
                ]
                if len(target.files) > 1
                else [],
                "audio": [
                    {
                        "index": stream.index,
                        "label": stream.label,
                        "language": language_tag(stream.language) or stream.language,
                        "default": stream == info.default_audio,
                    }
                    for stream in info.audio
                ],
                "subtitles": _subtitle_options(chosen, info),
                "chapters": [
                    {"start": chapter.start, "title": chapter.title} for chapter in info.chapters
                ],
                "qualities": [
                    height
                    for height in TRANSCODE_HEIGHTS
                    if info.video is not None and height < info.video.height * 0.95
                ],
                "previous": _neighbour(previous, names),
                "next": _neighbour(following, names),
                "backdrop": extras.get("backdrop"),
                "can_transcode": playback.can_transcode,
                "can_stream": playback.can_stream,
            }
        title = target.title + (f" · {target.episode_label}" if target.episode_label else "")
        return templates.TemplateResponse(
            request=request,
            name="watch.html",
            context={
                "request": request,
                "display": display,
                "config": config,
                "page_title": title,
                "title": target.title,
                "subtitle_line": subtitle_line,
                "backdrop": extras.get("backdrop"),
                "error": error,
            },
        )

    # Streaming API -----------------------------------------------------------------------

    @router.post("/api/playback/sessions", response_class=JSONResponse)
    async def start_session(request: Request) -> JSONResponse:
        playback = service(request)
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
        file = _file_from(playback, payload.get("file_id"))
        owner = payload.get("player")
        if file is None:
            return JSONResponse({"error": "This video is not in your library"}, status_code=404)
        if not isinstance(owner, str) or not _OWNER.match(owner):
            return JSONResponse({"error": "Missing player id"}, status_code=400)
        excluded = payload.get("exclude") or []
        max_height = payload.get("max_height")
        if max_height not in (None, *TRANSCODE_HEIGHTS):
            return JSONResponse({"error": "Unsupported quality"}, status_code=400)
        try:
            info = await playback.media_info(file)
            burn = _optional_index(payload.get("burn_subtitle"))
            if burn is not None and not any(
                stream.index == burn and not stream.text for stream in info.subtitles
            ):
                return JSONResponse({"error": "Unknown subtitle track"}, status_code=400)
            options = PlaybackOptions(
                audio_index=_optional_index(payload.get("audio")),
                burn_subtitle_index=burn,
                max_height=max_height,
                excluded=frozenset(value for value in excluded if value in {"direct", "remux"})
                if isinstance(excluded, list)
                else frozenset(),
            )
            started = await playback.start_playback(
                file,
                Capabilities.from_payload(payload.get("capabilities")),
                options,
                owner=owner,
                start=_float(payload.get("start")) or 0.0,
            )
        except PlaybackUnavailableError:
            return JSONResponse({"error": "This video is no longer available"}, status_code=404)
        except ProbeError:
            return JSONResponse({"error": "This file could not be read"}, status_code=422)
        except UnplayableError as exc:
            return JSONResponse({"error": str(exc)}, status_code=415)
        decision = started.decision
        return JSONResponse(
            {
                "mode": decision.mode,
                "label": decision.label,
                "reasons": list(decision.reasons),
                "url": started.url,
                "session": started.session.id if started.session else None,
                "duration": info.duration,
                "height": decision.target_height or (info.video.height if info.video else None),
            },
            headers=_NO_STORE,
        )

    @router.get("/api/playback/sessions/{session_id}/master.m3u8")
    async def master_playlist(session_id: str, request: Request) -> Response:
        session = service(request).sessions.get(session_id)
        if session is None:
            return PlainTextResponse("Playback session ended", status_code=404)
        return Response(
            service(request).master_playlist(session), media_type=_PLAYLIST, headers=_NO_STORE
        )

    @router.get("/api/playback/sessions/{session_id}/main.m3u8")
    async def media_playlist(session_id: str, request: Request) -> Response:
        session = service(request).sessions.get(session_id)
        if session is None:
            return PlainTextResponse("Playback session ended", status_code=404)
        return Response(session.plan.media_playlist(), media_type=_PLAYLIST, headers=_NO_STORE)

    @router.get("/api/playback/sessions/{session_id}/init.mp4")
    async def init_segment(session_id: str, request: Request) -> Response:
        session = service(request).sessions.get(session_id)
        if session is None:
            return PlainTextResponse("Playback session ended", status_code=404)
        try:
            data = await session.init_segment()
        except SessionError:
            return PlainTextResponse("The video could not be prepared", status_code=500)
        return Response(data, media_type="video/mp4", headers=_NO_STORE)

    @router.get("/api/playback/sessions/{session_id}/{index}.m4s")
    async def media_segment(session_id: str, index: int, request: Request) -> Response:
        session = service(request).sessions.get(session_id)
        if session is None:
            return PlainTextResponse("Playback session ended", status_code=404)
        try:
            path = await session.segment(index)
            data = await asyncio.to_thread(path.read_bytes)
        except KeyError:
            return PlainTextResponse("No such segment", status_code=404)
        except (SessionError, OSError):
            return PlainTextResponse("The video could not be prepared", status_code=503)
        return Response(data, media_type="video/iso.segment", headers=_NO_STORE)

    @router.api_route("/api/playback/sessions/{session_id}/stop", methods=["POST", "DELETE"])
    async def stop_session(session_id: str, request: Request) -> Response:
        await service(request).sessions.stop(session_id)
        return Response(status_code=204)

    @router.api_route("/api/playback/files/{file_id}/stream", methods=["GET", "HEAD"])
    async def direct_stream(file_id: int, request: Request) -> Response:
        playback = service(request)
        file = playback.file(file_id)
        if file is None:
            return PlainTextResponse("This video is not in your library", status_code=404)
        try:
            path = await playback.resolve_path(file)
            info = await playback.media_info(file)
        except (PlaybackUnavailableError, ProbeError):
            return PlainTextResponse("This video is no longer available", status_code=404)
        return FileResponse(
            path,
            media_type=_MEDIA_TYPES.get(info.container, "application/octet-stream"),
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @router.get("/api/playback/files/{file_id}/subtitles/{track}.vtt")
    async def subtitle_track(file_id: int, track: str, request: Request) -> Response:
        playback = service(request)
        file = playback.file(file_id)
        if file is None:
            return PlainTextResponse("This video is not in your library", status_code=404)
        try:
            text = await playback.subtitle(file, track)
        except (KeyError, PlaybackUnavailableError):
            return PlainTextResponse("No such subtitle track", status_code=404)
        except ProbeError:
            return PlainTextResponse("Subtitles could not be extracted", status_code=500)
        return Response(
            text,
            media_type="text/vtt; charset=utf-8",
            headers={"Cache-Control": "private, max-age=600"},
        )

    @router.post("/api/playback/progress", response_class=JSONResponse)
    async def progress(request: Request) -> JSONResponse:
        playback = service(request)
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse({"error": "Expected a JSON object"}, status_code=400)
        session_id = payload.get("session")
        if isinstance(session_id, str):
            playback.sessions.get(session_id)
        file = _file_from(playback, payload.get("file_id"))
        position = _float(payload.get("position"))
        if file is None or position is None:
            return JSONResponse({"error": "Unknown video"}, status_code=404)
        state = playback.record_progress(
            file,
            position,
            _float(payload.get("duration")) or 0.0,
            ended=payload.get("ended") is True,
        )
        return JSONResponse({"watched": state.watched, "position": state.position})

    @router.post("/api/playback/watched", response_class=JSONResponse)
    async def mark_watched(request: Request) -> JSONResponse:
        playback = service(request)
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or not isinstance(payload.get("watched"), bool):
            return JSONResponse({"error": "Expected watched: true or false"}, status_code=400)
        target = target_from_url(playback, str(payload.get("url") or ""))
        if target is None:
            return JSONResponse({"error": "This video is not in your library"}, status_code=404)
        state = playback.set_watched(target, payload["watched"])
        return JSONResponse({"watched": state.watched})

    return router


def target_from_url(playback: PlaybackService, url: str) -> WatchTarget | None:
    match = _TARGET.match(url.split("?", 1)[0])
    if match is None:
        return None
    if match.group(1):
        return playback.movie(int(match.group(2)))
    if match.group(3):
        return playback.episode(int(match.group(4)), int(match.group(5)), int(match.group(6)))
    return playback.local(int(match.group(8)))


async def _tmdb_extras(request: Request, target: WatchTarget) -> dict[str, Any]:
    """Backdrop and episode names from TMDB; the player works without them."""

    extras: dict[str, Any] = {}
    if target.tmdb_id is None:
        return extras
    catalog = request.app.state.catalog
    try:
        details = await asyncio.wait_for(
            catalog.details(target.media_type, target.tmdb_id), _TMDB_TIMEOUT
        )
        extras["backdrop"] = getattr(details, "backdrop_url", None)
        if target.media_type == "tv" and target.season is not None:
            seasons = await asyncio.wait_for(catalog.seasons(target.tmdb_id), _TMDB_TIMEOUT)
            names: dict[tuple[int, int], dict[str, Any]] = {}
            for season in seasons:
                for episode in season.episodes:
                    names[(season.season_number, episode.episode_number)] = {
                        "name": episode.name,
                        "still": episode.still_url,
                    }
            extras["episode_names"] = names
            current = names.get((target.season, target.episode or 0))
            if current:
                extras["episode_name"] = current["name"]
    except (TMDBError, TimeoutError, AttributeError):
        pass
    return extras


def _chosen_file(target: WatchTarget, raw: str | None) -> LibraryFile:
    if raw and raw.isdigit():
        for file in target.files:
            if file.id == int(raw):
                return file
    return target.files[0]


def _version_label(
    playback: PlaybackService, file: LibraryFile, chosen: LibraryFile, info: MediaInfo
) -> str:
    size = f"{file.size / 1_000_000_000:.1f} GB" if file.size >= 1e8 else ""
    record = playback.repository.probe_record(file.id)
    detail = info if file.id == chosen.id else (record.info if record else None)
    video = detail.video.label if detail and detail.video else ""
    return " · ".join(part for part in (video, size) if part) or file.name


def _format_key(info: MediaInfo) -> str:
    """Identifies files that a browser plays (or fails to play) the same way."""

    video = info.video
    codec = f"{video.codec}{video.bit_depth}" if video else "none"
    audio = info.default_audio.codec if info.default_audio else "none"
    return f"{info.container}:{codec}:{audio}"


def _media_summary(info: MediaInfo) -> str:
    parts = []
    if info.video is not None:
        parts.append(info.video.label)
    if info.default_audio is not None:
        parts.append(info.default_audio.label.split(" · ", 1)[-1])
    return " · ".join(parts)


def _subtitle_options(file: LibraryFile, info: MediaInfo) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    for index, subtitle in enumerate(info.external_subtitles):
        options.append(
            {
                "id": f"x{index}",
                "label": subtitle.label,
                "detail": subtitle.name,
                "language": language_tag(subtitle.language) or subtitle.language,
                "forced": subtitle.forced,
                "kind": "text",
                "url": f"/api/playback/files/{file.id}/subtitles/x{index}.vtt",
            }
        )
    for stream in info.subtitles:
        option: dict[str, Any] = {
            "id": f"s{stream.index}",
            "label": stream.label,
            "detail": "Embedded" if stream.text else "Embedded image · converts the video",
            "language": language_tag(stream.language) or stream.language,
            "forced": stream.forced,
            "default": stream.default,
            "kind": "text" if stream.text else "image",
        }
        if stream.text:
            option["url"] = f"/api/playback/files/{file.id}/subtitles/s{stream.index}.vtt"
        else:
            option["stream"] = stream.index
        options.append(option)
    return options


def _neighbour(
    target: WatchTarget | None, names: dict[tuple[int, int], dict[str, Any]]
) -> dict[str, Any] | None:
    if target is None:
        return None
    extra = names.get((target.season or 0, target.episode or 0), {})
    label = " · ".join(part for part in (target.episode_label, extra.get("name", "")) if part)
    return {
        "url": target.url,
        "label": label or target.files[0].name,
        "still": extra.get("still"),
    }


def _file_from(playback: PlaybackService, value: object) -> LibraryFile | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return playback.file(value)


def _optional_index(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number and 0 <= number < 10**7 else None


__all__ = ["create_playback_router", "target_from_url", "watch_url"]

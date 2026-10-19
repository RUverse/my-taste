from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mytaste.music.grouping import group_albums
from mytaste.music.http import MusicError
from mytaste.music.models import (
    CHART_COLLECTIONS,
    COLLECTION_ICONS,
    COLLECTIONS,
    DECADES,
    DESCENDING_SORTS,
    GENRES,
    GROUPINGS,
    MBID,
    SERVICES,
    SORTS,
    TYPES,
    Album,
    AlbumDetails,
    AlbumPage,
    AlbumQuery,
    mbid,
)
from mytaste.music.service import is_album_link
from mytaste.web.routes import collection_editor_context, collection_payload

_ROWS_PER_PAGE = 12
_COVER_NAME = re.compile(r"^([1-9][0-9]{0,19})-(250|500|1200)\.jpg$")
_DESCRIPTIONS = {
    "popular": "Apple Music's most played albums",
    "most-listened": "What ListenBrainz listeners played most this month",
    "new": "The most played albums and EPs of the last 30 days",
}
_CATALOG_LABELS = {"popular": "Chart order", "most-listened": "Listens this month"}
_DECADE_LABELS = {decade: f"{decade}s" for decade in DECADES if decade != "earlier"} | {
    "earlier": "Before 1960"
}


def _context(request: Request) -> dict[str, Any]:
    return {
        "request": request,
        "display": request.app.state.preferences.get_display(),
        "header_media": "music",
        "search_query": "",
        "current_path": request.url.path,
        "media_links": [
            {"url": "/", "label": "All", "active": False},
            {"url": "/?media=movie", "label": "Movies", "active": False},
            {"url": "/?media=tv", "label": "Series", "active": False},
        ],
        "configured_providers": (),
        "libraries": (),
        "sources_changed": False,
        **collection_editor_context(),
    }


def default_sort(query: AlbumQuery) -> str:
    """Charts and searches keep their own order; everything else starts most played."""

    if query.search or (query.collection in _CATALOG_LABELS and not query.artist):
        return "catalog"
    return "popularity"


def allowed_sorts(query: AlbumQuery) -> tuple[str, ...]:
    others = ("popularity", "release", "title", "artist")
    return ("catalog", *others) if default_sort(query) == "catalog" else others


def browse_url(query: AlbumQuery, group: str = "") -> str:
    path = "/collections/music" + (f"/{query.collection}" if query.collection != "popular" else "")
    params: list[tuple[str, Any]] = []
    for name, value in (
        ("q", query.search),
        ("artist", query.artist),
        ("genre", query.genre),
        ("type", query.type),
        ("decade", query.decade),
        ("sort", query.sort if query.sort != default_sort(query) else ""),
        ("order", query.order),
        ("group", group),
        ("page", query.page if query.page != 1 else ""),
    ):
        if value:
            params.append((name, value))
    return path + (f"?{urlencode(params)}" if params else "")


def _parse(request: Request, key: str) -> tuple[AlbumQuery, str]:
    params = request.query_params
    if key not in COLLECTIONS:
        raise ValueError("Choose a supported collection")
    try:
        page = int(params.get("page", "1"))
    except ValueError:
        raise ValueError("Page must be a number") from None
    query = AlbumQuery(
        collection=key,
        search=params.get("q", "").strip(),
        artist=params.get("artist", "").strip().lower(),
        genre=params.get("genre", ""),
        type=params.get("type", ""),
        decade=params.get("decade", ""),
        order=params.get("order", ""),
        page=page,
    )
    sort = params.get("sort", "")
    # A sort the view can't give (after switching collection) falls back to its own order.
    if sort not in allowed_sorts(query):
        sort = default_sort(query)
    query = replace(query, sort=sort)
    if sort == "catalog":
        query = replace(query, order="")
    group = params.get("group", "")
    return query, group if group in GROUPINGS else ""


def _saved_icons(request: Request) -> dict[str, tuple[str, ...]]:
    icons = getattr(request.app.state.collections, "saved_album_icons", None)
    return icons() if callable(icons) else {}


def compact_count(value: int | None) -> str:
    """1234567 as 1.2M, for listen counts on cards."""

    if not value:
        return ""
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if value >= size:
            number = value / size
            return (
                f"{number:.1f}".rstrip("0").rstrip(".") + suffix
                if number < 10
                else (f"{number:.0f}{suffix}")
            )
    return str(value)


def album_payload(album: Album) -> dict[str, Any]:
    return {
        **album.payload(),
        "artist": album.artist,
        "year": album.year,
        "type_label": album.type_label,
        "cover": album.cover_url(500),
        "cover_small": album.cover_url(250),
        "portable_id": album.portable_id,
        "listens_label": compact_count(album.listens),
        "artist_links": [
            {
                "name": credit.name,
                "join": credit.join,
                "url": f"/collections/music?artist={credit.mbid}",
            }
            if MBID.fullmatch(credit.mbid)
            else {"name": credit.name, "join": credit.join, "url": ""}
            for credit in album.artists
        ],
        "musicbrainz_url": f"https://musicbrainz.org/release-group/{album.mbid}",
    }


def details_payload(details: AlbumDetails) -> dict[str, Any]:
    minutes = round(details.length / 60_000)
    return {
        **album_payload(details.album),
        "release_mbid": details.release_mbid,
        "release_title": details.release_title,
        "label": details.label,
        "track_count": details.track_count,
        "length_label": f"{minutes} min" if minutes else "",
        "release_count": details.release_count,
        "spotify_id": details.spotify_id,
        "links": [
            {"service": service, "label": label, "url": details.links[service]}
            for service, label in SERVICES.items()
            if details.links.get(service)
        ],
        "media": [
            {
                "position": medium.position,
                # One disc needs no heading unless it has a title of its own.
                "label": " · ".join(
                    part
                    for part in (
                        f"{medium.format or 'Disc'} {medium.position}"
                        if len(details.media) > 1
                        else "",
                        medium.title,
                    )
                    if part
                ),
                "tracks": [
                    {
                        "position": track.position,
                        "title": track.title,
                        "duration": track.duration,
                        "artist": track.artist,
                    }
                    for track in medium.tracks
                ],
            }
            for medium in details.media
        ],
    }


def create_music_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    templates.env.filters.setdefault("compact_count", compact_count)

    @router.get("/collections/music", response_class=HTMLResponse)
    @router.get("/collections/music/{key}", response_class=HTMLResponse)
    async def browse(request: Request, key: str = "popular") -> Response:
        music = request.app.state.music
        preferences = request.app.state.preferences.get()
        region = preferences.region or "US"
        fragment = request.headers.get("X-MyTaste-Fragment")
        context = _context(request)
        status = 200
        error = ""
        notice = ""
        page = AlbumPage()
        groups: tuple[Any, ...] = ()
        row_pages = 1
        query = AlbumQuery()
        group = ""
        loading = False
        try:
            query, group = _parse(request, key)
            query.validate()
            if query.search and is_album_link(query.search):
                found = await music.find_link(query.search)
                if found and not fragment:
                    return RedirectResponse(f"/music/albums/{found}", status_code=303)
                if not found:
                    notice = "No album on MusicBrainz links to that page yet."
            loading = not fragment and request.query_params.get("render") != "1"
            if not loading and not notice:
                if group:
                    found_page = await music.shelves(region, replace(query, page=1))
                    every = group_albums(found_page.items, group)
                    row_pages = max(math.ceil(len(every) / _ROWS_PER_PAGE), 1)
                    row_page = min(query.page, row_pages)
                    start = (row_page - 1) * _ROWS_PER_PAGE
                    groups = every[start : start + _ROWS_PER_PAGE]
                    page = replace(found_page, page=row_page, pages=row_pages)
                else:
                    page = await music.browse(region, query)
        except ValueError as exc:
            error, status = str(exc), 422
        except MusicError as exc:
            error, status = str(exc), 503
        saved_icons = _saved_icons(request)
        next_url = (
            browse_url(replace(query, page=page.page + 1), group)
            if not error and page.page < page.pages
            else ""
        )
        card_context = {
            "page": page,
            "groups": groups,
            "group": group,
            "query": query,
            "saved_album_icons": saved_icons,
            "display": context["display"],
            "row_offset": (page.page - 1) * _ROWS_PER_PAGE if group else 0,
        }
        if fragment == "results":
            return templates.TemplateResponse(
                request=request,
                name="_music_more.html",
                context=card_context,
                status_code=status,
                headers={"X-Next-Page": next_url, "Cache-Control": "no-store"},
            )
        heading = (
            f"Search results for “{query.search}”"
            if query.search
            else page.artist_name or "Artist"
            if query.artist
            else COLLECTIONS.get(query.collection, "Music")
        )
        if query.search:
            description = ""
        elif query.artist:
            description = "Albums by popularity on ListenBrainz"
        elif query.collection in GENRES:
            description = f"The most played {GENRES[query.collection][0]} albums on ListenBrainz"
        else:
            description = _DESCRIPTIONS.get(query.collection, "")
            if query.collection == "popular":
                description += f" · {region}"
        collection_links = [
            {
                "kind": "smart",
                "key": name,
                "icon": COLLECTION_ICONS.get(name, ""),
                "label": label,
                "active": name == query.collection and not query.search and not query.artist,
                "url": browse_url(AlbumQuery(collection=name), group),
            }
            for name, label in COLLECTIONS.items()
        ]
        sorts = {name: SORTS[name] for name in allowed_sorts(query)}
        if "catalog" in sorts:
            sorts["catalog"] = (
                "Relevance" if query.search else _CATALOG_LABELS.get(query.collection, "Order")
            )
        natural_order = "desc" if query.sort in DESCENDING_SORTS else "asc"
        order = query.order or natural_order
        clear_url = browse_url(
            replace(
                query, genre="", type="", decade="", sort=default_sort(query), order="", page=1
            ),
            group,
        )
        chips = [
            {"label": label, "url": browse_url(replace(query, **{name: ""}, page=1), group)}
            for name, label in (
                ("type", TYPES.get(query.type, "")),
                ("decade", _DECADE_LABELS.get(query.decade, "")),
                ("genre", GENRES.get(query.genre, ("",))[0]),
                ("artist", page.artist_name or ("Artist" if query.artist else "")),
            )
            if label
        ]
        list_sorts = {
            name: browse_url(
                replace(
                    query,
                    sort=name,
                    order=("asc" if order == "desc" else "desc") if name == query.sort else "",
                    page=1,
                ),
                group,
            )
            for name in allowed_sorts(query)
        }
        context.update(
            {
                **card_context,
                "loading": loading,
                "full_url": browse_url(query, group)
                + ("&" if "?" in browse_url(query, group) else "?")
                + "render=1",
                "load_url": browse_url(query, group),
                "search_query": query.search,
                "region": region,
                "error": error,
                "notice": notice,
                "notices": page.notices,
                "pending": page.pending,
                "heading": heading,
                "description": description,
                "icon": COLLECTION_ICONS.get(query.collection, "list") if not query.search else "",
                "page_path": "/collections/music"
                + (f"/{query.collection}" if query.collection != "popular" else ""),
                "collection_links": collection_links,
                "current_link": next((link for link in collection_links if link["active"]), None),
                "sorts": sorts,
                "sort_label": sorts.get(query.sort, ""),
                "sort_order": order,
                "natural_order": natural_order,
                "default_sort": default_sort(query),
                "can_reverse": query.sort != "catalog",
                "sort_changed": query.sort != default_sort(query) or bool(query.order),
                "sort_reset_url": browse_url(
                    replace(query, sort=default_sort(query), order="", page=1), group
                ),
                "sort_flip_url": browse_url(
                    replace(query, order="asc" if order == "desc" else "desc", page=1), group
                ),
                "list_sorts": list_sorts,
                "types": TYPES,
                "decades": _DECADE_LABELS,
                "genres": {name: label for name, (label, _words) in GENRES.items()},
                "groupings": GROUPINGS,
                "grouping": group,
                "grouping_label": GROUPINGS.get(group, ""),
                "group_urls": {
                    name: browse_url(replace(query, page=1), name) for name in GROUPINGS
                },
                "group_clear_url": browse_url(replace(query, page=1)),
                "active_filters": chips,
                "active_filter_rules": sum(
                    1 for value in (query.type, query.decade, query.genre) if value
                ),
                "active_filter_count": len(chips),
                "clear_url": clear_url,
                "previous_url": browse_url(replace(query, page=page.page - 1), group)
                if page.page > 1 and not error
                else "",
                "next_url": next_url,
                "pagination_label": f"Rows page {page.page} of {page.pages}"
                if group
                else f"Page {page.page} of {page.pages}",
                "is_chart": query.collection in CHART_COLLECTIONS,
            }
        )
        if fragment == "music-page":
            return JSONResponse(
                {
                    "html": templates.env.get_template("_music_results.html").render(context),
                    "pending": page.pending,
                },
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )
        return templates.TemplateResponse(
            request=request,
            name="music.html",
            context=context,
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    # Details ----------------------------------------------------------------------------

    @router.get("/api/items/album/{key}/details")
    async def details(key: str, request: Request) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        try:
            found = await request.app.state.music.details(mbid(key))
        except (ValueError, LookupError):
            return JSONResponse({"error": "Unknown album"}, status_code=404, headers=headers)
        except MusicError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503, headers=headers)
        return JSONResponse(details_payload(found), headers=headers)

    @router.get("/music/albums/{key}", response_class=HTMLResponse)
    async def album_page(key: str, request: Request) -> Response:
        response = await details(key, request)
        payload = json.loads(response.body)
        if response.status_code != 200:
            return templates.TemplateResponse(
                request=request,
                name="album_error.html",
                context={**_context(request), "error": payload["error"]},
                status_code=response.status_code,
                headers={"Cache-Control": "no-store"},
            )
        return templates.TemplateResponse(
            request=request,
            name="album.html",
            context={**_context(request), "album": payload},
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/music/covers/{release}/{name}")
    async def cover(release: str, name: str, request: Request) -> Response:
        found = _COVER_NAME.fullmatch(name)
        covers = getattr(request.app.state.music, "covers", None)
        if not found or not MBID.fullmatch(release) or covers is None:
            return Response(status_code=404)
        path = await covers.release_image(release, int(found.group(1)), int(found.group(2)))
        if path is None:
            return Response(status_code=404, headers={"Cache-Control": "max-age=3600"})
        # The address names one image at one size, so it never changes.
        return FileResponse(
            path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )

    # Saving to collections --------------------------------------------------------------

    @router.get("/api/items/album/{key}/collections", response_class=JSONResponse)
    async def album_collections(key: str, request: Request) -> JSONResponse:
        try:
            key = mbid(key)
        except ValueError:
            return JSONResponse({"error": "Unknown album"}, status_code=404)
        service = request.app.state.collections
        saved = service.album_memberships(key)
        return JSONResponse(
            {
                "collections": [
                    {**collection_payload(item), "saved": item.id in saved}
                    for item in service.collections()
                ]
            }
        )

    @router.put("/api/collections/{collection_id}/items/album/{key}", response_class=JSONResponse)
    async def save_album(collection_id: int, key: str, request: Request) -> JSONResponse:
        try:
            album = await request.app.state.music.album(mbid(key))
            added = await asyncio.to_thread(
                request.app.state.collections.add_album, collection_id, album
            )
        except LookupError as exc:
            message = "Unknown album" if str(exc) == "Unknown album" else str(exc)
            return JSONResponse({"error": message}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "Unknown album"}, status_code=404)
        except MusicError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"saved": True, "added": added})

    @router.delete(
        "/api/collections/{collection_id}/items/album/{key}", response_class=JSONResponse
    )
    async def remove_album(collection_id: int, key: str, request: Request) -> JSONResponse:
        try:
            key = mbid(key)
        except ValueError:
            return JSONResponse({"error": "Unknown album"}, status_code=404)
        removed = request.app.state.collections.remove_album(collection_id, key)
        return JSONResponse({"saved": False, "removed": removed})

    return router

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from typing import Any, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mytaste.catalog.models import (
    BrowseCategory,
    BrowseMediaType,
    BrowseQuery,
    CatalogPage,
    MediaType,
    Provider,
    Region,
)
from mytaste.catalog.service import LocalSource
from mytaste.catalog.tmdb import TMDBError
from mytaste.library.models import Library, LibraryStatus
from mytaste.storage.preferences import DisplayPreferences, Preferences

_MEDIA_LABELS: tuple[tuple[BrowseMediaType, str], ...] = (
    ("all", "All"),
    ("movie", "Movies"),
    ("tv", "TV Shows"),
)
_ADD_STEPS = frozenset({"choose", "streaming", "local"})
_NO_SELECTION = "none"
_SEARCH_CATEGORY = BrowseCategory("popular", "Most Popular")
_DISPLAY_FLAGS = (
    "show_year",
    "show_rating",
    "show_media_type",
    "show_genres",
    "show_people",
    "show_providers",
    "autoplay_trailer",
    "sidebar_open",
)
_MAX_PROVIDER_ITEMS = 60


def create_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def render(
        request: Request,
        template: str,
        context: dict[str, object],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        shared = {
            "request": request,
            "current_path": request.url.path,
            "display": request.app.state.preferences.get_display(),
            "header_providers": (),
            "header_has_library": request.app.state.library.has_libraries,
            "header_media": "all",
            "search_query": "",
        }
        shared.update(context)
        return templates.TemplateResponse(
            request=request,
            name=template,
            context=shared,
            status_code=status_code,
        )

    @router.get("/healthz", response_class=JSONResponse)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> Response:
        preferences: Preferences = request.app.state.preferences.get()
        catalog = request.app.state.catalog
        library = request.app.state.library
        libraries: tuple[Library, ...] = library.libraries()
        if not preferences.configured and not libraries:
            return RedirectResponse("/settings", status_code=303)

        all_provider_ids = preferences.provider_ids if preferences.configured else ()
        all_library_ids = tuple(sorted(item.id for item in libraries))
        query = _parse_browse_query(request, all_provider_ids, all_library_ids)

        def url(value: BrowseQuery) -> str:
            return _browse_url(value, all_provider_ids, all_library_ids)

        async def load_providers() -> tuple[Provider, ...]:
            if not preferences.configured:
                return ()
            try:
                return await catalog.providers(preferences.region)
            except TMDBError:
                return ()

        async def browse_library(
            value: BrowseQuery,
        ) -> tuple[tuple[BrowseCategory, ...], BrowseQuery, CatalogPage]:
            options = await library.categories(value.media_type)
            chosen = _pick_category(options, value.category)
            value = replace(value, category=chosen.slug)
            return options, value, await library.browse(value, category=chosen)

        providers: tuple[Provider, ...] = ()
        categories: tuple[BrowseCategory, ...] = ()
        page = CatalogPage(items=())
        error: str | None = None
        notice: str | None = None
        try:
            if query.provider_ids:
                providers, categories = await asyncio.gather(
                    load_providers(),
                    catalog.categories(query.media_type),
                )
                category = _pick_category(categories, query.category)
                query = replace(query, category=category.slug)
                local = _local_source(library, query, category) if query.library_ids else None
                page = await catalog.browse(preferences.region, query, local=local)
            else:
                providers = await load_providers()
                categories, query, page = await browse_library(query)
        except TMDBError as exc:
            if query.library_ids:
                notice = (
                    "Streaming results are unavailable right now, so only your library is shown."
                )
                categories, query, page = await browse_library(query)
            else:
                error = str(exc)

        configured_ids = set(all_provider_ids)
        configured_providers = tuple(
            provider for provider in providers if provider.id in configured_ids
        )
        default_category = "latest" if query.provider_ids else "recent"
        category_links = tuple(
            {
                "label": category.label,
                "slug": category.slug,
                "active": not query.search and category.slug == query.category,
                "url": url(replace(query, category=category.slug, search="", page=1)),
            }
            for category in categories
        )
        media_links = tuple(
            {
                "label": label,
                "value": value,
                "active": query.media_type == value,
                "url": url(
                    replace(query, media_type=value, category=default_category, search="", page=1)
                ),
            }
            for value, label in _MEDIA_LABELS
        )
        sources_changed = (
            query.provider_ids != all_provider_ids or query.library_ids != all_library_ids
        )
        active_filter_count = sum(
            (
                sources_changed,
                query.year_from is not None or query.year_to is not None,
                query.minimum_rating is not None,
                query.include_unrated,
            )
        )
        current_category = next(
            (category for category in categories if category.slug == query.category),
            BrowseCategory(query.category, "Latest"),
        )
        heading = f"Search results for “{query.search}”" if query.search else current_category.label
        all_sources_url = url(
            replace(query, provider_ids=all_provider_ids, library_ids=all_library_ids, page=1)
        )
        source_options = (
            *(
                {
                    "kind": "provider",
                    "id": provider.id,
                    "name": provider.name,
                    "logo_url": provider.logo_url,
                    "detail": "Streaming",
                    "checked": provider.id in query.provider_ids,
                    "only_url": url(
                        replace(query, provider_ids=(provider.id,), library_ids=(), page=1)
                    ),
                }
                for provider in configured_providers
            ),
            *(
                {
                    "kind": "library",
                    "id": item.id,
                    "name": item.name,
                    "logo_url": None,
                    "detail": f"Local · {item.media_label}",
                    "checked": item.id in query.library_ids,
                    "only_url": url(
                        replace(query, provider_ids=(), library_ids=(item.id,), page=1)
                    ),
                }
                for item in libraries
            ),
        )
        this_year = date.today().year
        year_presets = tuple(
            {
                "label": label,
                "url": url(replace(query, year_from=start, year_to=end, page=1)),
                "active": (query.year_from, query.year_to) == (start, end),
            }
            for label, start, end in (
                ("This year", this_year, None),
                ("Last 5 years", this_year - 4, None),
                ("2010s", 2010, 2019),
                ("2000s", 2000, 2009),
                ("Before 2000", None, 1999),
            )
        )
        active_filters = _active_filters(
            query,
            source_options,
            sources_url=all_sources_url if sources_changed else None,
            url=url,
        )
        statuses: tuple[LibraryStatus, ...] = library.statuses() if libraries else ()
        library_scanning = any(status.state == "scanning" for status in statuses)
        return render(
            request,
            "index.html",
            {
                "preferences": preferences,
                "header_providers": configured_providers,
                "configured_providers": configured_providers,
                "header_media": query.media_type,
                "search_query": query.search,
                "query": query,
                "media_links": media_links,
                "category_links": category_links,
                "page": page,
                "heading": heading,
                "active_filter_count": active_filter_count,
                "clear_filters_url": url(
                    BrowseQuery(
                        media_type=query.media_type,
                        category=query.category,
                        provider_ids=all_provider_ids,
                        library_ids=all_library_ids,
                    )
                ),
                "previous_url": url(replace(query, page=query.page - 1))
                if query.page > 1
                else None,
                "next_url": url(replace(query, page=query.page + 1))
                if query.page < page.total_pages
                else None,
                "error": error,
                "notice": notice,
                "libraries": libraries,
                "library_keys": library.matched_keys() if libraries else frozenset(),
                "library_scanning": library_scanning,
                "streaming_selected": bool(query.provider_ids),
                "source_options": source_options,
                "sources_changed": sources_changed,
                "all_sources_url": all_sources_url,
                "year_presets": year_presets,
                "year_clear_url": url(replace(query, year_from=None, year_to=None, page=1)),
                "rating_options": (None, 5, 6, 7, 8),
                "active_filters": active_filters,
                "current_year": this_year,
            },
        )

    async def settings_context(
        request: Request,
        *,
        add_step: str = "",
        selected_ids: set[int] | None = None,
        page_error: str | None = None,
        streaming_error: str | None = None,
        library_error: str | None = None,
        library_form: dict[str, object] | None = None,
    ) -> dict[str, object]:
        preferences: Preferences = request.app.state.preferences.get()
        catalog = request.app.state.catalog
        library = request.app.state.library
        region = preferences.region
        regions: tuple[Region, ...] = ()
        providers: tuple[Provider, ...] = ()
        error: str | None = None
        try:
            regions = await catalog.regions()
            codes = {item.code for item in regions}
            if region not in codes:
                region = _guess_region(request.headers.get("accept-language", ""), codes)
            providers = await catalog.providers(region)
        except TMDBError as exc:
            error = str(exc)
            region = region or "US"

        enabled_ids = set(preferences.provider_ids) if region == preferences.region else set()
        enabled_providers = tuple(provider for provider in providers if provider.id in enabled_ids)
        library_rows = tuple(
            _library_payload(item, library.status(item.id)) for item in library.libraries()
        )
        return {
            "region": region,
            "region_name": next((item.name for item in regions if item.code == region), region),
            "regions": regions,
            "enabled_providers": enabled_providers,
            "available_providers": tuple(
                provider for provider in providers if provider.id not in enabled_ids
            ),
            "selected_ids": selected_ids or set(),
            "header_providers": enabled_providers,
            "has_sources": bool(preferences.provider_ids or library_rows),
            "library_rows": library_rows,
            "library_form": library_form or _empty_library_form(),
            "library_roots": tuple(str(root) for root in getattr(library, "roots", ())),
            "add_step": add_step if add_step in _ADD_STEPS else "",
            "error": error,
            "page_error": page_error,
            "streaming_error": streaming_error,
            "library_error": library_error,
        }

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        context = await settings_context(request, add_step=request.query_params.get("add", ""))
        return render(request, "settings.html", context)

    @router.post("/settings/services", response_class=HTMLResponse)
    async def add_services(request: Request) -> Response:
        form = await request.form()
        region = str(form.get("region") or "").strip().upper()
        selected_ids: set[int] = set()
        form_error: str | None = None
        try:
            selected_ids = {int(value) for value in form.getlist("provider_ids")}
        except (TypeError, ValueError):
            form_error = "The submitted streaming services were invalid."
        if form_error is None and not selected_ids:
            form_error = "Choose at least one streaming service."

        if form_error is None:
            try:
                valid_ids = await _region_provider_ids(request, region)
                if valid_ids is None:
                    form_error = "Choose a supported country or region."
                elif not selected_ids <= valid_ids:
                    form_error = "One of the selected services is unavailable in this region."
                else:
                    preferences: Preferences = request.app.state.preferences.get()
                    kept = {
                        provider_id
                        for provider_id in preferences.provider_ids
                        if preferences.region == region or provider_id in valid_ids
                    }
                    request.app.state.preferences.save(region, tuple(kept | selected_ids))
                    return RedirectResponse("/settings", status_code=303)
            except (TMDBError, ValueError) as exc:
                form_error = str(exc)

        context = await settings_context(
            request,
            add_step="streaming",
            selected_ids=selected_ids,
            streaming_error=form_error,
        )
        return render(request, "settings.html", context, status_code=422)

    @router.post("/settings/services/{provider_id}/remove")
    async def remove_service(provider_id: int, request: Request) -> Response:
        preferences: Preferences = request.app.state.preferences.get()
        if provider_id in preferences.provider_ids:
            request.app.state.preferences.save(
                preferences.region,
                tuple(value for value in preferences.provider_ids if value != provider_id),
            )
        return RedirectResponse("/settings", status_code=303)

    @router.post("/settings/region", response_class=HTMLResponse)
    async def change_region(request: Request) -> Response:
        form = await request.form()
        region = str(form.get("region") or "").strip().upper()
        try:
            valid_ids = await _region_provider_ids(request, region)
            if valid_ids is not None:
                preferences: Preferences = request.app.state.preferences.get()
                request.app.state.preferences.save(
                    region,
                    tuple(value for value in preferences.provider_ids if value in valid_ids),
                )
                return RedirectResponse("/settings", status_code=303)
            page_error = "Choose a supported country or region."
        except (TMDBError, ValueError) as exc:
            page_error = str(exc)
        context = await settings_context(request, page_error=page_error)
        return render(request, "settings.html", context, status_code=422)

    @router.post("/settings/libraries", response_class=HTMLResponse)
    async def add_library(request: Request) -> Response:
        form = await request.form()
        name = str(form.get("name") or "").strip()
        rows = _folder_rows(form)
        try:
            request.app.state.library.add(name, _folder_pairs(rows))
        except ValueError as exc:
            context = await settings_context(
                request,
                add_step="local",
                library_error=str(exc),
                library_form={"name": name, "folders": rows, "library_id": None},
            )
            return render(request, "settings.html", context, status_code=422)
        return RedirectResponse("/settings", status_code=303)

    @router.post("/settings/libraries/{library_id}/folders", response_class=HTMLResponse)
    async def add_library_folders(library_id: int, request: Request) -> Response:
        form = await request.form()
        rows = _folder_rows(form)
        try:
            request.app.state.library.add_folders(library_id, _folder_pairs(rows))
        except ValueError as exc:
            target = request.app.state.library.library(library_id)
            context = await settings_context(
                request,
                add_step="local",
                library_error=str(exc),
                library_form={
                    "name": target.name if target else "",
                    "folders": rows,
                    "library_id": library_id if target else None,
                },
            )
            return render(request, "settings.html", context, status_code=422)
        return RedirectResponse("/settings", status_code=303)

    @router.post("/settings/libraries/{library_id}/folders/{folder_id}/remove")
    async def remove_library_folder(library_id: int, folder_id: int, request: Request) -> Response:
        try:
            request.app.state.library.remove_folder(library_id, folder_id)
        except ValueError as exc:
            context = await settings_context(request, page_error=str(exc))
            return render(request, "settings.html", context, status_code=422)
        return RedirectResponse("/settings", status_code=303)

    @router.post("/settings/libraries/{library_id}/rescan")
    async def rescan_library(library_id: int, request: Request) -> Response:
        request.app.state.library.schedule_scan(library_id)
        return RedirectResponse("/settings", status_code=303)

    @router.post("/settings/libraries/{library_id}/rename", response_class=HTMLResponse)
    async def rename_library(library_id: int, request: Request) -> Response:
        form = await request.form()
        try:
            request.app.state.library.rename(library_id, str(form.get("name") or ""))
        except ValueError as exc:
            context = await settings_context(request, page_error=str(exc))
            return render(request, "settings.html", context, status_code=422)
        return RedirectResponse("/settings", status_code=303)

    @router.post("/settings/libraries/{library_id}/remove")
    async def remove_library(library_id: int, request: Request) -> Response:
        request.app.state.library.remove(library_id)
        return RedirectResponse("/settings", status_code=303)

    @router.get("/api/libraries/status", response_class=JSONResponse)
    async def library_status(request: Request) -> JSONResponse:
        library = request.app.state.library
        return JSONResponse(
            {
                "libraries": [
                    _library_payload(item, library.status(item.id)) for item in library.libraries()
                ]
            }
        )

    @router.get("/api/libraries/folders", response_class=JSONResponse)
    async def library_folders(request: Request) -> JSONResponse:
        try:
            listing = request.app.state.library.list_folders(request.query_params.get("path"))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "path": listing.path,
                "parent": listing.parent,
                "entries": [{"name": entry.name, "path": entry.path} for entry in listing.entries],
            }
        )

    @router.post("/api/preferences/display", response_class=JSONResponse)
    async def save_display_preferences(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            current: DisplayPreferences = request.app.state.preferences.get_display()
            values = {
                key: _required_bool(payload, key) if key in payload else getattr(current, key)
                for key in _DISPLAY_FLAGS
            }
            display = DisplayPreferences(
                card_size=str(payload.get("card_size") or current.card_size),
                **values,
            )
            request.app.state.preferences.save_display(display)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse({"status": "saved"})

    @router.get("/api/items/providers", response_class=JSONResponse)
    async def item_providers(request: Request) -> JSONResponse:
        """Map ``media:id`` keys to the user's enabled services that carry each title."""

        preferences: Preferences = request.app.state.preferences.get()
        keys = _item_keys(request.query_params.get("items", ""))
        if not preferences.configured or not keys:
            return JSONResponse({"providers": {}})
        catalog = request.app.state.catalog
        try:
            providers: tuple[Provider, ...] = await catalog.providers(preferences.region)
        except TMDBError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        enabled_ids = set(preferences.provider_ids)
        enabled = tuple(provider for provider in providers if provider.id in enabled_ids)
        results = await asyncio.gather(
            *(
                catalog.available_provider_ids(preferences.region, media_type, item_id)
                for media_type, item_id in keys
            ),
            return_exceptions=True,
        )
        payload: dict[str, list[dict[str, object]]] = {}
        for (media_type, item_id), result in zip(keys, results, strict=True):
            if isinstance(result, TMDBError):
                continue
            if isinstance(result, BaseException):
                raise result
            payload[f"{media_type}:{item_id}"] = [
                {"id": provider.id, "name": provider.name, "logo_url": provider.logo_url}
                for provider in enabled
                if provider.id in result
            ]
        return JSONResponse({"providers": payload})

    @router.get("/api/items/{media_type}/{item_id}/people", response_class=JSONResponse)
    async def item_people(media_type: str, item_id: int, request: Request) -> JSONResponse:
        if media_type not in {"movie", "tv"} or item_id <= 0:
            return JSONResponse({"error": "Invalid media item"}, status_code=404)
        try:
            label, names = await request.app.state.catalog.people(
                media_type,
                item_id,
            )
        except TMDBError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"label": label, "names": names})

    @router.get("/api/items/{media_type}/{item_id}/details", response_class=JSONResponse)
    async def item_details(media_type: str, item_id: int, request: Request) -> JSONResponse:
        if media_type not in {"movie", "tv"} or item_id <= 0:
            return JSONResponse({"error": "Invalid media item"}, status_code=404)
        try:
            details = await request.app.state.catalog.details(media_type, item_id)
        except TMDBError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse(
            {
                "id": details.id,
                "media_type": details.media_type,
                "media_label": "Movie" if details.media_type == "movie" else "Series",
                "title": details.title,
                "year": details.year,
                "overview": details.overview,
                "rating": details.rating,
                "runtime_minutes": details.runtime_minutes,
                "poster_url": details.poster_url,
                "backdrop_url": details.backdrop_url,
                "genres": details.genres,
                "cast": [
                    {
                        "name": person.name,
                        "character": person.character,
                        "profile_url": person.profile_url,
                    }
                    for person in details.cast
                ],
                "trailer_key": details.trailer_key,
                "trailer_url": details.trailer_url,
                "tmdb_url": details.detail_url,
            }
        )

    return router


def _empty_library_form() -> dict[str, object]:
    return {"name": "", "folders": [{"path": "", "media_type": "movie"}], "library_id": None}


def _folder_rows(form: Any) -> list[dict[str, str]]:
    """Read the folder rows of the add-library form; each row names its own media type."""

    rows: list[dict[str, str]] = []
    for index, raw in enumerate(form.getlist("path")):
        media_type = str(form.get(f"media_type_{index}") or "movie")
        rows.append({"path": str(raw).strip(), "media_type": media_type})
    return rows or [{"path": "", "media_type": "movie"}]


def _folder_pairs(rows: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(row["path"], row["media_type"]) for row in rows if row["path"]]


def _library_payload(library: Library, status: LibraryStatus) -> dict[str, object]:
    return {
        "id": library.id,
        "name": library.name,
        "folders": [
            {
                "id": folder.id,
                "path": folder.path,
                "media_type": folder.media_type,
                "media_label": folder.media_label,
            }
            for folder in library.folders
        ],
        "media_label": library.media_label,
        "state": status.state,
        "text": _library_status_text(library, status),
        "item_count": library.item_count,
        "file_count": library.file_count,
        "unmatched_count": library.unmatched_count,
        "last_scanned_at": library.last_scanned_at,
    }


def _library_status_text(library: Library, status: LibraryStatus) -> str:
    if status.state == "scanning":
        if status.total_items:
            return (
                f"Scanning… {status.scanned_files} files found, "
                f"{status.matched_items} of {status.total_items} titles matched"
            )
        return status.message or "Scanning…"
    if status.state == "error":
        return status.message or "The last scan failed."
    if library.last_scanned_at is None:
        return "Waiting for the first scan"
    parts: list[str] = []
    if "movie" in library.media_types or library.movie_count:
        parts.append(_count(library.movie_count, "movie"))
    if "tv" in library.media_types or library.show_count:
        parts.append(_count(library.show_count, "show"))
        parts.append(_count(library.episode_count, "episode"))
    if library.unmatched_count:
        parts.append(f"{library.unmatched_count} unmatched")
    parts.append(f"scanned {_relative_time(library.last_scanned_at)}")
    text = " · ".join(parts)
    if library.last_error:
        text = f"{text} · {library.last_error}"
    return text


def _count(value: int, noun: str) -> str:
    return f"{value} {noun}{'' if value == 1 else 's'}"


def _relative_time(timestamp: str) -> str:
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return timestamp
    now = datetime.now(moment.tzinfo)
    seconds = max(int((now - moment).total_seconds()), 0)
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = hours // 24
    return f"{days} day{'' if days == 1 else 's'} ago"


async def _region_provider_ids(request: Request, region: str) -> set[int] | None:
    """Return the provider ids offered in ``region``, or ``None`` for an unknown region."""

    catalog = request.app.state.catalog
    regions: tuple[Region, ...] = await catalog.regions()
    if region not in {item.code for item in regions}:
        return None
    providers: tuple[Provider, ...] = await catalog.providers(region)
    return {provider.id for provider in providers}


def _guess_region(accept_language: str, codes: set[str]) -> str:
    for part in accept_language.split(","):
        tag = part.split(";")[0].strip()
        subtags = tag.replace("_", "-").split("-")
        if len(subtags) > 1 and subtags[-1].upper() in codes:
            return subtags[-1].upper()
    if "US" in codes or not codes:
        return "US"
    return sorted(codes)[0]


def _pick_category(categories: tuple[BrowseCategory, ...], slug: str) -> BrowseCategory:
    return next((category for category in categories if category.slug == slug), categories[0])


def _local_source(library: Any, query: BrowseQuery, category: BrowseCategory) -> LocalSource:
    """Load the first ``limit`` library titles in the order of the streaming category."""

    local_query = replace(query, page=1)
    local_category = _SEARCH_CATEGORY if query.search else category

    async def load(limit: int) -> CatalogPage:
        return await library.browse(local_query, category=local_category, page_size=limit)

    return load


def _active_filters(
    query: BrowseQuery,
    source_options: tuple[dict[str, Any], ...],
    *,
    sources_url: str | None,
    url: Callable[[BrowseQuery], str],
) -> tuple[dict[str, str], ...]:
    """Describe each active filter with a link that removes it."""

    chips: list[dict[str, str]] = []
    if sources_url is not None:
        names = [str(option["name"]) for option in source_options if option["checked"]]
        label = (
            ", ".join(names)
            if len(names) <= 2
            else f"{len(names)} of {len(source_options)} sources"
        )
        chips.append({"label": label, "url": sources_url})
    if query.year_from is not None or query.year_to is not None:
        if query.year_from is not None and query.year_to is not None:
            label = (
                str(query.year_from)
                if query.year_from == query.year_to
                else f"{query.year_from}–{query.year_to}"
            )
        elif query.year_from is not None:
            label = f"{query.year_from} or later"
        else:
            label = f"{query.year_to} or earlier"
        chips.append(
            {"label": label, "url": url(replace(query, year_from=None, year_to=None, page=1))}
        )
    if query.minimum_rating is not None:
        chips.append(
            {
                "label": f"Rated {query.minimum_rating:g}+",
                "url": url(replace(query, minimum_rating=None, page=1)),
            }
        )
    if query.include_unrated:
        chips.append(
            {
                "label": "Including unrated",
                "url": url(replace(query, include_unrated=False, page=1)),
            }
        )
    return tuple(chips)


def _item_keys(raw: str) -> tuple[tuple[MediaType, int], ...]:
    """Parse ``movie:12,tv:34`` into unique (media type, TMDB id) pairs."""

    keys: dict[tuple[MediaType, int], None] = {}
    for token in raw.split(","):
        media_type, _, item_id = token.strip().partition(":")
        if media_type in {"movie", "tv"} and item_id.isdigit() and int(item_id) > 0:
            keys[(cast(MediaType, media_type), int(item_id))] = None
        if len(keys) >= _MAX_PROVIDER_ITEMS:
            break
    return tuple(keys)


def _parse_browse_query(
    request: Request,
    all_provider_ids: tuple[int, ...],
    all_library_ids: tuple[int, ...],
) -> BrowseQuery:
    params = request.query_params
    media_value = params.get("media", "all")
    media_type: BrowseMediaType = media_value if media_value in {"all", "movie", "tv"} else "all"
    provider_ids = _selection(params.getlist("providers"), all_provider_ids)
    library_ids = _selection(params.getlist("libraries"), all_library_ids)
    if not provider_ids and not library_ids:
        provider_ids, library_ids = all_provider_ids, all_library_ids

    current_year = date.today().year
    year_from = _optional_int(params.get("year_from"), 1900, current_year)
    year_to = _optional_int(params.get("year_to"), 1900, current_year)
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from
    minimum_rating = _optional_float(params.get("rating_min"), 0, 10)
    page = _optional_int(params.get("page"), 1, 500) or 1
    default_category = "latest" if provider_ids else "recent"
    return BrowseQuery(
        media_type=media_type,
        category=(params.get("category") or default_category)[:50],
        search=(params.get("q") or "").strip()[:100],
        provider_ids=provider_ids,
        year_from=year_from,
        year_to=year_to,
        minimum_rating=minimum_rating,
        include_unrated=params.get("unrated") == "1",
        page=page,
        library_ids=library_ids,
    )


def _selection(values: list[str], allowed: tuple[int, ...]) -> tuple[int, ...]:
    """Parse a source selection; absent means everything, ``none`` means nothing."""

    if not values:
        return allowed
    chosen = {
        int(token) for group in values for token in group.split(",") if token.strip().isdigit()
    }
    return tuple(value for value in allowed if value in chosen)


def _browse_url(
    query: BrowseQuery,
    all_provider_ids: tuple[int, ...],
    all_library_ids: tuple[int, ...],
) -> str:
    params: dict[str, str | int | float] = {
        "media": query.media_type,
        "category": query.category,
    }
    if query.search:
        params["q"] = query.search
    if query.provider_ids != all_provider_ids:
        params["providers"] = ",".join(str(value) for value in query.provider_ids) or _NO_SELECTION
    if query.library_ids != all_library_ids:
        params["libraries"] = ",".join(str(value) for value in query.library_ids) or _NO_SELECTION
    if query.year_from is not None:
        params["year_from"] = query.year_from
    if query.year_to is not None:
        params["year_to"] = query.year_to
    if query.minimum_rating is not None:
        params["rating_min"] = query.minimum_rating
    if query.include_unrated:
        params["unrated"] = 1
    if query.page > 1:
        params["page"] = query.page
    return f"/?{urlencode(params)}"


def _optional_int(value: str | None, minimum: int, maximum: int) -> int | None:
    if value in {None, ""}:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _optional_float(value: str | None, minimum: float, maximum: float) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _required_bool(payload: dict[object, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value

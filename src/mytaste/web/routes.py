from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any, TypeVar, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mytaste.catalog.filtering import FilterResolver
from mytaste.catalog.filters import PERSON_ROLES, GenreChoice, TitleFilters
from mytaste.catalog.grouping import GROUP_TITLE_LIMIT, GROUPINGS, TitleGroup, group_titles
from mytaste.catalog.mixing import mix_games
from mytaste.catalog.models import (
    SORT_KEYS,
    BrowseCategory,
    BrowseMediaType,
    BrowseQuery,
    CatalogItem,
    CatalogPage,
    MediaType,
    Provider,
    Region,
    SortKey,
    natural_descending,
)
from mytaste.catalog.service import LocalSource
from mytaste.catalog.tmdb import TMDBError
from mytaste.collections.models import (
    HOME_COLLECTION,
    ICONS,
    Collection,
    GameEntry,
    SmartCollection,
    smart_collection,
)
from mytaste.collections.service import GameAccess
from mytaste.games.http import StoreError
from mytaste.games.models import PLANS, PLATFORMS, SOURCES, Game, GameQuery, SteamAccount
from mytaste.library.models import Library, LibraryStatus
from mytaste.storage.games import GamePreferences
from mytaste.storage.preferences import DisplayPreferences, Preferences
from mytaste.web.filter_options import (
    FILTER_GROUPS,
    FilterOptions,
    active_filter_chips,
    filter_params,
    filter_rules,
    parse_filters,
)

T = TypeVar("T")

logger = logging.getLogger(__name__)

_MEDIA_LABELS: tuple[tuple[BrowseMediaType, str], ...] = (
    ("all", "All"),
    ("movie", "Movies"),
    ("tv", "Series"),
)
_SORT_LABELS: dict[SortKey, str] = {
    "popularity": "Popularity",
    "release": "Release date",
    "rating": "Rating",
    "title": "Title",
    "added": "Date added",
}
# Categories from before collections: Recently Added and A–Z became sorts.
_LEGACY_CATEGORIES: dict[str, tuple[str, str | None]] = {
    "recent": (HOME_COLLECTION, "added"),
    "alphabetical": (HOME_COLLECTION, "title"),
}
_ADD_STEPS = frozenset({"choose", "streaming", "local", "steam", "gamepass"})
_NO_SELECTION = "none"
# Short enough for a sidebar row; ``SOURCES`` has the full names.
_GAME_SOURCE_NAMES = {"gamepass": "Game Pass", "steam": "Steam"}
# TMDB's page length, which catalog pages keep; mixed pages use it too.
_CATALOG_PAGE_SIZE = 20
_SEARCH_CATEGORY = BrowseCategory(HOME_COLLECTION, "Popular")
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
_GROUP_ROWS_PER_PAGE = 12


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
        legacy = request.query_params.get("category")
        if legacy and not request.query_params.get("q"):
            # Links from before collections, such as /?category=latest, keep working.
            slug, sort = _LEGACY_CATEGORIES.get(legacy, (legacy, None))
            params = [(key, value) for key, value in request.query_params.multi_items()]
            params = [(key, value) for key, value in params if key != "category"]
            if sort is not None and "sort" not in request.query_params:
                params.append(("sort", sort))
            path = "/" if slug == HOME_COLLECTION else f"/collections/{slug}"
            target = f"{path}?{urlencode(params)}" if params else path
            return RedirectResponse(target, status_code=303)
        return await browse(request, HOME_COLLECTION)

    @router.get("/collections/{key}", response_class=HTMLResponse)
    async def collection_page(key: str, request: Request) -> Response:
        return await browse(request, key)

    async def browse(request: Request, key: str) -> Response:
        preferences: Preferences = request.app.state.preferences.get()
        catalog = request.app.state.catalog
        library = request.app.state.library
        collections = request.app.state.collections
        libraries: tuple[Library, ...] = library.libraries()
        if not preferences.configured and not libraries:
            return RedirectResponse("/settings", status_code=303)

        all_provider_ids = preferences.provider_ids if preferences.configured else ()
        all_library_ids = tuple(sorted(item.id for item in libraries))
        all_game_sources = _game_sources(request)
        query = _parse_browse_query(
            request, all_provider_ids, all_library_ids, key, all_game_sources
        )
        lists = await _filter_lists(catalog, preferences.region or "US")
        query = replace(
            query,
            filters=parse_filters(request.query_params, lists.genres, lists.certification_country),
        )
        # Grouped, the page number pages through rows and the services are asked for the first
        # titles of the collection instead.
        group = query.group
        row_page = query.page if group else 1
        query = replace(query, group="", page=1 if group else query.page)

        def url(value: BrowseQuery, *, grouping: str | None = None) -> str:
            chosen = group if grouping is None else grouping
            return _browse_url(
                replace(value, group=chosen),
                all_provider_ids,
                all_library_ids,
                lists.genres,
                all_game_sources,
            )

        def collection_url(value: BrowseQuery, collection_key: str) -> str:
            """Open another collection, keeping sources and filters but not the sort."""

            return url(
                replace(
                    value, category=collection_key, sort=None, descending=None, search="", page=1
                )
            )

        user_collections: tuple[Collection, ...] = collections.collections()
        manual: Collection | None = None
        smart = None
        if query.search:
            query = replace(query, category=HOME_COLLECTION)
        if query.category.isdigit():
            manual = next((item for item in user_collections if item.key == query.category), None)
            if manual is None:
                return RedirectResponse(collection_url(query, HOME_COLLECTION), status_code=303)
        else:
            smart = smart_collection(query.category)
            if smart is None or not smart.supports(query.media_type):
                return RedirectResponse(collection_url(query, HOME_COLLECTION), status_code=303)

        # Only library titles have an added date, so that sort needs a list or local-only view.
        sort_choices = tuple(
            value
            for value in SORT_KEYS
            if value != "added" or manual is not None or not query.provider_ids
        )
        default_sort: SortKey = (
            manual.default_sort if manual is not None else smart.sort if smart else "popularity"
        )
        if default_sort not in sort_choices:
            default_sort = "popularity"
        if query.sort is not None and query.sort not in sort_choices:
            query = replace(query, sort=None, descending=None)
        sort, descending = query.sort_for(default_sort)
        query = replace(
            query,
            sort=None if sort == default_sort else sort,
            descending=None if descending == natural_descending(sort) else descending,
        )

        async def load_providers() -> tuple[Provider, ...]:
            if not preferences.configured:
                return ()
            try:
                return await catalog.providers(preferences.region)
            except TMDBError:
                return ()

        async def load_categories(media_type: BrowseMediaType) -> tuple[BrowseCategory, ...]:
            if all_provider_ids:
                try:
                    return await catalog.categories(media_type)
                except TMDBError:
                    pass
            return await library.categories(media_type)

        filters = query.filters
        playback = request.app.state.playback
        # What local files contain, for the library filters' choices and for filtering.
        local_facets = playback.local_facets() if libraries else {}
        resolver = FilterResolver(
            filters,
            facts=request.app.state.facts,
            library=library,
            playback=playback,
            library_ids=query.library_ids,
            local=local_facets,
        )
        refine = resolver.allowed if filters.active else None
        item_ids = await resolver.local_item_ids() if query.library_ids else None
        exclude = resolver.streaming_exclude()

        # Games join All's smart collections; filters only TMDB titles have leave them out.
        games: list[GameEntry] = []
        notice: str | None = None
        if (
            manual is None
            and smart is not None
            and query.media_type == "all"
            and query.game_sources
            and not filters.active
        ):
            try:
                games = await _playable_games(request, preferences.region, query, smart)
            except StoreError:
                logger.warning("Games could not be mixed into %s", key, exc_info=True)
                notice = "Games are unavailable right now, so only movies and series are shown."

        async def mixed(
            titles: Callable[[int], Awaitable[CatalogPage]],
            category: BrowseCategory,
            page_size: int,
        ) -> CatalogPage:
            """The collection's titles with the games mixed in; grouped, its first titles."""

            order = ("popularity", True) if query.search else (sort, descending)
            if order[0] == "added":
                # Games have no added date; they are spread through the titles instead.
                order = ("popularity", True)
            found = await mix_games(
                titles,
                games,
                sort=order[0],
                descending=order[1],
                page=1 if group else query.page,
                page_size=GROUP_TITLE_LIMIT if group else page_size,
                limit=None if query.search else category.limit,
            )
            return replace(found, total_pages=1) if group else found

        async def browse_library(
            value: BrowseQuery,
        ) -> tuple[tuple[BrowseCategory, ...], BrowseQuery, CatalogPage]:
            options = await library.categories(value.media_type)
            chosen = _SEARCH_CATEGORY if value.search else _pick_category(options, value.category)
            if not value.search:
                value = replace(value, category=chosen.slug)
            size = GROUP_TITLE_LIMIT if group else 24
            if games:

                async def titles(number: int) -> CatalogPage:
                    if not value.library_ids:
                        return CatalogPage(items=(), page=number)
                    return await library.browse(
                        replace(value, page=number),
                        category=chosen,
                        page_size=size,
                        item_ids=item_ids,
                    )

                return options, value, await mixed(titles, chosen, size)
            found = await library.browse(value, category=chosen, page_size=size, item_ids=item_ids)
            return options, value, found

        providers: tuple[Provider, ...] = ()
        categories: tuple[BrowseCategory, ...] = ()
        page = CatalogPage(items=())
        error: str | None = None
        availability: tuple[int, int] | None = None
        if manual is not None:
            providers, categories, result = await asyncio.gather(
                load_providers(),
                load_categories(query.media_type),
                collections.browse(
                    manual,
                    query,
                    region=preferences.region if all_provider_ids else "",
                    provider_ids=all_provider_ids,
                    library_ids=all_library_ids,
                    page_size=GROUP_TITLE_LIMIT if group else None,
                    refine=refine,
                    game_access=_game_access(request, preferences.region),
                ),
            )
            page = result.page
            availability = (result.available, result.total)
        else:
            try:
                if query.provider_ids:
                    providers, categories = await asyncio.gather(
                        load_providers(),
                        catalog.categories(query.media_type),
                    )
                    category = (
                        _SEARCH_CATEGORY
                        if query.search
                        else _pick_category(categories, query.category)
                    )
                    query = replace(query, category=category.slug)
                    local = (
                        _local_source(library, query, category, item_ids)
                        if query.library_ids
                        else None
                    )
                    narrowing = {"refine": refine, "exclude": exclude} if filters.active else {}
                    if games:
                        browsed = query

                        async def titles(number: int) -> CatalogPage:
                            return await catalog.browse(
                                preferences.region,
                                replace(browsed, page=number),
                                local=local,
                                **narrowing,
                            )

                        page = await mixed(titles, category, _CATALOG_PAGE_SIZE)
                    else:
                        page = await catalog.browse(
                            preferences.region, query, local=local, **narrowing
                        )
                        if group:
                            page = await _first_titles(
                                catalog, preferences.region, query, local, page, narrowing
                            )
                else:
                    providers = await load_providers()
                    categories, query, page = await browse_library(query)
            except TMDBError as exc:
                if query.library_ids:
                    notice = (
                        "Streaming results are unavailable right now, so only your library is "
                        "shown."
                    )
                    categories, query, page = await browse_library(query)
                else:
                    error = str(exc)

        # A search the chosen sources have nothing for still shows what it finds elsewhere.
        other_items: tuple[CatalogItem, ...] = ()
        if (
            query.search
            and query.page == 1
            and not page.items
            and not error
            and preferences.region
            and not filters.local_only
        ):
            try:
                elsewhere = await catalog.search_everywhere(
                    preferences.region,
                    query,
                    refine=refine,
                    exclude=exclude if filters.active else frozenset(),
                )
                other_items = elsewhere.items
            except TMDBError:
                pass

        groups: tuple[TitleGroup, ...] = ()
        row_pages = 1
        if group and page.items:
            people = await _credits(catalog, page.items) if group == "director" else {}
            every_group = group_titles(page.items, group, people)
            row_pages = max(math.ceil(len(every_group) / _GROUP_ROWS_PER_PAGE), 1)
            row_page = min(row_page, row_pages)
            start = (row_page - 1) * _GROUP_ROWS_PER_PAGE
            groups = every_group[start : start + _GROUP_ROWS_PER_PAGE]

        configured_ids = set(all_provider_ids)
        configured_providers = tuple(
            provider for provider in providers if provider.id in configured_ids
        )
        current_category = next(
            (category for category in categories if category.slug == query.category), None
        )
        collection_links = (
            *(
                {
                    "kind": "manual",
                    "key": item.key,
                    "label": item.name,
                    "icon": item.icon,
                    "active": not query.search and manual is not None and manual.id == item.id,
                    "url": collection_url(query, item.key),
                }
                for item in user_collections
            ),
            *(
                {
                    "kind": "smart",
                    "key": category.slug,
                    "label": category.label,
                    "icon": category.icon,
                    "active": not query.search and manual is None and category == current_category,
                    "url": collection_url(query, category.slug),
                }
                for category in categories
            ),
        )
        media_links = tuple(
            {
                "label": label,
                "value": value,
                "active": query.media_type == value,
                "url": url(replace(query, media_type=value, page=1))
                if manual is not None or (smart is not None and smart.supports(value))
                else collection_url(replace(query, media_type=value), HOME_COLLECTION),
            }
            for value, label in _MEDIA_LABELS
        )
        # Game services are choices only where games are mixed in: All's smart collections.
        game_rows = all_game_sources if manual is None and query.media_type == "all" else ()
        sources_changed = (
            query.provider_ids != all_provider_ids
            or query.library_ids != all_library_ids
            or (bool(game_rows) and query.game_sources != all_game_sources)
        )
        if filters.local_only and query.provider_ids and manual is None and error is None:
            notice = (
                "Only titles in your local libraries are shown: filters on files and watch "
                "status do not apply to streaming."
            )
        rules = filter_rules(
            query,
            replace(
                lists,
                names=await _filter_names(catalog, filters),
                local=tuple(local_facets.values()),
                has_libraries=bool(libraries),
                certifications=lists.certifications_for(query.media_type),
            ),
            url,
        )
        active_rules = sum(1 for rule in rules if rule["active"])
        active_filter_count = sum(
            (
                sources_changed,
                query.year_from is not None or query.year_to is not None,
                query.minimum_rating is not None,
                query.include_unrated,
                active_rules,
            )
        )
        if query.search:
            heading, description, icon = f"Search results for “{query.search}”", "", ""
        elif manual is not None:
            heading, description, icon = manual.name, manual.description, manual.icon
        elif current_category is not None:
            heading = current_category.label
            description, icon = current_category.description, current_category.icon
        else:
            heading = smart.name if smart else ""
            description, icon = (smart.description, smart.icon) if smart else ("", "")
        all_sources_url = url(
            replace(
                query,
                provider_ids=all_provider_ids,
                library_ids=all_library_ids,
                game_sources=all_game_sources,
                page=1,
            )
        )
        no_games = () if game_rows else query.game_sources
        # Local libraries come first, then the streaming services.
        source_options = (
            *(
                {
                    "kind": "library",
                    "id": item.id,
                    "name": item.name,
                    "logo_url": None,
                    "detail": f"Local · {item.media_label}",
                    "checked": item.id in query.library_ids,
                    "only_url": url(
                        replace(
                            query,
                            provider_ids=(),
                            library_ids=(item.id,),
                            game_sources=no_games,
                            page=1,
                        )
                    ),
                }
                for item in libraries
            ),
            *(
                {
                    "kind": "provider",
                    "id": provider.id,
                    "name": provider.name,
                    "logo_url": provider.logo_url,
                    "detail": "Streaming",
                    "checked": provider.id in query.provider_ids,
                    "only_url": url(
                        replace(
                            query,
                            provider_ids=(provider.id,),
                            library_ids=(),
                            game_sources=no_games,
                            page=1,
                        )
                    ),
                }
                for provider in configured_providers
            ),
            *(
                {
                    "kind": "game",
                    "id": source,
                    "name": _GAME_SOURCE_NAMES[source],
                    "logo_url": None,
                    "detail": SOURCES[source],
                    "checked": source in query.game_sources,
                    "only_url": url(
                        replace(
                            query,
                            provider_ids=(),
                            library_ids=(),
                            game_sources=(source,),
                            page=1,
                        )
                    ),
                }
                for source in game_rows
            ),
        )
        # The one source left showing needs no "Only" link.
        shown = [option for option in source_options if option["checked"]]
        for option in source_options:
            option["only"] = len(shown) == 1 and option["checked"]
        this_year = date.today().year
        decade = this_year - this_year % 10

        def year_links(
            ranges: tuple[tuple[str, int | None, int | None], ...],
        ) -> tuple[dict[str, object], ...]:
            return tuple(
                {
                    "label": label,
                    "url": url(replace(query, year_from=start, year_to=end, page=1)),
                    "active": (query.year_from, query.year_to) == (start, end),
                }
                for label, start, end in ranges
            )

        year_presets = year_links(
            (("This year", this_year, None), ("Last 5 years", this_year - 4, None))
        )
        decade_presets = year_links(
            (
                *((f"{start}s", start, start + 9) for start in range(decade, 1950, -10)),
                ("Earlier", None, 1959),
            )
        )
        active_filters = (
            *_active_filters(
                query,
                source_options,
                sources_url=all_sources_url if sources_changed else None,
                url=url,
            ),
            *active_filter_chips(rules),
        )
        play_states = playback.states(_card_state_keys(page)) if libraries else {}
        is_home = manual is None and query.category == HOME_COLLECTION
        continue_items = (
            _continue_items(playback, query.media_type)
            if libraries and is_home and query.page == 1 and not query.search
            else ()
        )
        statuses: tuple[LibraryStatus, ...] = library.statuses() if libraries else ()
        library_scanning = any(status.state == "scanning" for status in statuses)
        page_path = url(replace(query, page=1)).partition("?")[0]
        flipped = not descending
        year_active = query.year_from is not None or query.year_to is not None
        rating_active = query.minimum_rating is not None or query.include_unrated
        sort_changed = query.sort is not None or query.descending is not None
        current_link = next((link for link in collection_links if link["active"]), None)
        context: dict[str, object] = {
            "preferences": preferences,
            "header_providers": configured_providers,
            "configured_providers": configured_providers,
            "header_media": query.media_type,
            "search_query": query.search,
            "query": query,
            "page_path": page_path,
            "media_links": media_links,
            "collection_links": collection_links,
            "current_link": current_link,
            "return_to": url(replace(query, page=row_page) if group else query),
            "collection": manual,
            "collection_payload": collection_payload(manual) if manual else None,
            **collection_editor_context(),
            "collection_availability": availability,
            "page": page,
            "other_items": other_items,
            "heading": heading,
            "description": description,
            "icon": icon,
            "sort_options": tuple(
                {"value": value, "label": _SORT_LABELS[value], "selected": value == sort}
                for value in sort_choices
            ),
            "default_sort": default_sort,
            "sort_descending": descending,
            "sort_natural_descending": natural_descending(sort),
            "sort_label": _SORT_LABELS[sort],
            "sort_changed": sort_changed,
            "sort_reset_url": url(replace(query, sort=None, descending=None, page=1)),
            "sort_flip_url": url(
                replace(
                    query,
                    descending=None if flipped == natural_descending(sort) else flipped,
                    page=1,
                )
            ),
            "year_active": year_active,
            "rating_active": rating_active,
            "rating_clear_url": url(
                replace(query, minimum_rating=None, include_unrated=False, page=1)
            ),
            "active_filter_count": active_filter_count,
            "clear_filters_url": url(
                BrowseQuery(
                    media_type=query.media_type,
                    category=query.category,
                    search=query.search,
                    provider_ids=all_provider_ids,
                    library_ids=all_library_ids,
                    sort=query.sort,
                    descending=query.descending,
                )
            ),
            "previous_url": (url(replace(query, page=row_page - 1)) if row_page > 1 else None)
            if group
            else url(replace(query, page=query.page - 1))
            if query.page > 1
            else None,
            "next_url": (url(replace(query, page=row_page + 1)) if row_page < row_pages else None)
            if group
            else url(replace(query, page=query.page + 1))
            if query.page < page.total_pages and not other_items
            else None,
            "pagination_label": f"Rows page {row_page} of {row_pages}"
            if group
            else f"Page {page.page} of {page.total_pages}",
            "groups": groups,
            "row_offset": (row_page - 1) * _GROUP_ROWS_PER_PAGE if group else 0,
            "grouping": group,
            "grouping_label": GROUPINGS.get(group, ""),
            "grouping_options": tuple(
                {
                    "value": value,
                    "label": label,
                    "selected": value == group,
                    "url": url(replace(query, page=1), grouping=value),
                }
                for value, label in GROUPINGS.items()
            ),
            "group_clear_url": url(replace(query, page=1), grouping=""),
            "grouped_count": len(page.items) if group else 0,
            "grouped_more": group and page.total_results > len(page.items),
            "error": error,
            "notice": notice,
            "libraries": libraries,
            "library_keys": library.matched_keys() if libraries else frozenset(),
            "saved_icons": collections.saved_icons(),
            "saved_game_icons": collections.saved_game_icons()
            if hasattr(collections, "saved_game_icons")
            else {},
            "has_games": bool(games)
            or any(
                getattr(item, "media_type", "") == "game"
                for item in (*page.items, *(item for row in groups for item in row.items))
            ),
            "game_rows": game_rows,
            "library_scanning": library_scanning,
            "streaming_selected": bool(query.provider_ids),
            "source_options": source_options,
            "sources_changed": sources_changed,
            "all_sources_url": all_sources_url,
            "year_presets": year_presets,
            "decade_presets": decade_presets,
            "filter_rules": rules,
            "filter_groups": tuple(
                {"key": value, "label": label, "rules": [r for r in rules if r["group"] == value]}
                for value, label in FILTER_GROUPS
            ),
            "active_filter_rules": active_rules
            + (1 if year_active else 0)
            + (1 if rating_active else 0),
            "year_clear_url": url(replace(query, year_from=None, year_to=None, page=1)),
            "rating_options": (None, 5, 6, 7, 8),
            "active_filters": active_filters,
            "current_year": this_year,
            "play_states": play_states,
            "continue_items": continue_items,
        }
        if request.headers.get("x-mytaste-fragment") == "results":
            # Infinite scroll asks for just the next batch and where the one after it is.
            response = render(request, "_results_more.html", context)
            response.headers["X-Next-Page"] = str(context["next_url"] or "")
            response.headers["Cache-Control"] = "no-store"
            return response
        return render(request, "index.html", context)

    async def settings_context(
        request: Request,
        *,
        add_step: str = "",
        selected_ids: set[int] | None = None,
        page_error: str | None = None,
        streaming_error: str | None = None,
        library_error: str | None = None,
        library_form: dict[str, object] | None = None,
        game_pass_form: GamePreferences | None = None,
        return_to: str | None = None,
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
        games = request.app.state.games
        game_preferences = request.app.state.game_preferences
        steam_account = _steam_account(games)
        game_pass = game_preferences.get() if game_preferences.configured() else None
        return {
            "game_pass_form": game_pass_form or game_preferences.get(),
            "steam_available": bool(getattr(games, "steam_available", False)),
            "steam_key": bool(getattr(games, "owned_games_available", False)),
            "steam_account": steam_account,
            "steam_status": _steam_status(steam_account) if steam_account else "",
            "steam_error": request.query_params.get("steam_error", "")[:300],
            "steam_message": {
                "connected": "Steam is connected.",
                "refreshed": "Your Steam games are up to date.",
            }.get(request.query_params.get("steam", ""), ""),
            "game_pass": game_pass,
            "game_plans": PLANS,
            "game_platforms": PLATFORMS,
            "region": region,
            "region_name": next((item.name for item in regions if item.code == region), region),
            "regions": regions,
            "enabled_providers": enabled_providers,
            "available_providers": tuple(
                provider for provider in providers if provider.id not in enabled_ids
            ),
            "selected_ids": selected_ids or set(),
            "header_providers": enabled_providers,
            "has_sources": bool(
                preferences.provider_ids or library_rows or steam_account or game_pass
            ),
            "library_rows": library_rows,
            "library_form": library_form or _empty_library_form(),
            "library_roots": tuple(str(root) for root in getattr(library, "roots", ())),
            "add_step": add_step if add_step in _ADD_STEPS else "",
            "error": error,
            "page_error": page_error,
            "streaming_error": streaming_error,
            "library_error": library_error,
            "return_to": return_to,
        }

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        context = await settings_context(
            request,
            add_step=request.query_params.get("add", ""),
            return_to=return_path(request.query_params.get("next")),
        )
        return render(request, "settings.html", context)

    @router.post("/settings/services", response_class=HTMLResponse)
    async def add_services(request: Request) -> Response:
        form = await request.form()
        return_to = return_path(form.get("next"))
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
                    return RedirectResponse(return_to or "/settings", status_code=303)
            except (TMDBError, ValueError) as exc:
                form_error = str(exc)

        context = await settings_context(
            request,
            add_step="streaming",
            selected_ids=selected_ids,
            streaming_error=form_error,
            return_to=return_to,
        )
        return render(request, "settings.html", context, status_code=422)

    @router.post("/settings/services/{provider_id}/remove")
    async def remove_service(provider_id: int, request: Request) -> Response:
        form = await request.form()
        preferences: Preferences = request.app.state.preferences.get()
        if provider_id in preferences.provider_ids:
            request.app.state.preferences.save(
                preferences.region,
                tuple(value for value in preferences.provider_ids if value != provider_id),
            )
        return RedirectResponse(return_path(form.get("next")) or "/settings", status_code=303)

    @router.post("/settings/region", response_class=HTMLResponse)
    async def change_region(request: Request) -> Response:
        form = await request.form()
        return_to = return_path(form.get("next"))
        region = str(form.get("region") or "").strip().upper()
        try:
            valid_ids = await _region_provider_ids(request, region)
            if valid_ids is not None:
                preferences: Preferences = request.app.state.preferences.get()
                request.app.state.preferences.save(
                    region,
                    tuple(value for value in preferences.provider_ids if value in valid_ids),
                )
                return RedirectResponse(_settings_url(return_to), status_code=303)
            page_error = "Choose a supported country or region."
        except (TMDBError, ValueError) as exc:
            page_error = str(exc)
        context = await settings_context(request, page_error=page_error, return_to=return_to)
        return render(request, "settings.html", context, status_code=422)

    @router.post("/settings/game-pass", response_class=HTMLResponse)
    async def save_game_pass(request: Request) -> Response:
        form = await request.form()
        return_to = return_path(form.get("next"))
        plan, platform = str(form.get("plan") or ""), str(form.get("platform") or "")
        try:
            request.app.state.game_preferences.save(plan, platform)
        except ValueError as exc:
            context = await settings_context(
                request,
                page_error=str(exc),
                game_pass_form=GamePreferences(plan, platform),
                return_to=return_to,
            )
            return render(request, "settings.html", context, status_code=422)
        return RedirectResponse(_settings_url(return_to, "gamepass"), status_code=303)

    @router.post("/settings/game-pass/remove")
    async def remove_game_pass(request: Request) -> Response:
        form = await request.form()
        request.app.state.game_preferences.clear()
        return RedirectResponse(_settings_url(return_path(form.get("next"))), status_code=303)

    @router.post("/settings/libraries", response_class=HTMLResponse)
    async def add_library(request: Request) -> Response:
        form = await request.form()
        return_to = return_path(form.get("next"))
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
                return_to=return_to,
            )
            return render(request, "settings.html", context, status_code=422)
        return RedirectResponse(return_to or "/settings", status_code=303)

    @router.post("/settings/libraries/{library_id}/folders", response_class=HTMLResponse)
    async def add_library_folders(library_id: int, request: Request) -> Response:
        form = await request.form()
        return_to = return_path(form.get("next"))
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
                return_to=return_to,
            )
            return render(request, "settings.html", context, status_code=422)
        return RedirectResponse(return_to or "/settings", status_code=303)

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
        form = await request.form()
        request.app.state.library.remove(library_id)
        return RedirectResponse(return_path(form.get("next")) or "/settings", status_code=303)

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
            display = replace(
                current,
                card_size=str(payload.get("card_size") or current.card_size),
                **values,
            )
            request.app.state.preferences.save_display(display)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse({"status": "saved"})

    @router.get("/api/collections", response_class=JSONResponse)
    async def list_collections(request: Request) -> JSONResponse:
        collections = request.app.state.collections.collections()
        return JSONResponse({"collections": [collection_payload(item) for item in collections]})

    @router.post("/api/collections", response_class=JSONResponse)
    async def create_collection(request: Request) -> JSONResponse:
        try:
            attributes = await _collection_attributes(request, required=True)
            name = attributes.pop("name")
            created = request.app.state.collections.create(name, **attributes)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse({"collection": collection_payload(created)}, status_code=201)

    @router.patch("/api/collections/{collection_id}", response_class=JSONResponse)
    async def update_collection(collection_id: int, request: Request) -> JSONResponse:
        service = request.app.state.collections
        current = service.get(collection_id)
        if current is None:
            return JSONResponse({"error": "Unknown collection"}, status_code=404)
        try:
            changes = await _collection_attributes(request, required=False)
            values = {
                "name": current.name,
                "description": current.description,
                "icon": current.icon,
                "default_sort": current.default_sort,
                **changes,
            }
            name = values.pop("name")
            updated = service.update(collection_id, name, **values)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        if updated is None:
            return JSONResponse({"error": "Unknown collection"}, status_code=404)
        return JSONResponse({"collection": collection_payload(updated)})

    @router.delete("/api/collections/{collection_id}", response_class=JSONResponse)
    async def delete_collection(collection_id: int, request: Request) -> JSONResponse:
        if not request.app.state.collections.delete(collection_id):
            return JSONResponse({"error": "Unknown collection"}, status_code=404)
        return JSONResponse({"status": "deleted"})

    @router.get("/api/items/{media_type}/{item_id}/collections", response_class=JSONResponse)
    async def item_collections(media_type: str, item_id: int, request: Request) -> JSONResponse:
        if media_type not in {"movie", "tv"} or item_id <= 0:
            return JSONResponse({"error": "Unknown title"}, status_code=404)
        service = request.app.state.collections
        saved = service.memberships(cast(MediaType, media_type), item_id)
        return JSONResponse(
            {
                "collections": [
                    {**collection_payload(item), "saved": item.id in saved}
                    for item in service.collections()
                ]
            }
        )

    @router.put(
        "/api/collections/{collection_id}/items/{media_type}/{item_id}",
        response_class=JSONResponse,
    )
    async def save_to_collection(
        collection_id: int, media_type: str, item_id: int, request: Request
    ) -> JSONResponse:
        if media_type not in {"movie", "tv"} or item_id <= 0:
            return JSONResponse({"error": "Unknown title"}, status_code=404)
        preferences: Preferences = request.app.state.preferences.get()
        try:
            added = await request.app.state.collections.add_item(
                collection_id,
                cast(MediaType, media_type),
                item_id,
                region=preferences.region if preferences.configured else "",
            )
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except TMDBError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"saved": True, "added": added})

    @router.delete(
        "/api/collections/{collection_id}/items/{media_type}/{item_id}",
        response_class=JSONResponse,
    )
    async def remove_from_collection(
        collection_id: int, media_type: str, item_id: int, request: Request
    ) -> JSONResponse:
        if media_type not in {"movie", "tv"}:
            return JSONResponse({"error": "Unknown title"}, status_code=404)
        removed = request.app.state.collections.remove_item(
            collection_id, cast(MediaType, media_type), item_id
        )
        return JSONResponse({"saved": False, "removed": removed})

    @router.get("/api/filters/suggest", response_class=JSONResponse)
    async def filter_suggestions(request: Request) -> JSONResponse:
        """Suggest people or keywords for a filter as the user types."""

        kind = request.query_params.get("kind", "")
        text = " ".join((request.query_params.get("q") or "").split())[:60]
        if kind not in {"person", "keyword"} or len(text) < 2:
            return JSONResponse({"results": []})
        catalog = request.app.state.catalog
        try:
            if kind == "person":
                people = await catalog.search_people(text)
                results = [
                    {"id": person_id, "name": name, "detail": detail}
                    for person_id, name, detail in people[:10]
                ]
            else:
                keywords = await catalog.search_keywords(text)
                results = [
                    {"id": keyword_id, "name": name, "detail": ""}
                    for keyword_id, name in keywords[:10]
                ]
        except TMDBError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"results": results})

    @router.get("/api/items/providers", response_class=JSONResponse)
    async def item_providers(request: Request) -> JSONResponse:
        """Map ``media:id`` keys to the user's enabled services that carry each title.

        Titles listed in ``any`` (search results from other services) map to every subscription
        service that carries them instead, under ``any`` in the response.
        """

        preferences: Preferences = request.app.state.preferences.get()
        keys = _item_keys(request.query_params.get("items", ""))
        any_keys = _item_keys(request.query_params.get("any", ""))
        if not preferences.configured or not (keys or any_keys):
            return JSONResponse({"providers": {}, "any": {}})
        catalog = request.app.state.catalog
        try:
            providers: tuple[Provider, ...] = await catalog.providers(preferences.region)
        except TMDBError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        enabled_ids = set(preferences.provider_ids)
        enabled = tuple(provider for provider in providers if provider.id in enabled_ids)
        requested = (
            *(("providers", key, enabled) for key in keys),
            *(("any", key, providers) for key in any_keys),
        )
        results = await asyncio.gather(
            *(
                catalog.available_provider_ids(preferences.region, media_type, item_id)
                for _, (media_type, item_id), _ in requested
            ),
            return_exceptions=True,
        )
        payload: dict[str, dict[str, list[dict[str, object]]]] = {"providers": {}, "any": {}}
        for (scope, (media_type, item_id), choices), result in zip(requested, results, strict=True):
            if isinstance(result, TMDBError):
                continue
            if isinstance(result, BaseException):
                raise result
            payload[scope][f"{media_type}:{item_id}"] = [
                {"id": provider.id, "name": provider.name, "logo_url": provider.logo_url}
                for provider in choices
                if provider.id in result
            ]
        return JSONResponse(payload)

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
                "years": details.years,
                "directed_by": details.directed_by,
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

    @router.get("/api/items/{media_type}/{item_id}/watch", response_class=JSONResponse)
    async def item_watch(media_type: str, item_id: int, request: Request) -> JSONResponse:
        """List the user's services that carry a title, with a link to it on each."""

        if media_type not in {"movie", "tv"} or item_id <= 0:
            return JSONResponse({"error": "Invalid media item"}, status_code=404)
        preferences: Preferences = request.app.state.preferences.get()
        if not preferences.configured:
            return JSONResponse({"options": []})
        try:
            options = await request.app.state.catalog.watch_options(
                preferences.region, media_type, item_id, preferences.provider_ids
            )
        except TMDBError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse(
            {
                "options": [
                    {
                        "provider_id": option.provider.id,
                        "name": option.provider.name,
                        "logo_url": option.provider.logo_url,
                        "url": option.url,
                        "direct": option.direct,
                    }
                    for option in options
                ]
            }
        )

    @router.get("/api/items/tv/{item_id}/episodes", response_class=JSONResponse)
    async def item_episodes(item_id: int, request: Request) -> JSONResponse:
        if item_id <= 0:
            return JSONResponse({"error": "Invalid media item"}, status_code=404)
        try:
            seasons = await request.app.state.catalog.seasons(item_id)
        except TMDBError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        library = request.app.state.library
        local = library.episode_keys(item_id) if library.has_libraries else frozenset()
        playback = request.app.state.playback
        states = (
            playback.states(f"tv:{item_id}:{season}:{episode}" for season, episode in local)
            if local
            else {}
        )
        next_up = playback.next_up(item_id) if local else None
        next_state = states.get(next_up.key) if next_up is not None else None

        def local_fields(season: int, episode: int) -> dict[str, object]:
            if (season, episode) not in local:
                return {}
            state = states.get(f"tv:{item_id}:{season}:{episode}")
            return {
                "play_url": f"/watch/tv/{item_id}/{season}/{episode}",
                "watched": bool(state and state.watched),
                "progress": round(state.progress, 3)
                if state and state.resumable and not state.watched
                else 0,
            }

        return JSONResponse(
            {
                "next_up": {
                    "url": next_up.url,
                    "label": next_up.episode_label,
                    "season": next_up.season,
                    "episode": next_up.episode,
                    "resume": bool(next_state and next_state.resumable and not next_state.watched),
                }
                if next_up is not None
                else None,
                "seasons": [
                    {
                        "season_number": season.season_number,
                        "name": season.name,
                        "episodes": [
                            {
                                "episode_number": episode.episode_number,
                                "name": episode.name,
                                "overview": episode.overview,
                                "air_date": episode.air_date,
                                "runtime_minutes": episode.runtime_minutes,
                                "still_url": episode.still_url,
                                "in_library": (season.season_number, episode.episode_number)
                                in local,
                                **local_fields(season.season_number, episode.episode_number),
                            }
                            for episode in season.episodes
                        ],
                    }
                    for season in seasons
                ],
            }
        )

    return router


def _settings_url(return_to: str | None, anchor: str = "") -> str:
    url = "/settings" + (f"?{urlencode({'next': return_to})}" if return_to else "")
    return url + (f"#{anchor}" if anchor else "")


def return_path(value: object) -> str | None:
    """Accept a path on this site to return to after a settings change, nothing else."""

    if not isinstance(value, str) or len(value) > 2000:
        return None
    if not value.startswith("/") or value.startswith(("//", "/\\")):
        return None
    return value


async def _first_titles(
    catalog: Any,
    region: str,
    query: BrowseQuery,
    local: LocalSource | None,
    first: CatalogPage,
    narrowing: dict[str, Any],
) -> CatalogPage:
    """Read the top of a collection, up to the number of titles grouping looks at."""

    items = list(first.items)
    number = 1
    while len(items) < GROUP_TITLE_LIMIT and number < first.total_pages:
        number += 1
        following = await catalog.browse(
            region, replace(query, page=number), local=local, **narrowing
        )
        if not following.items:
            break
        items.extend(following.items)
    return CatalogPage(
        items=tuple(items[:GROUP_TITLE_LIMIT]),
        total_pages=1,
        total_results=first.total_results,
    )


async def _credits(
    catalog: Any, items: tuple[CatalogItem, ...]
) -> dict[tuple[str, int], tuple[str, ...]]:
    """Look up the directors (or a series' creators) of each matched title."""

    async def one(item: CatalogItem) -> tuple[tuple[str, int], tuple[str, ...]] | None:
        try:
            _label, names = await catalog.people(item.media_type, item.id)
        except TMDBError:
            return None
        return (item.media_type, item.id), tuple(names)

    keys = {(item.media_type, item.id): item for item in items if item.id > 0}
    results = await asyncio.gather(*(one(item) for item in keys.values()))
    return dict(result for result in results if result is not None)


def collection_editor_context() -> dict[str, object]:
    """What the new-collection editor shown beside Save menus needs."""

    return {
        "collection_icons": ICONS,
        "collection_sort_options": tuple(
            (value, _SORT_LABELS[value])
            for value in ("added", "popularity", "release", "rating", "title")
        ),
    }


def collection_payload(collection: Collection) -> dict[str, object]:
    return {
        "id": collection.id,
        "name": collection.name,
        "description": collection.description,
        "icon": collection.icon,
        "default_sort": collection.default_sort,
        "item_count": collection.item_count,
        "url": f"/collections/{collection.key}",
    }


async def _collection_attributes(request: Request, *, required: bool) -> dict[str, str]:
    """Read the editable collection fields from a JSON body; ``name`` is needed to create."""

    payload = await request.json()
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    attributes: dict[str, str] = {}
    for key in ("name", "description", "icon", "default_sort"):
        if key in payload:
            value = payload[key]
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            attributes[key] = value
    if required and "name" not in attributes:
        raise ValueError("Give the collection a name")
    return attributes


def _steam_account(games: Any) -> SteamAccount | None:
    account = getattr(games, "account", None)
    return account() if callable(account) else None


def _steam_status(account: SteamAccount) -> str:
    if account.owned_status == "private":
        return "Game details are private"
    if account.owned_checked_at is None:
        return "Owned games not loaded yet"
    checked = datetime.fromtimestamp(account.owned_checked_at, UTC).isoformat(timespec="seconds")
    return f"{_count(len(account.owned), 'game')} · checked {_relative_time(checked)}"


def _game_sources(request: Request) -> tuple[str, ...]:
    """The game services that count as the user's: a chosen Game Pass plan, a Steam account."""

    sources = []
    if request.app.state.game_preferences.configured():
        sources.append("gamepass")
    if _steam_account(request.app.state.games) is not None:
        sources.append("steam")
    return tuple(sources)


async def _playable_games(
    request: Request, region: str, query: BrowseQuery, smart: SmartCollection
) -> list[GameEntry]:
    """The user's games that belong in a smart collection, most popular first.

    A search matches game titles anywhere; the year and rating filters apply to games as to
    titles, with a game's release year and its 0–10 store score.
    """

    playable = getattr(request.app.state.games, "playable", None)
    if not callable(playable) or not region or not (query.search or smart.has_games):
        return []
    preferred = request.app.state.game_preferences.get()
    games = await playable(
        region,
        request.app.state.settings.language,
        GameQuery(plan=preferred.plan, platform=preferred.platform),
        game_pass="gamepass" in query.game_sources,
        steam="steam" in query.game_sources,
    )
    today = date.today()
    search = query.search.casefold()
    kept = []
    for game in games:
        if search and search not in game.title.casefold():
            continue
        if not search and not smart.matches_game(game, today):
            continue
        year = int(game.release_date[:4]) if game.release_date[:4].isdigit() else None
        if query.year_from is not None and (year is None or year < query.year_from):
            continue
        if query.year_to is not None and (year is None or year > query.year_to):
            continue
        if query.minimum_rating is not None:
            score = game.score
            if (score is None and not query.include_unrated) or (
                score is not None and score < query.minimum_rating
            ):
                continue
        kept.append(GameEntry(game))
    return kept


def _game_access(request: Request, region: str) -> GameAccess:
    """Mark saved games the user can play with their Game Pass plan or Steam library."""

    async def access(games: list[Game]) -> tuple[list[Game], bool]:
        service = request.app.state.games
        defaults = request.app.state.game_preferences
        account = getattr(service, "account", None)
        configured = defaults.configured() or (callable(account) and account() is not None)
        mark = getattr(service, "access", None)
        if not callable(mark) or not region:
            return games, False
        preferred = defaults.get()
        try:
            marked = await mark(
                games,
                region,
                request.app.state.settings.language,
                GameQuery(plan=preferred.plan, platform=preferred.platform),
            )
        except StoreError:
            return games, False  # Unknown access shows every saved game.
        return marked, configured

    return access


def _card_state_keys(page: CatalogPage) -> list[str]:
    keys: list[str] = []
    for item in page.items:
        if item.media_type == "movie" and item.id > 0:
            keys.append(f"movie:{item.id}")
        elif item.id == 0 and item.local_file_id is not None:
            keys.append(f"file:{item.local_file_id}")
    return keys


def _continue_items(playback: Any, media_type: BrowseMediaType) -> tuple[dict[str, object], ...]:
    items: list[dict[str, object]] = []
    for entry in playback.continue_watching():
        target = entry.target
        if media_type != "all" and target.media_type != media_type:
            continue
        state = entry.state
        parts = [target.episode_label] if target.episode_label else []
        if entry.up_next:
            parts.insert(0, "Up next")
        elif state is not None and state.duration > state.position:
            minutes = max(1, round((state.duration - state.position) / 60))
            parts.append(f"{minutes} min left")
        items.append(
            {
                "title": target.title,
                "detail": " · ".join(parts),
                "url": target.url,
                "poster_url": f"https://image.tmdb.org/t/p/w342{target.poster_path}"
                if target.poster_path
                else None,
                "progress": round(state.progress * 100, 1) if state and not entry.up_next else 0,
            }
        )
    return tuple(items)


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


@dataclass(frozen=True, slots=True)
class _FilterLists(FilterOptions):
    movie_certifications: tuple[str, ...] = ()
    tv_certifications: tuple[str, ...] = ()

    def certifications_for(self, media_type: BrowseMediaType) -> tuple[str, ...]:
        if media_type == "movie":
            return self.movie_certifications
        if media_type == "tv":
            return self.tv_certifications
        return tuple(dict.fromkeys((*self.movie_certifications, *self.tv_certifications)))


async def _filter_lists(catalog: Any, region: str) -> _FilterLists:
    """TMDB's genres, content ratings, countries, and languages; empty when unavailable."""

    async def attempt(load: Callable[[], Awaitable[T]], fallback: T) -> T:
        try:
            return await load()
        except TMDBError:
            return fallback

    genres, ratings, countries, languages = await asyncio.gather(
        attempt(catalog.genre_choices, ()),
        attempt(lambda: catalog.certifications(region), {}),
        attempt(catalog.countries, ()),
        attempt(catalog.languages, ()),
    )
    rating_country = region
    if not any(ratings.values()) and region != "US":
        # Ratings are filtered by the user's country; countries TMDB has none for use the US.
        rating_country = "US"
        ratings = await attempt(lambda: catalog.certifications("US"), {})
    country_names = dict(countries)
    return _FilterLists(
        genres=genres,
        certification_country=rating_country,
        certification_region=country_names.get(rating_country, rating_country),
        countries=countries,
        languages=languages,
        movie_certifications=ratings.get("movie", ()),
        tv_certifications=ratings.get("tv", ()),
    )


async def _filter_names(catalog: Any, filters: TitleFilters) -> dict[tuple[str, int], str]:
    """Names of the people and keywords chosen in filters, which the URL holds as ids."""

    wanted = [("keyword", value) for value in filters.keyword_ids] + [
        ("person", value) for role in PERSON_ROLES for value in filters.people(role)
    ]

    async def name(kind: str, value: int) -> tuple[tuple[str, int], str]:
        try:
            return (kind, value), await catalog.name_of(kind, value)
        except TMDBError:
            return (kind, value), ""

    found = await asyncio.gather(*(name(kind, value) for kind, value in dict.fromkeys(wanted)))
    return {key: text for key, text in found if text}


def _pick_category(categories: tuple[BrowseCategory, ...], slug: str) -> BrowseCategory:
    return next((category for category in categories if category.slug == slug), categories[0])


def _local_source(
    library: Any,
    query: BrowseQuery,
    category: BrowseCategory,
    item_ids: frozenset[int] | None,
) -> LocalSource:
    """Load the first ``limit`` titles of the chosen libraries in the order the catalog asks.

    ``item_ids`` are the titles that passed the filters, when there are any.
    """

    async def load(value: BrowseQuery, limit: int) -> CatalogPage:
        # The catalog strips the library selection from its query; restore it here.
        local_query = replace(value, page=1, library_ids=query.library_ids)
        return await library.browse(
            local_query, category=category, page_size=limit, item_ids=item_ids
        )

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
    collection: str,
    all_game_sources: tuple[str, ...] = (),
) -> BrowseQuery:
    params = request.query_params
    media_value = params.get("media", "all")
    media_type: BrowseMediaType = media_value if media_value in {"all", "movie", "tv"} else "all"
    provider_ids = _selection(params.getlist("providers"), all_provider_ids)
    library_ids = _selection(params.getlist("libraries"), all_library_ids)
    chosen_games = {token for value in params.getlist("games") for token in value.split(",")}
    game_sources = (
        tuple(source for source in all_game_sources if source in chosen_games)
        if params.getlist("games")
        else all_game_sources
    )
    if not provider_ids and not library_ids and not game_sources:
        provider_ids, library_ids = all_provider_ids, all_library_ids
        game_sources = all_game_sources

    current_year = date.today().year
    year_from = _optional_int(params.get("year_from"), 1900, current_year)
    year_to = _optional_int(params.get("year_to"), 1900, current_year)
    if year_from is not None and year_to is not None and year_from > year_to:
        year_from, year_to = year_to, year_from
    minimum_rating = _optional_float(params.get("rating_min"), 0, 10)
    page = _optional_int(params.get("page"), 1, 500) or 1
    sort_value = params.get("sort")
    order = params.get("order")
    return BrowseQuery(
        media_type=media_type,
        category=collection[:50],
        search=(params.get("q") or "").strip()[:100],
        provider_ids=provider_ids,
        year_from=year_from,
        year_to=year_to,
        minimum_rating=minimum_rating,
        include_unrated=params.get("unrated") == "1",
        page=page,
        library_ids=library_ids,
        sort=cast(SortKey, sort_value) if sort_value in SORT_KEYS else None,
        descending={"asc": False, "desc": True}.get(order or ""),
        group=group if (group := params.get("group") or "") in GROUPINGS else "",
        game_sources=game_sources,
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
    genres: Sequence[GenreChoice] = (),
    all_game_sources: tuple[str, ...] = (),
) -> str:
    """Link to a collection (or search results) with the query's sources, filters, and sort.

    The home collection lives at ``/``; defaults are left out to keep links short.
    """

    params: dict[str, str | int | float] = {}
    if query.search:
        params["q"] = query.search
    if query.media_type != "all":
        params["media"] = query.media_type
    if query.provider_ids != all_provider_ids:
        params["providers"] = ",".join(str(value) for value in query.provider_ids) or _NO_SELECTION
    if query.library_ids != all_library_ids:
        params["libraries"] = ",".join(str(value) for value in query.library_ids) or _NO_SELECTION
    if query.game_sources != all_game_sources:
        params["games"] = ",".join(query.game_sources) or _NO_SELECTION
    if query.year_from is not None:
        params["year_from"] = query.year_from
    if query.year_to is not None:
        params["year_to"] = query.year_to
    if query.minimum_rating is not None:
        params["rating_min"] = query.minimum_rating
    if query.include_unrated:
        params["unrated"] = 1
    params.update(filter_params(query.filters, genres))
    if query.sort is not None and not query.search:
        params["sort"] = query.sort
    if query.descending is not None and not query.search:
        params["order"] = "desc" if query.descending else "asc"
    if query.group:
        params["group"] = query.group
    if query.page > 1:
        params["page"] = query.page
    if query.search or query.category == HOME_COLLECTION:
        path = "/"
    else:
        path = f"/collections/{query.category}"
    # Lists such as genre=drama,comedy keep their commas readable.
    return f"{path}?{urlencode(params, safe=',')}" if params else path


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

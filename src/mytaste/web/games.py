from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from mytaste.games.http import StoreError
from mytaste.games.models import (
    COLLECTION_ICONS,
    COLLECTIONS,
    DESCENDING_SORTS,
    GAME_PASS_COLLECTIONS,
    OWNED_COLLECTIONS,
    PLANS,
    PLATFORMS,
    REGION,
    SORTS,
    SOURCES,
    STEAM_COLLECTIONS,
    GamePage,
    GameQuery,
    parse_game_key,
)
from mytaste.games.service import (
    allowed_sorts,
    can_reverse,
    default_sort,
    steam_store_collection,
)
from mytaste.games.steam import SteamProfileError, openid_url
from mytaste.storage.games import GamePreferences
from mytaste.web.routes import collection_editor_context, collection_payload

_STATE_COOKIE = "mytaste_steam_state"
_NOTICES = {
    "steam-private": (
        "Your Steam game details are private, so owned games can't be listed. In Steam, set "
        "Privacy settings › Game details to Public, then refresh Steam on the Services page."
    ),
    "steam-key": (
        "Owned Steam games need a Steam Web API key on the server (MYTASTE_STEAM_API_KEY)."
    ),
    "gamepass-unavailable": "Game Pass availability could not be checked right now.",
}


def _steam(request: Request) -> bool:
    return bool(getattr(request.app.state.games, "steam_available", False))


def _account(request: Request) -> Any:
    account = getattr(request.app.state.games, "account", None)
    return account() if callable(account) else None


def _context(request: Request) -> dict[str, Any]:
    return {
        "request": request,
        "display": request.app.state.preferences.get_display(),
        "header_media": "game",
        "search_query": "",
        "current_path": request.url.path,
        "media_links": [
            {"url": "/", "label": "All", "active": False},
            {"url": "/?media=movie", "label": "Movies", "active": False},
            {"url": "/?media=tv", "label": "Series", "active": False},
        ],
        "plans": PLANS,
        "platforms": PLATFORMS,
        "configured_providers": (),
        "libraries": (),
        "sources_changed": False,
        **collection_editor_context(),
    }


def browse_url(query: GameQuery, *, steam: bool = True, connected: bool = True) -> str:
    path = "/collections/games" + (f"/{query.collection}" if query.collection != "all" else "")
    params: list[tuple[str, Any]] = [("plan", query.plan), ("platform", query.platform)]
    if steam and set(query.sources) != set(SOURCES):
        params.extend(("source", source) for source in query.sources or ("none",))
    default = default_sort(query, steam, connected)
    for name, value in (
        ("q", query.search),
        ("genre", query.genre),
        ("sort", query.sort if query.sort != default else ""),
        ("page", query.page if query.page != 1 else ""),
        ("order", query.order),
    ):
        if value:
            params.append((name, value))
    return path + "?" + urlencode(params)


def _parse_query(
    request: Request, key: str, defaults: GamePreferences, steam: bool, connected: bool
) -> GameQuery:
    params = request.query_params
    chosen = params.getlist("source")
    sources = tuple(source for source in SOURCES if source in chosen) if chosen else tuple(SOURCES)
    query = GameQuery(
        plan=params.get("plan", defaults.plan),
        platform=params.get("platform", defaults.platform),
        collection=key,
        search=params.get("q", "").strip(),
        genre=params.get("genre", ""),
        page=int(params.get("page", "1")),
        order=params.get("order", ""),
        sources=sources,
    )
    if key not in COLLECTIONS:
        raise ValueError("Choose a supported collection and sort order")
    sort = params.get("sort", "")
    # A sort that the collection cannot give (after switching collection or sources) falls
    # back to the collection's own order instead of failing.
    if sort not in allowed_sorts(query, steam):
        sort = default_sort(query, steam, connected)
    query = replace(query, sort=sort)
    if not can_reverse(query, steam):
        query = replace(query, order="")
    return query


def _saved_game_icons(request: Request) -> dict[str, tuple[str, ...]]:
    collections = request.app.state.collections
    icons = getattr(collections, "saved_game_icons", None)
    return icons() if callable(icons) else {}


def _callback_url(request: Request) -> str:
    return str(request.url_for("steam_callback"))


def create_games_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/collections/games", response_class=HTMLResponse)
    @router.get("/collections/games/{key}", response_class=HTMLResponse)
    async def browse(request: Request, key: str = "all") -> HTMLResponse:
        preferences = request.app.state.preferences.get()
        defaults = request.app.state.game_preferences.get()
        steam = _steam(request)
        account = _account(request)
        connected = account is not None
        context = _context(request)
        page = GamePage()
        status = 200
        error = ""
        loading = False
        fragment = request.headers.get("X-MyTaste-Fragment")
        query = GameQuery(plan=defaults.plan, platform=defaults.platform)
        try:
            query = _parse_query(request, key, defaults, steam, account is not None)
            query.validate()
            if preferences.region:
                loading = not fragment and request.query_params.get("render") != "1"
                if not loading:
                    page = await request.app.state.games.browse(
                        preferences.region, request.app.state.settings.language, query
                    )
        except ValueError as exc:
            error, status = str(exc), 422
        except StoreError as exc:
            error, status = str(exc), 503
        saved_icons = _saved_game_icons(request)
        card_context = {
            "page": page,
            "query": query,
            "platforms": PLATFORMS,
            "can_save_games": True,
            "saved_game_icons": saved_icons,
        }
        if fragment == "results":
            return templates.TemplateResponse(
                request=request,
                name="_games_more.html",
                context=card_context,
                status_code=status,
                headers={
                    "X-Next-Page": browse_url(
                        replace(query, page=query.page + 1), steam=steam, connected=connected
                    )
                    if not error and query.page < page.pages
                    else "",
                    "Cache-Control": "no-store",
                },
            )
        visible = [
            name
            for name in COLLECTIONS
            if steam or (name not in STEAM_COLLECTIONS and name not in OWNED_COLLECTIONS)
        ]
        if account is None:
            visible = [name for name in visible if name not in {"played", "most-played"}]
        collection_links = [
            {
                "kind": "smart",
                "key": name,
                "icon": COLLECTION_ICONS[name],
                "label": COLLECTIONS[name],
                "active": name == query.collection,
                "url": browse_url(
                    replace(
                        query,
                        collection=name,
                        page=1,
                        sort=default_sort(
                            replace(query, collection=name, search=""), steam, connected
                        ),
                        order="",
                    ),
                    steam=steam,
                    connected=connected,
                ),
            }
            for name in visible
        ]
        sorts = {name: SORTS[name] for name in allowed_sorts(query, steam)}
        if query.search and "catalog" in sorts:
            sorts["catalog"] = "Relevance"
        elif query.collection in STEAM_COLLECTIONS:
            sorts["catalog"] = "Steam ranking"
        elif query.collection in GAME_PASS_COLLECTIONS or query.collection == "all":
            sorts["catalog"] = "Game Pass order"
        natural_order = "desc" if query.sort in DESCENDING_SORTS else "asc"
        order = query.order or natural_order
        reversible = can_reverse(query, steam)
        store = steam_store_collection(query, steam)
        description = (
            "Steam"
            if query.collection in STEAM_COLLECTIONS
            else "Xbox Game Pass"
            if query.collection in GAME_PASS_COLLECTIONS or not steam
            else "Your games on Game Pass and Steam"
            if query.collection in OWNED_COLLECTIONS
            else "Steam and Xbox Game Pass"
        )
        source_rows = [
            {
                "key": "gamepass",
                "label": "Xbox Game Pass",
                "checked": "gamepass" in query.sources,
                "detail": f"{PLANS[query.plan]} · {PLATFORMS[query.platform]}",
            }
        ]
        if steam:
            source_rows.append(
                {
                    "key": "steam",
                    "label": "Steam",
                    "checked": "steam" in query.sources,
                    "detail": (account.persona or "Connected")
                    if account is not None
                    else "Store only · not connected",
                }
            )
        context.update(
            {
                **card_context,
                "loading": loading,
                "full_url": browse_url(query, steam=steam, connected=connected) + "&render=1",
                "search_query": query.search,
                "region": preferences.region,
                "error": error,
                "heading": COLLECTIONS.get(query.collection, "Games"),
                "sorts": sorts,
                "page_path": request.url.path,
                "collection_links": collection_links,
                "steam_available": steam,
                "steam_account": account,
                "source_rows": source_rows,
                "steam_store": store,
                "notices": [_NOTICES[name] for name in page.notices if name in _NOTICES],
                "previous_url": browse_url(
                    replace(query, page=query.page - 1), steam=steam, connected=connected
                )
                if query.page > 1 and not error
                else "",
                "next_url": browse_url(
                    replace(query, page=query.page + 1), steam=steam, connected=connected
                )
                if query.page < page.pages and not error
                else "",
                "clear_url": browse_url(
                    replace(
                        query,
                        search="",
                        genre="",
                        sort=default_sort(replace(query, search=""), steam, connected),
                        order="",
                        page=1,
                    ),
                    steam=steam,
                    connected=connected,
                ),
                "checked": datetime.fromtimestamp(page.checked_at, UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                if page.checked_at is not None
                else "",
                "current_link": next((link for link in collection_links if link["active"]), None),
                "icon": COLLECTION_ICONS.get(query.collection, "list"),
                "description": description
                + (f" · {preferences.region}" if preferences.region else ""),
                "sort_label": sorts.get(query.sort, SORTS.get(query.sort, "")),
                "sort_changed": query.sort != default_sort(query, steam, connected)
                or bool(query.order),
                "sort_order": order,
                "natural_order": natural_order,
                "default_sort": default_sort(query, steam, connected),
                "can_reverse": reversible,
                "sort_flip_url": browse_url(
                    replace(query, order="asc" if order == "desc" else "desc", page=1),
                    steam=steam,
                    connected=connected,
                ),
                "active_filter_rules": int(bool(query.genre)),
                "active_filter_count": int(bool(query.genre)),
                "active_filters": [
                    {
                        "label": query.genre,
                        "url": browse_url(
                            replace(query, genre="", page=1), steam=steam, connected=connected
                        ),
                    }
                ]
                if query.genre
                else [],
            }
        )
        if fragment == "games-page":
            return JSONResponse(
                {
                    "html": templates.env.get_template("_games_results.html").render(context),
                    "genres": page.genres,
                },
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )
        return templates.TemplateResponse(
            request=request,
            name="games.html",
            context=context,
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/games/settings", response_class=HTMLResponse)
    async def settings(request: Request) -> HTMLResponse:
        defaults = request.app.state.game_preferences.get()
        context = {
            **_context(request),
            "defaults": defaults,
            "region": request.app.state.preferences.get().region,
            "error": "",
        }
        return templates.TemplateResponse(
            request=request, name="game_settings.html", context=context
        )

    @router.post("/games/settings")
    async def save_settings(request: Request) -> Response:
        form = await request.form()
        plan, platform = str(form.get("plan", "")), str(form.get("platform", ""))
        region = str(form.get("region", "")).strip().upper()
        try:
            GameQuery(plan=plan, platform=platform).validate()
            if not REGION.fullmatch(region):
                raise ValueError("Enter a two-letter country code, such as DE or US")
        except ValueError as exc:
            context = {
                **_context(request),
                "defaults": GamePreferences(plan, platform),
                "region": region,
                "error": str(exc),
            }
            return templates.TemplateResponse(
                request=request, name="game_settings.html", context=context, status_code=422
            )
        preferences = request.app.state.preferences
        await asyncio.to_thread(preferences.save, region, preferences.get().provider_ids)
        await asyncio.to_thread(request.app.state.game_preferences.save, plan, platform)
        return RedirectResponse("/collections/games", status_code=303)

    # Steam account ----------------------------------------------------------------------

    def services_redirect(**params: str) -> RedirectResponse:
        query = urlencode({key: value for key, value in params.items() if value})
        return RedirectResponse("/settings" + (f"?{query}" if query else "") + "#steam", 303)

    @router.get("/games/steam/connect")
    async def steam_connect(request: Request) -> Response:
        if not _steam(request):
            return services_redirect(steam_error="Steam is not available on this server.")
        state = secrets.token_urlsafe(24)
        callback = _callback_url(request)
        realm = f"{request.url.scheme}://{request.url.netloc}/"
        response = RedirectResponse(openid_url(f"{callback}?state={state}", realm), 303)
        response.set_cookie(
            _STATE_COOKIE,
            state,
            max_age=600,
            path="/games/steam",
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
        return response

    @router.get("/games/steam/callback", name="steam_callback")
    async def steam_callback(request: Request) -> Response:
        state = request.cookies.get(_STATE_COOKIE, "")
        params = dict(request.query_params)
        try:
            if not state or not secrets.compare_digest(state, params.get("state", "")):
                raise SteamProfileError("That Steam sign-in expired. Try again.")
            games = request.app.state.games
            steam_id = await games.steam.verify_openid(
                params, f"{_callback_url(request)}?state={state}"
            )
            await games.connect(steam_id)
            response = services_redirect(steam="connected")
        except SteamProfileError as exc:
            response = services_redirect(steam_error=str(exc))
        except StoreError as exc:
            response = services_redirect(steam_error=str(exc))
        response.delete_cookie(_STATE_COOKIE, path="/games/steam")
        return response

    @router.post("/games/steam/profile")
    async def steam_profile(request: Request) -> Response:
        form = await request.form()
        try:
            await request.app.state.games.connect_profile(str(form.get("profile", "")))
        except (SteamProfileError, StoreError) as exc:
            return services_redirect(steam_error=str(exc), add="steam")
        return services_redirect(steam="connected")

    @router.post("/games/steam/refresh")
    async def steam_refresh(request: Request) -> Response:
        try:
            await request.app.state.games.refresh_owned(force=True)
        except (SteamProfileError, StoreError) as exc:
            return services_redirect(steam_error=str(exc))
        return services_redirect(steam="refreshed")

    @router.post("/games/steam/disconnect")
    async def steam_disconnect(request: Request) -> Response:
        await asyncio.to_thread(request.app.state.games.disconnect)
        return services_redirect()

    # Details ----------------------------------------------------------------------------

    @router.get("/api/games/{key}/details")
    @router.get("/api/items/game/{key}/details")
    async def details(key: str, request: Request) -> JSONResponse:
        try:
            parse_game_key(key)
            preferences = request.app.state.preferences.get()
            defaults = request.app.state.game_preferences.get()
            game = await request.app.state.games.details(
                key,
                preferences.region or "US",
                request.app.state.settings.language,
                plan=defaults.plan,
                platform=defaults.platform,
            )
        except ValueError:
            return JSONResponse(
                {"error": "Unknown game"}, status_code=404, headers={"Cache-Control": "no-store"}
            )
        except StoreError as exc:
            return JSONResponse(
                {"error": str(exc)}, status_code=503, headers={"Cache-Control": "no-store"}
            )
        return JSONResponse(
            {
                **game.payload(),
                "portable_id": game.portable_id,
                "store_url": game.store_url,
                "xbox_url": game.xbox_url,
                "steam_url": game.steam_url,
                "hours_played": game.hours_played,
                "last_played_date": game.last_played_date,
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/games/{key}/unlink")
    async def unlink(key: str, request: Request) -> JSONResponse:
        try:
            parse_game_key(key)
            xbox_ids = await request.app.state.games.unlink(key)
        except ValueError:
            return JSONResponse({"error": "Unknown game"}, status_code=404)
        return JSONResponse({"separated": xbox_ids})

    @router.get("/games/{key}", response_class=HTMLResponse)
    async def game_page(key: str, request: Request) -> Response:
        response = await details(key, request)
        if response.status_code != 200:
            return templates.TemplateResponse(
                request=request,
                name="game_error.html",
                context={**_context(request), "error": json.loads(response.body)["error"]},
                status_code=response.status_code,
                headers={"Cache-Control": "no-store"},
            )
        game = json.loads(response.body)
        if game["id"] != key:
            # Older Xbox links and games found on both stores move to the game's own address.
            return RedirectResponse(f"/games/{game['id']}", status_code=302)
        context = {**_context(request), "game": game}
        return templates.TemplateResponse(
            request=request,
            name="game.html",
            context=context,
            headers={"Cache-Control": "no-store"},
        )

    # Saving to collections --------------------------------------------------------------

    @router.get("/api/items/game/{key}/collections", response_class=JSONResponse)
    async def game_collections(key: str, request: Request) -> JSONResponse:
        try:
            parse_game_key(key)
        except ValueError:
            return JSONResponse({"error": "Unknown game"}, status_code=404)
        service = request.app.state.collections
        saved = service.game_memberships(key)
        return JSONResponse(
            {
                "collections": [
                    {**collection_payload(item), "saved": item.id in saved}
                    for item in service.collections()
                ]
            }
        )

    @router.put("/api/collections/{collection_id}/items/game/{key}", response_class=JSONResponse)
    async def save_game(collection_id: int, key: str, request: Request) -> JSONResponse:
        try:
            parse_game_key(key)
            preferences = request.app.state.preferences.get()
            defaults = request.app.state.game_preferences.get()
            game = await request.app.state.games.details(
                key,
                preferences.region or "US",
                request.app.state.settings.language,
                plan=defaults.plan,
                platform=defaults.platform,
            )
            added = await asyncio.to_thread(
                request.app.state.collections.add_game, collection_id, game
            )
        except LookupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        except ValueError:
            return JSONResponse({"error": "Unknown game"}, status_code=404)
        except StoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"saved": True, "added": added})

    @router.delete("/api/collections/{collection_id}/items/game/{key}", response_class=JSONResponse)
    async def remove_game(collection_id: int, key: str, request: Request) -> JSONResponse:
        try:
            parse_game_key(key)
        except ValueError:
            return JSONResponse({"error": "Unknown game"}, status_code=404)
        removed = request.app.state.collections.remove_game(collection_id, key)
        return JSONResponse({"saved": False, "removed": removed})

    return router

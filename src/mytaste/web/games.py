from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from mytaste.games.gamepass import GamePassError
from mytaste.games.models import (
    COLLECTIONS,
    PLANS,
    PLATFORMS,
    REGION,
    SORTS,
    GamePage,
    GameQuery,
    product_id,
)
from mytaste.storage.games import GamePreferences


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
    }


def browse_url(query: GameQuery) -> str:
    path = "/collections/games" + (f"/{query.collection}" if query.collection != "all" else "")
    params: dict[str, Any] = {"plan": query.plan, "platform": query.platform}
    for name, value in (
        ("q", query.search),
        ("genre", query.genre),
        ("sort", query.sort if query.sort != "catalog" else ""),
        ("page", query.page if query.page != 1 else ""),
    ):
        if value:
            params[name] = value
    return path + "?" + urlencode(params)


def create_games_router(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    @router.get("/collections/games", response_class=HTMLResponse)
    @router.get("/collections/games/{key}", response_class=HTMLResponse)
    async def browse(request: Request, key: str = "all") -> HTMLResponse:
        preferences = request.app.state.preferences.get()
        defaults = request.app.state.game_preferences.get()
        context = _context(request)
        page = GamePage()
        status = 200
        error = ""
        query = GameQuery(plan=defaults.plan, platform=defaults.platform, collection=key)
        try:
            params = request.query_params
            query = replace(
                query,
                plan=params.get("plan", defaults.plan),
                platform=params.get("platform", defaults.platform),
                search=params.get("q", "").strip(),
                genre=params.get("genre", ""),
                sort=params.get("sort", "catalog"),
                page=int(params.get("page", "1")),
            )
            query.validate()
            if preferences.region:
                page = await request.app.state.games.browse(
                    preferences.region, request.app.state.settings.language, query
                )
        except ValueError as exc:
            error, status = str(exc), 422
        except GamePassError as exc:
            error, status = str(exc), 503
        if request.headers.get("X-MyTaste-Fragment") == "results":
            return templates.TemplateResponse(
                request=request,
                name="_games_more.html",
                context={"page": page, "query": query, "platforms": PLATFORMS},
                status_code=status,
                headers={
                    "X-Next-Page": browse_url(replace(query, page=query.page + 1))
                    if not error and query.page < page.pages
                    else "",
                    "Cache-Control": "no-store",
                },
            )
        context.update(
            {
                "query": query,
                "page": page,
                "region": preferences.region,
                "error": error,
                "heading": COLLECTIONS.get(key, "Games"),
                "sorts": SORTS,
                "page_path": request.url.path,
                "collection_links": [
                    {
                        "label": label,
                        "active": value == key,
                        "url": browse_url(replace(query, collection=value, page=1, sort="catalog")),
                    }
                    for value, label in COLLECTIONS.items()
                ],
                "previous_url": browse_url(replace(query, page=query.page - 1))
                if query.page > 1 and not error
                else "",
                "next_url": browse_url(replace(query, page=query.page + 1))
                if query.page < page.pages and not error
                else "",
                "clear_url": browse_url(
                    replace(query, search="", genre="", sort="catalog", page=1)
                ),
                "checked": datetime.fromtimestamp(page.checked_at, UTC).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
                if page.checked_at is not None
                else "",
            }
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
    async def save_settings(request: Request):
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

    @router.get("/api/games/{key}/details")
    async def details(key: str, request: Request) -> JSONResponse:
        try:
            key = product_id(key)
            preferences = request.app.state.preferences.get()
            game = await request.app.state.games.details(
                key, preferences.region or "US", request.app.state.settings.language
            )
        except ValueError:
            return JSONResponse(
                {"error": "Unknown game"}, status_code=404, headers={"Cache-Control": "no-store"}
            )
        except GamePassError as exc:
            return JSONResponse(
                {"error": str(exc)}, status_code=503, headers={"Cache-Control": "no-store"}
            )
        return JSONResponse(
            {**game.payload(), "portable_id": game.portable_id, "store_url": game.store_url},
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/games/{key}", response_class=HTMLResponse)
    async def game_page(key: str, request: Request):
        response = await details(key, request)
        if response.status_code != 200:
            return templates.TemplateResponse(
                request=request,
                name="game_error.html",
                context={**_context(request), "error": json.loads(response.body)["error"]},
                status_code=response.status_code,
                headers={"Cache-Control": "no-store"},
            )
        context = {**_context(request), "game": json.loads(response.body)}
        return templates.TemplateResponse(
            request=request,
            name="game.html",
            context=context,
            headers={"Cache-Control": "no-store"},
        )

    return router

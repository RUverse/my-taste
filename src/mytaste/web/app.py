from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mytaste import __version__
from mytaste.catalog.facts import FactsService
from mytaste.catalog.service import CatalogService
from mytaste.catalog.tmdb import TMDBClient
from mytaste.collections.service import CollectionService
from mytaste.config import AppSettings, load_app_settings
from mytaste.games.gamepass import GamePassClient
from mytaste.games.matching import GameLinker
from mytaste.games.service import GamesService
from mytaste.games.steam import SteamClient
from mytaste.library.service import LibraryService
from mytaste.playback.service import PlaybackService
from mytaste.storage.collections import CollectionRepository
from mytaste.storage.facts import FactsRepository
from mytaste.storage.game_links import GameLinkRepository
from mytaste.storage.gamepass_cache import GamePassCache
from mytaste.storage.games import GamePreferenceRepository
from mytaste.storage.library import LibraryRepository
from mytaste.storage.playback import PlaybackRepository
from mytaste.storage.preferences import PreferenceRepository
from mytaste.storage.settings import InstanceSettings
from mytaste.storage.steam import SteamAccountRepository
from mytaste.storage.users import UserRepository
from mytaste.web.accounts import create_accounts_router
from mytaste.web.games import create_games_router
from mytaste.web.playback import create_playback_router
from mytaste.web.routes import create_router
from mytaste.web.signin import SignInMiddleware

_WEB_ROOT = Path(__file__).parent


def create_app(
    settings: AppSettings | None = None,
    *,
    catalog: Any | None = None,
    preferences: PreferenceRepository | None = None,
    library: Any | None = None,
    playback: PlaybackService | None = None,
    collections: CollectionService | None = None,
    facts: Any | None = None,
    games: Any | None = None,
) -> FastAPI:
    resolved_settings = settings or load_app_settings()
    users = UserRepository(resolved_settings.database_path)
    users.initialize()
    instance_settings = InstanceSettings(resolved_settings.database_path)
    instance_settings.initialize()
    repository = preferences or PreferenceRepository(resolved_settings.database_path)
    repository.initialize()
    game_preferences = GamePreferenceRepository(resolved_settings.database_path)
    game_preferences.initialize()
    owns_games = games is None
    if games is None:
        steam = linker = accounts = None
        if resolved_settings.steam_enabled:
            steam = SteamClient(
                api_key=resolved_settings.steam_api_key,
                timeout=resolved_settings.request_timeout,
            )
            links = GameLinkRepository(resolved_settings.database_path)
            links.initialize()
            linker = GameLinker(links, steam, timeout=max(15, resolved_settings.request_timeout))
            accounts = SteamAccountRepository(resolved_settings.database_path)
            accounts.initialize()
        games = GamesService(
            GamePassClient(timeout=resolved_settings.request_timeout),
            GamePassCache(
                (resolved_settings.cache_dir or resolved_settings.database_path.parent / "cache")
                / "gamepass"
                / "catalog.sqlite3",
                enabled=resolved_settings.gamepass_cache_enabled,
                stale_ttl=resolved_settings.gamepass_stale_ttl,
                # Steam's details for owned games and Game Pass matches share the cache.
                max_entries=16_384,
            ),
            steam=steam,
            linker=linker,
            accounts=accounts,
            catalog_ttl=resolved_settings.gamepass_catalog_ttl,
            metadata_ttl=resolved_settings.gamepass_metadata_ttl,
        )

    owns_catalog = catalog is None
    if catalog is None:
        client = TMDBClient(
            resolved_settings.require_tmdb_token(),
            language=resolved_settings.language,
            timeout=resolved_settings.request_timeout,
        )
        catalog = CatalogService(client)

    library_repository = LibraryRepository(resolved_settings.database_path)
    library_repository.initialize()
    owns_library = library is None
    if library is None:
        library = LibraryService(
            library_repository,
            catalog,
            roots=resolved_settings.library_roots,
            rescan_interval=resolved_settings.library_rescan_minutes * 60,
        )

    owns_playback = playback is None
    if playback is None:
        playback_repository = PlaybackRepository(resolved_settings.database_path)
        playback_repository.initialize()
        playback = PlaybackService(
            library_repository,
            playback_repository,
            cache_dir=resolved_settings.cache_dir
            or resolved_settings.database_path.parent / "cache",
            roots=resolved_settings.library_roots,
            ffmpeg=resolved_settings.ffmpeg,
            ffprobe=resolved_settings.ffprobe,
            max_transcodes=resolved_settings.max_transcodes,
            hwaccel=resolved_settings.hwaccel,
            background_probe=owns_library,
        )
    owns_collections = collections is None
    if collections is None:
        collection_repository = CollectionRepository(resolved_settings.database_path)
        collection_repository.initialize()
        collections = CollectionService(collection_repository, catalog, library)

    owns_facts = facts is None
    if facts is None:
        facts_repository = FactsRepository(resolved_settings.database_path)
        facts_repository.initialize()
        facts = FactsService(facts_repository, catalog)
    # Filters check library titles against TMDB facts, so those are fetched ahead of time.
    warm_facts = owns_facts and owns_library

    listeners = getattr(library, "scan_listeners", None)
    if isinstance(listeners, list):
        listeners.append(playback.library_changed)
        if warm_facts:
            listeners.append(lambda _library_id: facts.warm(library.matched_keys))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if owns_library:
            await library.start()
        if owns_playback:
            await playback.start()
        if warm_facts:
            facts.warm(library.matched_keys)
        try:
            yield
        finally:
            if owns_games:
                await games.close()
            if owns_facts:
                await facts.stop()
            if owns_collections:
                await collections.stop()
            if owns_playback:
                await playback.stop()
            if owns_library:
                await library.stop()
            if owns_catalog:
                await catalog.close()

    app = FastAPI(
        title="MyTaste",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.catalog = catalog
    app.state.preferences = repository
    app.state.library = library
    app.state.playback = playback
    app.state.collections = collections
    app.state.facts = facts
    app.state.settings = resolved_settings
    app.state.games = games
    app.state.game_preferences = game_preferences
    app.state.users = users
    app.state.instance_settings = instance_settings
    app.add_middleware(SignInMiddleware, users=users)

    def signed_in(request: Request) -> dict[str, Any]:
        return {"me": getattr(request.state, "user", None)}

    templates = Jinja2Templates(directory=_WEB_ROOT / "templates", context_processors=[signed_in])
    templates.env.globals["asset_version"] = hashlib.sha256(
        b"".join(
            (_WEB_ROOT / "static" / name).read_bytes() for name in ("app.css", "app.js", "games.js")
        )
    ).hexdigest()[:12]
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")
    app.include_router(create_accounts_router(templates))
    app.include_router(create_games_router(templates))
    app.include_router(create_router(templates))
    app.include_router(create_playback_router(templates))
    return app

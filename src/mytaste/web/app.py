from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from mytaste import __version__
from mytaste.catalog.service import CatalogService
from mytaste.catalog.tmdb import TMDBClient
from mytaste.config import AppSettings, load_app_settings
from mytaste.library.service import LibraryService
from mytaste.storage.library import LibraryRepository
from mytaste.storage.preferences import PreferenceRepository
from mytaste.web.routes import create_router

_WEB_ROOT = Path(__file__).parent


def create_app(
    settings: AppSettings | None = None,
    *,
    catalog: Any | None = None,
    preferences: PreferenceRepository | None = None,
    library: Any | None = None,
) -> FastAPI:
    resolved_settings = settings or load_app_settings()
    repository = preferences or PreferenceRepository(resolved_settings.database_path)
    repository.initialize()

    owns_catalog = catalog is None
    if catalog is None:
        client = TMDBClient(
            resolved_settings.require_tmdb_token(),
            language=resolved_settings.language,
            timeout=resolved_settings.request_timeout,
        )
        catalog = CatalogService(client)

    owns_library = library is None
    if library is None:
        library_repository = LibraryRepository(resolved_settings.database_path)
        library_repository.initialize()
        library = LibraryService(
            library_repository,
            catalog,
            roots=resolved_settings.library_roots,
            rescan_interval=resolved_settings.library_rescan_minutes * 60,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if owns_library:
            await library.start()
        try:
            yield
        finally:
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
    app.state.settings = resolved_settings

    templates = Jinja2Templates(directory=_WEB_ROOT / "templates")
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")
    app.include_router(create_router(templates))
    return app

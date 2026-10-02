from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Collection as Container
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from mytaste.catalog.models import (
    BrowseQuery,
    CatalogPage,
    MediaType,
    catalog_sort_key,
)
from mytaste.catalog.tmdb import TMDBError
from mytaste.collections.models import Collection, CollectionItem
from mytaste.storage.collections import CollectionRepository, utc_now

logger = logging.getLogger(__name__)

TitleKey = tuple[str, int]


@dataclass(frozen=True, slots=True)
class CollectionPage:
    """One page of a user collection.

    ``available`` of ``total`` titles (of the browsed media type) are on the user's services
    or in their libraries; the rest are left out so nothing unwatchable is listed.
    """

    page: CatalogPage
    available: int
    total: int


class CollectionService:
    """User collections: editing, saving titles, and browsing them like the catalog."""

    def __init__(
        self,
        repository: CollectionRepository,
        catalog: Any,
        library: Any,
        *,
        availability_ttl: float = 86_400,
        page_size: int = 24,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.library = library
        self.availability_ttl = availability_ttl
        self.page_size = page_size
        self._refresh: asyncio.Task[None] | None = None

    async def stop(self) -> None:
        if self._refresh is not None and not self._refresh.done():
            self._refresh.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh

    # Editing ----------------------------------------------------------------------------

    def collections(self) -> tuple[Collection, ...]:
        return self.repository.list()

    def get(self, collection_id: int) -> Collection | None:
        return self.repository.get(collection_id)

    def create(self, name: str, **attributes: str) -> Collection:
        return self.repository.create(name, **attributes)

    def update(self, collection_id: int, name: str, **attributes: str) -> Collection | None:
        return self.repository.update(collection_id, name=name, **attributes)

    def delete(self, collection_id: int) -> bool:
        return self.repository.delete(collection_id)

    def memberships(self, media_type: MediaType, tmdb_id: int) -> frozenset[int]:
        return self.repository.memberships(media_type, tmdb_id)

    async def add_item(
        self, collection_id: int, media_type: MediaType, tmdb_id: int, *, region: str = ""
    ) -> bool:
        """Save a title with a snapshot of its TMDB details; ``False`` if already saved."""

        if self.repository.get(collection_id) is None:
            raise LookupError("Unknown collection")
        details = await self.catalog.details(media_type, tmdb_id)
        added = self.repository.add_item(
            collection_id,
            CollectionItem(
                media_type=media_type,
                tmdb_id=tmdb_id,
                added_at=utc_now(),
                title=details.title,
                release_date=details.release_date,
                overview=details.overview,
                rating=details.rating,
                popularity=details.popularity,
                poster_path=details.poster_path,
                genre_ids=details.genre_ids,
                genres=details.genres,
            ),
        )
        if region:
            # Check now so the next view of the collection does not wait for TMDB.
            await self._availability(region, [(media_type, tmdb_id)])
        return added

    def remove_item(self, collection_id: int, media_type: MediaType, tmdb_id: int) -> bool:
        return self.repository.remove_item(collection_id, media_type, tmdb_id)

    # Browsing ---------------------------------------------------------------------------

    async def browse(
        self,
        collection: Collection,
        query: BrowseQuery,
        *,
        region: str,
        provider_ids: Sequence[int],
        library_ids: Sequence[int],
        page_size: int | None = None,
    ) -> CollectionPage:
        """Browse a collection, limited to the user's ``provider_ids`` and ``library_ids``.

        The query's own source selection, filters, and sort then apply on top.
        """

        items = [
            item
            for item in self.repository.items(collection.id)
            if query.media_type == "all" or item.media_type == query.media_type
        ]
        availability = (
            await self._availability(region, [item.key for item in items])
            if region and provider_ids
            else {}
        )
        local = self.library.matched_keys(tuple(library_ids)) if library_ids else frozenset()
        chosen_local = (
            local
            if set(query.library_ids) == set(library_ids)
            else self.library.matched_keys(tuple(query.library_ids))
            if query.library_ids
            else frozenset()
        )

        def reachable(
            item: CollectionItem, providers: Container[int], keys: Container[TitleKey]
        ) -> bool:
            if item.key in keys:
                return True
            if not providers:
                return False
            found = availability.get(item.key)
            # A title whose services could not be checked is shown rather than hidden.
            return found is None or any(provider in providers for provider in found)

        everything = frozenset(provider_ids)
        chosen = frozenset(query.provider_ids)
        accessible = [item for item in items if reachable(item, everything, local)]
        shown = [
            item
            for item in accessible
            if reachable(item, chosen, chosen_local) and _matches_filters(item, query)
        ]

        sort, descending = query.sort_for(collection.default_sort)
        if sort == "added":
            shown.sort(key=lambda item: (item.added_at, item.sequence), reverse=descending)
            titles = [item.to_catalog_item() for item in shown]
        else:
            titles = sorted(
                (item.to_catalog_item() for item in shown),
                key=catalog_sort_key(sort, descending),
                reverse=descending,
            )
        titles = [
            replace(title, in_library=True) if (title.media_type, title.id) in local else title
            for title in titles
        ]
        size = page_size or self.page_size
        total_pages = max((len(titles) + size - 1) // size, 1)
        page = min(max(query.page, 1), total_pages)
        start = (page - 1) * size
        return CollectionPage(
            page=CatalogPage(
                items=tuple(titles[start : start + size]),
                page=page,
                total_pages=total_pages,
                total_results=len(titles),
            ),
            available=len(accessible),
            total=len(items),
        )

    # Availability -----------------------------------------------------------------------

    async def _availability(
        self, region: str, keys: Sequence[TitleKey]
    ) -> dict[TitleKey, frozenset[int]]:
        """Return the services carrying each title, from SQLite when checked recently.

        Unknown titles are checked before returning; stale ones are returned as stored and
        re-checked in the background. Titles TMDB could not answer for are left out.
        """

        stored = self.repository.availability(region, keys)
        cutoff = (datetime.now(UTC) - timedelta(seconds=self.availability_ttl)).isoformat(
            timespec="seconds"
        )
        result = {key: providers for key, (providers, _checked) in stored.items()}
        missing = [key for key in dict.fromkeys(keys) if key not in stored]
        stale = [key for key, (_providers, checked) in stored.items() if checked < cutoff]
        if missing:
            result.update(await self._fetch(region, missing))
        if stale and (self._refresh is None or self._refresh.done()):
            self._refresh = asyncio.create_task(self._refresh_stale(region, stale))
        return result

    async def _fetch(self, region: str, keys: Sequence[TitleKey]) -> dict[TitleKey, frozenset[int]]:
        async def check(key: TitleKey) -> tuple[TitleKey, frozenset[int] | None]:
            media_type, tmdb_id = key
            try:
                return key, await self.catalog.available_provider_ids(region, media_type, tmdb_id)
            except TMDBError:
                return key, None

        results = await asyncio.gather(*(check(key) for key in keys))
        found = [(key, providers) for key, providers in results if providers is not None]
        self.repository.save_availability(region, found)
        return dict(found)

    async def _refresh_stale(self, region: str, keys: Sequence[TitleKey]) -> None:
        try:
            await self._fetch(region, keys)
        except Exception:
            logger.exception("Refreshing where saved titles stream failed")


def _matches_filters(item: CollectionItem, query: BrowseQuery) -> bool:
    year = int(item.release_date[:4]) if item.release_date[:4].isdigit() else None
    if query.year_from is not None and (year is None or year < query.year_from):
        return False
    if query.year_to is not None and (year is None or year > query.year_to):
        return False
    if query.minimum_rating is not None and item.rating < query.minimum_rating:
        return query.include_unrated and item.rating == 0
    return True

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from collections.abc import Collection as Container
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from mytaste.catalog.models import (
    BrowseQuery,
    CatalogPage,
    MediaType,
    catalog_sort_key,
)
from mytaste.catalog.service import Refine
from mytaste.catalog.tmdb import TMDBError
from mytaste.collections.models import Collection, CollectionItem, GameEntry
from mytaste.games.models import Game
from mytaste.storage.collections import CollectionRepository, utc_now

logger = logging.getLogger(__name__)

TitleKey = tuple[str, int]
# Marks which saved games the user can play and says whether they set up any game service.
GameAccess = Callable[[list[Game]], Awaitable[tuple[list[Game], bool]]]


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

    def saved_icons(self) -> dict[tuple[str, int], tuple[str, ...]]:
        """Map every saved title to the icons of its collections, in collection order.

        Collections without an icon share one empty entry, which cards draw as a check.
        """

        order = self.repository.list()
        saved = self.repository.saved_memberships()
        return {
            key: tuple(dict.fromkeys(item.icon for item in order if item.id in ids))
            for key, ids in saved.items()
        }

    def game_memberships(self, key: str) -> frozenset[int]:
        return self.repository.game_memberships(key)

    def saved_game_icons(self) -> dict[str, tuple[str, ...]]:
        """Like :meth:`saved_icons`, keyed by every Steam and Xbox key of a saved game."""

        order = self.repository.list()
        return {
            key: tuple(dict.fromkeys(item.icon for item in order if item.id in ids))
            for key, ids in self.repository.saved_game_memberships().items()
        }

    def add_game(self, collection_id: int, game: Game) -> bool:
        """Save a game with a snapshot of its details; ``False`` if it was already saved.

        Access (Game Pass, owned, play time) describes the user, not the game, so it is
        checked again when the collection is browsed rather than saved.
        """

        if self.repository.get(collection_id) is None:
            raise LookupError("Unknown collection")
        snapshot = replace(game, game_pass=False, owned=False, playtime=0, last_played=0)
        return self.repository.add_game(collection_id, snapshot)

    def remove_game(self, collection_id: int, key: str) -> bool:
        return self.repository.remove_game(collection_id, key)

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
        refine: Refine | None = None,
        game_access: GameAccess | None = None,
    ) -> CollectionPage:
        """Browse a collection, limited to the user's ``provider_ids`` and ``library_ids``.

        The query's own source selection, filters, and sort then apply on top; ``refine``
        applies the filters that need more than the saved snapshot of a title.

        Saved games join the titles when all media types are shown. ``game_access`` marks the
        ones on Game Pass or owned on Steam and says whether the user set up either; once they
        have, only games they can play are listed, like titles on their services.
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
        if refine is not None and query.filters.active:
            allowed = await refine([item.to_catalog_item() for item in shown])
            shown = [item for item in shown if item.key in allowed]

        games: list[GameEntry] = []
        all_games = 0
        playable_games = 0
        if query.media_type == "all":
            saved_games = self.repository.games(collection.id)
            all_games = len(saved_games)
            if saved_games:
                marked, configured = (
                    await game_access([entry.game for entry in saved_games])
                    if game_access is not None
                    else ([entry.game for entry in saved_games], False)
                )
                entries = [
                    GameEntry(game, entry.added_at, entry.sequence)
                    for game, entry in zip(marked, saved_games, strict=True)
                    if not configured or game.game_pass or game.owned
                ]
                playable_games = len(entries)
                # TMDB's filters (genres, people…) never match a game, and narrowing the
                # streaming services or libraries shows only what those carry.
                narrowed = set(query.provider_ids) != set(provider_ids) or set(
                    query.library_ids
                ) != set(library_ids)
                if not query.filters.active and not narrowed:
                    games = [entry for entry in entries if _matches_filters(entry, query)]

        sort, descending = query.sort_for(collection.default_sort)
        listed: list[Any] = []
        if sort == "added":
            ordered = sorted(
                [
                    *((item.added_at, item.sequence, item.to_catalog_item()) for item in shown),
                    *((entry.added_at, entry.sequence, entry) for entry in games),
                ],
                key=lambda row: (row[0], row[1]),
                reverse=descending,
            )
            listed = [row[2] for row in ordered]
        else:
            listed = sorted(
                [*(item.to_catalog_item() for item in shown), *games],
                key=catalog_sort_key(sort, descending),
                reverse=descending,
            )
        titles = [
            replace(title, in_library=True)
            if not isinstance(title, GameEntry) and (title.media_type, title.id) in local
            else title
            for title in listed
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
            available=len(accessible) + playable_games,
            total=len(items) + all_games,
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


def _matches_filters(item: CollectionItem | GameEntry, query: BrowseQuery) -> bool:
    year = int(item.release_date[:4]) if item.release_date[:4].isdigit() else None
    if query.year_from is not None and (year is None or year < query.year_from):
        return False
    if query.year_to is not None and (year is None or year > query.year_to):
        return False
    if query.minimum_rating is not None and item.rating < query.minimum_rating:
        return query.include_unrated and item.rating == 0
    return True

from __future__ import annotations

import asyncio
import json
import math
from typing import Any

from mytaste.games.filters import game_matches, game_sort_key
from mytaste.games.gamepass import GamePassClient, GamePassError
from mytaste.games.models import REGION, Game, GamePage, GameQuery, product_id
from mytaste.storage.gamepass_cache import CachedBatch, GamePassCache


class GamesService:
    def __init__(
        self,
        client: GamePassClient,
        cache: GamePassCache,
        *,
        catalog_ttl: float = 7200,
        metadata_ttl: float = 86_400,
        page_size: int = 24,
    ) -> None:
        self.client = client
        self.cache = cache
        self.catalog_ttl = catalog_ttl
        self.metadata_ttl = metadata_ttl
        self.page_size = page_size

    @staticmethod
    def _key(*values: str) -> str:
        return json.dumps(["gamepass-v1", *values], separators=(",", ":"))

    async def _ids(
        self, query: GameQuery, region: str, language: str, collection: str
    ) -> CachedBatch:
        key = self._key("membership", region, language, query.plan, query.platform, collection)

        async def load(_keys: list[str]) -> dict[str, Any]:
            return {key: await self.client.ids(query, region, language, collection)}

        return await self.cache.get_many([key], self.catalog_ttl, load)

    async def _products(self, ids: list[str], region: str, language: str) -> list[CachedBatch]:
        async def batch(selected: list[str]) -> CachedBatch:
            by_key = {self._key("metadata", region, language, key): key for key in selected}

            async def load(keys: list[str]) -> dict[str, Any]:
                games = await self.client.products([by_key[key] for key in keys], region, language)
                return {
                    key: games[value].payload() if games[value] is not None else {"excluded": True}
                    for key, value in by_key.items()
                    if key in keys and value in games
                }

            return await self.cache.get_many(list(by_key), self.metadata_ttl, load)

        tasks = [
            asyncio.create_task(batch(ids[start : start + 20])) for start in range(0, len(ids), 20)
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def browse(self, region: str, language: str, query: GameQuery) -> GamePage:
        query.validate()
        if not REGION.fullmatch(region):
            raise ValueError("Choose a two-letter country code")
        batches = []
        requested = await self._ids(query, region, language, query.collection)
        batches.append(requested)
        ids = list(next(iter(requested.values.values())))
        if query.collection not in {"all", "coming"}:
            eligible = await self._ids(query, region, language, "all")
            batches.append(eligible)
            allowed = set(next(iter(eligible.values.values())))
            ids = [key for key in ids if key in allowed]
        if query.platform == "cloud":
            cloud = await self._ids(query, region, language, "cloud")
            batches.append(cloud)
            allowed = set(next(iter(cloud.values.values())))
            ids = [key for key in ids if key in allowed]
        metadata = await self._products(ids, region, language)
        batches.extend(metadata)
        values = {key: value for batch in metadata for key, value in batch.values.items()}
        games = []
        for key in ids:
            value = values.get(self._key("metadata", region, language, key))
            if value and value.get("excluded"):
                continue
            games.append(
                Game.from_payload(value)
                if value
                else Game(key, f"Xbox game {key}", metadata_complete=False)
            )
        incomplete = any(not game.metadata_complete for game in games)
        if incomplete and (query.search or query.genre or query.sort != "catalog"):
            raise GamePassError(
                "Some game details are unavailable. "
                "Try Collection order or refresh before filtering."
            )
        genres = tuple(sorted({genre for game in games for genre in game.genres}, key=str.casefold))
        games = [game for game in games if game_matches(game, query)]
        if query.sort != "catalog":
            games.sort(key=lambda game: game_sort_key(game, query.sort))
        natural_order = "desc" if query.sort in {"release", "rating"} else "asc"
        if query.order and query.order != natural_order:
            games.reverse()
        start = (query.page - 1) * self.page_size
        return GamePage(
            tuple(games[start : start + self.page_size]),
            len(games),
            max(1, math.ceil(len(games) / self.page_size)),
            genres,
            min(batch.checked_at for batch in batches),
            any(batch.stale for batch in batches),
            incomplete,
        )

    async def details(self, key: str, region: str, language: str) -> Game:
        key = product_id(key)
        batches = await self._products([key], region, language)
        value = batches[0].values.get(self._key("metadata", region, language, key))
        if not value or value.get("excluded"):
            raise GamePassError("Game details are unavailable.")
        return Game.from_payload(value)

    async def close(self) -> None:
        await self.cache.close()
        await self.client.close()

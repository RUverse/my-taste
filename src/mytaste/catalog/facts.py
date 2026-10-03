"""Per-title facts for filtering, read from TMDB once and kept in SQLite."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from mytaste.catalog.filters import TitleFacts
from mytaste.catalog.tmdb import TMDBError
from mytaste.storage.facts import FactsRepository, TitleKey

logger = logging.getLogger(__name__)


class FactsService:
    """Look up ``TitleFacts``, fetching titles TMDB has not been asked about yet.

    Facts older than ``ttl`` seconds are still used and refreshed in the background. Titles of
    local libraries are fetched ahead of time (``warm``) so filtering them does not wait.
    """

    def __init__(
        self,
        repository: FactsRepository,
        catalog: Any,
        *,
        ttl: float = 30 * 86_400,
        concurrency: int = 6,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.ttl = ttl
        self._limit = asyncio.Semaphore(concurrency)
        self._background: set[asyncio.Task[None]] = set()

    async def stop(self) -> None:
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def facts(self, keys: Iterable[TitleKey]) -> dict[TitleKey, TitleFacts]:
        """Return the facts of each title; titles TMDB cannot answer for are left out."""

        wanted = [key for key in dict.fromkeys(keys) if key[1] > 0]
        if not wanted:
            return {}
        stored = self.repository.get(wanted)
        result = {key: facts for key, (facts, _fetched) in stored.items()}
        missing = [key for key in wanted if key not in stored]
        if missing:
            result.update(await self._fetch(missing))
        cutoff = self._cutoff()
        stale = [key for key, (_facts, fetched) in stored.items() if fetched < cutoff]
        if stale:
            self._spawn(self._fetch(stale))
        return result

    def warm(self, keys: Callable[[], Iterable[TitleKey]]) -> None:
        """Fetch, in the background, the facts of titles that have none or old ones."""

        async def run() -> None:
            wanted = [key for key in dict.fromkeys(await asyncio.to_thread(keys)) if key[1] > 0]
            stored = self.repository.get(wanted)
            cutoff = self._cutoff()
            await self._fetch(
                [key for key in wanted if key not in stored or stored[key][1] < cutoff]
            )

        self._spawn(run())

    async def _fetch(self, keys: Sequence[TitleKey]) -> dict[TitleKey, TitleFacts]:
        async def one(key: TitleKey) -> tuple[TitleKey, TitleFacts] | None:
            async with self._limit:
                try:
                    return key, await self.catalog.title_facts(*key)
                except TMDBError as exc:
                    logger.info("No filter facts for %s %s: %s", key[0], key[1], exc)
                    return None

        results = [result for result in await asyncio.gather(*map(one, keys)) if result]
        if results:
            self.repository.save(results)
        return dict(results)

    def _spawn(self, work: Any) -> None:
        task = asyncio.create_task(work)
        self._background.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self._background.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                logger.error("Fetching filter facts failed", exc_info=finished.exception())

        task.add_done_callback(done)

    def _cutoff(self) -> str:
        return (datetime.now(UTC) - timedelta(seconds=self.ttl)).isoformat(timespec="seconds")

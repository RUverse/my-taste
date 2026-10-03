"""Disposable, bounded Game Pass cache. Disabled mode never opens a database."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
Loader = Callable[[list[str]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class CachedBatch:
    values: dict[str, Any]
    checked_at: float
    stale: bool = False


class GamePassCache:
    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = False,
        stale_ttl: float = 86_400,
        clock: Callable[[], float] = time.time,
        max_entries: int = 4096,
    ) -> None:
        self.path = path
        self.enabled = enabled
        self.stale_ttl = stale_ttl
        self.clock = clock
        self.max_entries = max_entries
        self._inflight: dict[tuple[str, ...], asyncio.Task[CachedBatch]] = {}

    async def get_many(self, keys: list[str], ttl: float, loader: Loader) -> CachedBatch:
        if not keys:
            return CachedBatch({}, self.clock())
        if not self.enabled:
            return CachedBatch(await loader(keys), self.clock())
        try:
            stored = await asyncio.to_thread(self._read, keys)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            self._disable()
            return CachedBatch(await loader(keys), self.clock())
        now = self.clock()
        usable = {
            key: value
            for key, value in stored.items()
            if 0 <= now - value[1] <= ttl + self.stale_ttl
        }
        expired = [key for key in keys if key not in usable or now - usable[key][1] > ttl]
        if not expired:
            return CachedBatch(
                {key: value[0] for key, value in usable.items()},
                min(value[1] for value in usable.values()),
            )
        task_key = tuple(sorted(expired))
        task = self._inflight.get(task_key)
        if task is None:
            task = asyncio.create_task(self._refresh(expired, loader))
            self._inflight[task_key] = task
            task.add_done_callback(lambda result: self._finished(task_key, result))
        if all(key in usable for key in keys):
            return CachedBatch(
                {key: value[0] for key, value in usable.items()},
                min(value[1] for value in usable.values()),
                True,
            )
        # Shield shared work: disconnecting one request must not cancel another reader's refresh.
        refreshed = await asyncio.shield(task)
        values = {key: value[0] for key, value in usable.items()}
        values.update(refreshed.values)
        times = [value[1] for key, value in usable.items() if key not in refreshed.values]
        checked_at = min([refreshed.checked_at, *times])
        return CachedBatch(values, checked_at, self.clock() - checked_at > ttl)

    async def _refresh(self, keys: list[str], loader: Loader) -> CachedBatch:
        values = await loader(keys)
        checked_at = self.clock()
        if self.enabled:
            try:
                await asyncio.to_thread(self._write, values, checked_at)
            except (OSError, sqlite3.Error, ValueError, TypeError):
                self._disable()
        return CachedBatch(values, checked_at)

    def _finished(self, key: tuple[str, ...], task: asyncio.Task[CachedBatch]) -> None:
        self._inflight.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("Game Pass background refresh failed; previous catalog retained")

    def _disable(self) -> None:
        self.enabled = False
        logger.warning("Game Pass cache unavailable; using live requests")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS entries "
                "(key TEXT PRIMARY KEY, payload TEXT NOT NULL, checked REAL NOT NULL)"
            )
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    def _read(self, keys: list[str]) -> dict[str, tuple[Any, float]]:
        with contextlib.closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT key, payload, checked FROM entries "
                f"WHERE key IN ({','.join('?' for _ in keys)})",
                keys,
            ).fetchall()
        return {key: (json.loads(payload), float(checked)) for key, payload, checked in rows}

    def _write(self, values: dict[str, Any], checked_at: float) -> None:
        with contextlib.closing(self._connect()) as connection, connection:
            connection.executemany(
                "INSERT INTO entries VALUES (?, ?, ?) ON CONFLICT(key) "
                "DO UPDATE SET payload=excluded.payload, checked=excluded.checked",
                [(key, json.dumps(value), checked_at) for key, value in values.items()],
            )
            connection.execute(
                "DELETE FROM entries WHERE key IN "
                "(SELECT key FROM entries ORDER BY checked DESC, key LIMIT -1 OFFSET ?)",
                (self.max_entries,),
            )

    async def close(self) -> None:
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()

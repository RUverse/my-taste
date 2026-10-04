"""Bounded, retrying JSON requests shared by the game stores' clients."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx

USER_AGENT = "MyTaste (+https://github.com/RUverse/my-taste)"


class StoreError(Exception):
    """A game store could not be read completely or reliably."""


class StoreHTTP:
    """Retries transient failures and 429/5xx answers a few times, honoring short Retry-After
    waits; anything else becomes ``error`` with a message naming ``store``."""

    def __init__(
        self,
        store: str,
        error: type[StoreError],
        *,
        timeout: float = 10,
        client: httpx.AsyncClient | None = None,
        concurrency: int = 4,
    ) -> None:
        self.store = store
        self.error = error
        self.http = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT})
        self._owns_http = client is None
        self._limit = asyncio.Semaphore(concurrency)

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self.send(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise self.error(f"{self.store} returned an unreadable response.") from exc

    async def send(
        self, method: str, url: str, *, accept: frozenset[int] = frozenset(), **kwargs: Any
    ) -> httpx.Response:
        """Return the response; statuses in ``accept`` are returned instead of raising."""

        for attempt in range(3):
            try:
                async with self._limit:
                    response = await self.http.request(method, url, **kwargs)
                if response.status_code in accept:
                    return response
                if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                    try:
                        delay = float(response.headers.get("Retry-After", 0.5 * (attempt + 1)))
                    except ValueError:
                        delay = 1
                    if not math.isfinite(delay) or delay > 5:
                        raise self.error(f"{self.store} is busy. Try again shortly.")
                    await asyncio.sleep(max(0, delay))
                    continue
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.TransportError) and attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                raise self.error(f"{self.store} is unavailable. Please try again.") from exc
        raise self.error(f"{self.store} is unavailable.")

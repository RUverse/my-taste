"""Paced, retrying requests shared by the music catalogs' clients."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import httpx

from mytaste import __version__

# MusicBrainz asks every application to identify itself with a way to reach its developers.
USER_AGENT = f"MyTaste/{__version__} ( https://mytaste.cc )"
_RETRIED = frozenset({429, 500, 502, 503, 504})


class MusicError(Exception):
    """A music catalog could not be read completely or reliably."""


class MusicHTTP:
    """Requests to one service, at most one per ``interval`` seconds.

    MusicBrainz allows one request per second per address and answers 503 when busy, so 429
    and 5xx answers are retried with growing waits, honoring Retry-After up to ``max_wait``.
    """

    def __init__(
        self,
        service: str,
        *,
        interval: float = 0.0,
        timeout: float = 15,
        attempts: int = 4,
        max_wait: float = 10,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.service = service
        self.interval = interval
        self.attempts = attempts
        self.max_wait = max_wait
        self.http = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        self._owns_http = client is None
        self._lock = asyncio.Lock()
        self._next = 0.0
        # Requests someone is waiting for go before background ones.
        self._foreground = 0
        self._idle = asyncio.Event()
        self._idle.set()

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def _turn(self, background: bool) -> None:
        while background and self._foreground:
            await self._idle.wait()
        if self.interval <= 0:
            return
        async with self._lock:
            wait = self._next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = time.monotonic() + self.interval

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self.send(method, url, **kwargs)
        try:
            return response.json()
        except ValueError as exc:
            raise MusicError(f"{self.service} returned an unreadable response.") from exc

    async def send(
        self,
        method: str,
        url: str,
        *,
        accept: frozenset[int] = frozenset(),
        background: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Return the response; statuses in ``accept`` are returned instead of raising.
        ``background`` requests wait while requests someone is waiting for are under way."""

        if background:
            return await self._send(method, url, accept, True, kwargs)
        self._foreground += 1
        self._idle.clear()
        try:
            return await self._send(method, url, accept, False, kwargs)
        finally:
            self._foreground -= 1
            if not self._foreground:
                self._idle.set()

    async def _send(
        self,
        method: str,
        url: str,
        accept: frozenset[int],
        background: bool,
        kwargs: dict[str, Any],
    ) -> httpx.Response:
        for attempt in range(self.attempts):
            last = attempt == self.attempts - 1
            await self._turn(background)
            try:
                response = await self.http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise MusicError(f"{self.service} is unavailable. Try again.") from exc
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if response.status_code in accept:
                return response
            if response.status_code in _RETRIED and not last:
                await asyncio.sleep(self._delay(response, attempt))
                continue
            if response.is_success:
                return response
            if response.status_code in _RETRIED:
                raise MusicError(f"{self.service} is busy. Try again shortly.")
            raise MusicError(f"{self.service} could not answer ({response.status_code}).")
        raise MusicError(f"{self.service} is unavailable.")

    def _delay(self, response: httpx.Response, attempt: int) -> float:
        fallback = 1.0 * 2**attempt
        try:
            delay = float(response.headers.get("Retry-After", fallback))
        except ValueError:
            delay = fallback
        if not math.isfinite(delay) or delay < 0:
            delay = fallback
        return min(delay, self.max_wait)

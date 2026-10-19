"""Album covers from the Cover Art Archive, fetched once and served from a local cache.

Each image takes two redirects and a second or two to arrive, and the archive grants no
license to its images, so MyTaste keeps a bounded copy instead of sending browsers there.
The cache is pruned by last use once it outgrows its size limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from mytaste.music.http import MusicError, MusicHTTP
from mytaste.music.models import MBID

logger = logging.getLogger(__name__)

ARCHIVE = "https://coverartarchive.org"
SIZES = frozenset({250, 500, 1200})
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MISSING_TTL = 6 * 3600


class CoverCache:
    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 500 * 1024 * 1024,
        http: MusicHTTP | None = None,
        concurrency: int = 6,
        timeout: float = 20,
    ) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.http = http or MusicHTTP("Cover Art Archive", timeout=timeout, attempts=2)
        self._limit = asyncio.Semaphore(concurrency)
        self._pending: dict[Path, asyncio.Future[Path | None]] = {}
        self._missing: dict[Path, float] = {}
        self._size: int | None = None
        self._pruning: asyncio.Task[None] | None = None

    async def close(self) -> None:
        if self._pruning is not None and not self._pruning.done():
            self._pruning.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pruning
        await self.http.close()

    def path(self, release: str, image: int, size: int) -> Path:
        return self.root / release[:2] / release / f"{image}-{size}.jpg"

    async def release_image(self, release: str, image: int, size: int) -> Path | None:
        """One image of a release, as the Cover Art Archive lists it."""

        if not MBID.fullmatch(release) or image <= 0 or size not in SIZES:
            return None
        return await self._get(
            self.path(release, image, size), f"{ARCHIVE}/release/{release}/{image}-{size}.jpg"
        )

    async def _get(self, path: Path, url: str) -> Path | None:
        if path.is_file():
            # The modification time records the last use, which pruning goes by.
            with contextlib.suppress(OSError):
                os.utime(path)
            return path
        if self._missing.get(path, 0) > time.monotonic():
            return None
        pending = self._pending.get(path)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[Path | None] = asyncio.get_running_loop().create_future()
        self._pending[path] = future
        try:
            result = await self._download(path, url)
            future.set_result(result)
            return result
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception as exc:
            future.set_exception(exc)
            # Waiting requests see the error; reading it here keeps it from being logged.
            future.exception()
            raise
        finally:
            self._pending.pop(path, None)

    async def _download(self, path: Path, url: str) -> Path | None:
        async with self._limit:
            try:
                response = await self.http.send(
                    "GET", url, accept=frozenset({404}), headers={"Accept": "image/*"}
                )
            except MusicError:
                return None
        if response.status_code == 404:
            self._missing[path] = time.monotonic() + _MISSING_TTL
            return None
        content = response.content
        kind = response.headers.get("content-type", "")
        if not kind.startswith("image/") or not content or len(content) > _MAX_IMAGE_BYTES:
            self._missing[path] = time.monotonic() + _MISSING_TTL
            return None
        await asyncio.to_thread(_write, path, content)
        if self._size is not None:
            self._size += len(content)
        self._schedule_prune()
        return path

    def _schedule_prune(self) -> None:
        if self._pruning is None or self._pruning.done():
            self._pruning = asyncio.create_task(self._prune())

    async def _prune(self) -> None:
        try:
            if self._size is None or self._size > self.max_bytes:
                self._size = await asyncio.to_thread(_prune, self.root, self.max_bytes)
        except Exception:
            logger.exception("Pruning the cover cache failed")


def _write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(f".{os.getpid()}.part")
    partial.write_bytes(content)
    partial.replace(path)


def _prune(root: Path, max_bytes: int) -> int:
    """Delete the least recently used covers until the cache fits; return its size."""

    files: list[tuple[float, int, Path]] = []
    for entry in root.rglob("*.jpg") if root.exists() else ():
        try:
            stat = entry.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, stat.st_size, entry))
    total = sum(size for _mtime, size, _path in files)
    if total <= max_bytes:
        return total
    # Prune to 90% so the next few downloads don't start another pass.
    target = int(max_bytes * 0.9)
    for _mtime, size, entry in sorted(files):
        if total <= target:
            break
        with contextlib.suppress(OSError):
            entry.unlink()
            total -= size
    return total

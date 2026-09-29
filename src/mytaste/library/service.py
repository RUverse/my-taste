from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mytaste.catalog.models import (
    BrowseCategory,
    BrowseMediaType,
    BrowseQuery,
    CatalogItem,
    CatalogPage,
    Genre,
    MediaType,
)
from mytaste.catalog.tmdb import TMDBError
from mytaste.library.models import Library, LibraryStatus, ScannedFile
from mytaste.library.scanner import LibraryUnavailableError, scan_directory
from mytaste.storage.library import ItemDraft, LibraryRepository, utc_now

logger = logging.getLogger(__name__)

_FIXED_CATEGORIES = (
    BrowseCategory("recent", "Recently Added"),
    BrowseCategory("latest", "Latest"),
    BrowseCategory("popular", "Most Popular"),
    BrowseCategory("alphabetical", "A–Z"),
)
_MAX_GENRE_CATEGORIES = 6
_MAX_FOLDER_ENTRIES = 400


@dataclass(frozen=True, slots=True)
class FolderEntry:
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class FolderListing:
    path: str
    parent: str | None
    entries: tuple[FolderEntry, ...]


class LibraryService:
    """Manage storage libraries: validation, background scanning, matching, browsing."""

    def __init__(
        self,
        repository: LibraryRepository,
        catalog: Any,
        *,
        roots: Iterable[Path] = (),
        rescan_interval: float = 3600,
        match_concurrency: int = 4,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.roots = tuple(Path(root) for root in roots)
        self.rescan_interval = rescan_interval
        self._match_limit = asyncio.Semaphore(match_concurrency)
        self._statuses: dict[int, LibraryStatus] = {}
        self._scan_tasks: dict[int, asyncio.Task[LibraryStatus]] = {}
        self._periodic: asyncio.Task[None] | None = None
        self._matched_keys: frozenset[tuple[str, int]] | None = None

    # Lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        for library in self.repository.list_libraries():
            if library.last_scanned_at is None:
                self.schedule_scan(library.id)
        if self.rescan_interval > 0 and self._periodic is None:
            self._periodic = asyncio.create_task(self._rescan_loop())

    async def stop(self) -> None:
        tasks = [*self._scan_tasks.values()]
        if self._periodic is not None:
            tasks.append(self._periodic)
            self._periodic = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._scan_tasks.clear()

    async def _rescan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.rescan_interval)
            for library in self.repository.list_libraries():
                if library.id not in self._scan_tasks:
                    with contextlib.suppress(Exception):
                        await self.scan(library.id)

    # Libraries -------------------------------------------------------------------------

    def libraries(self) -> tuple[Library, ...]:
        return self.repository.list_libraries()

    def library(self, library_id: int) -> Library | None:
        return self.repository.get_library(library_id)

    @property
    def has_libraries(self) -> bool:
        return bool(self.repository.list_libraries())

    def status(self, library_id: int) -> LibraryStatus:
        return self._statuses.get(library_id, LibraryStatus(library_id=library_id))

    def statuses(self) -> tuple[LibraryStatus, ...]:
        return tuple(self.status(library.id) for library in self.libraries())

    def resolve_path(self, raw: str) -> Path:
        cleaned = raw.strip()
        if not cleaned:
            raise ValueError("Choose a folder")
        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Use an absolute folder path such as /mnt/media/Movies")
        resolved = candidate.resolve(strict=False)
        if self.roots and not any(_within(resolved, root) for root in self.roots):
            allowed = ", ".join(str(root) for root in self.roots)
            raise ValueError(f"Libraries must be inside an allowed folder: {allowed}")
        if not resolved.is_dir():
            raise ValueError("That folder does not exist or is not a directory")
        if not os.access(resolved, os.R_OK | os.X_OK):
            raise ValueError("MyTaste does not have permission to read that folder")
        return resolved

    def add(self, name: str, folders: Sequence[tuple[str, str]]) -> Library:
        """Create a library from ``(path, media_type)`` pairs and start scanning it."""

        resolved = self._resolve_folders(folders)
        default_name = Path(resolved[0][0]).name if len(resolved) == 1 else "Local library"
        library = self.repository.add_library(name or default_name, resolved)
        self.schedule_scan(library.id)
        return library

    def add_folders(self, library_id: int, folders: Sequence[tuple[str, str]]) -> Library:
        if self.repository.get_library(library_id) is None:
            raise ValueError("Unknown library")
        library = self.repository.add_folders(library_id, self._resolve_folders(folders))
        self._restart_scan(library_id)
        return library

    def remove_folder(self, library_id: int, folder_id: int) -> bool:
        removed = self.repository.remove_folder(library_id, folder_id)
        if removed:
            self._restart_scan(library_id)
        return removed

    def rename(self, library_id: int, name: str) -> bool:
        return self.repository.rename_library(library_id, name)

    def remove(self, library_id: int) -> bool:
        task = self._scan_tasks.pop(library_id, None)
        if task is not None:
            task.cancel()
        self._statuses.pop(library_id, None)
        removed = self.repository.remove_library(library_id)
        self._matched_keys = None
        return removed

    def schedule_scan(self, library_id: int) -> bool:
        existing = self._scan_tasks.get(library_id)
        if existing is not None and not existing.done():
            return False
        if self.repository.get_library(library_id) is None:
            return False
        task = asyncio.create_task(self.scan(library_id))
        self._scan_tasks[library_id] = task

        def forget(finished: asyncio.Task[LibraryStatus]) -> None:
            if self._scan_tasks.get(library_id) is finished:
                del self._scan_tasks[library_id]

        task.add_done_callback(forget)
        return True

    def _restart_scan(self, library_id: int) -> None:
        """Rescan after the folder list changed, replacing a scan of the old folder list."""

        task = self._scan_tasks.pop(library_id, None)
        if task is not None:
            task.cancel()
        self.schedule_scan(library_id)

    def _resolve_folders(self, folders: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        if not folders:
            raise ValueError("Choose a folder")
        taken = [
            (Path(folder.path), library.name)
            for library in self.repository.list_libraries()
            for folder in library.folders
        ]
        resolved: list[tuple[str, str]] = []
        for raw, media_type in folders:
            path = self.resolve_path(raw)
            for other, owner in taken:
                if path == other:
                    where = f"the library “{owner}”" if owner else "this list"
                    raise ValueError(f"{path} is already in {where}")
                if _within(path, other) or _within(other, path):
                    where = f"“{owner}”" if owner else "this list"
                    raise ValueError(f"{path} overlaps {other} in {where}")
            taken.append((path, ""))
            resolved.append((str(path), media_type))
        return resolved

    def list_folders(self, raw: str | None) -> FolderListing:
        if not raw:
            if self.roots:
                entries = tuple(FolderEntry(str(root), str(root)) for root in self.roots)
                return FolderListing(path="", parent=None, entries=entries)
            raw = "/"
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ValueError("Folder paths must be absolute")
        resolved = candidate.resolve(strict=False)
        if self.roots and not any(_within(resolved, root) for root in self.roots):
            raise ValueError("That folder is outside the allowed library roots")
        if not resolved.is_dir():
            raise ValueError("That folder does not exist")
        entries: list[FolderEntry] = []
        try:
            with os.scandir(resolved) as handle:
                for entry in handle:
                    if entry.name.startswith((".", "$")):
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    entries.append(FolderEntry(entry.name, str(resolved / entry.name)))
        except OSError as exc:
            raise ValueError(f"Cannot read that folder: {exc.strerror}") from exc
        entries.sort(key=lambda item: item.name.casefold())
        parent: str | None = None
        if resolved.parent != resolved:
            parent = str(resolved.parent)
            if self.roots and any(resolved == root for root in self.roots):
                parent = ""
        return FolderListing(
            path=str(resolved),
            parent=parent,
            entries=tuple(entries[:_MAX_FOLDER_ENTRIES]),
        )

    # Scanning --------------------------------------------------------------------------

    async def scan(self, library_id: int) -> LibraryStatus:
        library = self.repository.get_library(library_id)
        if library is None:
            raise ValueError("Unknown library")
        self._set_status(LibraryStatus(library_id, "scanning", "Scanning folders…"))
        files: list[ScannedFile] = []
        for folder in library.folders:
            try:
                result = await asyncio.to_thread(
                    scan_directory, Path(folder.path), folder.media_type
                )
            except LibraryUnavailableError as exc:
                # Keep the previous items: a drive that is briefly unmounted should not
                # empty the library.
                self.repository.record_error(library_id, str(exc))
                return self._set_status(LibraryStatus(library_id, "error", str(exc)))
            files.extend(result.files)

        groups: dict[str, list[ScannedFile]] = {}
        for file in files:
            groups.setdefault(file.group_key, []).append(file)
        existing = {item.group_key: item for item in self.repository.items(library_id)}
        progress = LibraryStatus(
            library_id,
            "scanning",
            "Matching titles…",
            scanned_files=len(files),
            total_items=len(groups),
        )
        self._set_status(progress)
        matched = 0
        tmdb_failures = 0

        async def build(group_key: str, files: list[ScannedFile]) -> ItemDraft:
            nonlocal matched, tmdb_failures
            ordered = tuple(files)
            first = ordered[0]
            current = existing.get(group_key)
            if current is not None and current.matched:
                draft = ItemDraft(
                    group_key=group_key,
                    media_type=first.media_type,
                    title=current.title,
                    files=ordered,
                    year=current.year,
                    tmdb_id=current.tmdb_id,
                    release_date=current.release_date,
                    overview=current.overview,
                    rating=current.rating,
                    poster_path=current.poster_path,
                    genre_ids=current.genre_ids,
                    popularity=current.popularity,
                )
            else:
                match: CatalogItem | None = None
                try:
                    match = await self._match(first.media_type, first.titles, first.year)
                except TMDBError as exc:
                    tmdb_failures += 1
                    logger.warning("TMDB match failed for %s: %s", first.titles[0], exc)
                draft = _draft_from_match(group_key, ordered, match)
            if draft.tmdb_id is not None:
                matched += 1
            self._set_status(
                replace(self.status(library_id), matched_items=matched, state="scanning")
            )
            return draft

        drafts = await asyncio.gather(*(build(key, files) for key, files in groups.items()))
        self.repository.replace_items(library_id, drafts, scanned_at=utc_now())
        self._matched_keys = None
        message = ""
        if tmdb_failures:
            message = "Some titles could not be matched because TMDB was unavailable."
            self.repository.record_error(library_id, message)
        return self._set_status(
            LibraryStatus(
                library_id,
                "idle",
                message,
                scanned_files=len(files),
                matched_items=matched,
                total_items=len(groups),
            )
        )

    async def _match(
        self,
        media_type: MediaType,
        titles: tuple[str, ...],
        year: int | None,
    ) -> CatalogItem | None:
        attempts: list[tuple[str, int | None]] = []
        if year is not None:
            attempts.extend((title, year) for title in titles)
        attempts.extend((title, None) for title in titles)
        async with self._match_limit:
            for title, attempt_year in attempts:
                match = await self.catalog.match_title(media_type, title, attempt_year)
                if match is not None:
                    return match
        return None

    def _set_status(self, status: LibraryStatus) -> LibraryStatus:
        self._statuses[status.library_id] = status
        return status

    # Browsing --------------------------------------------------------------------------

    def matched_keys(self) -> frozenset[tuple[str, int]]:
        if self._matched_keys is None:
            self._matched_keys = self.repository.matched_keys()
        return self._matched_keys

    async def categories(self, media_type: BrowseMediaType) -> tuple[BrowseCategory, ...]:
        movie_counts = self.repository.genre_ids("movie") if media_type != "tv" else {}
        tv_counts = self.repository.genre_ids("tv") if media_type != "movie" else {}
        movie_genres, tv_genres = await self._genres(media_type)
        movie_by_name = {genre.name.casefold(): genre.id for genre in movie_genres}
        tv_by_name = {genre.name.casefold(): genre.id for genre in tv_genres}
        names: dict[str, tuple[str, int]] = {}
        for genres, counts in ((movie_genres, movie_counts), (tv_genres, tv_counts)):
            for genre in genres:
                count = counts.get(genre.id, 0)
                if count:
                    key = genre.name.casefold()
                    names[key] = (genre.name, names.get(key, ("", 0))[1] + count)
        ranked = sorted(names.values(), key=lambda entry: (-entry[1], entry[0]))
        categories = list(_FIXED_CATEGORIES)
        for name, _count in ranked[:_MAX_GENRE_CATEGORIES]:
            categories.append(
                BrowseCategory(
                    slug=_slugify(name),
                    label=name,
                    movie_genre_id=movie_by_name.get(name.casefold()),
                    tv_genre_id=tv_by_name.get(name.casefold()),
                )
            )
        return tuple(categories)

    async def browse(
        self,
        query: BrowseQuery,
        *,
        category: BrowseCategory | None = None,
        page_size: int = 24,
    ) -> CatalogPage:
        """Browse library titles; ``category`` may come from the streaming catalog."""

        if category is None:
            categories = await self.categories(query.media_type)
            category = next(
                (candidate for candidate in categories if candidate.slug == query.category),
                categories[0],
            )
        page = self.repository.browse(query, category, page_size=page_size)
        return await self._add_genre_names(page)

    async def _genres(
        self, media_type: BrowseMediaType
    ) -> tuple[tuple[Genre, ...], tuple[Genre, ...]]:
        movie_genres: tuple[Genre, ...] = ()
        tv_genres: tuple[Genre, ...] = ()
        try:
            if media_type != "tv":
                movie_genres = await self.catalog.genres("movie")
            if media_type != "movie":
                tv_genres = await self.catalog.genres("tv")
        except TMDBError:
            return (), ()
        return movie_genres, tv_genres

    async def _add_genre_names(self, page: CatalogPage) -> CatalogPage:
        if not page.items:
            return page
        movie_genres, tv_genres = await self._genres("all")
        names = {
            "movie": {genre.id: genre.name for genre in movie_genres},
            "tv": {genre.id: genre.name for genre in tv_genres},
        }
        items = tuple(
            replace(
                item,
                genres=tuple(
                    names[item.media_type][genre_id]
                    for genre_id in item.genre_ids
                    if genre_id in names[item.media_type]
                )[:2],
            )
            for item in page.items
        )
        return replace(page, items=items)


def _draft_from_match(
    group_key: str,
    files: tuple[ScannedFile, ...],
    match: CatalogItem | None,
) -> ItemDraft:
    first = files[0]
    if match is None:
        return ItemDraft(
            group_key=group_key,
            media_type=first.media_type,
            title=first.titles[0],
            files=files,
            year=first.year,
        )
    try:
        year: int | None = int(match.year)
    except ValueError:
        year = first.year
    return ItemDraft(
        group_key=group_key,
        media_type=first.media_type,
        title=match.title,
        files=files,
        year=year,
        tmdb_id=match.id,
        release_date=match.release_date,
        overview=match.overview,
        rating=match.rating,
        poster_path=match.poster_path,
        genre_ids=match.genre_ids,
        popularity=match.popularity,
    )


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _slugify(value: str) -> str:
    return "-".join(value.casefold().replace("&", "and").split())

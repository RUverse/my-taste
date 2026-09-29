"""Walk a library folder and group video files into movie or show entries."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from mytaste.catalog.models import MediaType
from mytaste.library.models import ScannedFile
from mytaste.library.parser import (
    is_video_file,
    parse_episode_name,
    parse_name,
    season_from_folder,
    strip_extension,
)

_SKIPPED_DIRECTORIES = frozenset(
    {"$recycle.bin", "system volume information", "@eadir", "lost+found", "found.000"}
)
_SAMPLE_LIMIT_BYTES = 300 * 1024 * 1024


class LibraryUnavailableError(RuntimeError):
    """Raised when a library folder cannot be read."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    files: tuple[ScannedFile, ...]
    skipped_directories: int = 0


def scan_directory(root: Path, media_type: MediaType) -> ScanResult:
    """Synchronously walk ``root`` and return every recognizable video file."""

    resolved = Path(root)
    if not resolved.is_dir():
        raise LibraryUnavailableError(f"{resolved} is not an available folder")
    try:
        next(os.scandir(resolved), None)
    except OSError as exc:
        raise LibraryUnavailableError(f"{resolved} cannot be read: {exc.strerror}") from exc

    files: list[ScannedFile] = []
    skipped = 0
    for directory, subdirectories, names in os.walk(resolved, followlinks=False):
        kept: list[str] = []
        for name in subdirectories:
            if name.startswith(".") or name.casefold() in _SKIPPED_DIRECTORIES:
                skipped += 1
            else:
                kept.append(name)
        subdirectories[:] = sorted(kept)
        current = Path(directory)
        for name in sorted(names):
            if name.startswith(".") or not is_video_file(name):
                continue
            path = current / name
            try:
                stat = path.stat()
            except OSError:
                continue
            if "sample" in name.casefold().split(".") and stat.st_size < _SAMPLE_LIMIT_BYTES:
                continue
            modified_at = datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(
                timespec="seconds"
            )
            if media_type == "movie":
                scanned = _movie_file(path, resolved, stat.st_size, modified_at)
            else:
                scanned = _episode_file(path, resolved, stat.st_size, modified_at)
            files.append(scanned)
    return ScanResult(files=tuple(files), skipped_directories=skipped)


def _movie_file(path: Path, root: Path, size: int, modified_at: str) -> ScannedFile:
    from_file = parse_name(strip_extension(path.name))
    titles = [from_file.title]
    year = from_file.year
    if path.parent != root:
        from_folder = parse_name(path.parent.name)
        if from_folder.year is not None and year is None:
            titles.insert(0, from_folder.title)
            year = from_folder.year
        elif from_folder.year is not None and from_folder.year == year:
            titles.append(from_folder.title)
    ordered = _unique_titles(titles, strip_extension(path.name))
    return ScannedFile(
        path=str(path),
        size=size,
        modified_at=modified_at,
        group_key=f"movie:{ordered[0].casefold()}:{year or ''}",
        titles=ordered,
        year=year,
        media_type="movie",
    )


def _episode_file(path: Path, root: Path, size: int, modified_at: str) -> ScannedFile:
    from_file = parse_episode_name(strip_extension(path.name))
    relative = path.relative_to(root)
    show_folder = relative.parts[0] if len(relative.parts) > 1 else None
    season = from_file.season
    episode = from_file.episode
    year = from_file.year
    titles = [from_file.title]
    if show_folder is not None:
        from_folder = parse_name(show_folder)
        if from_folder.title:
            titles.insert(0, from_folder.title)
        if from_folder.year is not None:
            year = from_folder.year
        if season is None:
            for part in reversed(relative.parts[1:-1]):
                folder_season = season_from_folder(part)
                if folder_season is not None:
                    season = folder_season
                    break
            if season is None:
                season = season_from_folder(show_folder)
        group_key = f"tv:folder:{show_folder.casefold()}"
    else:
        group_key = f"tv:{from_file.title.casefold()}:{year or ''}"
    return ScannedFile(
        path=str(path),
        size=size,
        modified_at=modified_at,
        group_key=group_key,
        titles=_unique_titles(titles, strip_extension(path.name)),
        year=year,
        season=season,
        episode=episode,
        media_type="tv",
    )


def _unique_titles(titles: list[str], fallback: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    for title in titles:
        cleaned = title.strip()
        if cleaned and cleaned.casefold() not in seen:
            seen[cleaned.casefold()] = cleaned
    return tuple(seen.values()) or (fallback,)

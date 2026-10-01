from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from mytaste.catalog.models import CatalogItem, MediaType

ScanState = Literal["idle", "scanning", "error"]

_MEDIA_LABELS: dict[MediaType, str] = {"movie": "Movies", "tv": "TV Shows"}


@dataclass(frozen=True, slots=True)
class ParsedName:
    """Title information extracted from a file or folder name."""

    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """A single video file found in a library folder."""

    path: str
    size: int
    modified_at: str
    group_key: str
    titles: tuple[str, ...]
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    media_type: MediaType = "movie"


@dataclass(frozen=True, slots=True)
class LibraryFolder:
    """One scanned folder of a library; each folder holds either movies or shows."""

    id: int
    path: str
    media_type: MediaType

    @property
    def media_label(self) -> str:
        return _MEDIA_LABELS[self.media_type]


@dataclass(frozen=True, slots=True)
class Library:
    id: int
    name: str
    created_at: str
    folders: tuple[LibraryFolder, ...] = ()
    last_scanned_at: str | None = None
    last_error: str | None = None
    item_count: int = 0
    file_count: int = 0
    unmatched_count: int = 0
    movie_count: int = 0
    show_count: int = 0
    episode_count: int = 0

    @property
    def media_types(self) -> tuple[MediaType, ...]:
        present = {folder.media_type for folder in self.folders}
        return tuple(media_type for media_type in ("movie", "tv") if media_type in present)

    @property
    def media_label(self) -> str:
        return " & ".join(_MEDIA_LABELS[media_type] for media_type in self.media_types)


@dataclass(frozen=True, slots=True)
class LibraryStatus:
    library_id: int
    state: ScanState = "idle"
    message: str = ""
    scanned_files: int = 0
    matched_items: int = 0
    total_items: int = 0


@dataclass(frozen=True, slots=True)
class LibraryItem:
    """A movie or show in a library, grouped from one or more files."""

    id: int
    library_id: int
    media_type: MediaType
    group_key: str
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    release_date: str = ""
    overview: str = ""
    rating: float = 0.0
    poster_path: str | None = None
    genre_ids: tuple[int, ...] = ()
    popularity: float = 0.0
    added_at: str = ""
    file_count: int = 0
    season_count: int = 0
    episode_count: int = 0
    first_file_id: int | None = None

    @property
    def matched(self) -> bool:
        return self.tmdb_id is not None

    @property
    def summary(self) -> str:
        if self.media_type == "tv":
            parts = []
            if self.season_count:
                parts.append(f"{self.season_count} season{'s' if self.season_count != 1 else ''}")
            if self.episode_count:
                parts.append(
                    f"{self.episode_count} episode{'s' if self.episode_count != 1 else ''}"
                )
            return " · ".join(parts)
        if self.file_count > 1:
            return f"{self.file_count} files"
        return ""

    def to_catalog_item(self) -> CatalogItem:
        release_date = self.release_date or (f"{self.year:04d}-01-01" if self.year else "")
        return CatalogItem(
            id=self.tmdb_id or 0,
            media_type=self.media_type,
            title=self.title,
            release_date=release_date,
            overview=self.overview,
            rating=self.rating,
            poster_path=self.poster_path,
            genre_ids=self.genre_ids,
            popularity=self.popularity,
            library_summary=self.summary,
            in_library=True,
            local_file_id=self.first_file_id,
        )


@dataclass(frozen=True, slots=True)
class LibraryFile:
    """An indexed video file with its title. Paths are internal and never sent to clients."""

    id: int
    item_id: int
    library_id: int
    path: str
    size: int = 0
    modified_at: str = ""
    season: int | None = None
    episode: int | None = None
    media_type: MediaType = "movie"
    tmdb_id: int | None = None
    title: str = ""
    year: int | None = None
    poster_path: str | None = None

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

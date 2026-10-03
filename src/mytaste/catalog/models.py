from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from mytaste.catalog.filters import TitleFilters

MediaType = Literal["movie", "tv"]
BrowseMediaType = Literal["all", "movie", "tv"]
SortKey = Literal["popularity", "release", "rating", "title", "added"]

SORT_KEYS: tuple[SortKey, ...] = ("popularity", "release", "rating", "title", "added")
_ASCENDING_BY_DEFAULT: frozenset[SortKey] = frozenset({"title"})


def natural_descending(sort: SortKey) -> bool:
    """Whether a sort normally runs high to low: newest, most popular, best rated first."""

    return sort not in _ASCENDING_BY_DEFAULT


@dataclass(frozen=True, slots=True)
class Provider:
    id: int
    name: str
    logo_path: str | None = None
    priority: int = 9999

    @property
    def logo_url(self) -> str | None:
        if not self.logo_path:
            return None
        return f"https://image.tmdb.org/t/p/w92{self.logo_path}"


@dataclass(frozen=True, slots=True)
class Region:
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class Genre:
    id: int
    name: str
    media_type: MediaType


@dataclass(frozen=True, slots=True)
class BrowseCategory:
    """A smart collection resolved for browsing: an optional genre and release window over
    every title, its default sort, and optionally only its ``limit`` most popular titles."""

    slug: str
    label: str
    movie_genre_id: int | None = None
    tv_genre_id: int | None = None
    sort: SortKey = "popularity"
    released_within_days: int | None = None
    limit: int | None = None
    icon: str = ""
    description: str = ""

    @property
    def has_genre(self) -> bool:
        return self.movie_genre_id is not None or self.tv_genre_id is not None

    def genre_id_for(self, media_type: MediaType) -> int | None:
        return self.movie_genre_id if media_type == "movie" else self.tv_genre_id


@dataclass(frozen=True, slots=True)
class CatalogItem:
    id: int
    media_type: MediaType
    title: str
    release_date: str
    overview: str
    rating: float
    poster_path: str | None = None
    genre_ids: tuple[int, ...] = ()
    genres: tuple[str, ...] = ()
    popularity: float = 0.0
    library_summary: str = ""
    in_library: bool = False
    local_file_id: int | None = None

    @property
    def year(self) -> str:
        return self.release_date[:4] if len(self.release_date) >= 4 else "—"

    @property
    def media_label(self) -> str:
        return "Movie" if self.media_type == "movie" else "Series"

    @property
    def poster_url(self) -> str | None:
        if not self.poster_path:
            return None
        return f"https://image.tmdb.org/t/p/w500{self.poster_path}"

    @property
    def detail_url(self) -> str:
        namespace = "movie" if self.media_type == "movie" else "tv"
        return f"https://www.themoviedb.org/{namespace}/{self.id}"


@dataclass(frozen=True, slots=True)
class CastMember:
    id: int
    name: str
    character: str = ""
    profile_path: str | None = None

    @property
    def profile_url(self) -> str | None:
        if not self.profile_path:
            return None
        return f"https://image.tmdb.org/t/p/w185{self.profile_path}"


@dataclass(frozen=True, slots=True)
class MediaDetails:
    id: int
    media_type: MediaType
    title: str
    release_date: str
    overview: str
    rating: float
    runtime_minutes: int | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    genres: tuple[str, ...] = ()
    cast: tuple[CastMember, ...] = ()
    trailer_key: str | None = None
    # A movie's directors or a series' creators.
    directed_by: tuple[str, ...] = ()
    last_air_date: str = ""
    genre_ids: tuple[int, ...] = ()
    popularity: float = 0.0

    @property
    def year(self) -> str:
        return self.release_date[:4] if len(self.release_date) >= 4 else "—"

    @property
    def years(self) -> str:
        """The release year, or a series' first and latest air years such as "2021–2025"."""

        last = self.last_air_date[:4] if len(self.last_air_date) >= 4 else ""
        if self.year != "—" and last > self.year:
            return f"{self.year}–{last}"
        return self.year

    @property
    def poster_url(self) -> str | None:
        if not self.poster_path:
            return None
        return f"https://image.tmdb.org/t/p/w500{self.poster_path}"

    @property
    def backdrop_url(self) -> str | None:
        if not self.backdrop_path:
            return None
        return f"https://image.tmdb.org/t/p/original{self.backdrop_path}"

    @property
    def detail_url(self) -> str:
        namespace = "movie" if self.media_type == "movie" else "tv"
        return f"https://www.themoviedb.org/{namespace}/{self.id}"

    @property
    def trailer_url(self) -> str | None:
        if not self.trailer_key:
            return None
        return f"https://www.youtube.com/watch?v={self.trailer_key}"


@dataclass(frozen=True, slots=True)
class WatchLink:
    """A provider's page for one title, keyed by the provider name used on that page."""

    provider_id: int
    provider_name: str
    url: str


@dataclass(frozen=True, slots=True)
class WatchOption:
    """An enabled service that carries a title, and where to watch it there.

    ``direct`` is false when no provider page was found and ``url`` falls back to TMDB's list
    of offers for the title.
    """

    provider: Provider
    url: str
    direct: bool = True


@dataclass(frozen=True, slots=True)
class Episode:
    season_number: int
    episode_number: int
    name: str
    overview: str = ""
    air_date: str = ""
    runtime_minutes: int | None = None
    still_path: str | None = None

    @property
    def still_url(self) -> str | None:
        if not self.still_path:
            return None
        return f"https://image.tmdb.org/t/p/w300{self.still_path}"


@dataclass(frozen=True, slots=True)
class Season:
    season_number: int
    name: str
    episodes: tuple[Episode, ...] = ()


@dataclass(frozen=True, slots=True)
class Catalog:
    movies: tuple[CatalogItem, ...]
    shows: tuple[CatalogItem, ...]


@dataclass(frozen=True, slots=True)
class BrowseQuery:
    media_type: BrowseMediaType = "all"
    category: str = "latest"
    search: str = ""
    provider_ids: tuple[int, ...] = ()
    year_from: int | None = None
    year_to: int | None = None
    minimum_rating: float | None = None
    include_unrated: bool = False
    page: int = 1
    library_ids: tuple[int, ...] = ()
    # ``None`` keeps the collection's default sort and that sort's natural direction.
    sort: SortKey | None = None
    descending: bool | None = None
    # Rows to split the titles into (see ``catalog.grouping``); empty shows one grid.
    group: str = ""
    filters: TitleFilters = field(default_factory=TitleFilters)

    def sort_for(self, default: SortKey) -> tuple[SortKey, bool]:
        sort = self.sort or default
        return sort, natural_descending(sort) if self.descending is None else self.descending


def catalog_sort_key(sort: SortKey, descending: bool) -> Callable[[CatalogItem], tuple[Any, ...]]:
    """Return a key that orders titles the way TMDB and the library queries do.

    The key is meant for ``max`` (or ``sorted(reverse=True)``) when ``descending`` and ``min``
    otherwise; undated and unrated titles go last either way. ``added`` has no meaning for
    catalog titles and falls back to popularity.
    """

    if sort == "release":
        return lambda item: (
            bool(item.release_date) == descending,
            item.release_date,
            item.popularity,
        )
    if sort == "rating":
        return lambda item: ((item.rating > 0) == descending, item.rating, item.popularity)
    if sort == "title":
        return lambda item: (item.title.casefold(), item.release_date)
    return lambda item: (item.popularity, item.rating)


@dataclass(frozen=True, slots=True)
class CatalogPage:
    items: tuple[CatalogItem, ...]
    page: int = 1
    total_pages: int = 1
    total_results: int = 0

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MediaType = Literal["movie", "tv"]
BrowseMediaType = Literal["all", "movie", "tv"]


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
    slug: str
    label: str
    movie_genre_id: int | None = None
    tv_genre_id: int | None = None

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

    @property
    def year(self) -> str:
        return self.release_date[:4] if len(self.release_date) >= 4 else "—"

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


@dataclass(frozen=True, slots=True)
class CatalogPage:
    items: tuple[CatalogItem, ...]
    page: int = 1
    total_pages: int = 1
    total_results: int = 0

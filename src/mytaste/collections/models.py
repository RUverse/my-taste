from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from mytaste.catalog.models import (
    BrowseCategory,
    BrowseMediaType,
    CatalogItem,
    Genre,
    MediaType,
    SortKey,
)
from mytaste.games.models import Game

HOME_COLLECTION = "popular"

ICONS: tuple[str, ...] = (
    "bookmark",
    "heart",
    "star",
    "flame",
    "sparkle",
    "clock",
    "eye",
    "film",
    "tv",
    "smile",
    "ghost",
    "rocket",
    "trophy",
    "gift",
    "folder",
    "list",
)


@dataclass(frozen=True, slots=True)
class SmartCollection:
    """A predefined collection: a filter over every title with a default sort.

    Genre collections name the TMDB genre per media type; a collection without a genre for a
    media type is hidden in that mode (TMDB has no Thriller or Romance genre for series).
    """

    slug: str
    name: str
    sort: SortKey = "popularity"
    icon: str = ""
    description: str = ""
    movie_genre: str | None = None
    tv_genre: str | None = None
    released_within_days: int | None = None
    limit: int | None = None

    @property
    def has_genre(self) -> bool:
        return self.movie_genre is not None or self.tv_genre is not None

    def supports(self, media_type: BrowseMediaType) -> bool:
        if not self.has_genre:
            return True
        if media_type == "movie":
            return self.movie_genre is not None
        if media_type == "tv":
            return self.tv_genre is not None
        return True

    def resolve(
        self,
        media_type: BrowseMediaType,
        movie_genres: Iterable[Genre],
        tv_genres: Iterable[Genre],
    ) -> BrowseCategory | None:
        """Bind genre names to TMDB ids; ``None`` when nothing matches for ``media_type``."""

        movie_id = tv_id = None
        if self.has_genre:
            if media_type != "tv":
                movie_id = _genre_id(movie_genres, self.movie_genre)
            if media_type != "movie":
                tv_id = _genre_id(tv_genres, self.tv_genre)
            if movie_id is None and tv_id is None:
                return None
        return BrowseCategory(
            slug=self.slug,
            label=self.name,
            movie_genre_id=movie_id,
            tv_genre_id=tv_id,
            sort=self.sort,
            released_within_days=self.released_within_days,
            limit=self.limit,
            icon=self.icon,
            description=self.description,
        )


SMART_COLLECTIONS: tuple[SmartCollection, ...] = (
    SmartCollection(
        "popular",
        "Popular",
        icon="flame",
        description="The most popular movies and shows right now.",
        limit=200,
    ),
    SmartCollection(
        "latest",
        "Latest",
        sort="release",
        icon="sparkle",
        description="Released in the past year, newest first.",
        released_within_days=365,
    ),
    SmartCollection("comedy", "Comedy", movie_genre="Comedy", tv_genre="Comedy"),
    SmartCollection("thriller", "Thriller", movie_genre="Thriller"),
    SmartCollection("romance", "Romance", movie_genre="Romance"),
    SmartCollection("drama", "Drama", movie_genre="Drama", tv_genre="Drama"),
    SmartCollection("action", "Action", movie_genre="Action", tv_genre="Action & Adventure"),
    SmartCollection(
        "science-fiction", "Sci-Fi", movie_genre="Science Fiction", tv_genre="Sci-Fi & Fantasy"
    ),
    SmartCollection("animation", "Animation", movie_genre="Animation", tv_genre="Animation"),
    SmartCollection("crime", "Crime", movie_genre="Crime", tv_genre="Crime"),
    SmartCollection("mystery", "Mystery", movie_genre="Mystery", tv_genre="Mystery"),
    SmartCollection("horror", "Horror", movie_genre="Horror"),
    SmartCollection(
        "documentary", "Documentary", movie_genre="Documentary", tv_genre="Documentary"
    ),
    SmartCollection("family", "Family", movie_genre="Family", tv_genre="Family"),
)


def smart_collection(slug: str) -> SmartCollection | None:
    return next((item for item in SMART_COLLECTIONS if item.slug == slug), None)


def smart_categories(
    media_type: BrowseMediaType,
    movie_genres: Iterable[Genre] = (),
    tv_genres: Iterable[Genre] = (),
) -> tuple[BrowseCategory, ...]:
    """Resolve the predefined collections that have titles for ``media_type``."""

    movie_genres = tuple(movie_genres)
    tv_genres = tuple(tv_genres)
    return tuple(
        category
        for collection in SMART_COLLECTIONS
        if collection.supports(media_type)
        and (category := collection.resolve(media_type, movie_genres, tv_genres)) is not None
    )


def _genre_id(genres: Iterable[Genre], name: str | None) -> int | None:
    if name is None:
        return None
    wanted = name.casefold()
    return next((genre.id for genre in genres if genre.name.casefold() == wanted), None)


@dataclass(frozen=True, slots=True)
class Collection:
    """A user's list of titles, such as the Watchlist."""

    id: int
    name: str
    created_at: str
    description: str = ""
    icon: str = ""
    default_sort: SortKey = "added"
    position: int = 0
    item_count: int = 0
    portable_id: str = ""

    @property
    def key(self) -> str:
        return str(self.id)


@dataclass(frozen=True, slots=True)
class CollectionItem:
    """A title in a user collection, with the metadata needed to sort and filter it offline."""

    media_type: MediaType
    tmdb_id: int
    added_at: str
    title: str
    release_date: str = ""
    overview: str = ""
    rating: float = 0.0
    popularity: float = 0.0
    poster_path: str | None = None
    genre_ids: tuple[int, ...] = ()
    genres: tuple[str, ...] = ()
    sequence: int = 0

    @property
    def portable_id(self) -> str:
        return f"{self.media_type}-tmdb-{self.tmdb_id}"

    @property
    def key(self) -> tuple[MediaType, int]:
        return (self.media_type, self.tmdb_id)

    def to_catalog_item(self) -> CatalogItem:
        return CatalogItem(
            id=self.tmdb_id,
            media_type=self.media_type,
            title=self.title,
            release_date=self.release_date,
            overview=self.overview,
            rating=self.rating,
            poster_path=self.poster_path,
            genre_ids=self.genre_ids,
            genres=self.genres[:2],
            popularity=self.popularity,
        )


@dataclass(frozen=True, slots=True)
class SavedGame:
    """A game in a user collection, with the snapshot taken when it was last saved."""

    game: Game
    added_at: str
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class GameEntry:
    """A saved game on a collection page, shaped like a ``CatalogItem`` so it sorts and groups
    among movies and series. ``id`` 0 marks it as no TMDB title, so TMDB lookups skip it."""

    game: Game
    added_at: str = ""
    sequence: int = 0
    id: int = 0
    media_type: str = "game"
    popularity: float = 0.0
    in_library: bool = False
    local_file_id: None = None
    library_summary: str = ""

    @property
    def title(self) -> str:
        return self.game.title

    @property
    def release_date(self) -> str:
        return self.game.release_date

    @property
    def rating(self) -> float:
        return self.game.score or 0.0

    @property
    def genres(self) -> tuple[str, ...]:
        return self.game.genres

    @property
    def year(self) -> str:
        return self.game.year

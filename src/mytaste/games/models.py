from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

PLANS = {
    "ultimate": "Ultimate",
    "premium": "Premium",
    "essential": "Essential",
    "pc": "PC Game Pass",
}
PLATFORMS = {"pc": "Windows PC", "console": "Xbox console", "cloud": "Cloud"}
SOURCES = {"gamepass": "Xbox Game Pass", "steam": "Steam"}

# Collections keep their URL key; ``group`` decides which source can rank them. Game Pass and
# "yours" collections are small, fully known sets; Steam collections page through Steam's own
# ranking, which games only on Xbox cannot join.
GAME_PASS_COLLECTIONS = {
    "popular": "Popular on Game Pass",
    "recent": "Recently added to Game Pass",
    "coming": "Coming to Game Pass",
    "leaving": "Leaving Game Pass",
}
STEAM_COLLECTIONS = {
    "steam-popular": "Most played on Steam",
    "steam-sellers": "Top sellers on Steam",
    "steam-trending": "New and trending on Steam",
    "steam-rated": "Top rated on Steam",
    "steam-coming": "Coming soon to Steam",
}
OWNED_COLLECTIONS = {
    "mine": "My games",
    "played": "Recently played",
    "most-played": "Most played",
}
COLLECTIONS = {
    "all": "All games",
    **OWNED_COLLECTIONS,
    **STEAM_COLLECTIONS,
    **GAME_PASS_COLLECTIONS,
}
COLLECTION_ICONS = {
    "all": "list",
    "mine": "heart",
    "played": "clock",
    "most-played": "flame",
    "steam-popular": "flame",
    "steam-sellers": "star",
    "steam-trending": "sparkle",
    "steam-rated": "trophy",
    "steam-coming": "clock",
    "popular": "flame",
    "recent": "sparkle",
    "coming": "clock",
    "leaving": "clock",
}
SORTS = {
    "catalog": "Collection order",
    "title": "Title",
    "release": "Release date",
    "rating": "Rating",
    "played": "Last played",
    "playtime": "Time played",
}
DESCENDING_SORTS = frozenset({"release", "rating", "played", "playtime"})

# One genre list for every store, so a filter gives the same answer for a game wherever it
# comes from. The names are the Microsoft Store's categories; Steam games get them from their
# store tags (tag ids from IStoreService/GetTagList). Classics and Other have no Steam tag.
GENRE_TAGS: dict[str, tuple[int, ...]] = {
    "Action & adventure": (19, 21),
    "Role playing": (122,),
    "Shooter": (1774, 1663, 3814),
    "Simulation": (599,),
    "Strategy": (9,),
    "Family & kids": (5350, 4162),
    "Platformer": (1625,),
    "Racing & flying": (699, 15045, 1644),
    "Sports": (701,),
    "Puzzle & trivia": (1664, 10437),
    "Card & board": (1666, 1770),
    "Fighting": (1743,),
    "Music": (1621,),
    "Multi-player Online Battle Arena": (1718,),
    "Classics": (),
    "Other": (),
}

PRODUCT_ID = re.compile(r"^[A-Z0-9]{12}$")
REGION = re.compile(r"^[A-Z]{2}$")
GAME_KEY = re.compile(r"^(?:steam-[1-9][0-9]{0,9}|xbox-[A-Z0-9]{12})$")


def product_id(value: str) -> str:
    value = value.upper()
    if not PRODUCT_ID.fullmatch(value):
        raise ValueError("Invalid Xbox product ID")
    return value


def steam_appid(value: object) -> int:
    try:
        number = int(str(value))
    except ValueError:
        raise ValueError("Invalid Steam app ID") from None
    if not 0 < number < 10**10:
        raise ValueError("Invalid Steam app ID")
    return number


def game_key(*, steam: int = 0, xbox: str = "") -> str:
    """A game's key in URLs and saved collections: Steam's app ID when known, else Xbox's."""

    if steam:
        return f"steam-{steam}"
    if xbox:
        return f"xbox-{xbox}"
    raise ValueError("A game needs a Steam or Xbox ID")


def parse_game_key(value: str) -> tuple[str, str]:
    """Split ``steam-730`` or ``xbox-9NBLGGH4R315``; a bare Store ID is an older Xbox link."""

    if PRODUCT_ID.fullmatch(value.upper()):
        return "xbox", value.upper()
    store, _, raw = value.partition("-")
    if store == "xbox":
        return "xbox", product_id(raw)
    if store == "steam":
        return "steam", str(steam_appid(raw))
    raise ValueError("Unknown game")


def web_url(value: Any) -> str | None:
    """Provider data can supply protocol-relative assets, but never executable URLs."""
    if not isinstance(value, str):
        return None
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    return value


def genres_for_tags(tag_ids: tuple[int, ...] | list[int]) -> tuple[str, ...]:
    present = set(tag_ids)
    return tuple(name for name, tags in GENRE_TAGS.items() if present.intersection(tags))


@dataclass(frozen=True, slots=True)
class Game:
    """One game, possibly sold in several stores.

    ``id`` is the key from :func:`game_key`. Store facts keep their own scale: ``rating`` is
    the Microsoft Store's five stars and ``steam_score`` Steam's percentage of positive
    reviews. ``game_pass``, ``owned``, and play times describe the user's access, not the game.
    """

    id: str
    title: str
    overview: str = ""
    release_date: str = ""
    poster_url: str | None = None
    backdrop_url: str | None = None
    screenshots: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    developers: tuple[str, ...] = ()
    publisher: str = ""
    rating: float | None = None
    rating_count: int = 0
    metadata_complete: bool = True
    xbox_id: str = ""
    steam_appid: int = 0
    steam_score: int | None = None
    steam_reviews: int = 0
    steam_review_label: str = ""
    tags: tuple[str, ...] = ()
    tag_ids: tuple[int, ...] = ()
    coming_soon: str = ""
    game_pass: bool = False
    owned: bool = False
    playtime: int = 0
    last_played: int = 0

    @property
    def portable_id(self) -> str:
        return f"game-{self.id}"

    @property
    def year(self) -> str:
        return self.release_date[:4] or "—"

    @property
    def xbox_url(self) -> str:
        return f"https://www.xbox.com/games/store/-/{self.xbox_id}" if self.xbox_id else ""

    @property
    def steam_url(self) -> str:
        return f"https://store.steampowered.com/app/{self.steam_appid}/" if self.steam_appid else ""

    @property
    def store_url(self) -> str:
        return self.steam_url or self.xbox_url

    @property
    def score(self) -> float | None:
        """A comparable 0–10 score for sorting: Steam's reviews, else the Store's stars."""

        if self.steam_score is not None and self.steam_reviews:
            return self.steam_score / 10
        if self.rating is not None:
            return self.rating * 2
        return None

    @property
    def hours_played(self) -> str:
        if not self.playtime:
            return ""
        hours = self.playtime / 60
        return f"{hours:.1f} h" if hours < 10 else f"{hours:.0f} h"

    @property
    def last_played_date(self) -> str:
        if not self.last_played:
            return ""
        return datetime.fromtimestamp(self.last_played, UTC).strftime("%Y-%m-%d")

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Game:
        known = {field.name for field in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        for key in ("screenshots", "genres", "developers", "tags", "tag_ids"):
            values[key] = tuple(values.get(key, ()))
        return cls(**values)


@dataclass(frozen=True, slots=True)
class GameQuery:
    plan: str = "ultimate"
    platform: str = "pc"
    collection: str = "all"
    search: str = ""
    genre: str = ""
    sort: str = "catalog"
    page: int = 1
    order: str = ""
    sources: tuple[str, ...] = ("gamepass", "steam")

    def validate(self) -> None:
        if self.plan not in PLANS or self.platform not in PLATFORMS:
            raise ValueError("Choose a supported Game Pass plan and platform")
        if self.plan == "pc" and self.platform != "pc":
            raise ValueError(
                "PC Game Pass supports Windows PC. Choose another plan for console or cloud."
            )
        if self.collection not in COLLECTIONS or self.sort not in SORTS:
            raise ValueError("Choose a supported collection and sort order")
        if self.order not in {"", "asc", "desc"}:
            raise ValueError("Choose ascending or descending order")
        if not 1 <= self.page <= 1000:
            raise ValueError("Page must be between 1 and 1000")
        if len(self.search) > 200 or len(self.genre) > 100:
            raise ValueError("Search or genre is too long")
        if any(source not in SOURCES for source in self.sources):
            raise ValueError("Choose Game Pass, Steam, or both")

    @property
    def descending(self) -> bool:
        natural = self.sort in DESCENDING_SORTS
        return natural if not self.order else self.order == "desc"


@dataclass(frozen=True, slots=True)
class GamePage:
    items: tuple[Game, ...] = ()
    total: int = 0
    pages: int = 1
    genres: tuple[str, ...] = ()
    checked_at: float | None = None
    stale: bool = False
    incomplete: bool = False
    notices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SteamAccount:
    """The Steam account connected to MyTaste and what its owned-games list last said."""

    steam_id: str
    persona: str = ""
    avatar_url: str = ""
    profile_url: str = ""
    owned: tuple[OwnedGame, ...] = ()
    owned_checked_at: float | None = None
    owned_status: str = ""

    @property
    def owned_ids(self) -> frozenset[int]:
        return frozenset(game.appid for game in self.owned)


@dataclass(frozen=True, slots=True)
class OwnedGame:
    appid: int
    name: str = ""
    playtime: int = 0
    last_played: int = 0

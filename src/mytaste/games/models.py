from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

PLANS = {
    "ultimate": "Ultimate",
    "premium": "Premium",
    "essential": "Essential",
    "pc": "PC Game Pass",
}
PLATFORMS = {"pc": "Windows PC", "console": "Xbox console", "cloud": "Cloud"}
COLLECTIONS = {
    "all": "All games",
    "popular": "Popular",
    "recent": "Recently added",
    "coming": "Coming soon",
    "leaving": "Leaving soon",
}
SORTS = {
    "catalog": "Collection order",
    "title": "Title",
    "release": "Release date",
    "rating": "Store rating",
}
PRODUCT_ID = re.compile(r"^[A-Z0-9]{12}$")
REGION = re.compile(r"^[A-Z]{2}$")


def product_id(value: str) -> str:
    value = value.upper()
    if not PRODUCT_ID.fullmatch(value):
        raise ValueError("Invalid Xbox product ID")
    return value


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


@dataclass(frozen=True, slots=True)
class Game:
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

    @property
    def portable_id(self) -> str:
        return f"game-xbox-{self.id}"

    @property
    def year(self) -> str:
        return self.release_date[:4] or "—"

    @property
    def store_url(self) -> str:
        return f"https://www.xbox.com/games/store/-/{self.id}"

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Game:
        values = dict(data)
        for key in ("screenshots", "genres", "developers"):
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

    def validate(self) -> None:
        if self.plan not in PLANS or self.platform not in PLATFORMS:
            raise ValueError("Choose a supported Game Pass plan and platform")
        if self.plan == "pc" and self.platform != "pc":
            raise ValueError(
                "PC Game Pass supports Windows PC. Choose another plan for console or cloud."
            )
        if self.collection not in COLLECTIONS or self.sort not in SORTS:
            raise ValueError("Choose a supported collection and sort order")
        if not 1 <= self.page <= 1000:
            raise ValueError("Page must be between 1 and 1000")
        if len(self.search) > 200 or len(self.genre) > 100:
            raise ValueError("Search or genre is too long")


@dataclass(frozen=True, slots=True)
class GamePage:
    items: tuple[Game, ...] = ()
    total: int = 0
    pages: int = 1
    genres: tuple[str, ...] = ()
    checked_at: float | None = None
    stale: bool = False
    incomplete: bool = False

"""Find the Steam app for each Xbox product, so a game sold in both stores is listed once.

Sources, in order: IsThereAnyDeal's public ID lookups (Microsoft Store → ITAD → Steam),
Wikidata's Steam (P1733) and Microsoft Store (P5885) properties, and finally Steam's search
with the same normalized title and release year. Findings are kept in ``game_links`` and
rechecked weekly; "not the same game" corrections are kept for good.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from mytaste.games.http import StoreError, StoreHTTP
from mytaste.games.models import Game
from mytaste.storage.game_links import GameLink, GameLinkRepository

logger = logging.getLogger(__name__)

ITAD = "https://api.isthereanydeal.com"
_MICROSOFT_SHOP, _STEAM_SHOP = 48, 61
WIKIDATA = "https://query.wikidata.org/sparql"
_APP = re.compile(r"^app/([1-9][0-9]{0,9})$")
_MARKS = re.compile(r"[™®©]")
_SUFFIXES = re.compile(
    r"(?:^|\s)(?:standard edition|windows edition|for windows(?: 10)?|windows(?: 10)?|"
    r"pc edition|pc|game preview)$"
)


def normalize_title(title: str) -> str:
    """A title without store decorations: marks, accents, punctuation, and edition or
    platform suffixes such as "Standard Edition" or "(PC)"."""

    text = unicodedata.normalize("NFKD", _MARKS.sub("", title))
    text = "".join(char for char in text if not unicodedata.combining(char)).casefold()
    text = re.sub(r"[\W_]+", " ", text).strip()
    while (shorter := _SUFFIXES.sub("", text).strip()) != text:
        text = shorter
    return text


class LinkError(StoreError):
    """A matching source could not be read."""


class GameLinker:
    def __init__(
        self,
        repository: GameLinkRepository,
        steam: Any,
        *,
        timeout: float = 15,
        client: httpx.AsyncClient | None = None,
        ttl: float = 7 * 86_400,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.repository = repository
        self.steam = steam
        self.http = StoreHTTP("Game matching", LinkError, timeout=timeout, client=client)
        self.ttl = ttl
        self.clock = clock
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self.http.close()

    def _fresh(self, xbox_ids: Sequence[str]) -> dict[str, GameLink]:
        now = self.clock()
        return {
            key: link
            for key, link in self.repository.get(xbox_ids).items()
            if link.source == "user" or 0 <= now - link.checked_at <= self.ttl
        }

    async def links(self, games: Sequence[Game], region: str, language: str) -> dict[str, int]:
        """Map each Xbox game's product ID to its Steam app ID, or 0 when there is none."""

        xbox = {game.xbox_id: game for game in games if game.xbox_id}
        known = self._fresh(list(xbox))
        if len(known) < len(xbox):
            # One lookup at a time: concurrent pages would otherwise ask for the same games.
            async with self._lock:
                known = self._fresh(list(xbox))
                missing = [game for key, game in xbox.items() if key not in known]
                if missing:
                    found = await self._find(missing, region, language)
                    self.repository.save(found)
                    known.update({link.xbox_id: link for link in found})
        return {key: link.steam_appid for key, link in known.items()}

    def xbox_for(self, appid: int) -> list[str]:
        return [link.xbox_id for link in self.repository.for_steam(appid)]

    def unlink(self, xbox_ids: Sequence[str]) -> None:
        now = self.clock()
        self.repository.save(GameLink(key, 0, "user", now) for key in xbox_ids)

    async def _find(self, games: list[Game], region: str, language: str) -> list[GameLink]:
        now = self.clock()
        found: dict[str, tuple[int, str]] = {}
        complete = True
        for name, source in (("itad", self._itad), ("wikidata", self._wikidata)):
            remaining = [game.xbox_id for game in games if game.xbox_id not in found]
            if not remaining:
                break
            try:
                found.update(
                    {key: (appid, name) for key, appid in (await source(remaining)).items()}
                )
            except (StoreError, KeyError, TypeError, ValueError, AttributeError):
                logger.warning("Matching Xbox games through %s failed", name, exc_info=True)
                complete = False
        remaining_games = [game for game in games if game.xbox_id not in found]
        if remaining_games and self.steam is not None:
            try:
                found.update(
                    {
                        key: (appid, "title")
                        for key, appid in (
                            await self._titles(remaining_games, region, language)
                        ).items()
                    }
                )
            except StoreError:
                logger.warning("Matching Xbox games by title failed", exc_info=True)
                complete = False
        links = [GameLink(key, appid, source, now) for key, (appid, source) in found.items()]
        if complete:
            # Only a full search may conclude that a game is not on Steam.
            links += [
                GameLink(game.xbox_id, 0, "none", now)
                for game in remaining_games
                if game.xbox_id not in found
            ]
        return links

    async def _itad(self, xbox_ids: list[str]) -> dict[str, int]:
        # The Microsoft Store lookup is case-sensitive and ITAD stores both spellings.
        variants = list(dict.fromkeys(value for key in xbox_ids for value in (key, key.lower())))
        games = await self.http.json(
            "POST", f"{ITAD}/lookup/id/shop/{_MICROSOFT_SHOP}/v1", json=variants
        )
        itad = {
            key.upper(): value
            for key, value in games.items()
            if isinstance(value, str) and key.upper() in xbox_ids
        }
        if not itad:
            return {}
        apps = await self.http.json(
            "POST", f"{ITAD}/lookup/shop/{_STEAM_SHOP}/id/v1", json=list(set(itad.values()))
        )
        steam: dict[str, int] = {}
        for key, value in itad.items():
            for entry in apps.get(value) or ():
                if isinstance(entry, str) and (match := _APP.fullmatch(entry)):
                    steam[key] = int(match.group(1))
                    break
        return steam

    async def _wikidata(self, xbox_ids: list[str]) -> dict[str, int]:
        values = " ".join(f'"{key.lower()}" "{key}"' for key in xbox_ids)
        query = (
            "SELECT ?ms ?steam WHERE { VALUES ?ms { " + values + " } "
            "?game wdt:P5885 ?ms ; wdt:P1733 ?steam . }"
        )
        data = await self.http.json(
            "POST",
            WIKIDATA,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json"},
        )
        found: dict[str, int] = {}
        for row in data["results"]["bindings"]:
            key, appid = row["ms"]["value"].upper(), row["steam"]["value"]
            if key in xbox_ids and appid.isdigit() and int(appid) > 0:
                found.setdefault(key, int(appid))
        return found

    async def _titles(self, games: list[Game], region: str, language: str) -> dict[str, int]:
        async def match(game: Game) -> tuple[str, int]:
            wanted = normalize_title(game.title)
            if not wanted:
                return game.xbox_id, 0
            for candidate in await self.steam.search(game.title, region, language, limit=10):
                if normalize_title(candidate.title) != wanted:
                    continue
                years = {game.release_date[:4], candidate.release_date[:4]}
                if "" in years or len(years) == 1 or _adjacent(years):
                    return game.xbox_id, candidate.steam_appid
            return game.xbox_id, 0

        results = await asyncio.gather(*(match(game) for game in games))
        return {key: appid for key, appid in results if appid}


def _adjacent(years: set[str]) -> bool:
    first, second = sorted(years)
    return first.isdigit() and second.isdigit() and int(second) - int(first) <= 1

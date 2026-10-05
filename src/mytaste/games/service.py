from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from mytaste.games.filters import game_matches, game_sort_key
from mytaste.games.gamepass import GamePassClient, GamePassError
from mytaste.games.http import StoreError
from mytaste.games.models import (
    DESCENDING_SORTS,
    GAME_PASS_COLLECTIONS,
    GENRE_TAGS,
    OWNED_COLLECTIONS,
    REGION,
    STEAM_COLLECTIONS,
    Game,
    GamePage,
    GameQuery,
    OwnedGame,
    SteamAccount,
    game_key,
    parse_game_key,
)
from mytaste.games.steam import (
    ITEMS_PER_REQUEST,
    SORT_MOST_PLAYED,
    SORT_NEWEST,
    SORT_TITLE,
    SORT_TOP_RATED,
    SORT_TOP_SELLERS,
    SORT_TRENDING,
    SteamError,
    SteamProfileError,
)
from mytaste.storage.gamepass_cache import CachedBatch, GamePassCache

logger = logging.getLogger(__name__)

# Steam collections page through one of Steam's rankings: (sort, coming soon only).
_RANKINGS = {
    "steam-popular": (SORT_MOST_PLAYED, False),
    "steam-sellers": (SORT_TOP_SELLERS, False),
    "steam-trending": (SORT_TRENDING, False),
    "steam-rated": (SORT_TOP_RATED, False),
    "steam-coming": (SORT_TOP_SELLERS, True),
}
# All games pages through the whole store in one of the two orders Steam and every other
# source agree on.
_STORE_ORDERS = {"release": SORT_NEWEST, "title": SORT_TITLE}
_STREAM_CHUNK = 100
_MAX_RANKED = 2400  # 100 pages, like merged movie pages.
_STREAM_TTL = 600


def steam_store_collection(query: GameQuery, steam_available: bool) -> bool:
    """Whether the collection pages through Steam's store rather than a known set of games."""

    if not steam_available or query.search:
        return False
    if query.collection in STEAM_COLLECTIONS:
        return True
    return query.collection == "all" and "steam" in query.sources


def allowed_sorts(query: GameQuery, steam_available: bool) -> tuple[str, ...]:
    if query.collection in STEAM_COLLECTIONS and not query.search:
        return ("catalog",)
    if steam_store_collection(query, steam_available):
        return ("release", "title")
    if query.collection in OWNED_COLLECTIONS:
        return ("played", "playtime", "title", "release", "rating")
    return ("catalog", "title", "release", "rating")


def default_sort(query: GameQuery, steam_available: bool, steam_connected: bool = True) -> str:
    if query.search:
        return "catalog"
    if query.collection == "most-played":
        return "playtime"
    if query.collection == "mine" and not steam_connected:
        return "title"  # Nothing has been played until Steam is connected.
    return allowed_sorts(query, steam_available)[0]


def can_reverse(query: GameQuery, steam_available: bool) -> bool:
    """Known sets sort either way; Steam's rankings and store orders only run one way."""

    return not steam_store_collection(query, steam_available)


def merge_stores(steam: Game, xbox: Game) -> Game:
    """One game from its Steam and Xbox listings: Steam's details, Xbox's Store rating and
    Game Pass availability. Genres follow Steam's tags so filters agree with Steam's own."""

    return replace(
        steam,
        xbox_id=xbox.xbox_id,
        rating=xbox.rating,
        rating_count=xbox.rating_count,
        game_pass=steam.game_pass or xbox.game_pass,
        overview=steam.overview or xbox.overview,
        release_date=steam.release_date or xbox.release_date,
        poster_url=steam.poster_url or xbox.poster_url,
        backdrop_url=steam.backdrop_url or xbox.backdrop_url,
        screenshots=steam.screenshots or xbox.screenshots,
        genres=steam.genres if steam.tag_ids else xbox.genres,
        developers=steam.developers or xbox.developers,
        publisher=steam.publisher or xbox.publisher,
    )


@dataclass
class _Access:
    """What the user can play: Game Pass's current list and the owned Steam games."""

    game_pass: dict[str, Game] = field(default_factory=dict)  # by Xbox product ID
    game_pass_steam: dict[int, Game] = field(default_factory=dict)  # by Steam app ID
    owned: dict[int, OwnedGame] = field(default_factory=dict)

    def apply(self, game: Game) -> Game:
        twin = self.game_pass.get(game.xbox_id) or self.game_pass_steam.get(game.steam_appid)
        if twin is not None and not game.game_pass:
            game = merge_stores(game, twin) if game.steam_appid and not game.xbox_id else game
            game = replace(game, game_pass=True, xbox_id=game.xbox_id or twin.xbox_id)
        played = self.owned.get(game.steam_appid)
        if played is not None:
            game = replace(
                game, owned=True, playtime=played.playtime, last_played=played.last_played
            )
        return game


class _SteamStream:
    """The first games of one Steam ranking, fetched a chunk at a time as pages need them."""

    def __init__(
        self,
        fetch: Callable[[int, int], Awaitable[tuple[list[Game], int, int]]],
        accept: Callable[[Game], bool],
    ) -> None:
        self.fetch = fetch
        self.accept = accept
        self.items: list[Game] = []
        self.offset = 0
        self.total = 0
        self.exhausted = False
        self.created = time.monotonic()
        self._lock = asyncio.Lock()

    async def fill(self, wanted: int) -> None:
        async with self._lock:
            while len(self.items) < wanted and not self.exhausted:
                games, total, covered = await self.fetch(self.offset, _STREAM_CHUNK)
                self.total = total
                self.offset += covered
                self.items.extend(game for game in games if self.accept(game))
                if covered == 0 or self.offset >= min(total, _MAX_RANKED):
                    self.exhausted = True


class GamesService:
    def __init__(
        self,
        client: GamePassClient,
        cache: GamePassCache,
        *,
        steam: Any | None = None,
        linker: Any | None = None,
        accounts: Any | None = None,
        catalog_ttl: float = 7200,
        metadata_ttl: float = 86_400,
        owned_ttl: float = 6 * 3600,
        page_size: int = 24,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.cache = cache
        self.steam = steam
        self.linker = linker
        self.accounts = accounts
        self.catalog_ttl = catalog_ttl
        self.metadata_ttl = metadata_ttl
        self.owned_ttl = owned_ttl
        self.page_size = page_size
        self.clock = clock
        self._streams: dict[tuple[Any, ...], _SteamStream] = {}
        self._owned_lock = asyncio.Lock()

    @property
    def steam_available(self) -> bool:
        return self.steam is not None

    @property
    def owned_games_available(self) -> bool:
        return self.steam is not None and bool(getattr(self.steam, "api_key", None))

    @staticmethod
    def _key(*values: str) -> str:
        return json.dumps(["gamepass-v2", *values], separators=(",", ":"))

    @staticmethod
    def _steam_key(*values: str) -> str:
        return json.dumps(["steam-v1", *values], separators=(",", ":"))

    # Game Pass --------------------------------------------------------------------------

    async def _ids(
        self, query: GameQuery, region: str, language: str, collection: str
    ) -> CachedBatch:
        key = self._key("membership", region, language, query.plan, query.platform, collection)

        async def load(_keys: list[str]) -> dict[str, Any]:
            return {key: await self.client.ids(query, region, language, collection)}

        return await self.cache.get_many([key], self.catalog_ttl, load)

    async def _products(self, ids: list[str], region: str, language: str) -> list[CachedBatch]:
        async def batch(selected: list[str]) -> CachedBatch:
            by_key = {self._key("metadata", region, language, key): key for key in selected}

            async def load(keys: list[str]) -> dict[str, Any]:
                games = await self.client.products([by_key[key] for key in keys], region, language)
                return {
                    key: games[value].payload() if games[value] is not None else {"excluded": True}
                    for key, value in by_key.items()
                    if key in keys and value in games
                }

            return await self.cache.get_many(list(by_key), self.metadata_ttl, load)

        return await _gather([batch(ids[start : start + 20]) for start in range(0, len(ids), 20)])

    async def _membership(
        self, query: GameQuery, region: str, language: str, collection: str
    ) -> tuple[list[str], list[CachedBatch]]:
        batches = []
        requested = await self._ids(query, region, language, collection)
        batches.append(requested)
        ids = list(next(iter(requested.values.values())))
        if collection not in {"all", "coming"}:
            eligible = await self._ids(query, region, language, "all")
            batches.append(eligible)
            allowed = set(next(iter(eligible.values.values())))
            ids = [key for key in ids if key in allowed]
        if query.platform == "cloud":
            cloud = await self._ids(query, region, language, "cloud")
            batches.append(cloud)
            allowed = set(next(iter(cloud.values.values())))
            ids = [key for key in ids if key in allowed]
        return ids, batches

    async def _game_pass(
        self,
        query: GameQuery,
        region: str,
        language: str,
        collection: str,
        memo: dict[Any, Any] | None = None,
    ) -> tuple[list[Game], list[CachedBatch]]:
        """A Game Pass list in Microsoft's order, merged with Steam where the game is on both.

        ``memo`` shares lists within one request, so building a page never fetches a list twice
        even with caching disabled.
        """

        key = ("game-pass", region, language, query.plan, query.platform, collection)
        if memo is not None and key in memo:
            return memo[key]
        result = await self._load_game_pass(query, region, language, collection)
        if memo is not None:
            memo[key] = result
        return result

    async def _load_game_pass(
        self, query: GameQuery, region: str, language: str, collection: str
    ) -> tuple[list[Game], list[CachedBatch]]:
        ids, batches = await self._membership(query, region, language, collection)
        metadata = await self._products(ids, region, language)
        batches.extend(metadata)
        values = {key: value for batch in metadata for key, value in batch.values.items()}
        current = collection != "coming"
        games = []
        for key in ids:
            value = values.get(self._key("metadata", region, language, key))
            if value and value.get("excluded"):
                continue
            game = (
                Game.from_payload(value)
                if value
                else Game(
                    game_key(xbox=key), f"Xbox game {key}", xbox_id=key, metadata_complete=False
                )
            )
            games.append(replace(game, game_pass=current))
        if self.steam is None or self.linker is None:
            return games, batches
        try:
            links = await self.linker.links(
                [game for game in games if game.metadata_complete], region, language
            )
            steam = await self._steam_items(
                sorted({appid for appid in links.values() if appid}), region, language
            )
        except StoreError:
            logger.warning("Matching Game Pass games with Steam failed", exc_info=True)
            return games, batches
        merged: dict[str, Game] = {}
        for game in games:
            twin = steam.get(links.get(game.xbox_id, 0))
            if twin is not None:
                game = merge_stores(twin, game)
            # Editions that are one Steam game appear once, where the first one was listed.
            merged.setdefault(game.id, game)
        return list(merged.values()), batches

    # Steam ------------------------------------------------------------------------------

    async def _steam_items(
        self, appids: Sequence[int], region: str, language: str
    ) -> dict[int, Game]:
        """Steam's details for apps, from the cache when fresh; apps that are no game are left
        out."""

        found: dict[int, Game] = {}
        if self.steam is None or not appids:
            return found

        async def batch(selected: Sequence[int]) -> CachedBatch:
            by_key = {
                self._steam_key("item", region, language, str(appid)): appid for appid in selected
            }

            async def load(keys: list[str]) -> dict[str, Any]:
                games = await self.steam.items([by_key[key] for key in keys], region, language)
                return {
                    key: games[by_key[key]].payload()
                    if games.get(by_key[key]) is not None
                    else {"excluded": True}
                    for key in keys
                }

            return await self.cache.get_many(list(by_key), self.metadata_ttl, load)

        batches = await _gather(
            [
                batch(appids[start : start + ITEMS_PER_REQUEST])
                for start in range(0, len(appids), ITEMS_PER_REQUEST)
            ]
        )
        for cached in batches:
            for value in cached.values.values():
                if value and not value.get("excluded"):
                    game = Game.from_payload(value)
                    found[game.steam_appid] = game
        return found

    def _stream(
        self, region: str, language: str, sort: int, coming: bool, genre: str
    ) -> _SteamStream:
        key = (region, language, sort, coming, genre)
        now = time.monotonic()
        for stale in [
            name for name, stream in self._streams.items() if now - stream.created > _STREAM_TTL
        ]:
            del self._streams[stale]
        stream = self._streams.get(key)
        if stream is None:
            tags = GENRE_TAGS.get(genre, ()) if genre else ()

            async def fetch(start: int, count: int) -> tuple[list[Game], int, int]:
                if genre and not tags:
                    return [], 0, 0  # No Steam tag means no Steam game has the genre.
                return await self.steam.query(
                    region,
                    language,
                    sort=sort,
                    start=start,
                    count=count,
                    tag_ids=tags,
                    coming_soon=coming,
                )

            # Steam matches a tag anywhere on a game; keep only games whose listed tags
            # give them the genre, the rule every other source is checked with.
            stream = _SteamStream(fetch, lambda game: not genre or genre in game.genres)
            if len(self._streams) >= 16:
                del self._streams[min(self._streams, key=lambda name: self._streams[name].created)]
            self._streams[key] = stream
        return stream

    # Steam account ----------------------------------------------------------------------

    def account(self) -> SteamAccount | None:
        return self.accounts.get() if self.accounts is not None else None

    async def connect(self, steam_id: str) -> SteamAccount:
        if self.steam is None or self.accounts is None:
            raise SteamProfileError("Steam is not available on this server.")
        profile = {"persona": "", "avatar_url": "", "profile_url": ""}
        if self.owned_games_available:
            profile = await self.steam.summary(steam_id)
        account = self.accounts.connect(SteamAccount(steam_id, **profile))
        await self.refresh_owned(force=True)
        return self.account() or account

    async def connect_profile(self, value: str) -> SteamAccount:
        if self.steam is None:
            raise SteamProfileError("Steam is not available on this server.")
        return await self.connect(await self.steam.resolve(value))

    def disconnect(self) -> bool:
        self._streams.clear()
        return self.accounts.disconnect() if self.accounts is not None else False

    async def refresh_owned(self, *, force: bool = False) -> SteamAccount | None:
        """Bring the owned-games list up to date; on failure the previous list stays."""

        account = self.account()
        if account is None or not self.owned_games_available:
            return account
        fresh = (
            account.owned_checked_at is not None
            and 0 <= self.clock() - account.owned_checked_at < self.owned_ttl
        )
        if fresh and not force:
            return account
        async with self._owned_lock:
            account = self.account()
            if account is None:
                return None
            if (
                not force
                and account.owned_checked_at is not None
                and (0 <= self.clock() - account.owned_checked_at < self.owned_ttl)
            ):
                return account
            try:
                owned = await self.steam.owned(account.steam_id)
                profile = await self.steam.summary(account.steam_id)
            except (StoreError, SteamProfileError):
                logger.warning("Refreshing owned Steam games failed", exc_info=True)
                if force:
                    raise
                return account
            status = "private" if owned is None else "ok"
            self.accounts.save_owned(
                account.steam_id, owned or account.owned, self.clock(), status, profile
            )
            return self.account()

    async def _owned_games(self, region: str, language: str) -> tuple[list[Game], list[str]]:
        account = await self.refresh_owned()
        if account is None:
            return [], []
        notices = []
        if account.owned_status == "private":
            notices.append("steam-private")
        if not self.owned_games_available:
            notices.append("steam-key")
        details = await self._steam_items([game.appid for game in account.owned], region, language)
        games = []
        for owned in account.owned:
            game = details.get(owned.appid)
            if game is None:
                continue  # Tools, soundtracks, and delisted apps are not browsable games.
            games.append(
                replace(game, owned=True, playtime=owned.playtime, last_played=owned.last_played)
            )
        return games, notices

    # Browsing ---------------------------------------------------------------------------

    async def _access(
        self,
        query: GameQuery,
        region: str,
        language: str,
        notices: list[str],
        memo: dict[Any, Any] | None = None,
    ) -> _Access:
        access = _Access()
        await self.refresh_owned()
        try:
            games, _batches = await self._game_pass(query, region, language, "all", memo)
        except GamePassError:
            notices.append("gamepass-unavailable")
            games = []
        for game in games:
            if game.xbox_id:
                access.game_pass[game.xbox_id] = game
            if game.steam_appid:
                access.game_pass_steam[game.steam_appid] = game
        account = self.account()
        if account is not None:
            access.owned = {game.appid: game for game in account.owned}
        return access

    async def browse(self, region: str, language: str, query: GameQuery) -> GamePage:
        query.validate()
        if not REGION.fullmatch(region):
            raise ValueError("Choose a two-letter country code")
        steam = self.steam_available
        if not steam and (
            query.collection in STEAM_COLLECTIONS or query.collection in OWNED_COLLECTIONS
        ):
            raise ValueError("Steam is not available on this server")
        if query.sort not in allowed_sorts(query, steam):
            query = replace(
                query, sort=default_sort(query, steam, self.account() is not None), order=""
            )
        if not can_reverse(query, steam):
            query = replace(query, order="")
        if steam_store_collection(query, steam):
            return await self._browse_steam(region, language, query)
        return await self._browse_known(region, language, query)

    async def _known_games(
        self,
        region: str,
        language: str,
        query: GameQuery,
        notices: list[str],
        memo: dict[Any, Any],
    ) -> tuple[list[Game], list[CachedBatch]]:
        """The games of a collection that is a known set rather than a Steam ranking."""

        collection, sources = query.collection, set(query.sources)
        if collection in GAME_PASS_COLLECTIONS:
            return await self._game_pass(query, region, language, collection, memo)
        if not self.steam_available:
            # Without Steam, All games is Game Pass's list, as before Steam support.
            return await self._game_pass(query, region, language, "all", memo)
        want_game_pass = "gamepass" in sources and collection in {"all", "mine"}
        want_owned = (
            collection in {"played", "most-played"}
            or collection in STEAM_COLLECTIONS
            or "steam" in sources
        )
        games: list[Game] = []
        batches: list[CachedBatch] = []
        if want_game_pass:
            try:
                games, batches = await self._game_pass(query, region, language, "all", memo)
            except GamePassError:
                if not want_owned:
                    raise
                notices.append("gamepass-unavailable")
        if want_owned:
            owned, owned_notices = await self._owned_games(region, language)
            notices.extend(owned_notices)
            if collection == "played":
                return [game for game in owned if game.last_played], batches
            if collection == "most-played":
                return [game for game in owned if game.playtime], batches
            games = _union(games, owned)
        return games, batches

    async def _browse_known(self, region: str, language: str, query: GameQuery) -> GamePage:
        notices: list[str] = []
        memo: dict[Any, Any] = {}
        games, batches = await self._known_games(region, language, query, notices, memo)
        incomplete = any(not game.metadata_complete for game in games)
        if incomplete and (query.search or query.genre or query.sort != "catalog"):
            raise GamePassError(
                "Some game details are unavailable. "
                "Try Collection order or refresh before filtering."
            )
        access = (
            await self._access(query, region, language, notices, memo)
            if self.steam_available
            else None
        )
        if access is not None:
            games = [access.apply(game) for game in games]
        genres = tuple(sorted({genre for game in games for genre in game.genres}, key=str.casefold))
        games = [game for game in games if game_matches(game, query)]
        if (
            query.search
            and self.steam is not None
            and (
                query.collection in STEAM_COLLECTIONS
                or (query.collection == "all" and "steam" in query.sources)
            )
        ):
            found = await self.steam.search(query.search, region, language)
            extra = [access.apply(game) if access else game for game in found]
            # Relevance is Steam's order; the user's own matches follow it.
            games = _union(extra, games)
            games = [game for game in games if game_matches(game, replace(query, search=""))]
            genres = tuple(
                sorted({*genres, *(g for game in games for g in game.genres)}, key=str.casefold)
            )
        if query.sort != "catalog":
            games.sort(key=lambda game: game_sort_key(game, query.sort))
        natural = "desc" if query.sort in DESCENDING_SORTS else "asc"
        if query.order and query.order != natural:
            games.reverse()
        start = (query.page - 1) * self.page_size
        return GamePage(
            tuple(games[start : start + self.page_size]),
            len(games),
            max(1, math.ceil(len(games) / self.page_size)),
            genres,
            min((batch.checked_at for batch in batches), default=self.clock()),
            any(batch.stale for batch in batches),
            incomplete,
            tuple(dict.fromkeys(notices)),
        )

    async def _browse_steam(self, region: str, language: str, query: GameQuery) -> GamePage:
        notices: list[str] = []
        memo: dict[Any, Any] = {}
        access = await self._access(query, region, language, notices, memo)
        wanted = query.page * self.page_size
        if query.collection in _RANKINGS:
            sort, coming = _RANKINGS[query.collection]
            stream = self._stream(region, language, sort, coming, query.genre)
            await stream.fill(wanted)
            items = stream.items[:wanted]
            total = len(stream.items) if stream.exhausted else min(stream.total, _MAX_RANKED)
        else:
            stream = self._stream(region, language, _STORE_ORDERS[query.sort], False, query.genre)
            known, _batches = await self._known_games(
                region, language, replace(query, collection="mine"), notices, memo
            )
            known = [access.apply(game) for game in known if game_matches(game, query)]
            known.sort(key=lambda game: game_sort_key(game, query.sort))
            items = await _merge(
                stream, known, lambda game: game_sort_key(game, query.sort), wanted
            )
            store_only = sum(1 for game in known if not game.steam_appid)
            total = (
                len(items)
                if stream.exhausted and len(items) < wanted
                else min(stream.total, _MAX_RANKED) + store_only
            )
        page = [access.apply(game) for game in items[(query.page - 1) * self.page_size :]]
        return GamePage(
            tuple(page[: self.page_size]),
            total,
            max(1, min(math.ceil(total / self.page_size), _MAX_RANKED // self.page_size)),
            tuple(GENRE_TAGS),
            self.clock(),
            notices=tuple(dict.fromkeys(notices)),
        )

    # Details ----------------------------------------------------------------------------

    async def details(
        self,
        key: str,
        region: str,
        language: str,
        *,
        plan: str = "ultimate",
        platform: str = "pc",
    ) -> Game:
        store, value = parse_game_key(key)
        xbox: Game | None = None
        steam: Game | None = None
        if store == "xbox":
            xbox = await self._xbox_game(value, region, language)
            if self.linker is not None and self.steam is not None:
                try:
                    appid = (await self.linker.links([xbox], region, language)).get(value, 0)
                except StoreError:
                    appid = 0
                if appid:
                    steam = await self._steam_details(appid, region, language)
        else:
            if self.steam is None:
                raise SteamError("Steam is not available on this server.")
            steam = await self._steam_details(int(value), region, language)
            if steam is None:
                raise SteamError("Steam has no game with that ID.")
            for xbox_id in self.linker.xbox_for(steam.steam_appid) if self.linker else ():
                try:
                    xbox = await self._xbox_game(xbox_id, region, language)
                    break
                except GamePassError:
                    continue
        game = merge_stores(steam, xbox) if steam and xbox else steam or xbox
        assert game is not None
        query = GameQuery(plan=plan, platform=platform)
        if game.xbox_id:
            try:
                ids, _batches = await self._membership(query, region, language, "all")
                in_plan = set(ids)
                linked = (
                    set(self.linker.xbox_for(game.steam_appid))
                    if self.linker and game.steam_appid
                    else set()
                )
                game = replace(game, game_pass=bool(in_plan & {game.xbox_id, *linked}))
            except GamePassError:
                pass
        account = await self.refresh_owned()
        owned = {entry.appid: entry for entry in account.owned} if account else {}
        if game.steam_appid in owned:
            entry = owned[game.steam_appid]
            game = replace(game, owned=True, playtime=entry.playtime, last_played=entry.last_played)
        return game

    async def _xbox_game(self, xbox_id: str, region: str, language: str) -> Game:
        batches = await self._products([xbox_id], region, language)
        value = batches[0].values.get(self._key("metadata", region, language, xbox_id))
        if not value or value.get("excluded"):
            raise GamePassError("Game details are unavailable.")
        return Game.from_payload(value)

    async def _steam_details(self, appid: int, region: str, language: str) -> Game | None:
        key = self._steam_key("details", region, language, str(appid))

        async def load(_keys: list[str]) -> dict[str, Any]:
            games = await self.steam.items([appid], region, language, screenshots=True)
            game = games.get(appid)
            return {key: game.payload() if game is not None else {"excluded": True}}

        value = (await self.cache.get_many([key], self.metadata_ttl, load)).values.get(key)
        return Game.from_payload(value) if value and not value.get("excluded") else None

    async def unlink(self, key: str) -> list[str]:
        """Record that a merged game's Xbox and Steam listings are different games."""

        store, value = parse_game_key(key)
        if self.linker is None:
            return []
        xbox_ids = [value] if store == "xbox" else self.linker.xbox_for(int(value))
        self.linker.unlink(xbox_ids)
        self._streams.clear()
        return xbox_ids

    async def playable(
        self, region: str, language: str, query: GameQuery, *, game_pass: bool, steam: bool
    ) -> list[Game]:
        """The games the user can play, most popular first, for mixing with movies and series.

        Game Pass games follow Microsoft's popular list, then the rest of the plan's catalog
        (listed A–Z) by number of reviews; Steam library games not on Game Pass follow their
        number of Steam reviews. The two lists alternate, so both stores reach the top. Games
        without details are left out.
        """

        if not REGION.fullmatch(region):
            return []
        memo: dict[Any, Any] = {}
        listed: list[Game] = []
        if game_pass:
            popular, _ = await self._game_pass(query, region, language, "popular", memo)
            catalog, _ = await self._game_pass(query, region, language, "all", memo)
            ranked_ids = {game.id for game in popular}
            listed = _union(
                popular,
                sorted(
                    (game for game in catalog if game.id not in ranked_ids),
                    key=lambda game: -_reviews(game),
                ),
            )
        owned: list[Game] = []
        if steam and self.steam_available:
            owned, _notices = await self._owned_games(region, language)
        on_game_pass = {game.id for game in listed}
        merged = [game for game in _union(listed, owned) if game.metadata_complete]
        library = sorted(
            (game for game in merged if game.id not in on_game_pass),
            key=lambda game: -_reviews(game),
        )
        ranked = [game for game in merged if game.id in on_game_pass]
        mixed: list[Game] = []
        for index in range(max(len(ranked), len(library))):
            mixed.extend(source[index] for source in (ranked, library) if index < len(source))
        return mixed

    async def access(
        self, games: Sequence[Game], region: str, language: str, query: GameQuery
    ) -> list[Game]:
        """Mark which of ``games`` are on Game Pass for ``query``'s plan or owned on Steam."""

        if not region or not REGION.fullmatch(region):
            return [replace(game, game_pass=False) for game in games]
        access = await self._access(query, region, language, [])
        marked = []
        for game in games:
            game = replace(game, game_pass=False, owned=False, playtime=0, last_played=0)
            # Saved games may have been saved before a Steam match was found.
            if game.xbox_id and not game.steam_appid:
                twin = access.game_pass.get(game.xbox_id)
                if twin is not None and twin.steam_appid:
                    game = replace(game, steam_appid=twin.steam_appid)
            marked.append(access.apply(game))
        return marked

    async def close(self) -> None:
        await self.cache.close()
        await self.client.close()
        if self.steam is not None:
            await self.steam.close()
        if self.linker is not None:
            await self.linker.close()


def _reviews(game: Game) -> int:
    """How many people reviewed a game: a stand-in for popularity where no ranking exists."""

    return game.steam_reviews or game.rating_count or 0


def _union(first: Sequence[Game], second: Sequence[Game]) -> list[Game]:
    """Both lists in order, each game once; a later copy adds what the first copy lacks."""

    merged: dict[str, Game] = {}
    for game in [*first, *second]:
        current = merged.get(game.id)
        if current is None:
            merged[game.id] = game
        else:
            merged[game.id] = replace(
                current,
                owned=current.owned or game.owned,
                playtime=current.playtime or game.playtime,
                last_played=current.last_played or game.last_played,
                game_pass=current.game_pass or game.game_pass,
                xbox_id=current.xbox_id or game.xbox_id,
                rating=current.rating if current.rating is not None else game.rating,
                rating_count=current.rating_count or game.rating_count,
            )
    return list(merged.values())


async def _merge(
    stream: _SteamStream,
    known: list[Game],
    key: Callable[[Game], tuple[object, ...]],
    wanted: int,
) -> list[Game]:
    """The first ``wanted`` games of Steam's ranking merged with ``known``, sorted by ``key``.

    Steam ranks the same games in the same order, so both lists merge into one order; a game
    in both appears once, as the known copy that carries the user's access.
    """

    by_id = {game.id: game for game in known}
    merged: list[Game] = []
    seen: set[str] = set()
    position = known_position = 0
    while len(merged) < wanted:
        if position >= len(stream.items) and not stream.exhausted:
            await stream.fill(position + 1)
        remote = stream.items[position] if position < len(stream.items) else None
        local = known[known_position] if known_position < len(known) else None
        if remote is None and local is None:
            break
        if local is None or (remote is not None and key(remote) <= key(local)):
            game, position = remote, position + 1
        else:
            game, known_position = local, known_position + 1
        assert game is not None
        if game.id in seen:
            continue
        seen.add(game.id)
        merged.append(by_id.get(game.id, game))
    return merged


async def _gather(awaitables: list[Awaitable[CachedBatch]]) -> list[CachedBatch]:
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

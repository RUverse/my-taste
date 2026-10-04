"""Steam's public store services and, with a Web API key, a player's owned games.

Store browsing needs no key: IStoreQueryService pages through Steam's rankings and
IStoreBrowseService/GetItems describes up to a few hundred apps per request. Owned games and
vanity-name lookups need the instance's Web API key, which never leaves the server.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree

import httpx

from mytaste.games.http import StoreError, StoreHTTP
from mytaste.games.models import Game, OwnedGame, game_key, genres_for_tags, web_url

API = "https://api.steampowered.com"
ASSETS = "https://shared.akamai.steamstatic.com/store_item_assets/"
OPENID = "https://steamcommunity.com/openid/login"
_OPENID_NS = "http://specs.openid.net/auth/2.0"
_CLAIMED_ID = re.compile(r"^https://steamcommunity\.com/openid/id/(7656119\d{10})$")
STEAM_ID = re.compile(r"^7656119\d{10}$")
_VANITY = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
ITEMS_PER_REQUEST = 100

# Steam's EStoreQuerySort values, verified against the live store (they are not documented).
SORT_TITLE = 1
SORT_TOP_SELLERS = 10
SORT_TRENDING = 20
SORT_TOP_RATED = 21
SORT_MOST_PLAYED = 30
SORT_NEWEST = 40

_LANGUAGES = {
    "de": "german",
    "fr": "french",
    "es": "spanish",
    "it": "italian",
    "pt": "portuguese",
    "pt-BR": "brazilian",
    "ru": "russian",
    "ja": "japanese",
    "ko": "koreana",
    "zh-CN": "schinese",
    "zh-TW": "tchinese",
    "pl": "polish",
    "nl": "dutch",
    "sv": "swedish",
    "tr": "turkish",
    "uk": "ukrainian",
}
_DATA_REQUEST = {
    "include_assets": True,
    "include_release": True,
    "include_tag_count": 20,
    "include_reviews": True,
    "include_basic_info": True,
}


class SteamError(StoreError):
    """Steam could not be read completely or reliably."""


class SteamProfileError(ValueError):
    """A Steam profile link, sign-in, or privacy setting the user can fix."""


def steam_language(language: str) -> str:
    return _LANGUAGES.get(language, _LANGUAGES.get(language.split("-")[0], "english"))


def parse_profile(value: str) -> tuple[str, str]:
    """Return ``("id", steam_id)`` or ``("vanity", name)`` for a pasted profile link or name."""

    text = value.strip()
    if STEAM_ID.fullmatch(text):
        return "id", text
    if "/" in text or "." in text:
        parsed = urlparse(text if "://" in text else "https://" + text)
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.hostname in {"steamcommunity.com", "www.steamcommunity.com"} and len(parts) >= 2:
            if parts[0] == "profiles" and STEAM_ID.fullmatch(parts[1]):
                return "id", parts[1]
            if parts[0] == "id" and _VANITY.fullmatch(parts[1]):
                return "vanity", parts[1]
        raise SteamProfileError(
            "Paste a link like steamcommunity.com/id/yourname or steamcommunity.com/profiles/7656…"
        )
    if _VANITY.fullmatch(text):
        return "vanity", text
    raise SteamProfileError("Enter your Steam profile link or custom profile name")


def openid_url(return_to: str, realm: str) -> str:
    """Where to send the browser to sign in through Steam."""

    identifier = f"{_OPENID_NS}/identifier_select"
    return (
        OPENID
        + "?"
        + urlencode(
            {
                "openid.ns": _OPENID_NS,
                "openid.mode": "checkid_setup",
                "openid.return_to": return_to,
                "openid.realm": realm,
                "openid.identity": identifier,
                "openid.claimed_id": identifier,
            }
        )
    )


def normalize_item(item: Mapping[str, Any], tag_names: Mapping[int, str]) -> Game | None:
    """Turn a GetItems/Query store item into a game; ``None`` for anything but a visible game."""

    if item.get("success") != 1 or not item.get("visible", True) or item.get("type", 0) != 0:
        return None
    appid = int(item.get("appid") or item.get("id") or 0)
    title = item.get("name")
    if appid <= 0 or not isinstance(title, str) or not title.strip():
        return None
    assets = item.get("assets") or {}
    info = item.get("basic_info") or {}
    release = item.get("release") or {}
    reviews = (item.get("reviews") or {}).get("summary_filtered") or {}
    tag_ids = tuple(int(tag) for tag in item.get("tagids") or () if isinstance(tag, int))
    weighted = sorted(
        (tag for tag in item.get("tags") or () if isinstance(tag, dict)),
        key=lambda tag: -int(tag.get("weight") or 0),
    )
    screenshots = (item.get("screenshots") or {}).get("all_ages_screenshots") or ()
    released = release.get("steam_release_date")
    coming = bool(release.get("is_coming_soon"))
    exact = not coming or release.get("coming_soon_display") in (None, "date_full")
    release_date = (
        datetime.fromtimestamp(released, UTC).strftime("%Y-%m-%d")
        if isinstance(released, int) and released > 0 and exact
        else ""
    )
    score = reviews.get("percent_positive")
    count = int(reviews.get("review_count") or 0)
    return Game(
        id=game_key(steam=appid),
        steam_appid=appid,
        title=title.strip(),
        overview=str(info.get("short_description") or ""),
        release_date=release_date,
        poster_url=_asset(assets, "library_capsule") or _asset(assets, "header"),
        backdrop_url=_asset(assets, "library_hero") or _asset(assets, "page_background"),
        screenshots=tuple(
            url
            for shot in screenshots
            if isinstance(shot, dict) and (url := web_url(ASSETS + str(shot.get("filename"))))
        ),
        genres=genres_for_tags(tag_ids),
        developers=tuple(
            str(entry["name"]) for entry in info.get("developers") or () if entry.get("name")
        ),
        publisher=", ".join(
            str(entry["name"]) for entry in info.get("publishers") or () if entry.get("name")
        ),
        steam_score=int(score) if isinstance(score, int) and count else None,
        steam_reviews=count,
        steam_review_label=str(reviews.get("review_score_label") or "") if count else "",
        tags=tuple(
            tag_names[tag["tagid"]] for tag in weighted[:8] if tag.get("tagid") in tag_names
        ),
        tag_ids=tag_ids,
        coming_soon=(
            str(release.get("custom_release_date_message") or "Coming soon") if coming else ""
        ),
    )


def _asset(assets: Mapping[str, Any], name: str) -> str | None:
    template, filename = assets.get("asset_url_format"), assets.get(name)
    if not isinstance(template, str) or not isinstance(filename, str) or not filename:
        return None
    if "${FILENAME}" not in template:
        return None
    return web_url(ASSETS + template.replace("${FILENAME}", filename))


class SteamClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 10,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip() or None
        self.http = StoreHTTP("Steam", SteamError, timeout=timeout, client=client)
        self._tags: dict[str, tuple[float, dict[int, str]]] = {}

    async def close(self) -> None:
        await self.http.close()

    async def _service(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        data = await self.http.json(
            "GET", f"{API}/{path}/v1/", params={"input_json": json.dumps(body)}
        )
        response = data.get("response") if isinstance(data, dict) else None
        if not isinstance(response, dict):
            raise SteamError("Steam's store returned an unexpected response.")
        return response

    @staticmethod
    def _context(region: str, language: str) -> dict[str, str]:
        return {"language": steam_language(language), "country_code": region}

    async def tag_names(self, language: str) -> dict[int, str]:
        name = steam_language(language)
        cached = self._tags.get(name)
        if cached and time.monotonic() - cached[0] < 86_400:
            return cached[1]
        response = await self._service("IStoreService/GetTagList", {"language": name})
        tags = {
            int(tag["tagid"]): str(tag["name"])
            for tag in response.get("tags") or ()
            if isinstance(tag, dict) and "tagid" in tag and "name" in tag
        }
        self._tags[name] = (time.monotonic(), tags)
        return tags

    async def _games(self, response: dict[str, Any], language: str) -> list[Game | None]:
        names = await self.tag_names(language)
        try:
            return [
                normalize_item(item, names)
                for item in response.get("store_items") or ()
                if isinstance(item, dict)
            ]
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise SteamError("Steam returned incomplete game details.") from exc

    async def query(
        self,
        region: str,
        language: str,
        *,
        sort: int,
        start: int,
        count: int,
        tag_ids: tuple[int, ...] = (),
        coming_soon: bool = False,
    ) -> tuple[list[Game], int, int]:
        """One slice of a Steam ranking: its games, the ranking's total, and how many ranked
        entries the slice covered (the next slice starts that much further)."""

        filters: dict[str, Any] = {"type_filters": {"include_games": True}}
        filters["coming_soon_only" if coming_soon else "released_only"] = True
        if tag_ids:
            filters["tagids_must_match"] = [{"tagids": list(tag_ids)}]
        response = await self._service(
            "IStoreQueryService/Query",
            {
                "query": {"start": start, "count": count, "sort": sort, "filters": filters},
                "context": self._context(region, language),
                "data_request": _DATA_REQUEST,
            },
        )
        metadata = response.get("metadata") or {}
        total = int(metadata.get("total_matching_records") or 0)
        games = [game for game in await self._games(response, language) if game is not None]
        return games, total, len(response.get("ids") or ())

    async def items(
        self, appids: list[int], region: str, language: str, *, screenshots: bool = False
    ) -> dict[int, Game | None]:
        """Describe up to ``ITEMS_PER_REQUEST`` apps; ``None`` marks an app that is not a game."""

        if not appids:
            return {}
        if len(appids) > ITEMS_PER_REQUEST:
            raise ValueError("Too many Steam apps in one request")
        response = await self._service(
            "IStoreBrowseService/GetItems",
            {
                "ids": [{"appid": appid} for appid in appids],
                "context": self._context(region, language),
                "data_request": {**_DATA_REQUEST, "include_screenshots": screenshots},
            },
        )
        found: dict[int, Game | None] = {}
        names = await self.tag_names(language)
        for item in response.get("store_items") or ():
            if not isinstance(item, dict):
                continue
            appid = item.get("id")
            if isinstance(appid, int) and appid in appids:
                try:
                    found[appid] = normalize_item(item, names)
                except (TypeError, ValueError, KeyError, AttributeError) as exc:
                    raise SteamError("Steam returned incomplete game details.") from exc
        return found

    async def search(self, term: str, region: str, language: str, limit: int = 50) -> list[Game]:
        response = await self._service(
            "IStoreQueryService/SearchSuggestions",
            {
                "context": self._context(region, language),
                "search_term": term,
                "max_results": limit,
                "filters": {"type_filters": {"include_games": True}},
                "data_request": _DATA_REQUEST,
            },
        )
        return [game for game in await self._games(response, language) if game is not None]

    # Accounts ---------------------------------------------------------------------------

    def _key(self) -> str:
        if not self.api_key:
            raise SteamProfileError(
                "Owned games need a Steam Web API key on the server (MYTASTE_STEAM_API_KEY)."
            )
        return self.api_key

    async def resolve(self, value: str) -> str:
        """Return the 64-bit Steam ID for a pasted profile link, custom name, or ID."""

        kind, text = parse_profile(value)
        if kind == "id":
            return text
        if self.api_key:
            data = await self.http.json(
                "GET",
                f"{API}/ISteamUser/ResolveVanityURL/v1/",
                params={"key": self.api_key, "vanityurl": text},
            )
            response = data.get("response") if isinstance(data, dict) else None
            found = response.get("steamid") if isinstance(response, dict) else None
        else:
            # Without a key the profile's public XML still names its ID.
            reply = await self.http.send(
                "GET", f"https://steamcommunity.com/id/{text}/", params={"xml": 1}
            )
            try:
                found = ElementTree.fromstring(reply.content).findtext("steamID64")
            except ElementTree.ParseError:
                found = None
        if not isinstance(found, str) or not STEAM_ID.fullmatch(found):
            raise SteamProfileError(f"No Steam profile is called “{text}”.")
        return found

    async def summary(self, steam_id: str) -> dict[str, str]:
        data = await self.http.json(
            "GET",
            f"{API}/ISteamUser/GetPlayerSummaries/v2/",
            params={"key": self._key(), "steamids": steam_id},
        )
        players = ((data or {}).get("response") or {}).get("players") or []
        if not players or not isinstance(players[0], dict):
            raise SteamProfileError("That Steam profile does not exist.")
        player = players[0]
        return {
            "persona": str(player.get("personaname") or ""),
            "avatar_url": web_url(player.get("avatarfull") or player.get("avatarmedium")) or "",
            "profile_url": web_url(player.get("profileurl")) or "",
        }

    async def owned(self, steam_id: str) -> tuple[OwnedGame, ...] | None:
        """The account's games, or ``None`` when its game details are not public."""

        data = await self.http.json(
            "GET",
            f"{API}/IPlayerService/GetOwnedGames/v1/",
            params={
                "key": self._key(),
                "steamid": steam_id,
                "include_appinfo": 1,
                "include_played_free_games": 1,
            },
        )
        response = data.get("response") if isinstance(data, dict) else None
        if not isinstance(response, dict):
            raise SteamError("Steam returned an unexpected owned-games response.")
        if "games" not in response:
            # Steam answers an empty object for private game details.
            return () if response.get("game_count") == 0 else None
        try:
            return tuple(
                OwnedGame(
                    appid=int(game["appid"]),
                    name=str(game.get("name") or ""),
                    playtime=max(0, int(game.get("playtime_forever") or 0)),
                    last_played=max(0, int(game.get("rtime_last_played") or 0)),
                )
                for game in response["games"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SteamError("Steam returned an unreadable owned-games list.") from exc

    async def verify_openid(self, params: Mapping[str, str], return_to: str) -> str:
        """Check a sign-in reply with Steam itself and return the account's 64-bit ID."""

        if params.get("openid.mode") != "id_res":
            raise SteamProfileError("Steam sign-in was cancelled.")
        claimed = _CLAIMED_ID.fullmatch(params.get("openid.claimed_id", ""))
        if (
            params.get("openid.op_endpoint") != OPENID
            or params.get("openid.return_to") != return_to
            or not claimed
            or params.get("openid.identity") != params.get("openid.claimed_id")
        ):
            raise SteamProfileError("That Steam sign-in could not be verified. Try again.")
        body = {key: value for key, value in params.items() if key.startswith("openid.")}
        body["openid.mode"] = "check_authentication"
        reply = await self.http.send("POST", OPENID, data=body)
        if "is_valid:true" not in reply.text.splitlines():
            raise SteamProfileError("Steam did not confirm the sign-in. Try again.")
        return claimed.group(1)

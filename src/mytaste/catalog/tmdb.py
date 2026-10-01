from __future__ import annotations

import asyncio
import base64
import binascii
import html
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx

from mytaste.catalog.models import (
    CastMember,
    CatalogItem,
    CatalogPage,
    Episode,
    Genre,
    MediaDetails,
    MediaType,
    Provider,
    Region,
    Season,
    WatchLink,
)

_API_BASE_URL = "https://api.themoviedb.org/3"
_WEB_BASE_URL = "https://www.themoviedb.org"
_V3_API_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
_REGION_PATTERN = re.compile(r"^[A-Z]{2}$")
# TMDB's public watch page links each offer through JustWatch's click tracker. The tracker URL
# carries the provider's own title page in ``r`` and the offer context as base64 JSON in ``cx``.
_JUSTWATCH_LINK_PATTERN = re.compile(r'href="(https://click\.justwatch\.com/a\?[^"]+)"')
_SEASONS_PER_REQUEST = 20


class TMDBError(RuntimeError):
    """Raised when TMDB cannot provide a usable response."""


class TMDBClient:
    def __init__(
        self,
        token: str,
        *,
        language: str = "en-US",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.language = language
        self._api_key = token if _V3_API_KEY_PATTERN.fullmatch(token) else None
        headers = {
            "Accept": "application/json",
            "User-Agent": "MyTaste/0.3",
        }
        if self._api_key is None:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.AsyncClient(
            base_url=_API_BASE_URL,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        # The website never receives the API credential.
        self._web_client = httpx.AsyncClient(
            base_url=_WEB_BASE_URL,
            headers={"Accept": "text/html", "User-Agent": "MyTaste/0.3"},
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()
        await self._web_client.aclose()

    async def _get(self, path: str, params: Mapping[str, object] | None = None) -> dict[str, Any]:
        request_params = dict(params or {})
        if self._api_key is not None:
            request_params["api_key"] = self._api_key
        try:
            response = await self._client.get(path, params=request_params)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise TMDBError("TMDB took too long to respond") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 401:
                message = "TMDB rejected the configured API credential"
            elif status == 429:
                message = "TMDB rate limit reached; try again shortly"
            else:
                message = f"TMDB returned HTTP {status}"
            raise TMDBError(message) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise TMDBError("Could not read a response from TMDB") from exc
        if not isinstance(payload, dict):
            raise TMDBError("TMDB returned an unexpected response")
        return cast(dict[str, Any], payload)

    async def _get_optional(
        self, path: str, params: Mapping[str, object] | None = None
    ) -> dict[str, Any]:
        try:
            return await self._get(path, params)
        except TMDBError:
            return {}

    async def regions(self) -> tuple[Region, ...]:
        payload = await self._get(
            "/watch/providers/regions",
            {"language": self.language},
        )
        regions: list[Region] = []
        for raw in _object_list(payload.get("results")):
            code = str(raw.get("iso_3166_1") or "").upper()
            name = str(raw.get("english_name") or raw.get("native_name") or code).strip()
            if len(code) == 2 and name:
                regions.append(Region(code=code, name=name))
        return tuple(sorted(regions, key=lambda region: region.name.casefold()))

    async def providers(self, region: str) -> tuple[Provider, ...]:
        params = {"language": self.language, "watch_region": region}
        movie_payload, tv_payload = await asyncio.gather(
            self._get("/watch/providers/movie", params),
            self._get("/watch/providers/tv", params),
        )
        providers: dict[int, Provider] = {}
        for raw in (
            *_object_list(movie_payload.get("results")),
            *_object_list(tv_payload.get("results")),
        ):
            provider = _provider_from_payload(raw, region)
            if provider is None:
                continue
            current = providers.get(provider.id)
            if current is None or provider.priority < current.priority:
                providers[provider.id] = provider
        return tuple(
            sorted(
                providers.values(),
                key=lambda item: (item.priority, item.name.casefold()),
            )
        )

    async def genres(self, media_type: MediaType) -> tuple[Genre, ...]:
        payload = await self._get(
            f"/genre/{media_type}/list",
            {"language": self.language},
        )
        genres: list[Genre] = []
        for raw in _object_list(payload.get("genres")):
            try:
                genre_id = int(raw["id"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(raw.get("name") or "").strip()
            if genre_id > 0 and name:
                genres.append(Genre(id=genre_id, name=name, media_type=media_type))
        return tuple(genres)

    async def discover(
        self,
        media_type: MediaType,
        region: str,
        provider_ids: Sequence[int],
        *,
        sort_by: str,
        genre_id: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        minimum_rating: float | None = None,
        include_unrated: bool = False,
        page: int = 1,
        today: date | None = None,
    ) -> CatalogPage:
        unique_ids = sorted(set(provider_ids))
        if not unique_ids:
            return CatalogPage(items=(), page=page, total_results=0)

        current_date = today or date.today()
        params: dict[str, object] = {
            "language": self.language,
            "watch_region": region,
            "with_watch_monetization_types": "flatrate",
            "with_watch_providers": "|".join(str(value) for value in unique_ids),
            "include_adult": "false",
            "sort_by": sort_by,
            "page": page,
        }
        if genre_id is not None:
            params["with_genres"] = genre_id
        if minimum_rating is not None and not include_unrated:
            params["vote_average.gte"] = minimum_rating

        date_field = "primary_release_date" if media_type == "movie" else "first_air_date"
        if media_type == "tv":
            params["include_null_first_air_dates"] = "false"
        if year_from is not None:
            params[f"{date_field}.gte"] = f"{year_from:04d}-01-01"
        end_date = current_date
        if year_to is not None:
            end_date = min(end_date, date(year_to, 12, 31))
        params[f"{date_field}.lte"] = end_date.isoformat()

        payload = await self._get(f"/discover/{media_type}", params)
        items = tuple(
            item
            for raw in _object_list(payload.get("results"))
            if (item := _catalog_item_from_payload(raw, media_type)) is not None
            and (
                minimum_rating is None
                or item.rating >= minimum_rating
                or (include_unrated and item.rating == 0)
            )
        )
        return _page_from_payload(payload, items, page)

    async def search(
        self,
        media_type: MediaType,
        query: str,
        region: str,
        *,
        year_from: int | None = None,
        year_to: int | None = None,
        minimum_rating: float | None = None,
        include_unrated: bool = False,
        page: int = 1,
    ) -> CatalogPage:
        params: dict[str, object] = {
            "query": query,
            "language": self.language,
            "include_adult": "false",
            "page": page,
        }
        if media_type == "movie":
            params["region"] = region
        payload = await self._get(f"/search/{media_type}", params)
        items: list[CatalogItem] = []
        for raw in _object_list(payload.get("results")):
            item = _catalog_item_from_payload(raw, media_type)
            if item is None:
                continue
            try:
                release_year = int(item.year)
            except ValueError:
                continue
            if year_from is not None and release_year < year_from:
                continue
            if year_to is not None and release_year > year_to:
                continue
            if (
                minimum_rating is not None
                and item.rating < minimum_rating
                and not (include_unrated and item.rating == 0)
            ):
                continue
            items.append(item)
        return _page_from_payload(payload, tuple(items), page)

    async def lookup(
        self,
        media_type: MediaType,
        query: str,
        *,
        year: int | None = None,
    ) -> tuple[CatalogItem, ...]:
        """Search TMDB for a title, optionally narrowed to a release year."""

        params: dict[str, object] = {
            "query": query,
            "language": self.language,
            "include_adult": "false",
            "page": 1,
        }
        if year is not None:
            year_field = "primary_release_year" if media_type == "movie" else "first_air_date_year"
            params[year_field] = year
        payload = await self._get(f"/search/{media_type}", params)
        return tuple(
            item
            for raw in _object_list(payload.get("results"))
            if (item := _catalog_item_from_payload(raw, media_type)) is not None
        )

    async def watch_provider_ids(
        self,
        media_type: MediaType,
        item_id: int,
        region: str,
    ) -> frozenset[int]:
        payload = await self._get(f"/{media_type}/{item_id}/watch/providers")
        results = payload.get("results")
        if not isinstance(results, dict):
            return frozenset()
        regional = results.get(region)
        if not isinstance(regional, dict):
            return frozenset()
        provider_ids: set[int] = set()
        for raw in _object_list(regional.get("flatrate")):
            try:
                provider_ids.add(int(raw["provider_id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return frozenset(provider_ids)

    async def people(self, media_type: MediaType, item_id: int) -> tuple[str, tuple[str, ...]]:
        if media_type == "movie":
            payload = await self._get(f"/movie/{item_id}/credits", {"language": self.language})
            names = tuple(
                dict.fromkeys(
                    str(raw.get("name") or "").strip()
                    for raw in _object_list(payload.get("crew"))
                    if raw.get("job") == "Director" and raw.get("name")
                )
            )
            return "Director", names[:3]

        payload = await self._get(f"/tv/{item_id}", {"language": self.language})
        names = tuple(
            dict.fromkeys(
                str(raw.get("name") or "").strip()
                for raw in _object_list(payload.get("created_by"))
                if raw.get("name")
            )
        )
        return "Creator", names[:3]

    async def details(self, media_type: MediaType, item_id: int) -> MediaDetails:
        params = {"language": self.language}
        payload, credits, videos = await asyncio.gather(
            self._get(f"/{media_type}/{item_id}", params),
            self._get_optional(f"/{media_type}/{item_id}/credits", params),
            self._get_optional(f"/{media_type}/{item_id}/videos", params),
        )
        payload["credits"] = credits
        payload["videos"] = videos
        details = _media_details_from_payload(payload, media_type)
        if details is None:
            raise TMDBError("TMDB returned incomplete media details")
        return details

    async def watch_links(
        self,
        media_type: MediaType,
        item_id: int,
        region: str,
    ) -> tuple[WatchLink, ...]:
        """Read direct provider links from TMDB's public watch page for a title.

        The API only links to TMDB's own watch page, so this parses that page. It returns an
        empty tuple when the page is unavailable or its markup no longer matches.
        """

        if not _REGION_PATTERN.fullmatch(region):
            return ()
        try:
            response = await self._web_client.get(
                f"/{media_type}/{item_id}/watch", params={"locale": region}
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return ()
        return _watch_links_from_html(response.text)

    async def seasons(self, item_id: int) -> tuple[Season, ...]:
        """Return every season of a series with its episodes, regular seasons first."""

        params = {"language": self.language}
        show = await self._get(f"/tv/{item_id}", params)
        numbers: list[int] = []
        for raw in _object_list(show.get("seasons")):
            try:
                number = int(raw["season_number"])
            except (KeyError, TypeError, ValueError):
                continue
            if number >= 0 and int(raw.get("episode_count") or 0) > 0:
                numbers.append(number)
        chunks = [
            numbers[start : start + _SEASONS_PER_REQUEST]
            for start in range(0, len(numbers), _SEASONS_PER_REQUEST)
        ]
        payloads = await asyncio.gather(
            *(
                self._get(
                    f"/tv/{item_id}",
                    {
                        **params,
                        "append_to_response": ",".join(f"season/{number}" for number in chunk),
                    },
                )
                for chunk in chunks
            )
        )
        seasons: list[Season] = []
        for chunk, payload in zip(chunks, payloads, strict=True):
            for number in chunk:
                raw_season = payload.get(f"season/{number}")
                if isinstance(raw_season, dict):
                    season = _season_from_payload(raw_season, number)
                    if season.episodes:
                        seasons.append(season)
        # Specials (season 0) go last, the way streaming apps list them.
        return tuple(
            sorted(seasons, key=lambda season: (season.season_number == 0, season.season_number))
        )

    async def latest(
        self,
        media_type: MediaType,
        region: str,
        provider_ids: Sequence[int],
        *,
        today: date | None = None,
    ) -> tuple[CatalogItem, ...]:
        sort_by = "primary_release_date.desc" if media_type == "movie" else "first_air_date.desc"
        result = await self.discover(
            media_type,
            region,
            provider_ids,
            sort_by=sort_by,
            today=today,
        )
        return result.items


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _watch_links_from_html(markup: str) -> tuple[WatchLink, ...]:
    links: dict[int, WatchLink] = {}
    subscription: set[int] = set()
    for match in _JUSTWATCH_LINK_PATTERN.finditer(markup):
        query = parse_qs(urlsplit(html.unescape(match.group(1))).query)
        target = _unwrap_affiliate((query.get("r") or [""])[0])
        offer = _justwatch_offer(query.get("cx", [""])[0])
        if offer is None or not _is_safe_link(target):
            continue
        provider_id, provider_name, monetization = offer
        is_subscription = monetization in {"flatrate", "ads", "free"}
        # Keep the first link per provider, but let a subscription offer replace a rent/buy one.
        if provider_id not in links or (is_subscription and provider_id not in subscription):
            links[provider_id] = WatchLink(provider_id, provider_name, target)
        if is_subscription:
            subscription.add(provider_id)
    return tuple(links.values())


def _justwatch_offer(context: str) -> tuple[int, str, str] | None:
    try:
        decoded = base64.urlsafe_b64decode(context + "=" * (-len(context) % 4))
        payload = json.loads(decoded)
    except (binascii.Error, ValueError):
        return None
    entries = payload.get("data") if isinstance(payload, dict) else None
    for entry in _object_list(entries):
        data = entry.get("data")
        if not isinstance(data, dict) or "providerId" not in data:
            continue
        try:
            provider_id = int(data["providerId"])
        except (TypeError, ValueError):
            return None
        name = str(data.get("provider") or "").strip()
        if provider_id <= 0 or not name:
            return None
        return provider_id, name, str(data.get("monetizationType") or "")
    return None


def _unwrap_affiliate(url: str) -> str:
    """Prefer the provider page over an affiliate redirect that carries it in ``u``."""

    destination = (parse_qs(urlsplit(url).query).get("u") or [""])[0]
    return destination if _is_safe_link(destination) else url


def _is_safe_link(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and not parts.username
        and not parts.password
    )


def _season_from_payload(payload: dict[str, Any], season_number: int) -> Season:
    episodes: list[Episode] = []
    for raw in _object_list(payload.get("episodes")):
        try:
            episode_number = int(raw["episode_number"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            runtime = int(raw.get("runtime") or 0) or None
        except (TypeError, ValueError):
            runtime = None
        still_path = raw.get("still_path")
        episodes.append(
            Episode(
                season_number=season_number,
                episode_number=episode_number,
                name=str(raw.get("name") or "").strip() or f"Episode {episode_number}",
                overview=str(raw.get("overview") or "").strip(),
                air_date=str(raw.get("air_date") or "").strip(),
                runtime_minutes=runtime if runtime and runtime > 0 else None,
                still_path=str(still_path) if still_path else None,
            )
        )
    name = str(payload.get("name") or "").strip()
    if not name:
        name = "Specials" if season_number == 0 else f"Season {season_number}"
    return Season(
        season_number=season_number,
        name=name,
        episodes=tuple(sorted(episodes, key=lambda episode: episode.episode_number)),
    )


def _provider_from_payload(payload: dict[str, Any], region: str) -> Provider | None:
    try:
        provider_id = int(payload["provider_id"])
    except (KeyError, TypeError, ValueError):
        return None
    name = str(payload.get("provider_name") or "").strip()
    if provider_id <= 0 or not name:
        return None

    priority_value: object = payload.get("display_priority", 9999)
    priorities = payload.get("display_priorities")
    if isinstance(priorities, dict):
        priority_value = priorities.get(region, priority_value)
    try:
        priority = int(priority_value)
    except (TypeError, ValueError):
        priority = 9999

    logo_path = payload.get("logo_path")
    return Provider(
        id=provider_id,
        name=name,
        logo_path=str(logo_path) if logo_path else None,
        priority=priority,
    )


def _catalog_item_from_payload(
    payload: dict[str, Any], media_type: MediaType
) -> CatalogItem | None:
    try:
        item_id = int(payload["id"])
    except (KeyError, TypeError, ValueError):
        return None
    title_field = "title" if media_type == "movie" else "name"
    date_field = "release_date" if media_type == "movie" else "first_air_date"
    title = str(payload.get(title_field) or "").strip()
    release_date = str(payload.get(date_field) or "").strip()
    if item_id <= 0 or not title or not release_date:
        return None
    try:
        rating = round(float(payload.get("vote_average") or 0), 1)
    except (TypeError, ValueError):
        rating = 0.0
    poster_path = payload.get("poster_path")
    genre_ids: list[int] = []
    for value in payload.get("genre_ids") or ():
        try:
            genre_id = int(value)
        except (TypeError, ValueError):
            continue
        if genre_id > 0:
            genre_ids.append(genre_id)
    try:
        popularity = float(payload.get("popularity") or 0)
    except (TypeError, ValueError):
        popularity = 0.0
    return CatalogItem(
        id=item_id,
        media_type=media_type,
        title=title,
        release_date=release_date,
        overview=str(payload.get("overview") or "").strip(),
        rating=rating,
        poster_path=str(poster_path) if poster_path else None,
        genre_ids=tuple(genre_ids),
        popularity=popularity,
    )


def _media_details_from_payload(
    payload: dict[str, Any], media_type: MediaType
) -> MediaDetails | None:
    try:
        item_id = int(payload["id"])
    except (KeyError, TypeError, ValueError):
        return None
    title_field = "title" if media_type == "movie" else "name"
    date_field = "release_date" if media_type == "movie" else "first_air_date"
    title = str(payload.get(title_field) or "").strip()
    if item_id <= 0 or not title:
        return None

    try:
        rating = round(float(payload.get("vote_average") or 0), 1)
    except (TypeError, ValueError):
        rating = 0.0

    runtime_value: object = payload.get("runtime")
    if media_type == "tv":
        runtimes = payload.get("episode_run_time")
        runtime_value = runtimes[0] if isinstance(runtimes, list) and runtimes else None
    try:
        runtime_minutes = int(runtime_value) if runtime_value is not None else None
    except (TypeError, ValueError):
        runtime_minutes = None
    if runtime_minutes is not None and runtime_minutes <= 0:
        runtime_minutes = None

    genres = tuple(
        dict.fromkeys(
            name
            for raw in _object_list(payload.get("genres"))
            if (name := str(raw.get("name") or "").strip())
        )
    )
    credits = payload.get("credits")
    cast_payload = credits.get("cast") if isinstance(credits, dict) else None
    cast_members: list[CastMember] = []
    for raw in _object_list(cast_payload):
        try:
            person_id = int(raw["id"])
        except (KeyError, TypeError, ValueError):
            continue
        name = str(raw.get("name") or "").strip()
        if person_id <= 0 or not name:
            continue
        profile_path = raw.get("profile_path")
        cast_members.append(
            CastMember(
                id=person_id,
                name=name,
                character=str(raw.get("character") or "").strip(),
                profile_path=str(profile_path) if profile_path else None,
            )
        )

    videos = payload.get("videos")
    video_payload = videos.get("results") if isinstance(videos, dict) else None
    poster_path = payload.get("poster_path")
    backdrop_path = payload.get("backdrop_path")
    return MediaDetails(
        id=item_id,
        media_type=media_type,
        title=title,
        release_date=str(payload.get(date_field) or "").strip(),
        overview=str(payload.get("overview") or "").strip(),
        rating=rating,
        runtime_minutes=runtime_minutes,
        poster_path=str(poster_path) if poster_path else None,
        backdrop_path=str(backdrop_path) if backdrop_path else None,
        genres=genres,
        cast=tuple(cast_members[:8]),
        trailer_key=_youtube_trailer_key(_object_list(video_payload)),
    )


def _youtube_trailer_key(videos: list[dict[str, Any]]) -> str | None:
    candidates = []
    for position, video in enumerate(videos):
        key = str(video.get("key") or "").strip()
        if video.get("site") != "YouTube" or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            continue
        video_type = str(video.get("type") or "")
        if video_type not in {"Trailer", "Teaser"}:
            continue
        candidates.append(
            (
                video_type == "Trailer",
                bool(video.get("official")),
                str(video.get("published_at") or ""),
                -position,
                key,
            )
        )
    return max(candidates)[-1] if candidates else None


def _page_from_payload(
    payload: dict[str, Any],
    items: tuple[CatalogItem, ...],
    fallback_page: int,
) -> CatalogPage:
    def positive_int(value: object, fallback: int) -> int:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback
        return max(parsed, 1)

    def nonnegative_int(value: object, fallback: int) -> int:
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback
        return max(parsed, 0)

    return CatalogPage(
        items=items,
        page=positive_int(payload.get("page"), fallback_page),
        total_pages=min(positive_int(payload.get("total_pages"), 1), 500),
        total_results=nonnegative_int(payload.get("total_results"), len(items)),
    )

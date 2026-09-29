from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, cast

import httpx

from mytaste.catalog.models import (
    CastMember,
    CatalogItem,
    CatalogPage,
    Genre,
    MediaDetails,
    MediaType,
    Provider,
    Region,
)

_API_BASE_URL = "https://api.themoviedb.org/3"
_V3_API_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")


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

    async def close(self) -> None:
        await self._client.aclose()

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

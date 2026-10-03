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

from mytaste.catalog.filters import PersonRole, TitleFacts, credit_roles
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

    async def _get_list(
        self, path: str, params: Mapping[str, object] | None = None
    ) -> list[dict[str, Any]]:
        """Fetch an endpoint that answers with a JSON list rather than an object."""

        request_params = dict(params or {})
        if self._api_key is not None:
            request_params["api_key"] = self._api_key
        try:
            response = await self._client.get(path, params=request_params)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise TMDBError("Could not read a list from TMDB") from exc
        except ValueError as exc:
            raise TMDBError("TMDB returned an unexpected response") from exc
        return _object_list(payload)

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
        genres: str | int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        minimum_rating: float | None = None,
        include_unrated: bool = False,
        released_after: date | None = None,
        minimum_votes: int | None = None,
        extra: Sequence[tuple[str, str]] = (),
        page: int = 1,
        today: date | None = None,
    ) -> CatalogPage:
        """Discover titles on the given services; ``extra`` adds filter parameters as is."""

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
        if genres is not None:
            params["with_genres"] = genres
        if minimum_rating is not None and not include_unrated:
            params["vote_average.gte"] = minimum_rating
        if minimum_votes is not None:
            params["vote_count.gte"] = minimum_votes
        params.update(extra)

        date_field = "primary_release_date" if media_type == "movie" else "first_air_date"
        if media_type == "tv":
            params["include_null_first_air_dates"] = "false"
        start_date = date(year_from, 1, 1) if year_from is not None else None
        if released_after is not None:
            start_date = max(start_date or released_after, released_after)
        if start_date is not None:
            params[f"{date_field}.gte"] = start_date.isoformat()
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

    async def title_facts(self, media_type: MediaType, item_id: int) -> TitleFacts:
        """Read the facts filters check: ratings, origin, runtime, keywords, and people."""

        if media_type == "movie":
            appended = "credits,release_dates,keywords"
        else:
            # Series credits only cover the latest season; aggregate credits cover them all.
            appended = "aggregate_credits,content_ratings,keywords"
        payload = await self._get(
            f"/{media_type}/{item_id}",
            {"language": self.language, "append_to_response": appended},
        )
        return _facts_from_payload(payload, media_type)

    async def person_credits(
        self, person_id: int
    ) -> tuple[tuple[CatalogItem, frozenset[PersonRole]], ...]:
        """Every title a person is credited on, with what they did on it."""

        payload = await self._get(
            f"/person/{person_id}/combined_credits", {"language": self.language}
        )
        found: dict[tuple[str, int], tuple[CatalogItem, set[PersonRole]]] = {}
        for key, acting in (("cast", True), ("crew", False)):
            for raw in _object_list(payload.get(key)):
                media_type = raw.get("media_type")
                if media_type not in {"movie", "tv"} or raw.get("adult"):
                    continue
                roles = credit_roles(raw, media_type, cast=acting)
                item = _catalog_item_from_payload(raw, media_type)
                if not roles or item is None:
                    continue
                entry = found.setdefault((media_type, item.id), (item, set()))
                entry[1].update(roles)
        return tuple((item, frozenset(roles)) for item, roles in found.values())

    async def search_people(self, query: str) -> tuple[tuple[int, str, str], ...]:
        """Find people by name: ``(id, name, what they are known for)``."""

        payload = await self._get(
            "/search/person",
            {"query": query, "language": self.language, "include_adult": "false"},
        )
        people: list[tuple[int, str, str]] = []
        for raw in _object_list(payload.get("results")):
            try:
                person_id = int(raw["id"])
            except (KeyError, TypeError, ValueError):
                continue
            name = str(raw.get("name") or "").strip()
            known_for = ", ".join(
                title
                for item in _object_list(raw.get("known_for"))[:2]
                if (title := str(item.get("title") or item.get("name") or "").strip())
            )
            detail = " · ".join(
                part for part in (str(raw.get("known_for_department") or ""), known_for) if part
            )
            if person_id > 0 and name:
                people.append((person_id, name, detail))
        return tuple(people)

    async def search_keywords(self, query: str) -> tuple[tuple[int, str], ...]:
        payload = await self._get("/search/keyword", {"query": query})
        return tuple(
            (int(raw["id"]), name)
            for raw in _object_list(payload.get("results"))
            if isinstance(raw.get("id"), int)
            and raw["id"] > 0
            and (name := str(raw.get("name") or "").strip())
        )

    async def person_name(self, person_id: int) -> str:
        payload = await self._get(f"/person/{person_id}", {"language": self.language})
        return str(payload.get("name") or "").strip()

    async def keyword_name(self, keyword_id: int) -> str:
        payload = await self._get(f"/keyword/{keyword_id}")
        return str(payload.get("name") or "").strip()

    async def certifications(self, media_type: MediaType) -> dict[str, tuple[str, ...]]:
        """Content ratings per country, mildest first."""

        payload = await self._get(f"/certification/{media_type}/list")
        raw_countries = payload.get("certifications")
        result: dict[str, tuple[str, ...]] = {}
        if not isinstance(raw_countries, dict):
            return result
        for country, entries in raw_countries.items():
            ranked: list[tuple[int, str]] = []
            for raw in _object_list(entries):
                rating = str(raw.get("certification") or "").strip()
                try:
                    order = int(raw.get("order") or 0)
                except (TypeError, ValueError):
                    order = 0
                if rating:
                    ranked.append((order, rating))
            result[str(country)] = tuple(rating for _order, rating in sorted(ranked))
        return result

    async def countries(self) -> tuple[tuple[str, str], ...]:
        payload = await self._get_list("/configuration/countries", {"language": self.language})
        return tuple(
            sorted(
                (
                    (code, name)
                    for raw in payload
                    if len(code := str(raw.get("iso_3166_1") or "").upper()) == 2
                    and (name := str(raw.get("english_name") or "").strip())
                ),
                key=lambda pair: pair[1].casefold(),
            )
        )

    async def languages(self) -> tuple[tuple[str, str], ...]:
        payload = await self._get_list("/configuration/languages")
        return tuple(
            sorted(
                (
                    (code, name)
                    for raw in payload
                    if len(code := str(raw.get("iso_639_1") or "").lower()) == 2
                    and code != "xx"
                    and (name := str(raw.get("english_name") or "").strip())
                ),
                key=lambda pair: pair[1].casefold(),
            )
        )

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
    genre_ids: list[int] = []
    for raw in _object_list(payload.get("genres")):
        try:
            genre_id = int(raw.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if genre_id > 0 and genre_id not in genre_ids:
            genre_ids.append(genre_id)
    try:
        popularity = float(payload.get("popularity") or 0)
    except (TypeError, ValueError):
        popularity = 0.0
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

    if media_type == "movie":
        crew_payload = credits.get("crew") if isinstance(credits, dict) else None
        makers = (
            raw.get("name") for raw in _object_list(crew_payload) if raw.get("job") == "Director"
        )
    else:
        makers = (raw.get("name") for raw in _object_list(payload.get("created_by")))
    directed_by = tuple(dict.fromkeys(name for raw in makers if (name := str(raw or "").strip())))

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
        cast=tuple(cast_members[:20]),
        trailer_key=_youtube_trailer_key(_object_list(video_payload)),
        directed_by=directed_by[:3],
        last_air_date=str(payload.get("last_air_date") or "").strip() if media_type == "tv" else "",
        genre_ids=tuple(genre_ids),
        popularity=popularity,
    )


def _facts_from_payload(payload: dict[str, Any], media_type: MediaType) -> TitleFacts:
    countries = [str(value).upper() for value in payload.get("origin_country") or () if value]
    if not countries:
        countries = [
            str(raw.get("iso_3166_1") or "").upper()
            for raw in _object_list(payload.get("production_countries"))
        ]
    certifications: dict[tuple[str, str], None] = {}
    if media_type == "movie":
        release_dates = payload.get("release_dates")
        for raw in _object_list(
            release_dates.get("results") if isinstance(release_dates, dict) else None
        ):
            country = str(raw.get("iso_3166_1") or "").upper()
            for release in _object_list(raw.get("release_dates")):
                rating = str(release.get("certification") or "").strip()
                if country and rating:
                    certifications[(country, rating)] = None
        runtime_value: object = payload.get("runtime")
        credits = payload.get("credits")
    else:
        ratings = payload.get("content_ratings")
        for raw in _object_list(ratings.get("results") if isinstance(ratings, dict) else None):
            country = str(raw.get("iso_3166_1") or "").upper()
            rating = str(raw.get("rating") or "").strip()
            if country and rating:
                certifications[(country, rating)] = None
        runtimes = payload.get("episode_run_time")
        runtime_value = runtimes[0] if isinstance(runtimes, list) and runtimes else None
        if not runtime_value:
            last = payload.get("last_episode_to_air")
            runtime_value = last.get("runtime") if isinstance(last, dict) else None
        credits = payload.get("aggregate_credits")
    try:
        runtime = int(runtime_value or 0) or None  # type: ignore[call-overload]
    except (TypeError, ValueError):
        runtime = None

    keywords = payload.get("keywords")
    raw_keywords = (
        keywords.get("keywords" if media_type == "movie" else "results")
        if isinstance(keywords, dict)
        else None
    )
    people: dict[PersonRole, dict[int, None]] = {
        "cast": {},
        "director": {},
        "writer": {},
        "producer": {},
    }
    credit_lists = credits if isinstance(credits, dict) else {}
    for key, acting in (("cast", True), ("crew", False)):
        for raw in _object_list(credit_lists.get(key)):
            person_id = raw.get("id")
            if not isinstance(person_id, int) or person_id <= 0:
                continue
            for role in credit_roles(raw, media_type, cast=acting):
                people[role][person_id] = None
    for raw in _object_list(payload.get("created_by")):
        person_id = raw.get("id")
        if isinstance(person_id, int) and person_id > 0:
            people["director"][person_id] = None
            people["writer"][person_id] = None
    return TitleFacts(
        original_language=str(payload.get("original_language") or "").lower(),
        countries=tuple(dict.fromkeys(code for code in countries if len(code) == 2)),
        runtime=runtime if runtime and runtime > 0 else None,
        certifications=tuple(certifications),
        keyword_ids=tuple(
            raw["id"]
            for raw in _object_list(raw_keywords)
            if isinstance(raw.get("id"), int) and raw["id"] > 0
        ),
        cast=tuple(people["cast"]),
        directors=tuple(people["director"]),
        writers=tuple(people["writer"]),
        producers=tuple(people["producer"]),
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

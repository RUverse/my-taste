from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from mytaste.games.http import StoreError, StoreHTTP
from mytaste.games.models import Game, GameQuery, game_key, product_id, web_url

logger = logging.getLogger(__name__)

_SUBSCRIPTIONS = {
    "ultimate": "cfq7ttc0khs0",
    "premium": "cfq7ttc0p85b",
    "essential": "cfq7ttc0k5dj",
    "pc": "cfq7ttc0kgq8",
}
_ALL = {
    "ultimate": "97c6c862-d28a-4907-a3d5-c401f2296a53",
    "premium": "09a72c0d-c466-426a-9580-b78955d8173a",
    "essential": "34031711-5a70-4196-bab7-45757dc2294e",
    "pc": "609d944c-d395-4c0a-9ea4-e9f39b52c1ad",
}
_LISTS = {
    "popular": ("eab7757c-ff70-45af-bfa6-79d3cfb2bf81", "a884932a-f02b-40c8-a903-a008c23b1df1"),
    "recent": ("06323672-b8c8-43cc-b0de-32d5a9834749", "06323672-b8c8-43cc-b0de-32d5a9834749"),
    "coming": ("095bda36-f5cd-43f2-9ee1-0a72f371fb96", "4165f752-d702-49c8-886b-fb57936f6bae"),
    "leaving": ("393f05bf-e596-4ef6-9487-6d4fa0eab987", "cc7fc951-d00f-410e-9e02-5e4628e04163"),
    "cloud": ("29a81209-df6f-41fd-a528-2ae6b91f719c", "29a81209-df6f-41fd-a528-2ae6b91f719c"),
}


class GamePassError(StoreError):
    """The upstream catalog could not be read completely or reliably."""


class GamePassClient:
    def __init__(self, *, timeout: float = 10, client: httpx.AsyncClient | None = None) -> None:
        self.http = StoreHTTP("Xbox's catalog", GamePassError, timeout=timeout, client=client)

    async def close(self) -> None:
        await self.http.close()

    async def _get(self, url: str, params: dict[str, str]) -> Any:
        return await self.http.json("GET", url, params=params)

    async def ids(self, query: GameQuery, region: str, language: str, collection: str) -> list[str]:
        pc = query.platform == "pc"
        sigl = _ALL[query.plan] if collection == "all" else _LISTS[collection][int(pc)]
        if collection == "coming" and query.plan == "premium":
            sigl = "f7534504-9c98-45aa-b8e9-95670783bc03"
        data = await self._get(
            "https://catalog.gamepass.com/sigls/v3",
            {
                "id": sigl,
                "language": language,
                "market": region,
                "platformContext": "pc" if pc else "ConsoleGen8;ConsoleGen9",
                "subscriptionContext": _SUBSCRIPTIONS[query.plan],
            },
        )
        if (
            not isinstance(data, list)
            or not data
            or not isinstance(data[0], dict)
            or "siglId" not in data[0]
        ):
            raise GamePassError("Xbox's catalog format has changed. Please try again later.")
        try:
            return list(dict.fromkeys(product_id(entry["id"]) for entry in data[1:]))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise GamePassError("Xbox's catalog contains invalid product IDs.") from exc

    async def products(self, ids: list[str], region: str, language: str) -> dict[str, Game | None]:
        if not ids:
            return {}
        data = await self._get(
            "https://displaycatalog.mp.microsoft.com/v7.0/products",
            {"bigIds": ",".join(ids), "market": region, "languages": language},
        )
        if not isinstance(data, dict) or not isinstance(data.get("Products"), list):
            raise GamePassError("Xbox's game metadata format has changed.")
        try:
            result = {}
            for value in data["Products"]:
                key = product_id(value["ProductId"])
                if key in ids:
                    result[key] = normalize_game(value)
            return result
        except (KeyError, ValueError, TypeError, AttributeError, IndexError) as exc:
            raise GamePassError("Xbox returned incomplete game metadata.") from exc


def normalize_game(data: dict[str, Any]) -> Game | None:
    properties = data.get("Properties") or {}
    skus = data.get("DisplaySkuAvailabilities") or []
    if properties.get("IsDemo") or (
        skus
        and all(sku.get("Sku", {}).get("Properties", {}).get("IsTrial") is True for sku in skus)
    ):
        return None
    if data.get("ProductType") not in {None, "Game"}:
        return None
    # Free games can appear for subscriber benefits. They are not included paid games.
    for sku in skus:
        if sku.get("Sku", {}).get("Properties", {}).get("IsTrial") is True:
            continue
        for offer in sku.get("Availabilities") or []:
            price = (offer.get("OrderManagementData") or {}).get("Price") or {}
            if (
                "Purchase" in (offer.get("Actions") or [])
                and not offer.get("RemediationRequired")
                and price.get("MSRP") == 0
                and price.get("ListPrice") == 0
            ):
                return None
    localized = data["LocalizedProperties"][0]
    title = localized.get("ProductTitle")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Missing game title")
    images: dict[str, list[str]] = {}
    for image in localized.get("Images") or []:
        url = web_url(image.get("Uri"))
        if url:
            images.setdefault(image.get("ImagePurpose", ""), []).append(url)
    market = (data.get("MarketProperties") or [{}])[0]
    usage = next(
        (
            entry
            for entry in market.get("UsageData") or []
            if entry.get("AggregateTimeSpan") == "AllTime"
        ),
        {},
    )
    score = usage.get("AverageRating")
    rating = float(score) if isinstance(score, (float, int)) and 0 < score <= 5 else None
    release = str(market.get("OriginalReleaseDate") or "")[:10]
    # Normalize release dates before exposing them to sorting and portable snapshots.
    try:
        if date.fromisoformat(release).year >= 9000:
            release = ""  # Microsoft uses distant dates for unannounced releases.
    except ValueError:
        release = ""
    key = product_id(data["ProductId"])
    return Game(
        id=game_key(xbox=key),
        xbox_id=key,
        title=title.strip(),
        overview=str(
            localized.get("ProductDescription") or localized.get("ShortDescription") or ""
        ),
        release_date=release,
        poster_url=next(iter(images.get("Poster") or images.get("BoxArt") or []), None),
        backdrop_url=next(
            iter(images.get("SuperHeroArt") or images.get("BrandedKeyArt") or []), None
        ),
        screenshots=tuple(images.get("Screenshot", [])),
        genres=tuple(str(genre) for genre in properties.get("Categories") or []),
        developers=(str(localized["DeveloperName"]),) if localized.get("DeveloperName") else (),
        publisher=str(localized.get("PublisherName") or ""),
        rating=rating,
        rating_count=max(0, int(usage.get("RatingCount") or 0)),
    )

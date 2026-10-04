"""Provider-neutral snapshots for the draft .taste format's future exchange adapter.

Portable IDs are independent of database row IDs. This module contains no catalog fetching
or archive I/O; importing/exporting the ZIP container remains a separate interface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mytaste.games.models import Game

if TYPE_CHECKING:
    from mytaste.collections.models import CollectionItem


def game_snapshot(game: Game) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": "game",
        "title": game.title,
        "summary": game.overview,
        "ids": {
            **({"steam": game.steam_appid} if game.steam_appid else {}),
            **({"microsoft_store": game.xbox_id} if game.xbox_id else {}),
        },
        "meta": {
            "developers": list(game.developers),
            "publisher": game.publisher,
            "genres": list(game.genres),
            "release_date": game.release_date,
        },
        "links": [
            *([{"url": game.steam_url, "label": "Steam"}] if game.steam_url else []),
            *([{"url": game.xbox_url, "label": "Xbox"}] if game.xbox_url else []),
        ],
        "files": [],
    }
    if game.release_date:
        value["year"] = int(game.year)
    ratings: dict[str, Any] = {}
    if game.steam_score is not None:
        ratings["steam"] = {"value": game.steam_score, "scale": 100, "count": game.steam_reviews}
    if game.rating is not None:
        ratings["microsoft_store"] = {"value": game.rating, "scale": 5, "count": game.rating_count}
    if ratings:
        value["meta"]["ratings"] = ratings
    if game.poster_url:
        # Until an asset's media type is known it remains a link, not an invalid typed file.
        value["links"].append({"url": game.poster_url, "label": "Cover"})
    return value


def title_snapshot(item: CollectionItem) -> dict[str, Any]:
    value: dict[str, Any] = {
        "kind": item.media_type,
        "title": item.title,
        "summary": item.overview,
        "ids": {"tmdb": item.tmdb_id},
        "meta": {
            "genres": list(item.genres),
            "release_date": item.release_date,
            "ratings": {"tmdb": {"value": item.rating, "scale": 10}},
        },
    }
    if item.release_date[:4].isdigit():
        value["year"] = int(item.release_date[:4])
    return value

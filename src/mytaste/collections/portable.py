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
        "ids": {"microsoft_store": game.id},
        "meta": {
            "developers": list(game.developers),
            "publisher": game.publisher,
            "genres": list(game.genres),
            "release_date": game.release_date,
        },
        "links": [{"url": game.store_url, "label": "Xbox"}],
        "files": [],
    }
    if game.release_date:
        value["year"] = int(game.year)
    if game.rating is not None:
        value["meta"]["ratings"] = {
            "microsoft_store": {"value": game.rating, "scale": 5, "count": game.rating_count}
        }
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

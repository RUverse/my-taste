from __future__ import annotations

from mytaste.games.models import Game, GameQuery


def game_matches(game: Game, query: GameQuery) -> bool:
    return (not query.search or query.search.casefold() in game.title.casefold()) and (
        not query.genre or query.genre.casefold() in {genre.casefold() for genre in game.genres}
    )


def game_sort_key(game: Game, sort: str) -> tuple[object, ...]:
    if sort == "release":
        return (not game.release_date, -int(game.release_date.replace("-", "") or "0"), game.id)
    if sort == "rating":
        return (game.rating is None, -(game.rating or 0), -game.rating_count, game.id)
    return (game.title.casefold(), game.id)

from __future__ import annotations

from mytaste.games.models import Game, GameQuery


def game_matches(game: Game, query: GameQuery) -> bool:
    return (not query.search or query.search.casefold() in game.title.casefold()) and (
        not query.genre or query.genre.casefold() in {genre.casefold() for genre in game.genres}
    )


def game_sort_key(game: Game, sort: str) -> tuple[object, ...]:
    """Order games in a sort's natural direction: titles A–Z, everything else highest first.

    Missing values go last. Titles compare case-insensitively, as Steam's store does, so
    Steam's ranking and known games merge into one order.
    """

    if sort == "release":
        return (not game.release_date, -int(game.release_date.replace("-", "") or "0"), game.id)
    if sort == "rating":
        score = game.score
        return (score is None, -(score or 0), -(game.steam_reviews or game.rating_count), game.id)
    if sort == "played":
        return (not game.last_played, -game.last_played, game.title.casefold(), game.id)
    if sort == "playtime":
        return (not game.playtime, -game.playtime, game.title.casefold(), game.id)
    return (game.title.casefold(), game.id)

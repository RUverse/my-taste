import asyncio
from datetime import date

from mytaste.catalog.mixing import GAME_EVERY, mix_games
from mytaste.catalog.models import CatalogItem, CatalogPage
from mytaste.collections.models import GameEntry, smart_collection
from mytaste.games.models import Game


def title(number, *, released="2020-01-01", rating=7.0):
    return CatalogItem(number, "movie", f"Title {number:03}", released, "", rating)


def game(name, *, released="2021-06-01", genres=(), tag_ids=(), coming_soon=""):
    return GameEntry(
        Game(
            f"steam-{abs(hash(name)) % 10**6 + 1}",
            name,
            release_date=released,
            genres=genres,
            tag_ids=tag_ids,
            coming_soon=coming_soon,
        )
    )


def loader(items, page_size=20, total=None):
    """Pages like the catalog's: past the last page, the last page comes back again."""

    calls = []
    pages = max((len(items) + page_size - 1) // page_size, 1)

    async def load(number):
        calls.append(number)
        number = min(number, pages)
        return CatalogPage(
            items=tuple(items[(number - 1) * page_size : number * page_size]),
            page=number,
            total_pages=pages,
            total_results=len(items) if total is None else total,
        )

    load.calls = calls
    return load


def every_page(load, games, **options):
    first = asyncio.run(mix_games(load, games, page=1, **options))
    pages = [first] + [
        asyncio.run(mix_games(load, games, page=number, **options))
        for number in range(2, first.total_pages + 1)
    ]
    return first, [item for page in pages for item in page.items]


def test_popularity_spreads_games_through_titles_and_keeps_the_rest_after():
    titles = [title(number) for number in range(1, 31)]
    games = [game(f"Game {number}") for number in range(1, 16)]
    first, items = every_page(loader(titles), games, sort="popularity", descending=True)
    assert [item.title for item in first.items[:GAME_EVERY]] == [
        "Title 001",
        "Title 002",
        "Title 003",
        "Game 1",
    ]
    assert first.total_results == 45 and first.total_pages == 3
    assert len(items) == 45 and len({id(item) for item in items}) == 45
    # One game in every four cards while titles last; the remaining games follow in rank order.
    assert [item.title for item in items if isinstance(item, GameEntry)] == [
        f"Game {number}" for number in range(1, 16)
    ]
    assert all(isinstance(item, GameEntry) for item in items[40:])


def test_least_popular_first_reverses_the_games_too():
    titles = [title(number) for number in range(1, 9)]
    games = [game("Most popular"), game("Least popular")]
    page = asyncio.run(
        mix_games(loader(titles), games, sort="popularity", descending=False, page=1)
    )
    assert [item.title for item in page.items if isinstance(item, GameEntry)] == [
        "Least popular",
        "Most popular",
    ]


def test_limited_collections_take_only_the_games_that_fit_beside_their_titles():
    titles = [title(number) for number in range(1, 13)]
    games = [game(f"Game {number}") for number in range(1, 10)]
    first, items = every_page(loader(titles), games, sort="popularity", descending=True, limit=200)
    assert [item.title for item in items if isinstance(item, GameEntry)] == [
        "Game 1",
        "Game 2",
        "Game 3",
        "Game 4",
    ]
    assert first.total_results == 16 and items[-1].title == "Game 4"
    # Re-sorted, Popular keeps the same games.
    _first, by_title = every_page(
        loader(sorted(titles, key=lambda item: item.title)),
        games,
        sort="title",
        descending=False,
        limit=200,
    )
    assert {item.title for item in by_title if isinstance(item, GameEntry)} == {
        "Game 1",
        "Game 2",
        "Game 3",
        "Game 4",
    }
    # With only games chosen, Popular is the most popular games up to its limit.
    only_games = asyncio.run(
        mix_games(loader([]), games, sort="popularity", descending=True, page=1, limit=5)
    )
    assert [item.title for item in only_games.items] == [f"Game {n}" for n in range(1, 6)]
    assert only_games.total_results == 5 and only_games.total_pages == 1


def test_shared_sorts_merge_games_where_they_rank():
    titles = [
        title(number, released=f"20{number:02}-01-01") for number in range(25, 0, -1)
    ]  # Newest first, as TMDB returns them.
    games = [
        game("Old", released="2001-06-01"),
        game("New", released="2024-06-01"),
        game("Undated", released=""),
    ]
    first, items = every_page(loader(titles), games, sort="release", descending=True)
    dates = [item.release_date for item in items]
    assert dates[:3] == ["2025-01-01", "2024-06-01", "2024-01-01"]
    assert items.index(next(item for item in items if item.title == "Old")) == 25
    assert items[-1].title == "Undated" and first.total_results == 28
    by_title = every_page(
        loader(sorted(titles, key=lambda item: item.title)), games, sort="title", descending=False
    )[1]
    assert [item.title for item in by_title][:3] == ["New", "Old", "Title 001"]


def test_titles_stop_at_the_last_page_and_pages_are_read_lazily():
    titles = [title(number) for number in range(1, 46)]
    load = loader(titles)
    page = asyncio.run(
        mix_games(load, [game("A game")], sort="popularity", descending=True, page=1)
    )
    assert load.calls == [1] and len(page.items) == 20
    load = loader(titles)
    _first, items = every_page(load, [], sort="popularity", descending=True)
    assert len(items) == 45  # The repeated last page is not read twice.


def test_smart_collections_take_games_whose_genre_clearly_matches():
    horror = game("Dread", tag_ids=(1667,))
    family = game("Kart", genres=("Family & kids",))
    shooter = game("Blast", genres=("Shooter",))
    assert smart_collection("horror").matches_game(horror.game)
    assert not smart_collection("horror").matches_game(family.game)
    assert smart_collection("family").matches_game(family.game)
    assert smart_collection("action").matches_game(shooter.game)
    assert smart_collection("popular").matches_game(shooter.game)
    # Steam's least-voted tags are often stray; derived genres don't count for Steam games.
    simulator = game(
        "Flight", genres=("Simulation", "Family & kids"), tag_ids=(599, *range(1, 19), 1721)
    )
    assert not smart_collection("horror").matches_game(simulator.game)
    assert not smart_collection("family").matches_game(simulator.game)
    for name in ("romance", "documentary", "drama", "animation"):
        assert not smart_collection(name).has_games
        assert not smart_collection(name).matches_game(horror.game)
    latest = smart_collection("latest")
    today = date(2026, 10, 5)
    assert latest.matches_game(game("Fresh", released="2026-03-01").game, today)
    assert not latest.matches_game(game("Old", released="2024-03-01").game, today)
    assert not latest.matches_game(game("Soon", released="2026-12-01").game, today)
    assert not latest.matches_game(
        game("Announced", released="2026-09-01", coming_soon="Coming soon").game, today
    )

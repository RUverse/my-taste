"""Mix the user's games into a collection of movies and series.

Titles come from a paged loader, already in the collection's order; games are a complete list,
most popular first. Sorted by release date, rating, or title, both merge by the same key
(``catalog_sort_key``), so a game lands exactly where it ranks. Popularity has no shared scale
between TMDB and the game stores, so games are spread through the titles instead: every
``GAME_EVERY``th card is a game while both last. Page *n* is always the *n*th slice of one
order either way.
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from mytaste.catalog.models import CatalogPage, SortKey, catalog_sort_key

GAME_EVERY = 4

TitleLoader = Callable[[int], Awaitable[CatalogPage]]


@dataclass(slots=True)
class _Titles:
    """The collection's titles, read one page at a time as the merge needs them."""

    load: TitleLoader
    items: list[Any] = field(default_factory=list)
    next_page: int = 1
    total_pages: int | None = None
    total_results: int = 0
    done: bool = False

    async def fill(self, count: int) -> None:
        while len(self.items) < count and not self.done:
            number = self.next_page
            page = await self.load(number)
            if self.total_pages is None:
                self.total_pages = page.total_pages
                self.total_results = page.total_results
            self.next_page += 1
            # Loaders repeat their last page for numbers past the end, so stop there.
            if page.page != number or not page.items:
                self.done = True
                break
            self.items.extend(page.items)
            if number >= self.total_pages:
                self.done = True


async def mix_games(
    load: TitleLoader,
    games: Sequence[Any],
    *,
    sort: SortKey,
    descending: bool,
    page: int,
    page_size: int = 20,
    limit: int | None = None,
) -> CatalogPage:
    """Return page ``page`` of the collection's titles with ``games`` mixed in.

    Collections with a ``limit``, such as Popular, are their top titles; they take only the
    games that fit beside those titles at one in ``GAME_EVERY``, the most popular first, or up
    to ``limit`` games when only games are shown.
    """

    titles = _Titles(load)
    await titles.fill(1)
    ranked = list(games)
    if limit is not None:
        share = titles.total_results // (GAME_EVERY - 1)
        ranked = ranked[: share if titles.items else limit]
    wanted = max(page, 1) * page_size
    if sort == "popularity":
        if not descending:
            ranked.reverse()
    else:
        ranked.sort(key=catalog_sort_key(sort, descending), reverse=descending)
    key = catalog_sort_key(sort, descending)

    merged: list[Any] = []
    title_index = game_index = 0
    while len(merged) < wanted:
        if title_index >= len(titles.items):
            await titles.fill(title_index + wanted - len(merged))
        title = titles.items[title_index] if title_index < len(titles.items) else None
        game = ranked[game_index] if game_index < len(ranked) else None
        if title is None and game is None:
            break
        if game is None:
            take_game = False
        elif title is None:
            take_game = True
        elif sort == "popularity":
            take_game = len(merged) % GAME_EVERY == GAME_EVERY - 1
        else:
            # Ties go to the title, so games never jump ahead of an equal title.
            take_game = key(game) > key(title) if descending else key(game) < key(title)
        if take_game:
            merged.append(game)
            game_index += 1
        else:
            merged.append(title)
            title_index += 1

    total_results = titles.total_results + len(ranked)
    total_pages = max(math.ceil(total_results / page_size), 1)
    if len(merged) < wanted:
        # Everything is read: the real length replaces the estimates.
        total_results = len(merged)
        total_pages = max(math.ceil(len(merged) / page_size), 1)
    number = min(max(page, 1), total_pages)
    return CatalogPage(
        items=tuple(merged[(number - 1) * page_size : number * page_size]),
        page=number,
        total_pages=total_pages,
        total_results=total_results,
    )

# Steam games

Games combines Steam with Xbox Game Pass ([Game Pass plan](game-pass-implementation.md)). A user
connects their Steam account to see which games they own and how long they played them, while
the whole Steam store stays browsable. A game sold in both stores is one game with both
services, like a movie on two streaming services. Games can be saved to collections next to
movies and series.

## Upstream sources

Researched and tested with anonymous requests on October 4, 2026.

| Need | Source | Key |
| --- | --- | --- |
| Rankings, filtered pages, totals | `IStoreQueryService/Query` | none |
| Details of up to ~200 apps per request: name, tags, reviews, release, developers, image assets, screenshots | `IStoreBrowseService/GetItems` | none |
| Text search (up to 100 results, no paging) | `IStoreQueryService/SearchSuggestions` | none |
| Tag names | `IStoreService/GetTagList` | none |
| Owned games with play time | `IPlayerService/GetOwnedGames` | Web API key |
| Persona, avatar | `ISteamUser/GetPlayerSummaries` | Web API key |
| Custom profile name → Steam ID | `ISteamUser/ResolveVanityURL`, or `steamcommunity.com/id/<name>/?xml=1` | key, or none |
| Sign-in | OpenID 2.0 at `steamcommunity.com/openid/login` | none |
| Xbox product ↔ Steam app | IsThereAnyDeal `/lookup/id/shop/48/v1` then `/lookup/shop/61/id/v1`; Wikidata P5885/P1733 | none |

Findings that shaped the design:

- **Owned games need a key.** The public profile's games page (and its old `?xml=1` form) now
  redirects every anonymous request to a login page. A key reads any public profile, so one
  server key serves every user, who only connect their profile. Steam answers an empty object
  when a profile's Game details are private; MyTaste shows how to make them public.
- **Query sort values are undocumented.** Verified behaviour: `1` title A–Z (case-insensitive),
  `10` top sellers, `20` new and trending, `21` top rated, `30` most played, `40` newest release
  first. Only `1` and `40` can be reproduced for games that are not on Steam, so they are the
  orders of All games. Tags in one `tagids_must_match` entry are alternatives.
- **Image URLs come from the item.** Newer apps use hashed file names; `assets.asset_url_format`
  with the asset name is the only reliable address. `appdetails` is avoided: one app per
  request and about 200 requests per five minutes.
- **Matching coverage**, against the 595 PC Game Pass Ultimate games in Germany:
  IsThereAnyDeal mapped 528 to a Steam app, PCGamingWiki 504, Wikidata 147; IsThereAnyDeal and
  PCGamingWiki agreed on 473 of 477. Most games left are not on Steam (Microsoft, EA, Ubisoft,
  Battle.net). IsThereAnyDeal's ID lookups need no key but are a courtesy, so MyTaste credits it,
  keeps findings for a week, and falls back to Wikidata and to Steam search with the same
  normalized title and release year. PCGamingWiki is not used: its CC BY-NC-SA license does not
  fit an MIT project, and its anonymous Cargo queries are refused.
- **Not used:** SteamDB forbids scraping and offers no API; SteamSpy's data is stale (no play
  times, years-old names); IGDB, RAWG, and GG.deals need a key from every self-hoster.

## Model

`Game` (`games/models.py`) is one game in any number of stores. Its key is `steam-<appid>` when
the game is on Steam, else `xbox-<product id>`; older bare Store IDs still resolve, and pages
for a game's other key redirect to it. Merged games take Steam's details and genres and keep the
Microsoft Store rating and Game Pass availability (`merge_stores`). Store facts keep their own
scale; `Game.score` converts both to 0–10 only to sort. `game_pass`, `owned`, `playtime`, and
`last_played` describe the user, are applied per request, and are never saved.

Genres: one list for both stores, the Microsoft Store's categories. Steam games get them from
their tags (`GENRE_TAGS`). Steam matches a tag anywhere on a game while GetItems lists a game's
top 20 tags, so Steam's pages are filtered again by the listed tags; the genre filter then
gives the same answer for a game in every collection.

## Collections and order

| Collection | Source | Orders |
| --- | --- | --- |
| All games | Steam's store merged with Game Pass and owned games | Newest release, title |
| My games, Recently played, Most played | Game Pass list and owned games, fully known | Any (last played, time played, title, release, rating) |
| Most played, Top sellers, New and trending, Top rated, Coming soon on Steam | One Steam ranking | Steam's |
| Game Pass collections | Microsoft's lists | Microsoft's order or any sort |

Steam's rankings cannot place games that are only on Xbox, so those games are left out of
them and appear in All games, the Game Pass collections, and My games. All games merges
Steam's pages with the known games by the same sort key (`_merge`), so page *n* is always the
*n*th slice of one order and every game appears once, as the copy that carries the user's
access. Steam pages are fetched 100 at a time and kept in memory for ten minutes per ranking,
genre, and region, so following pages reuse them; lists stop after 100 pages like merged movie
lists. Search covers the collection's own games plus Steam's search for Steam collections and
All games, ordered by Steam's relevance, then the user's own matches.

## Accounts

Both game services are managed on the Services page, which the games sidebar opens with
**Manage**: the Xbox Game Pass card holds the plan and platform (saved on change), and Steam is
added from **Add**. The Services page connects Steam with Valve's **Sign in through Steam** button. The callback
carries a random `state` that must match an HttpOnly, SameSite=Lax cookie; the reply must name
Steam's endpoint, our exact callback, and a matching claimed ID, and Steam must confirm it with
`check_authentication`. The page to return to after Services travels in the same cookie. Pasting a profile link is the fallback. One account is connected per
installation (`steam_account`); owned games are refreshed after six hours, or on demand, and a
failed refresh keeps the previous list.

## Saving games

Collections now store shared saved items (`saved_items`: a movie or series by TMDB ID, a game by
Steam and/or Xbox ID, with a snapshot) and ordered entries (`collection_entries`). The first
start copies `collection_items` in one transaction, keeping order and dates; the old table is
left untouched so the copy loses nothing. A game saved under one store's ID is found by either
ID once the stores are matched and keeps its first portable ID (`game-xbox-…` or
`game-steam-…`), as the [portable collections design](taste-collections.md) requires.

Collection pages mix games with titles and sort them together. Like titles, games are listed
only when the user can play them (Game Pass or owned) once a Game Pass plan is chosen or Steam
is connected. TMDB filters never match a game, and narrowing streaming services hides games.

## Later

- One Steam account per tailnet user from Tailscale Serve's `Tailscale-User-Login` header.
- `.taste` export of saved games through the portable adapter, using `ids.steam` and
  `ids.microsoft_store`.
- Wishlists (`IWishlistService`) and prices from Steam's store data.

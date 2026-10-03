# Xbox Game Pass implementation plan

Add Xbox Game Pass browsing to MyTaste's Games category using Microsoft's catalog feeds and
Store metadata. Users choose their subscription plan and platform; their existing country
preference determines availability. Browsing does not require connecting an Xbox account.

Game Pass browsing and the optional SQLite cache are implemented. **Browsing stays available
with caching disabled**, the default. See [deployment](deployment.md#game-pass-caching) to enable
caching later. Portable collection IDs and provider snapshot adapters are implemented; shared
saved-item persistence, game saving, and the `.taste` import/export interface remain later work.

The sections below retain the architecture and rollout plan. The current `Game` model also
carries detail fields; there is no separate `GameDetails` class. Metadata is hydrated for the
entire selected collection, even in collection order. Refresh coalescing covers identical
requested batches within one worker, not overlapping batches or separate processes.

## Initial user experience

- Add a Games entry to navigation, leading to the Game Pass catalog.
- Offer All games, Popular, Recently added, Coming soon, and Leaving soon collections.
- Let users select Essential, Premium, Ultimate, or PC Game Pass, and PC, console, or cloud.
  Show unsupported plan/platform combinations explicitly rather than substituting a higher tier.
- Show covers, titles, release years, genres, platform badges, and descriptions. Details include
  screenshots, developer, publisher, and an Open in Xbox link to the official product page.
- Support title search, genre filters, and sorting by title, release date, and Store rating.
  Preserve Microsoft's ordering for Popular and Recently added collections.
- Label Store ratings on their own five-star scale. A Store release date describes that Store
  product or edition and may differ from the original game's release date.
- Keep region, plan, platform, filters, search, sorting, and pagination consistent across pages.
  Persist plan/platform defaults; put temporary browse choices in the URL.

Game saving and `.taste` import/export can follow this first browsing release. Plan their shared
identity and persistence foundation now, following [portable collections](taste-collections.md),
so saved games can join mixed movie/TV/game collections later. Existing saved titles use numeric
TMDB IDs and movie/TV-only constraints; migrate them explicitly into shared saved items and
ordered collection entries before adding game saving. Do not introduce a separate game-only
collection store or expose a nonfunctional Save button. Xbox login, installation, embedded cloud
gameplay, and local game scanning are outside this first release. An Open in Xbox link hands
those actions to Microsoft's app/site.

## Verified upstream sources

The [official catalog page](https://www.xbox.com/en-US/xbox-game-pass/games) and its
[catalog JavaScript](https://www.xbox.com/en-us/xbox-game-pass/games/js/xgpcatPopulate-2025.js)
use these endpoints, verified with anonymous requests during research on October 3, 2026:

| Endpoint | Purpose | Required context |
| --- | --- | --- |
| `https://catalog.gamepass.com/sigls/v3` | Collection membership and ordered Store product IDs | `id`, `language`, `market`, `platformContext`, `subscriptionContext` |
| `https://displaycatalog.mp.microsoft.com/v7.0/products` | Metadata and image URLs for a batch of product IDs | `bigIds`, `market`, `languages` |

Keep the collection IDs and subscription product IDs in one versioned mapping in the client.
Use `ConsoleGen8;ConsoleGen9` for a combined console context and `pc` for PC. Cloud eligibility
comes from the dedicated cloud list intersected with the selected plan's eligible games;
appearing in a general cloud catalog alone must not imply inclusion in Game Pass.

For the initial All games mappings, Ultimate uses collection
`97c6c862-d28a-4907-a3d5-c401f2296a53`, Premium uses
`09a72c0d-c466-426a-9580-b78955d8173a`, Essential uses
`34031711-5a70-4196-bab7-45757dc2294e`, and PC Game Pass uses
`609d944c-d395-4c0a-9ea4-e9f39b52c1ad` with a PC platform context. The official JavaScript is
the reference for the other collection mappings and subscription IDs. Validate mappings and
the plan/platform matrix again when implementing; they can change independently of MyTaste.

These are publicly reachable website endpoints. Research did not establish a supported
third-party catalog API contract. Isolate their response parsing and mapping so upstream changes
are easy to repair. Catalog access does not automatically verify a user's entitlement:
[Microsoft's subscription verification API](https://learn.microsoft.com/en-us/gaming/gdk/docs/store/commerce/service-to-service/xstore-detecting-game-pass?view=gdk-2604)
requires publisher authorization. Plan selection remains manual.

## Models and service boundaries

Create `src/mytaste/games/` with the following responsibilities:

| Module | Responsibility |
| --- | --- |
| `models.py` | Immutable `Game`, `GameDetails`, `GameQuery`, `GamePage`, plan/platform enums, and collection definitions |
| `gamepass.py` | `GamePassClient`: asynchronous HTTP transport, upstream mappings, response validation, and metadata normalization |
| `service.py` | `GamesService`: availability intersections, filtering, ordering, pagination, enrichment, and optional caching |
| `filters.py` | Game filters and one matching rule shared by all game collections |

Use uppercase Store product IDs as strings, represented as `kind=game` and an external ID such
as `ids.microsoft_store = "9NPDN9R45JX4"`. Assign a separate stable portable item ID such as
`game-xbox-9NPDN9R45JX4`; `.taste` IDs cannot contain colons. Keep games separate from TMDB's
catalog `MediaType` and numeric IDs, while sharing a generic saved-item representation for
collections. Game image URLs are explicit fields and must not pass through TMDB image URL
construction. The ID namespace is a proposed adapter convention, detailed in the portable plan.

Deduplicate repeated product IDs. Merge distinct editions only when Microsoft supplies an
explicit relationship and availability is preserved; do not merge by title text. Clearly
separate full included games, trials, and free games offering only subscription benefits.
Start with verified full-game membership and add other offer types only with explicit labels.

Use a shared `httpx.AsyncClient`, the existing request timeout, metadata batches of 20 as the
official page does, and an initial concurrency limit of four. Bound retries for transient
network failures, HTTP 429, and 5xx responses; honor `Retry-After` without unbounded waits.
Cancel unfinished work on shutdown. Do not retry malformed successful responses indefinitely.

Build the selected plan/platform/region membership first, then intersect collection membership
and normalize metadata. Apply all filters and the requested ordering before pagination. Use
product ID as the final tie breaker. Microsoft list position is an ordering, not a numeric
popularity score or a precise subscription-added timestamp. Leaving soon is a provider label;
do not invent an exact departure date from list membership.

For a simple initial implementation, hydrate the full eligible collection before global title
search, genre filtering, or metadata sorting. Never filter just the current page or silently
truncate the collection. This can require dozens of metadata requests with caching disabled;
measure latency and call volume before deployment. If a requested global operation cannot
complete, return an actionable error rather than presenting an incomplete result as complete.

## Preferences and web integration

Add game preferences in a new SQLite table, storing the selected plan and platform defaults.
Reuse the existing region preference. A games-only setup must work without movie subscriptions
or a local library; it must not depend on `Preferences.configured`, which currently requires
TMDB provider IDs. Updating game preferences must preserve streaming subscriptions, and vice
versa. The existing movie/TV application still requires its TMDB token in this first release.

Add `web/games.py` and an injectable `games` service argument to `create_app()`. The application
owns and closes only the service it creates; injected fakes must never trigger live requests.
Do not prefetch games at startup when nobody has configured or opened the Games category.

Proposed browse routes are `/collections/games` and `/collections/games/{key}`. Use query
parameters such as `plan`, `platform`, `genre`, `q`, `sort`, and `page` for the remaining state.
Details live at `/api/games/{product_id}/details`. Register the games router before the existing
`/collections/{key}` route so the Games entry is not captured as a movie/TV collection.

Reuse the base layout, semantic CSS, display preferences, and accessible dialog patterns. Add
game-specific card and detail markup instead of passing strings into `_cards.html`, which
compares IDs numerically and generates TMDB routes. Extract shared presentation only where it
avoids duplication without changing movie/TV semantics. Game details must not request actors,
episodes, watch progress, or TMDB provider badges. Show developer/publisher in the relevant
metadata area and use a separate, labeled Store rating formatter.

Show useful states for missing setup, unsupported combinations, no matching games, partial
metadata, and upstream outages. Preserve titles with missing artwork using placeholders.
Escape descriptions as text and validate provider URLs; do not insert upstream HTML into the UI.

## Optional cache

The cache should be ready to add or enable at deployment, while remaining disabled by default.
It caches upstream catalog data, not preferences or user collections. Existing TMDB and
playback caches are independent and keep their existing behavior.

| Setting | Default | Meaning |
| --- | --- | --- |
| `MYTASTE_GAMEPASS_CACHE_ENABLED` | `false` | Enable Game Pass cache reads, writes, and refresh tasks |
| `MYTASTE_GAMEPASS_CATALOG_TTL_SECONDS` | `7200` | Keep membership and ordered collection lists fresh for two hours |
| `MYTASTE_GAMEPASS_METADATA_TTL_SECONDS` | `86400` | Keep normalized game metadata fresh for one day |
| `MYTASTE_GAMEPASS_STALE_TTL_SECONDS` | `86400` | Maximum time after expiry that a previous entry may be used during refresh or an outage |

**Disabled behavior:** fetch upstream data for each independent request. Reuse results within
that request, but retain no Game Pass results between requests. Do not create a cache database,
warm the catalog, start refresh tasks, or silently fall back to an old persistent entry. An
outage produces the normal retry/error state. Game preferences still persist in the app database.

**Enabled behavior:** start with SQLite under
`MYTASTE_CACHE_DIR/gamepass/catalog.sqlite3`, with at most 4,096 persistent entries and
process-local tracking of active refresh tasks.
This fits the existing single-worker deployment and survives restarts without requiring Redis.
Keep the cache disposable and separate from the application database. Run SQLite operations
in worker threads so disk access does not block the event loop. Schema initialization must be
repeatable; version the normalized payload and rebuild incompatible cache data. Store UTC
timestamps for persistent expiry and inject the clock in tests so restarts preserve entry age.

Key membership by schema version, collection, region, language, plan, and platform. Key metadata
by schema version, region, language, and product ID. A changed preference must resolve new keys
immediately; never reuse another region or tier's membership. Keep query-specific pages derived
from the cached upstream data rather than persisting every combination of filters.

Return fresh entries immediately. When an entry expires, allow its previous value within the
stale limit and schedule one refresh for that key; coalesce simultaneous refreshes. Without a
previous entry, await the initial fetch. Track refresh timestamps and show when availability
comes from an older snapshot. Past the stale limit, show an upstream error rather than claiming
current availability.

Publish validated replacement lists atomically and invalidate any derived pages. A failed or
malformed response must not overwrite the previous list or mark missing games as removed.
Distinguish a valid empty collection from a transport/schema failure. Metadata failures may
leave placeholders but must not publish a partial globally filtered or sorted result. Refresh
only contexts that users access. Bound entries and remove old unused contexts so changing
regions does not grow the cache indefinitely.

Use an injected clock and cache interface for deterministic tests. Put the SQLite implementation
in `storage/gamepass_cache.py`; keep HTTP transport independent of cache policy. If the cache
cannot be opened or becomes corrupt, log the failure and fall back to live requests without
preventing movie/TV browsing. Document that outage fallback is then unavailable.

## Implementation sequence

1. **Models and client:** define game identities with separate portable and external IDs,
   collection/plan mappings, normalized fields, and fixtures for the two upstream endpoints.
   Specify the shared saved-item/entry foundation from [the portable collection plan](taste-collections.md)
   before game saving; test valid, missing, malformed, trial, and duplicate data before routes.
2. **Service and preferences:** implement membership intersections, consistent filters,
   ordering, pagination, and persistent game defaults. Cover games-only setup and preference
   independence. Keep all fetching asynchronous and concurrency bounded.
3. **Games interface:** add navigation, collection routes, setup controls, cards, details,
   filters, and progressive pagination. Handle provider failures without disrupting other
   categories. Update the fake services used by route tests.
4. **Optional caching:** implement the disabled and enabled behaviors above as a separate
   change. Leave the switch false; verify expiry, stale limits, refresh coalescing, persistence,
   preference isolation, eviction, corrupt storage, and shutdown.
5. **Deployment documentation:** promote the proposed settings into README configuration and
   real deployment instructions once implemented. Update the Compose example and explain
   cache storage, persistence, clearing, upstream errors, and how to enable or disable caching.
6. **Portable saved collections:** implement the shared persistence migration and game saving,
   then `.taste` export and import through a versioned adapter. Preserve mixed media, saved
   order, notes/tags, attachments, and unknown fields. Export all saved entries regardless of
   current availability, filters, or page. Keep durable collection assets out of disposable caches.

Each implementation change starts from `origin/dev` and opens a PR against `dev`, following
[CONTRIBUTING.md](../CONTRIBUTING.md). Steps 1–5 are delivered in the initial browsing release; step 6 remains a follow-up.

## Validation and deployment

Automated tests must use sanitized fixtures and fake clients, never Microsoft or TMDB. Verify
that two pages share one deterministic global order, plans and regions never leak availability,
cloud eligibility respects the selected subscription, partial results are labeled or rejected,
and switching Games/movie/TV contexts cannot generate incorrect API or playback requests.
Exercise the cache switch both ways, including the guarantee that disabled mode performs no
cache reads, writes, or background work.

Before handing off implementation, run the repository's pytest, Ruff, JavaScript syntax, and
build checks from [AGENTS.md](../AGENTS.md). Inspect the actual Games pages at desktop and mobile
widths in both OS themes, including keyboard interaction, loading, empty, error, and stale states.
An optional manual upstream smoke test can verify mappings separately from the offline suite.

To enable caching at deployment, set:

```bash
MYTASTE_GAMEPASS_CACHE_ENABLED=true
MYTASTE_GAMEPASS_CATALOG_TTL_SECONDS=7200
MYTASTE_GAMEPASS_METADATA_TTL_SECONDS=86400
MYTASTE_GAMEPASS_STALE_TTL_SECONDS=86400
```

Native deployments put these settings in the service environment; Docker deployments add
them to the Compose
`environment` block and recreate the container. Restart the single worker, open Games to load
the initial context, and check that subsequent requests use the cache. For restart persistence,
mount the Game Pass cache directory on writable storage. It contains disposable catalog data
and needs no backup. Returning the enabled switch to false and restarting bypasses the cache.

Keep `/healthz` independent of Microsoft. Upstream failures should affect Games browsing and
its freshness indicator, not cause container restart loops.

# Music

Music becomes a fourth kind beside movies, series, and games. People browse albums as a wall of
covers that can be laid out in several ways, save them to the same collections as their films
and games, and press Play to hear them on a service they connected. For now those services are
Spotify and SoundCloud.

Music works the way films already do. TMDB is the catalog for films, and Netflix or a local
folder is where a film plays. For music, **MusicBrainz is the catalog**, and **Spotify and
SoundCloud are where an album plays**. The research below is why: neither service can be the
catalog of a self-hosted app under its 2026 rules.

## What the research found

Researched on October 18, 2026, from the official developer sites and with keyless live requests.

**Spotify** ([quota modes](https://developer.spotify.com/documentation/web-api/concepts/quota-modes),
[February 2026 changes](https://developer.spotify.com/documentation/web-api/references/changes/february-2026),
[Developer Policy](https://developer.spotify.com/policy),
[Design Guidelines](https://developer.spotify.com/documentation/design))

- **One app per household, five people.** Since February 2026, an app in development mode allows
  at most five allowlisted Spotify users. The developer who owns it must have Premium, or the app
  stops working. Extended quota is only for registered businesses with at least 250,000 monthly
  users. MyTaste can't ship a shared client ID. Each owner registers their own Spotify app, adds
  the Spotify email of each person at home, and puts the client ID and secret in `.env`. Spotify
  is not an option on the hosted service.
- **Spotify can't be the catalog.** New releases, categories, top tracks, recommendations,
  related artists, every "get several" batch endpoint, and the `popularity`, `label`, and
  `genres` fields are gone for new apps. Search returns at most 10 results per page.
  `external_ids` (UPC, ISRC) came back in March 2026, so a Spotify album can still be matched.
- **Playback inside MyTaste needs Premium.** Both the Web Playback SDK (the browser becomes a
  Spotify device) and Connect control (`PUT /me/player/play` on the user's phone, desktop app, or
  speaker) need Premium. The policy limits streaming to Premium users. Free users get the
  official embed (sometimes only 30-second clips) and "Listen on Spotify" links. `/me` no longer
  reports the plan, so MyTaste only learns it when playback is refused.
- **Spotify content keeps to itself.** The policy forbids products "integrated with streams or
  content from another service". The design guidelines say Spotify content must "never be seated
  next to content from similar services", gets its own row, and shows at most 20 items per row.
  Spotify cover art may not be cropped, overlaid with controls or text, or shown without a link
  back. Spotify content may only be stored "as strictly necessary" and kept up to date.
- **Refresh tokens last six months** from the user's sign-in
  ([June 2026](https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration)), and
  refreshing doesn't extend them. People reconnect twice a year.
- **Redirect URIs must be HTTPS**, except `http://127.0.0.1`. Plain-HTTP LAN addresses can't
  connect Spotify. An HTTPS name such as a Tailscale `*.ts.net` address can.

**SoundCloud** ([register an app](https://developers.soundcloud.com/docs/api/register-app),
[API terms](https://developers.soundcloud.com/docs/api/terms-of-use),
[widget API](https://developers.soundcloud.com/docs/api/html5-widget))

- **API keys need a paid Artist Pro subscription**, one app per account (May 2026). Each instance
  owner who wants "Connect SoundCloud" would need their own subscription. Every API call needs a
  token, even resolving a link.
- **No API key is needed for the widget or oEmbed.** `soundcloud.com/oembed` resolves a pasted
  track or set link to its title, uploader, artwork, and widget URL. The widget
  (`w.soundcloud.com/player`) plays full public tracks, and `SC.Widget` drives it from script.
  Go+ tracks are previews or blocked everywhere outside SoundCloud's own apps.
- **No mixing.** The terms forbid "any playback experience which aggregates and streams User
  Content with content from other services". They also forbid storing audio or keeping user
  content beyond the session. Attribution means the uploader, SoundCloud as the source, a
  backlink, and the official logo.

**Catalogs and matching**

- **MusicBrainz**: release groups (an album across all its editions) with dates, types, artists,
  tracklists, barcodes, and links to Spotify, Apple Music, Deezer, Bandcamp, and SoundCloud
  pages. Its core data is CC0; genre tags and ratings are CC BY-NC-SA. It allows 1 request per
  second per IP and needs a User-Agent with contact details. 503s from its global limiter were
  common (7 of 23 requests) even at half that rate. Found 15 of 15 popular 2024–25 albums, all
  with cover art, but it lags on brand-new and regional releases.
- **Cover Art Archive**: front covers at 250, 500, and 1200 px for about 3.2 million release
  groups. Each image goes through two redirects, takes about 1.8 s, and some mirrors return 500.
  No license is granted, the same footing as TMDB posters. Covers must be fetched once and served
  from a local cache.
- **ListenBrainz** (keyless): listen and listener counts for any set of release groups, each
  artist's albums by popularity, sitewide weekly and monthly charts (skewed toward K-pop), fresh
  releases (unranked, 76% with covers), and batch metadata by MBID. About 30 requests per 10 s.
- **Apple**: the marketing RSS feed lists the 100 most-played albums per country, updated daily.
  `lookup?upc=` finds an album by barcode. Covers come at up to the source size, often 1500 px.
  The feed is sometimes slow, and its art is meant to promote Apple Music next to a link to it.
- **Deezer** has the richest keyless API, but its terms limit it to "strictly private use within
  a family", its chart follows the server's IP country, and parts are already broken. Use it only
  as a fallback.
- **Not used**: Odesli/song.link closed its public API (401 `PUBLIC_API_ACCESS_DEPRECATED`). The
  Last.fm API needs a key on every instance and excludes its images.
- **Matching**: Spotify album → `external_ids.upc` → MusicBrainz `release?query=barcode:` (14 of
  15) → release group. MusicBrainz URL relationships answer the other way round, and also find
  Apple Music, Deezer, and Bandcamp pages. Tracks match on ISRC. SoundCloud uploads are rarely
  in MusicBrainz.

## The shape

- **Music is its own tab** beside Games. Its albums don't join All's smart collections. Saved
  albums do appear in mixed user collections.
- **Albums are MusicBrainz release groups.** Grids show MusicBrainz facts and Cover Art Archive
  covers, so they can mix freely with films, carry hover controls, and be stored durably.
  Spotify and SoundCloud say where an album plays, like streaming services do for a film.
- **Your Spotify library is your own data, brought in rather than mirrored.** Your saved albums
  and followed artists are matched to MusicBrainz and shown as ordinary albums. Albums that don't
  match yet appear in a separate Spotify row that follows Spotify's rules.
- **SoundCloud works without an account**: paste a track or set link to save it, and it plays in
  SoundCloud's own widget. Connecting a SoundCloud account needs an Artist Pro app, so it waits
  (see [Later](#later)).
- **One provider plays at a time.** A queue never mixes Spotify and SoundCloud, and no audio is
  stored.

## Model

`music/models.py`:

- `Album` is a release group: `mbid`, title, artist credit (names plus artist MBIDs), first
  release date, primary type (album, single, EP, broadcast, other), secondary types (live,
  compilation, soundtrack, remix, …), genres, `cover` (Cover Art Archive release MBID and image
  ID), barcodes of its releases, and `listens`/`listeners` from ListenBrainz. Facts keep their
  source's scale, as games do.
- `Track` is a recording within one chosen release: position, title, length, ISRCs.
- `SoundCloudItem` is a track or set that only exists on SoundCloud, keyed by its URN, with the
  uploader, permalink, and artwork URL.
- `MusicLinks` for an album: Spotify album ID, Apple Music ID, Deezer ID, Bandcamp and SoundCloud
  URLs, each with where it came from (MusicBrainz relationship, barcode search) and when it was
  checked.

Keys in URLs: `/music/albums/<mbid>`, `/music/soundcloud/<urn>`. Albums are merged only by MBID,
barcode, or ISRC, in `music/matching.py`. Never by title alone, except when a chart entry is
matched by artist, title, and release date together.

The catalog client (`music/musicbrainz.py`, `listenbrainz.py`, `coverart.py`, `apple.py`) owns
transport, the one-request-per-second queue, retries with backoff on 503 and 429, and the
User-Agent `MyTaste/<version> ( https://mytaste.cc )`. `MusicService` owns caching, matching, and
ordering, like `CatalogService` and `GamesService`. Grids are filled from ListenBrainz's batch
metadata and MusicBrainz searches of up to 100 MBIDs at a time (`rgid:a OR rgid:b …`). One
MusicBrainz request per album happens only when a detail opens, for the tracklist and links.

Covers are downloaded once at 250 and 500 px into `$data/covers/`, served from
`/music/covers/<release mbid>/<image id>-<size>.jpg`, and pruned by last use when they exceed a
size cap (default 500 MB). Missing covers fall back to the existing placeholder.

## The Music tab

### Collections

| Collection | Source | Order |
| --- | --- | --- |
| Popular | Apple Music's 100 most-played albums in the user's region, matched to MusicBrainz | Chart |
| Most listened | ListenBrainz's sitewide top albums this month | Listens |
| New releases | ListenBrainz fresh releases, albums and EPs, last 30 days | Date, or popularity once listens arrive |
| Your albums | Spotify saved albums | Date saved, or any sort |
| New from your artists | Release groups of followed and saved artists, last 90 days | Date |
| Genre collections | MusicBrainz genres, like the film genre collections | Popularity |

Popular and Most listened are two separate charts, side by side. Apple's is mainstream and
current, but each entry has to be matched to MusicBrainz, and an entry that doesn't match is left
out. ListenBrainz's already uses MBIDs, but it follows what its own listeners play. Popular is the
one place a non-MetaBrainz list decides the order. The album's Apple Music page is linked from its
details, as the feed's terms expect.

### Sorts and the global order

Every music sort works for every album: popularity (ListenBrainz listen count), release date,
title, artist, and date added. A collection that combines sources, such as Your albums, merges
them by the shared sort key, so page *n* is always the *n*th slice of one order, as for films
and games. SoundCloud items have no shared popularity scale. In a popularity order they are
spread every fourth card, like games in All.

### Filters

`MusicFilters` and one rule, `music_matches`, give the same answer for an album from any source:
type (album, EP, single, live, compilation, soundtrack), decade and year, genre, and "plays on"
(Spotify, SoundCloud, or either). Film filters never match an album, and music filters never match
a film, as with games. Grouping rows offer artist, decade, genre, and type.

### Search

A MusicBrainz release-group search (artist and title), ranked by ListenBrainz popularity among
the first 100 results, then the user's own saved and library matches. A pasted Spotify,
SoundCloud, Apple Music, or Bandcamp link goes straight to that album.

## Views

![Mockup of the Grid, Wall, List, Shelves, and Crate views, an album in a mixed collection, and
the player bar, drawn with made-up covers](screenshots/music-views-mockup.png)

The sidebar's Appearance section gets a **View** choice for music: Grid, Wall, List, and later
Crate. It is a display preference (`music_view` in `display_preferences`) that changes at once
without reloading. Card size (comfortable or compact) applies to Grid and to the grouped rows.

- **Grid**: square covers with 6 px corners, title, artist, and year below. Hover or focus
  shows Play and the + save button. Dots show where it plays when **Sources** is on.
- **Wall**: covers edge to edge with 2 px gaps and no text. The most popular or most recently
  added take a 2×2 tile. Hover shows the caption and Play.
- **List**: dense rows (cover, title, artist, type, year, tracks, length, where it plays) that
  sort by clicking a column. Used for large libraries.
- **Shelves**: the existing Grouping rows (by artist, decade, genre, or type), scrolling sideways.
- **Crate** (later): covers flip past in 3D, driven by arrow keys, the wheel, or a swipe; Enter
  plays. With reduced motion it becomes a flat carousel.

In a mixed user collection an album keeps the 2:3 card: the square cover on top, and below it a
panel with the type, title, artist, and Play.

Spotify artwork, shown only in the "not matched yet" row, follows Spotify's rules: no crop, no
controls over the image, 4 px corners on phones and 8 px elsewhere, at most 20 items, a link to
Spotify at the end of the row, and the Spotify logo in the row heading. SoundCloud items show the
uploader and the SoundCloud logo linking to the sound.

## Details

Album details reuse the details panel. The cover flies in from the card, the blurred cover is the
backdrop, and the panel shows the title, artist (linking to the artist's albums by popularity),
year, type, label, genres, and the tracklist of the release linked to Spotify (else the earliest
official release) with lengths. Play, Shuffle, and per-track play sit beside Save. **Listen on**
links (Spotify, Apple Music, Bandcamp, Deezer, SoundCloud) come from MusicBrainz relationships,
like the Watch button for films. When the user hasn't connected Spotify or isn't on Premium, the
official Spotify embed sits in the panel instead.

## Playing

### Spotify

Play turns the album into a Spotify album ID (MusicBrainz link, else a `upc:` search) and calls
`PUT /me/player/play` with `context_uri`, plus `offset` for a track. The device menu lists
**This browser** (a Web Playback SDK player started on first use) and the user's Spotify Connect
devices: phone, desktop app, speakers. If no device is active, MyTaste uses this browser. A 403 on
play with the reason `PREMIUM_REQUIRED`, or the SDK's `account_error`, marks the account Free.
From then on Play shows the embed and the "Listen on Spotify" link, with a note that full
playback in MyTaste needs Premium.

The SDK needs the user's access token in the page, so `/api/music/spotify/token` returns a
fresh one-hour token to its signed-in owner only. Refresh tokens never leave the server.

### SoundCloud

SoundCloud plays in its official widget, docked in the player bar where the controls would be.
The widget carries SoundCloud's attribution, and `SC.Widget` reports progress and the end of the
track to MyTaste. Starting a Spotify album stops the widget, and starting a SoundCloud track
pauses Spotify. A queue never holds both.

### The player bar

A bar at the bottom of every page shows the cover, title, artist, a progress bar, previous,
play/pause, next, and the device menu (Spotify) or the docked widget (SoundCloud). The Media
Session API puts the track on the lock screen and makes the keyboard's media keys work.
`/watch/...` pauses music when a film starts.

**Music keeps playing while you browse.** Music on another Spotify device is unaffected by page
loads. Music in this browser would stop on every page load, because the SDK player and the
widget live in the page. Links and GET forms inside the app therefore load the next page with
`fetch`, swap the header and `<main>`, and update the history. Back and forward work, and an
error falls back to a normal page load. To make that possible, `app.js` and `games.js` bind their
`document` and `window` listeners and observers to a per-page `AbortSignal`, then run again on the
new content. Today they have 15 such listeners, plus a few observers and timers.

## Connecting services

On the **Services** page, **Add** gains a Music step with Spotify and SoundCloud. Each person
connects their own accounts (`music_accounts`, per user).

- **Spotify** appears once the owner sets `MYTASTE_SPOTIFY_CLIENT_ID` and
  `MYTASTE_SPOTIFY_CLIENT_SECRET`. Until then, the admin sees setup steps: create an app on the
  Spotify developer dashboard with a Premium account, register the exact callback MyTaste shows
  (`https://<host>/music/spotify/callback`, built from the forwarded host like the Steam
  callback), and add each person's Spotify email under Users Management, five at most. Members
  see "Ask the owner to set up Spotify". Sign-in is the authorization code flow with PKCE and
  `state` in an HttpOnly cookie. The scopes are `user-library-read`, `user-follow-read`,
  `user-read-playback-state`, `user-modify-playback-state`, and `streaming`. A Spotify user who
  isn't on the allowlist gets a 403 on every call. MyTaste explains that the owner has to add
  them.
- **SoundCloud** needs no setup and no account. Pasted links play in the widget. Connecting an
  account is in [Later](#later).
- **Library refresh**: saved albums and followed artists are read after connecting, then every
  six hours or on demand, like Steam's owned games. They are matched to MusicBrainz in the
  background, and a failed refresh keeps the previous list.
- **Reconnect**: MyTaste stores when the user signed in and asks them to reconnect a week before
  the six-month limit. `invalid_grant` marks the account as needing reconnection rather than
  retrying.
- **Disconnect** deletes the tokens and everything read from that account (its library rows and
  Spotify-only snapshots) at once. Spotify requires deletion within five days. Spotify has no
  revoke endpoint, so the page links to the user's Spotify app settings.
- **Hosted mode**: the music catalog, saving, Listen-on links, embeds, and SoundCloud's widget
  work. Spotify accounts are turned off, because Spotify's terms don't allow one shared app for
  strangers.

## Saving music to collections

The + button and the details Save menu work for albums and SoundCloud tracks and sets as for
films and games. Songs, Spotify playlists, and artists come later.

- **Migration**: `saved_items.kind` is limited to `movie`, `tv`, and `game` by a CHECK
  constraint, which SQLite can't alter. The table is rebuilt once in a transaction, using
  SQLite's documented procedure: create the new table, copy, drop, rename, recreate the indexes,
  then run `foreign_key_check` before committing. IDs, portable IDs, and every entry's order and
  date are preserved. Nothing is deleted from the old data, and the live database is backed up
  first. The new kinds are `album`, `track`, `playlist`, and `artist`. SoundCloud tracks use
  `track` and SoundCloud sets use `playlist`. `artist` and Spotify's tracks and playlists are
  allowed now so the later steps don't need a second rebuild. The new columns are `mbid`,
  `spotify_id`, and `soundcloud_urn`, each with a partial unique index per kind.
- **Snapshots**: an album's snapshot holds MusicBrainz facts (CC0), so it is durable like a film's.
  A Spotify-only or SoundCloud item keeps only what is needed to show and play it (ID, title,
  artist or uploader, artwork URL, permalink). That snapshot is refreshed when shown and older
  than a day, and removed from view if the provider drops the item.
- **Portable IDs and `.taste`**: `album-mb-<mbid>`, `album-spotify-<id>`,
  `track-soundcloud-<urn digits>`, and `playlist-soundcloud-<urn digits>`. The draft already
  defines `music.album` (ids `musicbrainz`, `spotify`, `upc`) and `music.track` (`musicbrainz`,
  `spotify`, `isrc`). `music.playlist` and the `soundcloud` ID name are proposed to the taste
  repository before export ships. Availability uses services `spotify`, `soundcloud`,
  `apple-music`, and `bandcamp`.

## Storage

| Table | Holds | Scope |
| --- | --- | --- |
| `music_release_groups` | Catalog facts per MBID, with fetch time | shared cache |
| `music_links` | MBID ↔ Spotify, Apple, Deezer, Bandcamp, SoundCloud, with source and check time | shared cache, refreshed weekly |
| `music_popularity` | ListenBrainz listens and listeners per MBID | shared cache, refreshed daily |
| `music_accounts` | Provider, stable account ID, name, tokens, sign-in time, scopes, Free/Premium once known | per user |
| `music_library` | The user's saved albums and followed artists per provider, with MBID once matched | per user |
| `saved_items` (rebuilt) | Saved albums, tracks, playlists beside films and games | shared, entries per user |
| `display_preferences.music_view` | Grid, Wall, List, Crate | per user |

The database now holds account tokens, so backups must be treated as secrets. `docs/deployment.md`
will say so.

## Order of work

Each step is useful on its own and ships as its own pull request.

1. **Catalog and views, no accounts.** The music package, MusicBrainz/ListenBrainz/Cover Art
   Archive/Apple clients with the request queue and cover cache, the Music tab with Popular, Most
   listened, New releases, and genres, search, filters, sorts, Grid/Wall/List/Shelves, album
   details with Listen-on links and the Spotify embed, and saving albums (with the `saved_items`
   rebuild). Done when a fresh instance with no music settings browses, saves, and opens albums,
   an existing database upgrades with every collection intact, and a cold Popular page renders
   within MusicBrainz's rate limit.
2. **Spotify.** Setup, connect, and disconnect on Services. Your albums and New from your artists
   with matching. Play through Connect and the Web Playback SDK. The player bar, Free/Premium
   handling, and reconnect prompts. Done when two people at home each play their own Spotify on
   different devices, and a Free account sees the embed instead of an error.
3. **Keep playing while browsing.** Fetch-and-swap navigation for the whole app, per-page
   listeners in `app.js` and `games.js`, and Media Session. Done when an album started in this
   browser keeps playing across collections, details, search, settings, and back/forward.
4. **SoundCloud.** Saving pasted track and set links with oEmbed, and the docked widget in the
   player bar.

Tests use recorded responses for every provider and never call live services, as the existing
web tests do. The migration is tested on a copy of a database with films, series, and games in
several collections.

## Decisions

The owner answered the plan's questions on October 19, 2026.

1. **MusicBrainz is the catalog**, and Spotify and SoundCloud are where music plays. MusicBrainz
   won't have everything, so another catalog may fill the gaps later.
2. **Both charts**: Popular (Apple Music, the user's region) and Most listened (ListenBrainz) are
   separate collections.
3. **No Artist Pro**, so SoundCloud is pasted links and the widget. Connect SoundCloud waits.
4. **Fetch-and-swap navigation** keeps music playing. Full page reloads go.
5. **Albums and SoundCloud tracks and sets are saved first.** The rest is listed under Later.
6. **Music stays out of All.**

## Later

Postponed from the first four steps:

- **Saving songs.** MusicBrainz recordings and Spotify tracks as `track` items, matched by ISRC,
  with a song row in the List view and per-song Save in details.
- **Spotify playlists.** Saved as `playlist` items that keep only the ID and what is needed to
  show and play them, refreshed from Spotify, and shown in Spotify-only rows under its rules.
- **Artists.** Saving artists to collections and a full artist page (discography by type,
  popularity, links), as `artist` items keyed by MBID.
- **Connect SoundCloud** (likes, playlists, and follows in Your albums). It needs an Artist Pro
  app per instance, set with `MYTASTE_SOUNDCLOUD_CLIENT_ID` and `_SECRET`, OAuth 2.1 with PKCE,
  and single-use refresh tokens that are stored again on each refresh.
- **The Crate view.**
- **`.taste` export of music**, once `music.playlist` and the `soundcloud` ID name are accepted in
  the taste repository.
- **Apple Music and Bandcamp as play targets**, opening the album there from Play.
- **More catalogs for the gaps.** MusicBrainz lags on brand-new, regional, and independent
  releases. A second source can supply albums it lacks: Apple's keyless lookup and search (by
  barcode, then artist and title), or Discogs or Deezer if their terms fit. Such an album gets its
  own key, such as `album-apple-<id>`, and is merged into the MusicBrainz album by barcode once
  that exists, keeping its collection entries. Until then, unmatched albums link to Harmony, which
  imports a Spotify or Apple release into MusicBrainz, so users can fill the gap themselves.

Further out:

- Local music files in storage libraries (FLAC, MP3, AAC), played by the existing playback stack.
  No provider rules apply, so they can sit in any grid or queue.
- [Spotify Soloist](https://developer.spotify.com/documentation/soloist) on the Pi to make the
  server itself a Spotify speaker in the living room.
- A MusicBrainz mirror or shared catalog cache on the Hub, to spare MusicBrainz as instances grow.
- Sending plays to ListenBrainz when a user links their account.

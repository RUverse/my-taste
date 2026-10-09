# Accounts, the hosted service, and the Hub

MyTaste is used in three ways that share one codebase:

1. **Self-hosted**: someone runs MyTaste on their own machine for themselves and the people they
   invite, streaming their own libraries.
2. **Hosted**: people sign in to a MyTaste instance that we run and use the same features,
   without running anything.
3. **Shared**: users of either kind can publish collections and submit ratings to a central
   service, and browse and add what others have shared.

This document describes how the app gets users, how it connects to that central service (the
**Hub**), and the order to build it in. The app stays open source in this repository; the Hub is
a separate service whose code starts out private. This document covers only what the app needs
from the Hub: its accounts, its sign-in, and its API.

## Decisions

- **One app, two modes.** `MYTASTE_MODE=self-hosted` (the default) or `hosted`. The hosted
  service is our own instance of the same app, not a fork.
- **Every instance has local accounts.** They work without the Hub and without an internet
  connection to us. The first account created on an instance is its owner.
- **Hub accounts can sign in to any instance**, like Plex accounts sign in to any Plex server.
  An instance owner can invite friends by their Hub account, and the same login works on every
  instance they are invited to. On the hosted service, Hub accounts are the only way in.
- **Nothing leaves a self-hosted instance unless a user asks.** Publishing a collection or
  turning on ratings sends exactly that, to the Hub, as that user. File names, folders,
  libraries, and watch history are never sent.
- **Collections travel as `.taste` files** ([format](https://github.com/RUverse/taste)), in both
  directions. See [Portable collections](taste-collections.md) for the field mapping.
- **The hosted service does not stream files for now.** Local libraries and playback are
  turned off there; streaming services, games, and collections work as usual. Linked cloud
  storage may come later.

## Pieces

```
 Self-hosted MyTaste                  Hosted MyTaste (ours)
 local + Hub accounts                 Hub accounts only
 libraries and playback               no libraries or playback
            \                               /
             \ sign-in, publish, rate      / sign-in, publish, rate
              v                           v
                     MyTaste Hub (ours)
          accounts · sign-in · collections · ratings
```

Instances only ever call out to the Hub; the Hub never needs to reach an instance. That keeps
self-hosted instances working behind home routers, VPNs, and tailnets.

## Accounts in the app

### Users and roles

- `owner`: the first account. Manages users, libraries, folders, service API keys, and instance
  settings. There is exactly one owner; ownership can be handed to an admin.
- `admin`: everything the owner can do except removing the owner.
- `member`: uses the app with their own preferences and collections, and sees the libraries the
  owner has shared with them.

A user signs in with a local password, with their Hub account, or both once linked. A local
user can link one Hub account; the link is what lets them publish and rate.

At home, people switch profiles the way they do on a TV. The sign-in page can show a **"Who's
watching?"** screen with everyone's profiles: picking one opens it, after its PIN or password if
it has one. Members may use a short PIN or nothing at all; the owner and admins always use a
password. The owner can turn the screen off, and then everyone signs in with a username and
password.

### Sign-in and sessions

- **Local passwords and PINs** are hashed with `hashlib.scrypt` from the standard library, with a
  random salt per user and parameters stored next to the hash so they can be raised later.
  After five wrong tries a profile waits before the next one, for a while that grows.
- **Hub sign-in** uses OpenID Connect with the authorization code flow and PKCE. Each instance
  registers with the Hub once (the owner clicks "Connect this server to MyTaste") and gets a
  client id; its redirect URL is the address the owner uses, so it works on a LAN or tailnet
  because the browser, not the Hub, follows the redirect.
- **Sessions** live in the database. The browser gets a random session id in an `HttpOnly`,
  `Secure` (when served over HTTPS), `SameSite=Lax` cookie. Changing requests must also come from
  the same origin (`Origin` header check). Signing out deletes the session; the owner can sign a
  user out everywhere.
- **Hub tokens.** When a user links their Hub account, the instance keeps that user's refresh
  token (encrypted with a key generated on first start and kept outside the database) and uses
  it only for that user's publishing and rating.
- **First start.** If there are no users, every page redirects to a setup page that creates the
  owner. Existing single-profile data becomes the owner's.
- **Invites.** The owner invites someone by Hub username or email (Hub sign-in), or creates a
  local account with a one-time setup link (no Hub needed).

### What becomes per user

Today the database holds one profile. These tables split by user (a `user_id` column, or a key
that includes it):

| Data | Table today | After |
| --- | --- | --- |
| Region | `preferences` (one row) | per user |
| Streaming subscriptions | `subscriptions` | per user |
| Card and sidebar options | `display_preferences` (one row) | per user, except `site_title`, which becomes an instance setting |
| Collections and their entries | `collections`, `collection_entries` | per user; a shared collection keeps its Hub id |
| Watch progress, Continue watching | `playstate` | per user |
| Game Pass plan and platform | `game_preferences` (one row) | per user |
| Steam account and owned games | `steam_account` (one row) | per user |

These stay shared by the whole instance: libraries and their folders, items, and files; probe
results (`media_info`); title facts; Xbox ↔ Steam matches; the Game Pass cache; title
availability (it is keyed by region already); and `saved_items`, the catalogue of title
snapshots that collection entries point at.

**Library access.** A new `library_access (library_id, user_id)` table says who can see each
library. The owner sees everything. Every place that lists library titles, opens a title,
resolves a file for playback (`PlaybackService.resolve_path`), or reports Continue watching
checks it, so a member cannot reach another library's file by guessing ids.

**Migration.** The first start of the new version adds the `users` table and the new columns,
creates no user yet, and marks every existing row as belonging to user 1. The setup page then
creates user 1 as the owner, so nothing has to be copied. Singleton tables (`CHECK (id = 1)`)
are rebuilt with `user_id` as the key, through explicit migrations as the storage modules
already do. Back up the database before the first start.

### What changes on screen

- A sign-in page, the first-start setup page, and a small account menu (switch user, sign out,
  link Hub account).
- Settings split into **Your settings** (region, services, display, Steam, Game Pass) and
  **Server settings** for admins (users and invites, libraries and who can see them, instance
  name, Hub connection).
- `/watch/...`, the APIs, and every page require a session. `/healthz` stays open.

## The hosted mode

`MYTASTE_MODE=hosted` changes these things and nothing else:

- Library, folder, and playback routes are not registered, and their sidebar sections and
  settings are hidden.
- Sign-in is only through the Hub, and a Hub account signing in for the first time gets a
  member account automatically. There is no owner; operators use admin accounts.
- Storage moves to Postgres, because one database serves many people and several app
  processes. The storage modules already sit behind small repository classes; those get a
  Postgres implementation alongside the SQLite one. Self-hosted instances keep SQLite.
- Per-user Steam sign-in and Game Pass choices work as on a self-hosted instance.

## What the app needs from the Hub

The Hub's code is private, but its interface is part of the app's contract:

- **OpenID Connect provider**: discovery document, authorization and token endpoints, a
  `userinfo` endpoint with a stable subject id, username, and display name; dynamic client
  registration for instances.
- **Collections**: upload a `.taste` file as a new collection or a new version of one the user
  published before; fetch a collection as `.taste`; list and search public collections; delete
  one's own.
- **Ratings**: set or clear the user's rating of a title, identified the same way
  `saved_items` identifies it (TMDB movie or series id, Steam app id, Xbox product id); read a
  title's community rating (count and score).

Ratings need care because anyone can run an instance. The Hub counts at most one rating per Hub
account per title, rate-limits changes, and may weigh new accounts less. Scores are shown as a
count plus a damped average, so a handful of votes cannot top a list.

## Order of work

Each step is useful on its own and ships as its own pull requests.

1. **Users in the app** ([#24](https://github.com/RUverse/my-taste/pull/24)). Local accounts,
   the profile screen, sessions, setup page, roles, per-user data, library access, and the
   migration. No Hub yet. Done when two users on one instance see their own
   services, collections, and Continue watching, a member cannot open a library they were not
   given (including by URL), and an existing database upgrades with all its data under the
   owner.
2. **Hub, first version** (private repository `RUverse/mytaste-hub`). Accounts, OpenID
   Connect, instance registration.
   In the app: "Connect this server to MyTaste", "Sign in with MyTaste", invites by Hub account,
   and linking a local user to a Hub account. The landing page's Login and a waitlist move onto
   the Hub at this point.
3. **Sharing collections.** `.taste` export and import in the app (the remaining milestones of
   [Portable collections](taste-collections.md)), then publishing to and adding from the Hub.
4. **Ratings.** Rate titles in the app, send them to the Hub when the user has turned that on,
   and show community ratings.
5. **Hosted mode.** The mode switch, Postgres storage, and running our instance.

## Open questions

- How the Hub sends email (invites, password resets for Hub accounts) and from which domain.
- Account deletion and data export for Hub accounts, and how deleting one affects collections
  others have added (they keep their own copy, since adding a collection copies it).
- Whether members of a self-hosted instance may connect their own Steam accounts, or only the
  owner (the Steam Web API key belongs to the owner).

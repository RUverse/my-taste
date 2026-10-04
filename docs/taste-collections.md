# Portable collections with the taste format

Design MyTaste's saved collections so they can later import and export `.taste` files, including
collections that mix movies, series, and games. Implement the shared identity and persistence
foundation before adding game saving; the import/export interface can follow later. The Game
Pass catalog cache remains optional and disabled by default. Portable collection data is durable
user data and must survive cache clearing or a title leaving Game Pass.

## Draft reviewed

This plan follows the `.taste` **0.1 draft** in
[RUverse/taste at commit df7d7ad](https://github.com/RUverse/taste/tree/df7d7ad2ac4f2544ba716324817c067a0cb9f807),
specifically the [specification](https://github.com/RUverse/taste/blob/df7d7ad2ac4f2544ba716324817c067a0cb9f807/spec/SPEC.md),
[schema](https://github.com/RUverse/taste/blob/df7d7ad2ac4f2544ba716324817c067a0cb9f807/spec/taste.schema.json),
Python container/document/validation code, and mixed-media fixtures. This is a proposed MyTaste
adapter. The initial Games release adds stable collection IDs, kind-qualified TMDB/Store item
IDs, and snapshot builders in `collections/portable.py`. It does not add game saving, shared
saved-item persistence, archive serialization, or import/export controls. Those follow the
milestones below; no format changes are proposed here.

The archive is a ZIP containing `mimetype`, `taste.json`, and optional files addressed by SHA-256.
The manifest separates shared media items from ordered entries in collections. Collections,
entries, items, and files can carry metadata that MyTaste does not yet display. The draft
requires writers to preserve unknown fields and supports unknown media kinds. Version 0.2 is
not automatically compatible with 0.1, so the adapter must explicitly support each draft version.

## Identity and storage decisions to make now

Keep three identities distinct: MyTaste's database primary key, a stable portable item ID, and
external catalog IDs. Portable IDs must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; colons are
invalid. Use IDs such as `movie-tmdb-153`, `tv-tmdb-153`, and `game-xbox-9NPDN9R45JX4`, or a
stable generated ID for an item without a catalog match. Assign them once and persist them;
renaming an item must not change its portable ID. Assign portable collection IDs independently
of collection names and the local integer collection ID.

Represent identity as a media kind plus namespaced external IDs. `movie` with `ids.tmdb = 153`
and `tv` with `ids.tmdb = 153` are different identities. The proposed MyTaste convention for
Microsoft products is `ids.microsoft_store = "9NPDN9R45JX4"`. The draft accepts arbitrary ID
names with string/integer values, so it needs no schema change. `microsoft_store` is our proposed
namespace, not an existing standard mandated by the draft; coordinate its naming in the taste
repo before publishing interoperable files. Add IGDB or Steam IDs later only when verified.

Use a shared saved-item table and an ordered collection-entry table instead of a separate game
collection store. A saved item holds its kind, portable ID, external IDs, metadata snapshot,
files, and preserved JSON. An entry holds its collection/item relationship, explicit position,
note, selected file IDs, addition time, actor, reason, and preserved JSON. Index exact provider
identities for resolution. Keep provider catalog models separate; adapt them into this generic
saved-item representation when saving rather than changing every TMDB model to accept games.

An explicit SQLite migration must preserve existing integer collection IDs and routes, saved
movie/TV metadata, addition timestamps, and entry order. The current repository orders entries
by `added_at, rowid`; persist that order as positions during migration. Generate stable portable
IDs and move repeated media snapshots into shared items without losing differing entry data.
Keep movie/TV route adapters working while game saving uses string provider IDs. Do not rely on
`CREATE TABLE IF NOT EXISTS` to remove the old movie/TV constraints.

Preserve complete imported JSON at document, collection, entry, item, link, availability, and
file levels, including fields that the UI does not understand. Keep the source document context
and ID mapping so document-level extensions survive re-export. Treat editable columns as
projections of this stored representation; an edit patches the intended fields rather than
rebuilding the record from the handful of fields shown in the UI. Preserve unsupported kinds
as generic saved items and label them in the interface, even if they have no catalog adapter.

## Field mapping

| MyTaste data | Draft field | Behavior |
| --- | --- | --- |
| Collection title and description | `name`, `description` | Preserve `vibe` separately; do not infer or overwrite it from description |
| Collection icon and ordering | `icon`, collection array order | Preserve unsupported icon names; show a fallback icon locally |
| Membership and saved order | `entries[].item`, entries array order | Export the saved order, independently of a temporary browse sort |
| Per-collection annotation | Entry `note`, `reason`, `show` | Keep it on the entry, allowing one media item to have different notes in different collections |
| Title, kind, year, synopsis | Item `title`, `kind`, `year`, `summary` | Map movies to `movie`, series to `tv`, games to `game` |
| TMDB and Microsoft IDs | Item `ids` | Preserve names and numeric/string value types; never match by title alone |
| Developer, publisher, genres, platform, release date, provider ratings | Item `meta` | Separate provider facts from the user's tags; retain rating source and scale |
| User labels and personal ratings | `tags` at the applicable level | Preserve the supported scalar and string-array types |
| Official product or detail page | Item `links` | Keep labeled links independent of current subscription availability |
| Availability snapshot | Item `availability` | Export known `service`, `region`, `type`, `url`, and original `checked` time |
| Covers, posters, screenshots, thumbnails | Collection `cover`, item `files` | Retain file IDs, roles, captions, location metadata, and blob/URL references |
| Creation/addition and attribution | `created`, `added`, `added_by` | Keep collection, item, and membership timestamps/actors distinct |
| MyTaste display/default-sort preferences | `x-mytaste` extensions | Add only necessary app metadata without replacing other tools' extensions |

For Game Pass availability, propose service name `xbox-game-pass` and an `x-mytaste` extension
on each offer for its verified plan/platform context. These are proposed adapter conventions;
the draft has no standard subscription-tier field. Keep them as extensions unless the format
later adopts a common representation. Imported availability is a dated snapshot, not proof
of entitlement. Resolve and recheck it when browsing, without deleting the saved item or
overwriting unrelated user metadata. Preserve unknown service names when resolution is unavailable.

For example, this minimal proposed export uses a valid portable ID and keeps the Store ID separate:

```json
{
  "taste": "0.1",
  "collections": [
    {
      "id": "games-for-a-rainy-day",
      "name": "Games for a rainy day",
      "entries": [{"item": "game-xbox-9NPDN9R45JX4", "note": "Try this next."}]
    }
  ],
  "items": {
    "game-xbox-9NPDN9R45JX4": {
      "kind": "game",
      "title": "1000xRESIST",
      "ids": {"microsoft_store": "9NPDN9R45JX4"},
      "meta": {"developers": ["sunset visitor 斜陽過客"]}
    }
  }
}
```

## Later export behavior

Export one or several user collections as a `.taste` download. Read all saved entries directly
from persistence, including unavailable, unmatched, unsupported, and currently filtered-out
items. Do not export just the visible catalog page. Store each shared item once and include
referenced parent items and files needed to keep `parent` and `show` references valid. Keep
collection and membership order deterministic; preserve unknown fields and referenced blobs.

Export smart/provider collections as explicitly dated snapshots when requested. The draft
does not describe executable queries, so a smart collection export must not imply that it
will keep updating. Any preserved MyTaste query belongs in an extension, alongside the actual
snapshot entries.

Start with a lightweight mode using existing remote image references and no automatic asset
downloads. Preserve imported embedded files rather than converting them to URLs. A later
self-contained export can fetch selected posters/screenshots and write them as SHA-256 blobs,
including deduplicated thumbnails. Use verified media types; omit an optional file or report
it if its type cannot be established rather than guessing from a URL alone.

Use the reference Python package `taste-format` behind an adapter such as
`collections/portable.py`, pinning the supported release or reviewed repository revision.
Do not fork ZIP/manifest serialization into game-provider code. Its atomic writer already
implements the required container layout. Build downloads in a worker thread with temporary
files cleaned up after streaming; validate the result before offering it to the browser.

## Later import behavior

Validate the archive, version, manifest, references, and blob hashes before applying changes.
Opening `Taste` is not enough: the draft reader reports some container issues separately and
does not automatically perform all manifest validation. Wrap the library with bounded upload,
entry-count, manifest, and expanded-byte limits before reading archive contents. Read entries
without extracting arbitrary paths and keep remote links passive during import.

Offer a preview listing collections, supported/unsupported kinds, matching items, and conflicts.
Default to creating new collections; users can explicitly choose a merge. Resolve exact known
provider IDs with the media kind. Leave items without a verified match as imported snapshots.
Do not use name similarity as an automatic identity match, and do not treat archive-local IDs
as globally unique across unrelated imports. Remap collisions consistently, including entries,
parents, and selected file references. Preserve imported IDs whenever possible.

Make repeated import/merge idempotent for the selected document and destination. Preserve
different per-collection notes and report metadata conflicts rather than silently discarding
either payload. Resolve conflicting edits deliberately, retaining provenance and unknown data.
An item that leaves Game Pass stays saved; current availability affects browsing only.

Store imported referenced blobs and source metadata in managed application data storage, not
the Game Pass cache or playback segment directory. Include that storage in backup/deployment
documentation when import ships. Stage and validate blobs before the database transaction,
commit collection changes together, and clean up unreferenced staged files after failure.
Imported attachments are not automatically registered as local playback libraries.

Milestone 1 started with Steam support: saved movies, series, and games now share
`saved_items` and ordered `collection_entries`. Preservation fields for imported JSON are
still to come.

## Implementation milestones and verification

1. Add portable identities, generic saved-item/entry persistence, preservation fields, and an
   explicit migration before game saving. Keep catalog caches independent of saved snapshots.
2. Add provider-to-saved-item adapters for existing movies/TV and Game Pass games. Establish
   the ID/service/extension conventions with the taste draft before shipping exchange files.
3. Add `.taste` export through the reference library, followed by import preview and transactional
   create/merge. Deliver the exchange controls after the initial Game Pass browsing interface.
4. Add optional asset embedding, further media resolvers, and supported draft-version upgrades
   as separate changes. Neither import nor export should require Game Pass caching to be enabled.

Before shipping the identity foundation, test existing SQLite migrations, shared media in two
collections, distinct movie/TV IDs with the same number, string Store IDs, stable portable IDs
after rename, explicit entry order, and persistence after catalog cache clearing.

Before shipping exchange, validate generated archives with the pinned reference library and
check them against the draft's shared fixtures. Test import/export/import for mixed kinds,
unknown fields at every level, unsupported services/icons, empty collections, notes/tags/actors,
parents, selected files, covers, thumbnails, and blob hashes. Include unavailable saved titles,
repeated imports, conflicting IDs, rollback, invalid archives, and unknown draft versions.
Round-trip preservation means semantic equality of user data, allowing documented changes to
generator/modified timestamps and any deliberate ID remapping. Re-exporting an imported file
must retain its supported and unsupported content even when the UI displays only part of it.

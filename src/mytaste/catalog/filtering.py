"""Apply a view's filters to the titles TMDB could not filter itself."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mytaste.catalog.filters import LocalFacets, TitleFacts, TitleFilters, title_matches
from mytaste.catalog.models import CatalogItem

TitleKey = tuple[str, int]


class FilterResolver:
    """Check local, saved, credited, and searched titles against one request's filters.

    TMDB filters discovered streaming titles itself (see ``catalog.filters.discover_params``);
    everything else is checked here with the same rules, using the facts TMDB gives per title
    and what the local files contain, so a title passes or fails wherever it comes from.
    """

    def __init__(
        self,
        filters: TitleFilters,
        *,
        facts: Any,
        library: Any,
        playback: Any,
        library_ids: Sequence[int],
        local: Mapping[int, LocalFacets] | None = None,
    ) -> None:
        """``local`` passes in ``playback.local_facets()`` when the caller already has it."""

        self.filters = filters
        self.facts = facts
        self.library = library
        self.playback = playback
        self.library_ids = tuple(library_ids)
        self._local = local
        self._local_by_key: dict[TitleKey, LocalFacets] | None = None

    @property
    def active(self) -> bool:
        return self.filters.active

    def _local_facets(self) -> Mapping[int, LocalFacets]:
        if self._local is None:
            self._local = self.playback.local_facets() if self.filters.needs_local else {}
        return self._local

    def _by_key(self) -> dict[TitleKey, LocalFacets]:
        """The local facets of matched titles in the chosen libraries, by TMDB key."""

        if self._local_by_key is None:
            found: dict[TitleKey, LocalFacets] = {}
            if self.filters.needs_local and self.library_ids:
                local = self._local_facets()
                for ref in self.library.title_refs(self.library_ids):
                    facets = local.get(ref.item_id)
                    if ref.tmdb_id is None or facets is None:
                        continue
                    key = (ref.media_type, ref.tmdb_id)
                    found[key] = found[key].merge(facets) if key in found else facets
            self._local_by_key = found
        return self._local_by_key

    async def _facts(self, keys: Sequence[TitleKey]) -> dict[TitleKey, TitleFacts]:
        if not self.filters.needs_facts or not keys:
            return {}
        return await self.facts.facts(keys)

    async def local_item_ids(self) -> frozenset[int] | None:
        """The library titles that pass, or ``None`` when nothing is filtered."""

        if not self.active:
            return None
        refs = self.library.title_refs(self.library_ids) if self.library_ids else ()
        facts = await self._facts(
            [(ref.media_type, ref.tmdb_id) for ref in refs if ref.tmdb_id is not None]
        )
        local = self._local_facets()
        return frozenset(
            ref.item_id
            for ref in refs
            if title_matches(
                self.filters,
                genre_ids=ref.genre_ids,
                facts=facts.get((ref.media_type, ref.tmdb_id)) if ref.tmdb_id else None,
                local=local.get(ref.item_id),
            )
        )

    async def allowed(self, items: Sequence[CatalogItem]) -> frozenset[TitleKey]:
        """The keys of the given TMDB titles that pass the filters."""

        keys = [(item.media_type, item.id) for item in items if item.id > 0]
        if not self.active:
            return frozenset(keys)
        facts = await self._facts(keys)
        local = self._by_key()
        return frozenset(
            (item.media_type, item.id)
            for item in items
            if item.id > 0
            and title_matches(
                self.filters,
                genre_ids=item.genre_ids,
                facts=facts.get((item.media_type, item.id)),
                local=local.get((item.media_type, item.id)),
            )
        )

    def streaming_exclude(self) -> frozenset[TitleKey]:
        """Streaming titles TMDB returns that their local copies rule out.

        Only watch progress does this: TMDB cannot know what was watched here.
        """

        if not self.filters.watch:
            return frozenset()
        return frozenset(
            key for key, facets in self._by_key().items() if facets.watch != self.filters.watch
        )

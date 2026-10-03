"""Filters beyond release year and rating, and the facts they are checked against.

Every filter has to give the same answer for a title wherever it comes from: TMDB applies the
detail filters to streaming results (see ``discover_params``), while titles from local
libraries, user collections, people's credits, and search results are checked with
``title_matches`` against facts read from TMDB once per title (``TitleFacts``) and against what
was found in the title's files (``LocalFacets``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from mytaste.catalog.models import Genre, MediaType

WatchStatus = Literal["", "unwatched", "in_progress", "watched"]
PersonRole = Literal["cast", "director", "writer", "producer"]

PERSON_ROLES: tuple[PersonRole, ...] = ("cast", "director", "writer", "producer")
WATCH_STATUSES: tuple[tuple[WatchStatus, str], ...] = (
    ("unwatched", "Unwatched"),
    ("in_progress", "In progress"),
    ("watched", "Watched"),
)
RESOLUTIONS: tuple[str, ...] = ("4K", "1080p", "720p", "576p", "480p", "SD")
DYNAMIC_RANGES: tuple[tuple[str, str], ...] = (("hdr", "HDR"), ("sdr", "SDR"))
CHANNEL_LAYOUTS: tuple[str, ...] = ("Mono", "Stereo", "5.1", "6.1", "7.1")
_SELF_CREDIT = re.compile(r"\b(?:self|himself|herself|themselves)\b", re.IGNORECASE)
_PRODUCER_JOBS = frozenset({"Producer", "Executive Producer"})

# TMDB names some series genres after two movie genres; each movie genre stands for the pair.
_TV_ALIASES: dict[str, tuple[str, ...]] = {
    "Action & Adventure": ("Action", "Adventure"),
    "Sci-Fi & Fantasy": ("Science Fiction", "Fantasy"),
    "War & Politics": ("War",),
}


@dataclass(frozen=True, slots=True)
class TitleFilters:
    """The filters of a browse view; empty fields do not filter.

    Values in one field are alternatives ("any of"); different fields must all match.
    """

    genre_ids: tuple[int, ...] = ()
    exclude_genre_ids: tuple[int, ...] = ()
    certifications: tuple[str, ...] = ()
    # The country whose content ratings ``certifications`` names (the user's region).
    certification_country: str = ""
    countries: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    runtime_min: int | None = None
    runtime_max: int | None = None
    keyword_ids: tuple[int, ...] = ()
    cast: tuple[int, ...] = ()
    directors: tuple[int, ...] = ()
    writers: tuple[int, ...] = ()
    producers: tuple[int, ...] = ()
    watch: WatchStatus = ""
    resolutions: tuple[str, ...] = ()
    video_codecs: tuple[str, ...] = ()
    dynamic_ranges: tuple[str, ...] = ()
    audio_codecs: tuple[str, ...] = ()
    audio_channels: tuple[str, ...] = ()
    audio_languages: tuple[str, ...] = ()
    subtitle_languages: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self != TitleFilters(certification_country=self.certification_country)

    def people(self, role: PersonRole) -> tuple[int, ...]:
        return {
            "cast": self.cast,
            "director": self.directors,
            "writer": self.writers,
            "producer": self.producers,
        }[role]

    @property
    def has_people(self) -> bool:
        return bool(self.cast or self.directors or self.writers or self.producers)

    @property
    def has_runtime(self) -> bool:
        return self.runtime_min is not None or self.runtime_max is not None

    @property
    def needs_facts(self) -> bool:
        """Whether titles must be checked against facts TMDB only gives per title."""

        return bool(
            self.certifications
            or self.countries
            or self.languages
            or self.has_runtime
            or self.keyword_ids
            or self.has_people
        )

    @property
    def has_file_filters(self) -> bool:
        return bool(
            self.resolutions
            or self.video_codecs
            or self.dynamic_ranges
            or self.audio_codecs
            or self.audio_channels
            or self.audio_languages
            or self.subtitle_languages
        )

    @property
    def needs_local(self) -> bool:
        """Whether titles must be checked against their local files or watch progress."""

        return self.has_file_filters or bool(self.watch)

    @property
    def local_only(self) -> bool:
        """Only titles with local files can match: streaming results are left out entirely."""

        return self.has_file_filters or self.watch in {"in_progress", "watched"}


@dataclass(frozen=True, slots=True)
class GenreChoice:
    """A genre that can be filtered by in any mode, with its TMDB id per media type."""

    slug: str
    label: str
    movie_id: int | None = None
    tv_id: int | None = None
    tv_label: str = ""

    @property
    def ids(self) -> tuple[int, ...]:
        return tuple(value for value in (self.movie_id, self.tv_id) if value is not None)

    def label_for(self, media_type: str) -> str:
        return self.tv_label if media_type == "tv" and self.tv_label else self.label

    def supports(self, media_type: str) -> bool:
        if media_type == "movie":
            return self.movie_id is not None
        if media_type == "tv":
            return self.tv_id is not None
        return True


def genre_choices(
    movie_genres: Iterable[Genre], tv_genres: Iterable[Genre]
) -> tuple[GenreChoice, ...]:
    """Merge TMDB's movie and series genres into one list, sorted by name.

    Same-named genres share TMDB ids. A series genre such as "Action & Adventure" is reached
    through each movie genre it combines.
    """

    tv_by_name = {genre.name: genre for genre in tv_genres}
    aliased: dict[str, Genre] = {
        movie_name: tv_by_name[tv_name]
        for tv_name, movie_names in _TV_ALIASES.items()
        if tv_name in tv_by_name
        for movie_name in movie_names
    }
    choices: dict[str, GenreChoice] = {}
    used_tv: set[int] = set()
    for genre in movie_genres:
        tv = tv_by_name.get(genre.name) or aliased.get(genre.name)
        if tv is not None:
            used_tv.add(tv.id)
        choices[genre.name] = GenreChoice(
            slug=slugify(genre.name),
            label=genre.name,
            movie_id=genre.id,
            tv_id=tv.id if tv is not None else None,
            tv_label=tv.name if tv is not None else "",
        )
    for genre in tv_by_name.values():
        if genre.id not in used_tv and genre.name not in choices:
            choices[genre.name] = GenreChoice(slugify(genre.name), genre.name, tv_id=genre.id)
    return tuple(sorted(choices.values(), key=lambda choice: choice.label.casefold()))


def slugify(value: str) -> str:
    return re.sub(r"[^0-9a-z]+", "-", value.casefold().replace("&", "and")).strip("-")


@dataclass(frozen=True, slots=True)
class TitleFacts:
    """What TMDB knows about one title that discover can filter on but listings omit."""

    original_language: str = ""
    countries: tuple[str, ...] = ()
    runtime: int | None = None
    # (country, rating) pairs; a movie can carry several ratings in one country.
    certifications: tuple[tuple[str, str], ...] = ()
    keyword_ids: tuple[int, ...] = ()
    cast: tuple[int, ...] = ()
    directors: tuple[int, ...] = ()
    writers: tuple[int, ...] = ()
    producers: tuple[int, ...] = ()

    def people(self, role: PersonRole) -> tuple[int, ...]:
        return {
            "cast": self.cast,
            "director": self.directors,
            "writer": self.writers,
            "producer": self.producers,
        }[role]

    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: list(map(list, value)) if item.name == "certifications" else value
            for item in fields(self)
            if (value := getattr(self, item.name)) not in ((), "", None)
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TitleFacts:
        runtime = payload.get("runtime")
        return cls(
            original_language=str(payload.get("original_language") or ""),
            countries=tuple(str(value) for value in payload.get("countries", ())),
            runtime=int(runtime) if isinstance(runtime, int) and runtime > 0 else None,
            certifications=tuple(
                (str(pair[0]), str(pair[1]))
                for pair in payload.get("certifications", ())
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ),
            keyword_ids=_ints(payload.get("keyword_ids")),
            cast=_ints(payload.get("cast")),
            directors=_ints(payload.get("directors")),
            writers=_ints(payload.get("writers")),
            producers=_ints(payload.get("producers")),
        )


@dataclass(frozen=True, slots=True)
class LocalFacets:
    """What the files of a local title contain, and how far it has been watched."""

    resolutions: frozenset[str] = field(default_factory=frozenset)
    video_codecs: frozenset[str] = field(default_factory=frozenset)
    dynamic_ranges: frozenset[str] = field(default_factory=frozenset)
    audio_codecs: frozenset[str] = field(default_factory=frozenset)
    audio_channels: frozenset[str] = field(default_factory=frozenset)
    audio_languages: frozenset[str] = field(default_factory=frozenset)
    subtitle_languages: frozenset[str] = field(default_factory=frozenset)
    watch: WatchStatus = "unwatched"

    def merge(self, other: LocalFacets) -> LocalFacets:
        """Combine two copies of one title, such as the same movie in two libraries."""

        watch: WatchStatus = self.watch if self.watch == other.watch else "in_progress"
        return LocalFacets(
            resolutions=self.resolutions | other.resolutions,
            video_codecs=self.video_codecs | other.video_codecs,
            dynamic_ranges=self.dynamic_ranges | other.dynamic_ranges,
            audio_codecs=self.audio_codecs | other.audio_codecs,
            audio_channels=self.audio_channels | other.audio_channels,
            audio_languages=self.audio_languages | other.audio_languages,
            subtitle_languages=self.subtitle_languages | other.subtitle_languages,
            watch=watch,
        )


def title_matches(
    filters: TitleFilters,
    *,
    genre_ids: Sequence[int],
    facts: TitleFacts | None,
    local: LocalFacets | None,
) -> bool:
    """Check one title against every filter except release year and rating.

    ``facts`` is ``None`` for titles TMDB has no facts for (such as unmatched local files) and
    ``local`` is ``None`` for titles without local files; neither can then match a filter that
    needs them. A title without local files counts as unwatched.
    """

    if filters.genre_ids and not set(genre_ids) & set(filters.genre_ids):
        return False
    if filters.exclude_genre_ids and set(genre_ids) & set(filters.exclude_genre_ids):
        return False
    if filters.needs_facts and (facts is None or not _facts_match(filters, facts)):
        return False
    if filters.watch and (local.watch if local is not None else "unwatched") != filters.watch:
        return False
    return not filters.has_file_filters or (local is not None and _files_match(filters, local))


def _facts_match(filters: TitleFilters, facts: TitleFacts) -> bool:
    if filters.certifications:
        ratings = {
            rating
            for country, rating in facts.certifications
            if country == filters.certification_country
        }
        if not ratings & set(filters.certifications):
            return False
    if filters.countries and not set(facts.countries) & set(filters.countries):
        return False
    if filters.languages and facts.original_language not in filters.languages:
        return False
    if filters.has_runtime:
        if facts.runtime is None:
            return False
        if filters.runtime_min is not None and facts.runtime < filters.runtime_min:
            return False
        if filters.runtime_max is not None and facts.runtime > filters.runtime_max:
            return False
    if filters.keyword_ids and not set(facts.keyword_ids) & set(filters.keyword_ids):
        return False
    return all(
        not wanted or set(wanted) & set(facts.people(role))
        for role in PERSON_ROLES
        if (wanted := filters.people(role))
    )


def _files_match(filters: TitleFilters, local: LocalFacets) -> bool:
    return all(
        not wanted or set(wanted) & found
        for wanted, found in (
            (filters.resolutions, local.resolutions),
            (filters.video_codecs, local.video_codecs),
            (filters.dynamic_ranges, local.dynamic_ranges),
            (filters.audio_codecs, local.audio_codecs),
            (filters.audio_channels, local.audio_channels),
            (filters.audio_languages, local.audio_languages),
            (filters.subtitle_languages, local.subtitle_languages),
        )
    )


def discover_params(filters: TitleFilters) -> tuple[tuple[str, str], ...]:
    """TMDB discover parameters for the detail filters; "|" means any of, "," all of.

    Genres to include are left to the caller because they combine with a collection's genre.
    """

    params: list[tuple[str, str]] = []
    if filters.exclude_genre_ids:
        # A comma excludes titles with any of the genres.
        params.append(("without_genres", ",".join(map(str, sorted(filters.exclude_genre_ids)))))
    if filters.certifications and filters.certification_country:
        params.append(("certification_country", filters.certification_country))
        params.append(("certification", "|".join(filters.certifications)))
    if filters.countries:
        params.append(("with_origin_country", "|".join(filters.countries)))
    if filters.languages:
        params.append(("with_original_language", "|".join(filters.languages)))
    if filters.has_runtime:
        # TMDB counts an unknown runtime as 0 minutes; leave those titles out, as facts do.
        params.append(("with_runtime.gte", str(max(filters.runtime_min or 1, 1))))
    if filters.runtime_max is not None:
        params.append(("with_runtime.lte", str(filters.runtime_max)))
    if filters.keyword_ids:
        params.append(("with_keywords", "|".join(map(str, filters.keyword_ids))))
    return tuple(params)


def genre_queries(collection_genre: int | None, filters: TitleFilters) -> tuple[str | None, ...]:
    """The ``with_genres`` values to request; their results together make up the view.

    TMDB cannot combine "and" with "or", so a genre collection filtered by several genres is
    read as one request per genre, and the merge removes titles that come back twice.
    """

    chosen = sorted(set(filters.genre_ids))
    if not chosen:
        return (str(collection_genre) if collection_genre is not None else None,)
    if collection_genre is None:
        return ("|".join(map(str, chosen)),)
    if collection_genre in chosen:
        return (str(collection_genre),)
    return tuple(f"{collection_genre},{value}" for value in chosen)


def credit_roles(
    raw: Mapping[str, Any], media_type: MediaType, *, cast: bool
) -> frozenset[PersonRole]:
    """Classify one TMDB credit: acting (not as oneself), directing, writing, or producing.

    Series creators count as directors and writers, the way series are credited on TMDB. The
    same rules read a person's credits and a title's credits, so both agree on who did what.
    """

    if cast:
        characters = [str(raw.get("character") or "")]
        characters += [str(role.get("character") or "") for role in _objects(raw.get("roles"))]
        if any(_SELF_CREDIT.search(character) for character in characters if character):
            return frozenset()
        return frozenset({"cast"})
    jobs = {str(raw.get("job") or "")}
    jobs |= {str(job.get("job") or "") for job in _objects(raw.get("jobs"))}
    department = str(raw.get("department") or "")
    roles: set[PersonRole] = set()
    if "Director" in jobs or (media_type == "tv" and "Creator" in jobs):
        roles.add("director")
    if department in {"Writing", "Creator"} or "Creator" in jobs:
        roles.add("writer")
    if jobs & _PRODUCER_JOBS:
        roles.add("producer")
    return frozenset(roles)


def _objects(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _ints(value: object) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, int))

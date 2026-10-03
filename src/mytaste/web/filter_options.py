"""The browse filters' URL parameters and the controls the sidebar shows for them."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from mytaste.catalog.filters import (
    CHANNEL_LAYOUTS,
    DYNAMIC_RANGES,
    RESOLUTIONS,
    WATCH_STATUSES,
    GenreChoice,
    LocalFacets,
    TitleFilters,
)
from mytaste.catalog.models import BrowseMediaType, BrowseQuery
from mytaste.playback.models import codec_label, language_name

_MAX_VALUES = 20
_TEXT_VALUE = re.compile(r"^[\w.+\- ]{1,16}$")
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")
_LANGUAGE = re.compile(r"^[a-z]{2}$")
RUNTIME_PRESETS: tuple[tuple[str, int | None, int | None], ...] = (
    ("Under 30 min", None, 30),
    ("30–60 min", 30, 60),
    ("1–1½ h", 60, 90),
    ("1½–2 h", 90, 120),
    ("Over 2 h", 120, None),
)


@dataclass(frozen=True, slots=True)
class ListFilter:
    """A filter whose URL parameter holds a comma-separated list of values."""

    param: str
    field: str
    label: str
    group: str
    kind: str  # chips, pick (a list to add from), or search (suggestions as you type)
    value: str  # how values are validated: text, country, language, or id
    chip: str = ""
    suggest: str = ""


# The order of the "Add a filter" menu, apart from release year and rating.
LIST_FILTERS: tuple[ListFilter, ...] = (
    ListFilter("genre", "genre_ids", "Genre", "Details", "chips", "genre"),
    ListFilter(
        "exclude_genre", "exclude_genre_ids", "Exclude genres", "Details", "chips", "genre", "Not"
    ),
    ListFilter("content_rating", "certifications", "Content rating", "Details", "chips", "text"),
    ListFilter("country", "countries", "Country", "Details", "pick", "country"),
    ListFilter("language", "languages", "Original language", "Details", "pick", "language"),
    ListFilter("keyword", "keyword_ids", "Keyword", "Details", "search", "id", suggest="keyword"),
    ListFilter("actor", "cast", "Actor", "People", "search", "id", suggest="person"),
    ListFilter("director", "directors", "Director", "People", "search", "id", suggest="person"),
    ListFilter("writer", "writers", "Writer", "People", "search", "id", suggest="person"),
    ListFilter("producer", "producers", "Producer", "People", "search", "id", suggest="person"),
    ListFilter("resolution", "resolutions", "Resolution", "Library", "chips", "text"),
    ListFilter("video_codec", "video_codecs", "Video codec", "Library", "chips", "text"),
    ListFilter("dynamic_range", "dynamic_ranges", "Dynamic range", "Library", "chips", "text"),
    ListFilter("audio_codec", "audio_codecs", "Audio codec", "Library", "chips", "text"),
    ListFilter("audio_channels", "audio_channels", "Audio channels", "Library", "chips", "text"),
    ListFilter(
        "audio_language", "audio_languages", "Audio language", "Library", "chips", "language"
    ),
    ListFilter(
        "subtitle_language",
        "subtitle_languages",
        "Subtitle language",
        "Library",
        "chips",
        "language",
        "Subtitles",
    ),
)
FILTER_GROUPS: tuple[tuple[str, str], ...] = (
    ("Details", "Details"),
    ("People", "People"),
    ("Library", "Local files"),
)


def parse_filters(
    params: Any,
    genres: Sequence[GenreChoice],
    certification_country: str,
) -> TitleFilters:
    """Read the filters from query parameters; invalid values are dropped."""

    by_slug = {choice.slug: choice for choice in genres}
    values: dict[str, Any] = {}
    for spec in LIST_FILTERS:
        tokens = _tokens(params.getlist(spec.param))
        if spec.value == "genre":
            parsed: tuple[Any, ...] = tuple(
                dict.fromkeys(
                    genre_id
                    for token in tokens
                    if (choice := by_slug.get(token)) is not None
                    for genre_id in choice.ids
                )
            )
        elif spec.value == "id":
            parsed = tuple(
                dict.fromkeys(
                    int(token) for token in tokens if token.isdigit() and 0 < int(token) < 10**9
                )
            )
        elif spec.value == "country":
            parsed = tuple(
                dict.fromkeys(token.upper() for token in tokens if _COUNTRY.match(token))
            )
        elif spec.value == "language":
            parsed = tuple(dict.fromkeys(token for token in tokens if _LANGUAGE.match(token)))
        else:
            parsed = tuple(dict.fromkeys(token for token in tokens if _TEXT_VALUE.match(token)))
        values[spec.field] = parsed[:_MAX_VALUES]
    runtime_min = _minutes(params.get("runtime_min"))
    runtime_max = _minutes(params.get("runtime_max"))
    if runtime_min is not None and runtime_max is not None and runtime_min > runtime_max:
        runtime_min, runtime_max = runtime_max, runtime_min
    watch = params.get("watch") or ""
    return TitleFilters(
        **values,
        certification_country=certification_country if values["certifications"] else "",
        runtime_min=runtime_min,
        runtime_max=runtime_max,
        watch=watch if watch in {value for value, _label in WATCH_STATUSES} else "",
    )


def filter_params(filters: TitleFilters, genres: Sequence[GenreChoice]) -> list[tuple[str, str]]:
    """The query parameters that reproduce ``filters``, the inverse of ``parse_filters``."""

    params: list[tuple[str, str]] = []
    for spec in LIST_FILTERS:
        chosen = getattr(filters, spec.field)
        if spec.value == "genre":
            chosen = genre_slugs(chosen, genres)
        if chosen:
            params.append((spec.param, ",".join(str(value) for value in chosen)))
    if filters.runtime_min is not None:
        params.append(("runtime_min", str(filters.runtime_min)))
    if filters.runtime_max is not None:
        params.append(("runtime_max", str(filters.runtime_max)))
    if filters.watch:
        params.append(("watch", filters.watch))
    return params


def genre_slugs(genre_ids: Sequence[int], genres: Sequence[GenreChoice]) -> tuple[str, ...]:
    chosen = set(genre_ids)
    return tuple(choice.slug for choice in genres if choice.ids and set(choice.ids) <= chosen)


def without(filters: TitleFilters, field: str) -> TitleFilters:
    """``filters`` with one filter removed."""

    if field == "runtime":
        return replace(filters, runtime_min=None, runtime_max=None)
    if field == "certifications":
        return replace(filters, certifications=(), certification_country="")
    return replace(filters, **{field: "" if field == "watch" else ()})


@dataclass(frozen=True, slots=True)
class FilterOptions:
    """What the filter controls offer: TMDB's lists and what the local files contain."""

    genres: tuple[GenreChoice, ...] = ()
    certifications: tuple[str, ...] = ()
    certification_country: str = ""
    certification_region: str = ""
    countries: tuple[tuple[str, str], ...] = ()
    languages: tuple[tuple[str, str], ...] = ()
    names: Mapping[tuple[str, int], str] | None = None
    local: Sequence[LocalFacets] = ()
    has_libraries: bool = False


def filter_rules(
    query: BrowseQuery,
    options: FilterOptions,
    url: Callable[[BrowseQuery], str],
) -> tuple[dict[str, Any], ...]:
    """Describe each filter the sidebar can show: its choices, values, and removal link."""

    filters = query.filters
    names = options.names or {}
    media_type = query.media_type
    rules: list[dict[str, Any]] = []

    def clear_url(field: str) -> str:
        return url(replace(query, filters=without(filters, field), page=1))

    for spec in LIST_FILTERS:
        if spec.group == "Library" and not options.has_libraries:
            continue
        chosen = getattr(filters, spec.field)
        choices: list[tuple[str, str]] = []
        selected_values: tuple[str, ...]
        if spec.value == "genre":
            selected_values = genre_slugs(chosen, options.genres)
            choices = _genre_options(options.genres, media_type)
        else:
            selected_values = tuple(str(value) for value in chosen)
            if spec.param == "content_rating":
                choices = [(value, value) for value in options.certifications]
            elif spec.param == "country":
                choices = list(options.countries)
            elif spec.param == "language":
                choices = list(options.languages)
            elif spec.group == "Library":
                choices = _local_options(spec.field, options.local)
        labels = dict(choices)
        selected = [
            {"value": value, "label": _value_label(spec, value, labels, names)}
            for value in selected_values
        ]
        if spec.kind == "chips":
            # Values in the URL that the current lists lack stay visible so they can be removed.
            known = {value for value, _label in choices}
            choices += [
                (item["value"], item["label"]) for item in selected if item["value"] not in known
            ]
            if not choices and not chosen:
                continue
        rules.append(
            {
                "key": spec.param,
                "param": spec.param,
                "label": spec.label,
                "chip_label": spec.chip or spec.label,
                "group": spec.group,
                "kind": spec.kind,
                "suggest": spec.suggest,
                "active": bool(chosen),
                "clear_url": clear_url(spec.field),
                "options": [
                    {"value": value, "label": label, "checked": value in selected_values}
                    for value, label in choices
                    if spec.kind == "chips" or value not in selected_values
                ],
                "selected": selected,
                "hint": (
                    f"As rated in {options.certification_region}"
                    if spec.param == "content_rating" and options.certification_region
                    else ""
                ),
            }
        )
        if spec.param == "language":
            rules.append(_runtime_rule(query, clear_url("runtime"), url))
    if options.has_libraries:
        rules.append(
            {
                "key": "watch",
                "param": "watch",
                "label": "Watch status",
                "chip_label": "",
                "group": "Library",
                "kind": "radio",
                "suggest": "",
                "active": bool(filters.watch),
                "clear_url": clear_url("watch"),
                "options": [
                    {"value": value, "label": label, "checked": filters.watch == value}
                    for value, label in WATCH_STATUSES
                ],
                "selected": [
                    {"value": value, "label": label}
                    for value, label in WATCH_STATUSES
                    if value == filters.watch
                ],
                "hint": "Titles you have not played here count as unwatched",
            }
        )
    return tuple(rules)


def _runtime_rule(
    query: BrowseQuery, clear_url: str, url: Callable[[BrowseQuery], str]
) -> dict[str, Any]:
    filters = query.filters
    label = runtime_label(filters.runtime_min, filters.runtime_max)
    return {
        "key": "runtime",
        "param": "runtime",
        "label": "Runtime",
        "chip_label": "Runtime",
        "group": "Details",
        "kind": "runtime",
        "suggest": "",
        "active": filters.has_runtime,
        "clear_url": clear_url,
        "options": [
            {
                "value": "",
                "label": text,
                "checked": (filters.runtime_min, filters.runtime_max) == (low, high),
                "url": url(
                    replace(
                        query,
                        filters=replace(filters, runtime_min=low, runtime_max=high),
                        page=1,
                    )
                ),
            }
            for text, low, high in RUNTIME_PRESETS
        ],
        "selected": [{"value": "", "label": label}] if label else [],
        "minimum": filters.runtime_min,
        "maximum": filters.runtime_max,
        "hint": "In minutes. Series are measured by episode; some have no length on TMDB.",
    }


def runtime_label(low: int | None, high: int | None) -> str:
    for text, preset_low, preset_high in RUNTIME_PRESETS:
        if (low, high) == (preset_low, preset_high):
            return text
    if low is not None and high is not None:
        return f"{low}–{high} min"
    if low is not None:
        return f"{low} min or longer"
    if high is not None:
        return f"Up to {high} min"
    return ""


def _genre_options(
    genres: Sequence[GenreChoice], media_type: BrowseMediaType
) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = []
    seen_tv: set[int] = set()
    for choice in genres:
        if not choice.supports(media_type):
            continue
        if media_type == "tv" and choice.tv_id is not None:
            # Movie genres that share a series genre, such as Action and Adventure, show once.
            if choice.tv_id in seen_tv:
                continue
            seen_tv.add(choice.tv_id)
        options.append((choice.slug, choice.label_for(media_type)))
    return sorted(options, key=lambda option: option[1].casefold())


def _local_options(field: str, local: Sequence[LocalFacets]) -> list[tuple[str, str]]:
    found: set[str] = set().union(*(getattr(facets, field) for facets in local))
    if field == "resolutions":
        return [(value, value) for value in RESOLUTIONS if value in found]
    if field == "dynamic_ranges":
        return [(value, label) for value, label in DYNAMIC_RANGES if value in found]
    if field == "audio_channels":
        ordered = [value for value in CHANNEL_LAYOUTS if value in found]
        return [(value, value) for value in ordered + sorted(found - set(ordered))]
    if field in {"audio_languages", "subtitle_languages"}:
        return sorted(
            ((value, language_name(value) or value) for value in found),
            key=lambda option: option[1].casefold(),
        )
    return sorted(((value, codec_label(value)) for value in found), key=lambda pair: pair[1])


def _value_label(
    spec: ListFilter,
    value: str,
    labels: Mapping[str, str],
    names: Mapping[tuple[str, int], str],
) -> str:
    if value in labels:
        return labels[value]
    if spec.suggest and value.isdigit():
        return names.get((spec.suggest, int(value))) or f"#{value}"
    if spec.value == "language":
        return language_name(value) or value
    if spec.param in {"video_codec", "audio_codec"}:
        return codec_label(value)
    if spec.param == "dynamic_range":
        return dict(DYNAMIC_RANGES).get(value, value)
    return value


def active_filter_chips(rules: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """One removable chip per active filter, such as "Genre: Drama, Comedy"."""

    chips: list[dict[str, str]] = []
    for rule in rules:
        if not rule["active"]:
            continue
        values = ", ".join(item["label"] for item in rule["selected"][:3])
        if len(rule["selected"]) > 3:
            values += f" +{len(rule['selected']) - 3}"
        prefix = rule["chip_label"]
        chips.append(
            {"label": f"{prefix}: {values}" if prefix else values, "url": rule["clear_url"]}
        )
    return chips


def _tokens(values: Sequence[str]) -> list[str]:
    return [token.strip() for value in values for token in value.split(",") if token.strip()]


def _minutes(value: str | None) -> int | None:
    if not value or not value.strip().isdigit():
        return None
    minutes = int(value)
    return minutes if 0 < minutes <= 1000 else None

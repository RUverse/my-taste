from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any
from urllib.parse import urlparse

MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Collections keep their URL key. Popular and Most listened are separate charts: Apple Music's
# most played in the user's region, and what ListenBrainz's listeners played this month.
CHART_COLLECTIONS = {
    "popular": "Popular",
    "most-listened": "Most listened",
    "new": "New releases",
}

# One genre list for every source, so a filter gives the same answer for an album wherever it
# comes from. An album belongs to a genre when one of its main genre tags (see
# ``main_genres``) contains one of the words, so "alternative rock" is Rock and "pop rap" is
# both Pop and Hip hop.
GENRES: dict[str, tuple[str, tuple[str, ...]]] = {
    "rock": ("Rock", ("rock", "grunge", "shoegaze")),
    "pop": ("Pop", ("pop",)),
    "hip-hop": ("Hip hop", ("hip hop", "rap", "trap", "drill")),
    "electronic": (
        "Electronic",
        ("electronic", "electronica", "house", "techno", "trance", "ambient", "idm", "dubstep"),
    ),
    "rnb": ("R&B and soul", ("r&b", "soul", "funk", "disco")),
    "metal": ("Metal", ("metal", "metalcore", "deathcore")),
    "punk": ("Punk", ("punk", "emo", "post-hardcore")),
    "folk": ("Folk and country", ("folk", "country", "americana", "bluegrass")),
    "jazz": ("Jazz and blues", ("jazz", "blues", "bossa nova")),
    "classical": ("Classical and scores", ("classical", "orchestral", "opera", "film score")),
    "latin": ("Latin", ("latin", "reggaeton", "salsa", "cumbia", "bachata", "corrido")),
}
COLLECTIONS = {**CHART_COLLECTIONS, **{key: label for key, (label, _words) in GENRES.items()}}
COLLECTION_ICONS = {"popular": "flame", "most-listened": "star", "new": "sparkle"} | {
    key: "music" for key in GENRES
}

SORTS = {
    "catalog": "Collection order",
    "popularity": "Popularity",
    "release": "Release date",
    "title": "Title",
    "artist": "Artist",
}
DESCENDING_SORTS = frozenset({"popularity", "release"})

# Album types as filters: MusicBrainz's primary types, then the secondary types people look
# for. A live album is still an album.
TYPES = {
    "album": "Albums",
    "ep": "EPs",
    "single": "Singles",
    "live": "Live",
    "compilation": "Compilations",
    "soundtrack": "Soundtracks",
}
PRIMARY_TYPES = frozenset({"album", "ep", "single", "broadcast", "other"})
DECADES = ("2020", "2010", "2000", "1990", "1980", "1970", "1960", "earlier")
GROUPINGS = {"artist": "Artist", "decade": "Decade", "genre": "Genre", "type": "Type"}
VIEWS = {"grid": "Grid", "wall": "Wall", "list": "List"}

# Where an album plays, from MusicBrainz's links on its releases.
SERVICES = {
    "spotify": "Spotify",
    "apple-music": "Apple Music",
    "bandcamp": "Bandcamp",
    "deezer": "Deezer",
    "soundcloud": "SoundCloud",
}
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")


def mbid(value: object) -> str:
    text = str(value).strip().lower()
    if not MBID.fullmatch(text):
        raise ValueError("Unknown album")
    return text


def web_url(value: Any) -> str | None:
    """Provider data can supply links, but never executable or credentialed URLs."""

    if not isinstance(value, str):
        return None
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return value.strip().replace("http://", "https://", 1)


def genre_matches(genre: str, tags: tuple[str, ...]) -> bool:
    words = GENRES.get(genre, ("", ()))[1]
    return any(_contains_word(tag, word) for tag in tags for word in words)


def _contains_word(tag: str, word: str) -> bool:
    return re.search(rf"(?:^|[\s-]){re.escape(word)}(?:$|[\s-])", tag.casefold()) is not None


def main_genres(tags: list[tuple[str, int]], limit: int = 4) -> tuple[str, ...]:
    """The genres that describe an album: its most voted genre tags, ignoring stray ones with
    under half the votes of the top tag."""

    if not tags:
        return ()
    ordered = sorted(tags, key=lambda tag: (-tag[1], tag[0]))
    top = max(ordered[0][1], 1)
    return tuple(name for name, count in ordered if count * 2 >= top)[:limit]


@dataclass(frozen=True, slots=True)
class ArtistCredit:
    name: str
    mbid: str = ""
    join: str = ""


@dataclass(frozen=True, slots=True)
class Album:
    """One album: a MusicBrainz release group, which covers every edition of it.

    ``listens`` and ``listeners`` are ListenBrainz's all-time counts, the popularity every
    music sort compares. The cover is one Cover Art Archive image, served from MyTaste's cache.
    """

    mbid: str
    title: str
    artists: tuple[ArtistCredit, ...] = ()
    release_date: str = ""
    primary_type: str = ""
    secondary_types: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    cover_release: str = ""
    cover_id: int = 0
    color: str = ""
    listens: int | None = None
    listeners: int | None = None

    @property
    def key(self) -> str:
        return self.mbid

    @property
    def portable_id(self) -> str:
        return f"album-mb-{self.mbid}"

    @property
    def artist(self) -> str:
        return "".join(credit.name + credit.join for credit in self.artists).strip()

    @property
    def year(self) -> str:
        return self.release_date[:4]

    @property
    def decade(self) -> str:
        if not self.year.isdigit():
            return ""
        start = int(self.year) - int(self.year) % 10
        return str(start) if start >= 1960 else "earlier"

    @property
    def type_label(self) -> str:
        """What the album is, such as "Live album" or "EP"."""

        primary = {"ep": "EP", "single": "Single", "broadcast": "Broadcast", "other": "Release"}
        base = primary.get(self.primary_type.casefold(), "Album")
        extra = [kind for kind in self.secondary_types if kind.casefold() in TYPES]
        if not extra:
            return base
        first = extra[0].casefold()
        if first == "live":
            return f"Live {base.lower()}" if base != "EP" else "Live EP"
        return extra[0].capitalize()

    def has_type(self, value: str) -> bool:
        primary = self.primary_type.casefold()
        if value in PRIMARY_TYPES:
            return primary == value
        return value in {kind.casefold() for kind in self.secondary_types}

    def cover_url(self, size: int = 250) -> str:
        if self.cover_release and self.cover_id:
            return f"/music/covers/{self.cover_release}/{self.cover_id}-{size}.jpg"
        return ""

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> Album:
        known = {item.name for item in fields(cls)}
        values = {key: value for key, value in data.items() if key in known}
        values["artists"] = tuple(
            ArtistCredit(**{key: str(part.get(key, "")) for key in ("name", "mbid", "join")})
            for part in values.get("artists", ())
            if isinstance(part, dict)
        )
        for name in ("secondary_types", "genres"):
            values[name] = tuple(str(value) for value in values.get(name, ()))
        values["cover_id"] = int(values.get("cover_id") or 0)
        return cls(**values)


@dataclass(frozen=True, slots=True)
class Track:
    position: str
    title: str
    length: int = 0
    artist: str = ""

    @property
    def duration(self) -> str:
        if not self.length:
            return ""
        seconds = round(self.length / 1000)
        return f"{seconds // 60}:{seconds % 60:02d}"


@dataclass(frozen=True, slots=True)
class Medium:
    position: int
    format: str = ""
    title: str = ""
    tracks: tuple[Track, ...] = ()


@dataclass(frozen=True, slots=True)
class AlbumDetails:
    """What the details panel shows beyond the card: one release's tracklist and label, and
    where the album plays, from links on any of its releases."""

    album: Album
    release_mbid: str = ""
    release_title: str = ""
    label: str = ""
    media: tuple[Medium, ...] = ()
    links: dict[str, str] = field(default_factory=dict)
    release_count: int = 0

    @property
    def spotify_id(self) -> str:
        url = self.links.get("spotify", "")
        candidate = url.rstrip("/").rsplit("/", 1)[-1] if url else ""
        return candidate if SPOTIFY_ID.fullmatch(candidate) else ""

    @property
    def track_count(self) -> int:
        return sum(len(medium.tracks) for medium in self.media)

    @property
    def length(self) -> int:
        return sum(track.length for medium in self.media for track in medium.tracks)

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> AlbumDetails:
        media = tuple(
            Medium(
                position=int(medium.get("position") or 0),
                format=str(medium.get("format", "")),
                title=str(medium.get("title", "")),
                tracks=tuple(
                    Track(
                        position=str(track.get("position", "")),
                        title=str(track.get("title", "")),
                        length=int(track.get("length") or 0),
                        artist=str(track.get("artist", "")),
                    )
                    for track in medium.get("tracks", ())
                ),
            )
            for medium in data.get("media", ())
        )
        return cls(
            album=Album.from_payload(data.get("album", {})),
            release_mbid=str(data.get("release_mbid", "")),
            release_title=str(data.get("release_title", "")),
            label=str(data.get("label", "")),
            media=media,
            links={str(key): str(value) for key, value in dict(data.get("links", {})).items()},
            release_count=int(data.get("release_count") or 0),
        )


@dataclass(frozen=True, slots=True)
class AlbumQuery:
    collection: str = "popular"
    search: str = ""
    artist: str = ""
    genre: str = ""
    type: str = ""
    decade: str = ""
    sort: str = "catalog"
    order: str = ""
    page: int = 1

    def validate(self) -> None:
        if self.collection not in COLLECTIONS or self.sort not in SORTS:
            raise ValueError("Choose a supported collection and sort order")
        if self.order not in {"", "asc", "desc"}:
            raise ValueError("Choose ascending or descending order")
        if not 1 <= self.page <= 1000:
            raise ValueError("Page must be between 1 and 1000")
        if len(self.search) > 200:
            raise ValueError("Search is too long")
        if self.artist and not MBID.fullmatch(self.artist):
            raise ValueError("Unknown artist")
        if self.genre and self.genre not in GENRES:
            raise ValueError("Choose a supported genre")
        if self.type and self.type not in TYPES:
            raise ValueError("Choose a supported album type")
        if self.decade and self.decade not in DECADES:
            raise ValueError("Choose a supported decade")

    @property
    def descending(self) -> bool:
        natural = self.sort in DESCENDING_SORTS
        return natural if not self.order else self.order == "desc"

    @property
    def filtered(self) -> bool:
        return bool(self.genre or self.type or self.decade)


@dataclass(frozen=True, slots=True)
class AlbumPage:
    items: tuple[Album, ...] = ()
    total: int = 0
    pages: int = 1
    page: int = 1
    # Chart entries still being matched to MusicBrainz; they appear once matched.
    pending: int = 0
    notices: tuple[str, ...] = ()
    artist_name: str = ""


def album_matches(album: Album, query: AlbumQuery) -> bool:
    """The one filter rule, for an album from any source."""

    if query.genre and not genre_matches(query.genre, album.genres):
        return False
    if query.type and not album.has_type(query.type):
        return False
    return not (query.decade and album.decade != query.decade)

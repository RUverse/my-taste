"""Best-effort title, year, season, and episode extraction from media file names."""

from __future__ import annotations

import re
from datetime import date

from mytaste.library.models import ParsedName

VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".divx",
        ".flv",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".ogm",
        ".ogv",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    }
)

_SEPARATORS = re.compile(r"[.\-_\[\]\(\)\{\}+,;:!?\s]+")
_YEAR_PREFIX = re.compile(r"^\s*((?:19|20)\d{2})\s*-\s*(.+)$")
_EPISODE_PATTERNS = (
    re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{1,3})(?![a-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![a-z0-9])(\d{1,2})e(\d{2,3})(?![a-z0-9])", re.IGNORECASE),
)
_EPISODE_ONLY = re.compile(r"(?<![a-z0-9])(?:e|ep|episode)[ ._-]*(\d{1,3})(?![a-z0-9])", re.I)
_SEASON_FOLDER = re.compile(r"^(?:s|season|staffel)[ ._-]*(\d{1,2})$", re.IGNORECASE)
_SEASON_TOKEN = re.compile(r"(?<![a-z0-9])s(\d{1,2})(?![a-z0-9])", re.IGNORECASE)
_YEAR_TOKEN = re.compile(r"^(?:19|20)\d{2}$")

_TAG_TEXT = """
    10bit 8bit 4k 6ch 2ch 1ch aac ac3 amzn atmos atvp avc bd bdrip bluray blu brip brrip
    cam complete criterion dd dd5 ddp divx dl dsnp dts dual dubbed dvd dvdrip eng extended
    farsi german hardsub hdcam hdr hdrip hdtv hevc hmax internal ita korean limited multi nf
    proper remastered remux repack season sub subbed subs truehd uhd unrated uncut web webdl
    webrip x264 x265 xvid yify yts rarbg etrg ganool psa eztv rmteam mkvcage film2media
    film2movie tinymoviez zarfilm avadl expressmovie hd mkv mp4 avi srt ir com org net info
    www all
"""
_TAG_WORDS: frozenset[str] = frozenset(_TAG_TEXT.split())
_TAG_PATTERNS = (
    re.compile(r"^(480|540|576|720|1080|1440|2160|4320)[pi]?$", re.IGNORECASE),
    re.compile(r"^\d{1,2}ch$", re.IGNORECASE),
    re.compile(r"^(aac|ac3|dd|ddp|dts)\d", re.IGNORECASE),
    re.compile(r"^h\.?26[45]$", re.IGNORECASE),
    re.compile(r"^s\d{1,2}$", re.IGNORECASE),
    re.compile(r"^\d{3,4}mb$", re.IGNORECASE),
)


def is_video_file(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(extension) for extension in VIDEO_EXTENSIONS)


def strip_extension(name: str) -> str:
    lowered = name.lower()
    for extension in VIDEO_EXTENSIONS:
        if lowered.endswith(extension):
            return name[: -len(extension)]
    return name


def is_tag(token: str) -> bool:
    lowered = token.casefold()
    return lowered in _TAG_WORDS or any(pattern.match(token) for pattern in _TAG_PATTERNS)


def tokenize(value: str) -> list[str]:
    return [token for token in _SEPARATORS.split(value) if token]


def parse_name(value: str, *, today: date | None = None) -> ParsedName:
    """Parse a movie file or folder name into a title and optional year."""

    prefixed = _YEAR_PREFIX.match(value)
    if prefixed:
        tokens = tokenize(prefixed.group(2))
        title = " ".join(_strip_trailing_tags(tokens))
        return ParsedName(title=title or prefixed.group(2).strip(), year=int(prefixed.group(1)))

    tokens = tokenize(value)
    year, year_index = _find_year(tokens, today)
    if year_index is not None:
        title_tokens = _strip_trailing_tags(tokens[:year_index])
    else:
        title_tokens = _cut_at_first_tag(tokens)
    title = " ".join(title_tokens).strip()
    if not title and tokens:
        title = " ".join(tokens)
    return ParsedName(title=title, year=year)


def parse_episode_name(value: str, *, today: date | None = None) -> ParsedName:
    """Parse an episode file name into show title, year, season, and episode."""

    match_position: int | None = None
    season: int | None = None
    episode: int | None = None
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(value)
        if match:
            match_position = match.start()
            season = int(match.group(1))
            episode = int(match.group(2))
            break

    if match_position is None:
        season_match = _SEASON_TOKEN.search(value)
        episode_match = _EPISODE_ONLY.search(value)
        if season_match:
            season = int(season_match.group(1))
            match_position = season_match.start()
        if episode_match:
            episode = int(episode_match.group(1))
            if match_position is None or episode_match.start() < match_position:
                match_position = episode_match.start()

    title_source = value if match_position is None else value[:match_position]
    parsed = parse_name(title_source, today=today)
    return ParsedName(title=parsed.title, year=parsed.year, season=season, episode=episode)


def season_from_folder(name: str) -> int | None:
    match = _SEASON_FOLDER.match(name.strip())
    if match:
        return int(match.group(1))
    token = _SEASON_TOKEN.search(name)
    return int(token.group(1)) if token else None


def _find_year(tokens: list[str], today: date | None) -> tuple[int | None, int | None]:
    latest = (today or date.today()).year + 1
    candidates = [
        (index, int(token))
        for index, token in enumerate(tokens)
        if _YEAR_TOKEN.match(token) and 1900 <= int(token) <= latest
    ]
    preferred = [candidate for candidate in candidates if candidate[0] > 0]
    if preferred:
        index, year = preferred[-1]
        return year, index
    return None, None


def _strip_trailing_tags(tokens: list[str]) -> list[str]:
    result = list(tokens)
    while result and is_tag(result[-1]):
        result.pop()
    return result or list(tokens)


def _cut_at_first_tag(tokens: list[str]) -> list[str]:
    for index, token in enumerate(tokens):
        if index > 0 and is_tag(token):
            return tokens[:index]
    return list(tokens)

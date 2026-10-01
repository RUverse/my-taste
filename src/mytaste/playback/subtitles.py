"""Find subtitle files next to a video and convert subtitle text to WebVTT.

Browsers only render WebVTT, so SRT, ASS and embedded text tracks are converted on the server.
Many Persian and Arabic subtitles are Windows-1256 files, and some MKVs store them that way
(so ffmpeg decodes them as Windows-1252); both cases are detected and repaired here.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from mytaste.library.parser import is_video_file, parse_episode_name, strip_extension
from mytaste.playback.models import ExternalSubtitle, language_tag

SUBTITLE_EXTENSIONS = {".srt": "srt", ".ass": "ass", ".ssa": "ass", ".vtt": "vtt"}
_SUBTITLE_FOLDERS = frozenset({"subs", "subtitles", "sub"})
_MAX_SUBTITLE_BYTES = 8 * 1024 * 1024
_TOKEN = re.compile(r"[^\w]+|_", re.UNICODE)
_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN_SUPPLEMENT = re.compile(r"[À-ÿ]")
_SRT_TIME = re.compile(
    r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)
_ASS_OVERRIDE = re.compile(r"\{[^}]*\}")
_HTML_TAG = re.compile(r"</?(?!/?(?:b|i|u)>)[^>]*>", re.IGNORECASE)
_PERSIAN_LETTERS = re.compile(r"[\u067e\u0686\u0698\u06af\u06a9\u06cc]")
_ENGLISH_WORDS = frozenset({"the", "you", "and", "to", "is", "what", "it", "that", "i", "of"})
_NAMES_IN_WORDS = (("farsi", "fa"), ("persian", "fa"), ("english", "en"), ("arabic", "ar"))
_SNIFF_CHARACTERS = 20000
_FORCED = frozenset({"forced", "foreign"})
_HEARING_IMPAIRED = frozenset({"sdh", "hi", "cc"})


def find_external_subtitles(
    video: Path, *, season: int | None = None, episode: int | None = None
) -> tuple[ExternalSubtitle, ...]:
    """Return subtitle files that belong to ``video``, best guesses first."""

    folder = video.parent
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return ()
    videos = [entry.name for entry in entries if _visible(entry.name) and is_video_file(entry.name)]
    alone = len(videos) <= 1
    stem = _normalize(strip_extension(video.name))
    candidates: list[tuple[Path, bool]] = []
    for entry in entries:
        if not _visible(entry.name):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir and entry.name.casefold() in _SUBTITLE_FOLDERS:
            candidates.extend(_subtitle_folder(Path(entry.path), stem, alone))
        elif _subtitle_format(entry.name):
            candidates.append((Path(entry.path), False))

    found: list[ExternalSubtitle] = []
    for path, matched in candidates:
        name_stem = strip_extension(path.name)
        if not (matched or alone or _normalize(name_stem).startswith(stem)):
            parsed = parse_episode_name(name_stem)
            if episode is None or (parsed.season, parsed.episode) != (season, episode):
                continue
        found.append(_describe(path))
    found.sort(key=lambda item: (item.forced, item.language == "", item.name.casefold()))
    return tuple(found)


def _subtitle_folder(folder: Path, stem: str, alone: bool) -> list[tuple[Path, bool]]:
    found: list[tuple[Path, bool]] = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return found
    for entry in entries:
        if not _visible(entry.name):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                if _normalize(entry.name) == stem:
                    found.extend(
                        (Path(child.path), True)
                        for child in os.scandir(entry.path)
                        if _visible(child.name) and _subtitle_format(child.name)
                    )
                continue
        except OSError:
            continue
        if _subtitle_format(entry.name):
            found.append((Path(entry.path), alone))
    return found


def _describe(path: Path) -> ExternalSubtitle:
    stem = strip_extension(path.name).casefold()
    tokens = [token for token in _TOKEN.split(stem) if token]
    language = ""
    for position, token in enumerate(reversed(tokens)):
        tag = language_tag(token)
        # Two-letter codes only count at the end ("Movie.en.srt"); names count anywhere.
        if tag and (len(token) > 3 or position < 2):
            language = tag
            break
    if not language:
        language = next((tag for name, tag in _NAMES_IN_WORDS if name in stem), "")
    if not language:
        try:
            language = guess_language(read_subtitle_file(path)[:_SNIFF_CHARACTERS])
        except (OSError, ValueError):
            language = ""
    return ExternalSubtitle(
        path=str(path),
        format=_subtitle_format(path.name) or "srt",
        language=language,
        forced=bool(_FORCED.intersection(tokens)),
        hearing_impaired=bool(_HEARING_IMPAIRED.intersection(tokens[-3:])),
    )


def _subtitle_format(name: str) -> str | None:
    return SUBTITLE_EXTENSIONS.get(os.path.splitext(name)[1].casefold())


def _visible(name: str) -> bool:
    return not name.startswith(".")


def _normalize(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def guess_language(text: str) -> str:
    """Guess Persian, Arabic or English from subtitle text; ``""`` when unsure."""

    arabic_script = len(_ARABIC.findall(text))
    if arabic_script > 50:
        return "fa" if len(_PERSIAN_LETTERS.findall(text)) * 50 > arabic_script else "ar"
    words = re.findall(r"[a-z']+", text.casefold())
    if len(words) > 50 and sum(word in _ENGLISH_WORDS for word in words) * 8 > len(words):
        return "en"
    return ""


# Decoding ------------------------------------------------------------------------------------


def read_subtitle_file(path: Path) -> str:
    with open(path, "rb") as handle:
        data = handle.read(_MAX_SUBTITLE_BYTES + 1)
    if len(data) > _MAX_SUBTITLE_BYTES:
        raise ValueError("Subtitle file is too large")
    return decode_subtitle_bytes(data)


def decode_subtitle_bytes(data: bytes) -> str:
    """Decode subtitle bytes, recognising BOMs, UTF-8 and the Windows-1256 Arabic script."""

    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return repair_mojibake(data.decode("utf-8"))
    except UnicodeDecodeError:
        pass
    arabic = data.decode("cp1256", errors="replace")
    western = data.decode("cp1252", errors="replace")
    if len(_ARABIC.findall(arabic)) > len(_LATIN_SUPPLEMENT.findall(western)):
        return arabic
    return western


def repair_mojibake(text: str) -> str:
    """Undo Windows-1256 text that was decoded as Windows-1252 (common in Persian MKVs)."""

    suspicious = len(_LATIN_SUPPLEMENT.findall(text))
    if suspicious < 20 or suspicious < len(_ARABIC.findall(text)):
        return text
    raw = bytearray()
    for character in text:
        try:
            raw += character.encode("cp1252")
        except UnicodeEncodeError:
            if ord(character) > 0xFF:
                return text
            raw.append(ord(character))
    repaired = bytes(raw).decode("cp1256", errors="replace")
    return repaired if len(_ARABIC.findall(repaired)) > suspicious // 2 else text


# Conversion ----------------------------------------------------------------------------------


def to_webvtt(text: str, source_format: str) -> str:
    if source_format == "ass":
        return ass_to_webvtt(text)
    if source_format == "vtt" or text.lstrip("﻿").startswith("WEBVTT"):
        return normalize_webvtt(text)
    return srt_to_webvtt(text)


def srt_to_webvtt(text: str) -> str:
    cues: list[str] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        lines = [line for line in block.strip("\n").split("\n")]
        for index, line in enumerate(lines):
            match = _SRT_TIME.search(line)
            if match is None:
                continue
            body = [_clean_cue_line(value) for value in lines[index + 1 :]]
            body = [value for value in body if value.strip()]
            if body:
                cues.append(f"{_cue_time(match.groups()[:4])} --> {_cue_time(match.groups()[4:])}")
                cues[-1] += "\n" + "\n".join(body)
            break
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")


def ass_to_webvtt(text: str) -> str:
    cues: list[tuple[float, str]] = []
    fields: list[str] = []
    in_events = False
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("["):
            in_events = line.casefold() == "[events]"
            continue
        if not in_events:
            continue
        if line.casefold().startswith("format:"):
            fields = [value.strip().casefold() for value in line.split(":", 1)[1].split(",")]
            continue
        if not line.casefold().startswith("dialogue:") or not fields:
            continue
        values = line.split(":", 1)[1].split(",", len(fields) - 1)
        if len(values) != len(fields):
            continue
        event = dict(zip(fields, (value.strip() for value in values), strict=True))
        start = _ass_seconds(event.get("start", ""))
        end = _ass_seconds(event.get("end", ""))
        body = _ASS_OVERRIDE.sub("", values[-1]).replace("\\N", "\n").replace("\\n", "\n")
        body = body.replace("\\h", " ").strip()
        if start is None or end is None or end <= start or not body:
            continue
        cue = f"{_format_seconds(start)} --> {_format_seconds(end)}\n{_escape(body)}"
        cues.append((start, cue))
    cues.sort(key=lambda item: item[0])
    return "WEBVTT\n\n" + "\n\n".join(cue for _, cue in cues) + ("\n" if cues else "")


def normalize_webvtt(text: str) -> str:
    cleaned = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    return cleaned if cleaned.startswith("WEBVTT") else "WEBVTT\n\n" + cleaned


def _clean_cue_line(line: str) -> str:
    line = _ASS_OVERRIDE.sub("", line)
    line = _HTML_TAG.sub("", line)
    return line.replace("-->", "→").rstrip()


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cue_time(parts: tuple[str, ...]) -> str:
    hours, minutes, seconds, fraction = parts
    milliseconds = int(fraction.ljust(3, "0")[:3])
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{milliseconds:03d}"


def _ass_seconds(value: str) -> float | None:
    match = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2})(?:[.,](\d{1,3}))?", value)
    if match is None:
        return None
    hours, minutes, seconds, fraction = match.groups()
    fraction_value = int((fraction or "0").ljust(3, "0")[:3]) / 1000
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + fraction_value


def _format_seconds(value: float) -> str:
    milliseconds = round(value * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

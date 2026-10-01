"""Immutable descriptions of a media file's streams, as read by ffprobe."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

_LANGUAGES: dict[str, tuple[str, str]] = {
    # ISO 639-2 (bibliographic and terminology) and 639-1 codes -> (BCP 47 tag, English name)
    "ara": ("ar", "Arabic"),
    "bul": ("bg", "Bulgarian"),
    "chi": ("zh", "Chinese"),
    "zho": ("zh", "Chinese"),
    "cze": ("cs", "Czech"),
    "ces": ("cs", "Czech"),
    "dan": ("da", "Danish"),
    "dut": ("nl", "Dutch"),
    "nld": ("nl", "Dutch"),
    "eng": ("en", "English"),
    "est": ("et", "Estonian"),
    "fin": ("fi", "Finnish"),
    "fre": ("fr", "French"),
    "fra": ("fr", "French"),
    "ger": ("de", "German"),
    "deu": ("de", "German"),
    "gre": ("el", "Greek"),
    "ell": ("el", "Greek"),
    "heb": ("he", "Hebrew"),
    "hin": ("hi", "Hindi"),
    "hrv": ("hr", "Croatian"),
    "hun": ("hu", "Hungarian"),
    "ind": ("id", "Indonesian"),
    "ita": ("it", "Italian"),
    "jpn": ("ja", "Japanese"),
    "kor": ("ko", "Korean"),
    "lav": ("lv", "Latvian"),
    "lit": ("lt", "Lithuanian"),
    "may": ("ms", "Malay"),
    "msa": ("ms", "Malay"),
    "nor": ("no", "Norwegian"),
    "nob": ("no", "Norwegian"),
    "per": ("fa", "Persian"),
    "fas": ("fa", "Persian"),
    "pol": ("pl", "Polish"),
    "por": ("pt", "Portuguese"),
    "rum": ("ro", "Romanian"),
    "ron": ("ro", "Romanian"),
    "rus": ("ru", "Russian"),
    "slo": ("sk", "Slovak"),
    "slk": ("sk", "Slovak"),
    "slv": ("sl", "Slovenian"),
    "spa": ("es", "Spanish"),
    "srp": ("sr", "Serbian"),
    "swe": ("sv", "Swedish"),
    "tha": ("th", "Thai"),
    "tur": ("tr", "Turkish"),
    "ukr": ("uk", "Ukrainian"),
    "vie": ("vi", "Vietnamese"),
}
_LANGUAGES.update({tag: (tag, name) for tag, name in list(_LANGUAGES.values())})
_LANGUAGES["farsi"] = ("fa", "Persian")
_LANGUAGES["persian"] = ("fa", "Persian")
_LANGUAGES.update(
    {name.casefold(): (tag, name) for tag, name in {value for value in _LANGUAGES.values()}}
)

_CHANNEL_LABELS = {1: "Mono", 2: "Stereo", 6: "5.1", 7: "6.1", 8: "7.1"}
_CODEC_LABELS = {
    "aac": "AAC",
    "ac3": "Dolby Digital",
    "eac3": "Dolby Digital Plus",
    "truehd": "Dolby TrueHD",
    "dts": "DTS",
    "mp3": "MP3",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "flac": "FLAC",
    "h264": "H.264",
    "hevc": "HEVC",
    "av1": "AV1",
    "vp9": "VP9",
    "vp8": "VP8",
    "mpeg4": "MPEG-4",
    "mpeg2video": "MPEG-2",
    "vc1": "VC-1",
    "subrip": "SRT",
    "ass": "ASS",
    "ssa": "SSA",
    "webvtt": "WebVTT",
    "mov_text": "Text",
    "hdmv_pgs_subtitle": "PGS",
    "dvd_subtitle": "VobSub",
    "dvb_subtitle": "DVB",
}
TEXT_SUBTITLE_CODECS = frozenset({"subrip", "ass", "ssa", "webvtt", "mov_text", "text"})


def language_tag(value: str | None) -> str:
    """Return a BCP 47 language tag for an ISO 639 code or English name, or ``""``."""

    if not value:
        return ""
    return _LANGUAGES.get(value.strip().casefold(), ("", ""))[0]


def language_name(value: str | None) -> str:
    if not value:
        return ""
    return _LANGUAGES.get(value.strip().casefold(), ("", ""))[1]


def codec_label(codec: str) -> str:
    return _CODEC_LABELS.get(codec, codec.upper())


@dataclass(frozen=True, slots=True)
class VideoStream:
    index: int
    codec: str
    profile: str = ""
    level: int = 0
    pix_fmt: str = ""
    bit_depth: int = 8
    width: int = 0
    height: int = 0
    frame_rate: float = 0.0
    codec_tag: str = ""
    hdr: bool = False
    duration: float = 0.0

    @property
    def resolution_label(self) -> str:
        if self.width >= 3200 or self.height >= 1800:
            return "4K"
        for height, label in ((1000, "1080p"), (650, "720p"), (540, "576p"), (400, "480p")):
            if self.height >= height or self.width >= height * 16 // 9:
                return label
        return "SD"

    @property
    def label(self) -> str:
        parts = [self.resolution_label, codec_label(self.codec)]
        if self.bit_depth > 8:
            parts.append(f"{self.bit_depth}-bit")
        if self.hdr:
            parts.append("HDR")
        return " · ".join(parts)


@dataclass(frozen=True, slots=True)
class AudioStream:
    index: int
    codec: str
    profile: str = ""
    channels: int = 2
    language: str = ""
    title: str = ""
    default: bool = False

    @property
    def label(self) -> str:
        name = language_name(self.language) or _clean_title(self.title) or "Unknown language"
        channels = _CHANNEL_LABELS.get(self.channels, f"{self.channels} ch")
        return f"{name} · {codec_label(self.codec)} {channels}"


@dataclass(frozen=True, slots=True)
class SubtitleStream:
    index: int
    codec: str
    language: str = ""
    title: str = ""
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False

    @property
    def text(self) -> bool:
        return self.codec in TEXT_SUBTITLE_CODECS

    @property
    def label(self) -> str:
        return _subtitle_label(self.language, self.title, self.forced, self.hearing_impaired)


@dataclass(frozen=True, slots=True)
class ExternalSubtitle:
    """A subtitle file next to the video. ``path`` is internal and never sent to clients."""

    path: str
    format: str
    language: str = ""
    forced: bool = False
    hearing_impaired: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def label(self) -> str:
        return _subtitle_label(self.language, "", self.forced, self.hearing_impaired)


@dataclass(frozen=True, slots=True)
class Chapter:
    start: float
    end: float
    title: str = ""


@dataclass(frozen=True, slots=True)
class MediaInfo:
    container: str
    duration: float
    start_time: float = 0.0
    size: int = 0
    bit_rate: int = 0
    video: VideoStream | None = None
    audio: tuple[AudioStream, ...] = ()
    subtitles: tuple[SubtitleStream, ...] = ()
    external_subtitles: tuple[ExternalSubtitle, ...] = ()
    chapters: tuple[Chapter, ...] = ()
    keyframes: tuple[float, ...] = field(default=(), repr=False)

    @property
    def default_audio(self) -> AudioStream | None:
        return next((stream for stream in self.audio if stream.default), None) or (
            self.audio[0] if self.audio else None
        )

    def audio_stream(self, index: int | None) -> AudioStream | None:
        if index is None:
            return self.default_audio
        return next((stream for stream in self.audio if stream.index == index), None)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["keyframes"] = [round(value * 1000) for value in self.keyframes]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MediaInfo:
        video = payload.get("video")
        return cls(
            container=str(payload.get("container", "")),
            duration=float(payload.get("duration", 0)),
            start_time=float(payload.get("start_time", 0)),
            size=int(payload.get("size", 0)),
            bit_rate=int(payload.get("bit_rate", 0)),
            video=VideoStream(**video) if video else None,
            audio=tuple(AudioStream(**item) for item in payload.get("audio", ())),
            subtitles=tuple(SubtitleStream(**item) for item in payload.get("subtitles", ())),
            external_subtitles=tuple(
                ExternalSubtitle(**item) for item in payload.get("external_subtitles", ())
            ),
            chapters=tuple(Chapter(**item) for item in payload.get("chapters", ())),
            keyframes=tuple(value / 1000 for value in payload.get("keyframes", ())),
        )


def _clean_title(title: str) -> str:
    cleaned = " ".join(title.split())
    if not cleaned or len(cleaned) > 40 or "." in cleaned and " " not in cleaned:
        return ""
    if any(marker in cleaned.casefold() for marker in ("http", "www", ".org", ".com", ".net")):
        return ""
    return cleaned


def _subtitle_label(language: str, title: str, forced: bool, hearing_impaired: bool) -> str:
    name = language_name(language) or _clean_title(title) or "Unknown language"
    extras = []
    if forced:
        extras.append("Forced")
    if hearing_impaired:
        extras.append("SDH")
    detail = _clean_title(title)
    if detail and language_name(language) and detail.casefold() != name.casefold():
        lowered = detail.casefold()
        if not (forced and "forced" in lowered) and not (hearing_impaired and "sdh" in lowered):
            extras.append(detail)
    return f"{name} ({', '.join(extras)})" if extras else name

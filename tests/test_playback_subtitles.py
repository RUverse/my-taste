from __future__ import annotations

from pathlib import Path

from mytaste.playback.models import SubtitleStream, language_name, language_tag
from mytaste.playback.subtitles import (
    ass_to_webvtt,
    decode_subtitle_bytes,
    find_external_subtitles,
    guess_language,
    repair_mojibake,
    srt_to_webvtt,
)

# Windows-1256 has no Farsi yeh, so real files use the Arabic one.
PERSIAN = "اين يک زيرنويس فارسي است که براي آزمايش نوشته شده و چند کلمه ديگر هم دارد. " * 3
SRT = "1\n00:00:01,000 --> 00:00:02,500\nHello\n"


def touch(path: Path, text: str = SRT, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


def names(found) -> list[tuple[str, str]]:
    return [(item.name, item.language) for item in found]


def test_a_lone_video_owns_every_subtitle_in_its_folder(tmp_path: Path) -> None:
    folder = tmp_path / "Film (2020)"
    video = touch(folder / "Film.2020.1080p.mkv", "")
    touch(
        folder / "Film.2020.720p.YIFY.srt",
        "1\n00:00:01,000 --> 00:00:02,000\n" + "the you and to " * 30,
    )
    touch(folder / "Film.2020.farsi.srt", PERSIAN, "cp1256")
    touch(folder / "Subs" / "English.forced.srt")
    touch(folder / "._Film.2020.srt")
    touch(folder / "notes.txt")

    found = find_external_subtitles(video)

    assert names(found) == [
        ("Film.2020.720p.YIFY.srt", "en"),
        ("Film.2020.farsi.srt", "fa"),
        ("English.forced.srt", "en"),
    ]
    assert found[-1].forced and found[-1].label == "English (Forced)"


def test_shared_folders_match_by_name_or_episode(tmp_path: Path) -> None:
    folder = tmp_path / "Show" / "Season 1"
    first = touch(folder / "Show.S01E01.720p.mkv", "")
    touch(folder / "Show.S01E02.720p.mkv", "")
    touch(folder / "Show.S01E01.720p.en.srt")
    touch(folder / "Show.S01E01.Other.Group.srt")
    touch(folder / "Show.S01E02.720p.srt")
    touch(folder / "Subs" / "Show.S01E01.720p" / "2_Persian.srt", PERSIAN)

    found = find_external_subtitles(first, season=1, episode=1)

    assert names(found) == [
        ("2_Persian.srt", "fa"),
        ("Show.S01E01.720p.en.srt", "en"),
        ("Show.S01E01.Other.Group.srt", ""),
    ]


def test_windows_1256_and_mojibake_are_decoded() -> None:
    assert decode_subtitle_bytes(PERSIAN.encode("cp1256")) == PERSIAN
    assert decode_subtitle_bytes(PERSIAN.encode("utf-8")) == PERSIAN
    assert decode_subtitle_bytes("﻿BOM".encode()) == "BOM"
    assert decode_subtitle_bytes("Café crème".encode("cp1252")) == "Café crème"
    # What a muxer that assumed Windows-1252 stored: every byte became a Latin character.
    garbled = "".join(
        bytes([byte]).decode("cp1252", errors="ignore") or chr(byte)
        for byte in PERSIAN.encode("cp1256")
    )
    assert garbled != PERSIAN
    assert repair_mojibake(garbled) == PERSIAN
    assert repair_mojibake("Plain English text") == "Plain English text"


def test_language_guesses() -> None:
    assert guess_language(PERSIAN) == "fa"
    assert guess_language("مرحبا بكم في هذا الاختبار الطويل جدا " * 5) == "ar"
    assert guess_language("what is it that you want to do and the rest " * 10) == "en"
    assert guess_language("1234 5678") == ""


def test_srt_and_ass_become_webvtt() -> None:
    srt = (
        "\ufeff1\r\n00:00:01,5 --> 00:00:03,000\r\n<font color=red>Hi</font> <i>there</i>\r\n"
        "\r\n2\r\n00:01:00,000 --> 00:01:02,000\r\n\r\n"
    )
    ass = (
        "[Script Info]\nTitle: x\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:02.50,0:00:04.00,Default,,0,0,0,,{\\i1}Hello, world{\\i0}\\Nnext <line>\n"
        "Dialogue: 0,0:00:01.00,0:00:01.50,Default,,0,0,0,,First\n"
    )

    assert srt_to_webvtt(srt) == "WEBVTT\n\n00:00:01.500 --> 00:00:03.000\nHi <i>there</i>\n"
    assert ass_to_webvtt(ass) == (
        "WEBVTT\n\n00:00:01.000 --> 00:00:01.500\nFirst\n\n"
        "00:00:02.500 --> 00:00:04.000\nHello, world\nnext &lt;line&gt;\n"
    )


def test_languages_and_labels() -> None:
    assert (language_tag("per"), language_tag("fas"), language_tag("Farsi")) == ("fa", "fa", "fa")
    assert language_name("eng") == "English"
    assert language_tag("xx") == ""
    stream = SubtitleStream(
        index=3, codec="subrip", language="eng", title="SDH", hearing_impaired=True
    )
    assert stream.label == "English (SDH)"
    assert SubtitleStream(index=4, codec="hdmv_pgs_subtitle", title="www.site.org").label == (
        "Unknown language"
    )

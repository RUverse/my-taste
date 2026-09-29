from __future__ import annotations

from datetime import date

import pytest

from mytaste.library.parser import (
    is_video_file,
    parse_episode_name,
    parse_name,
    season_from_folder,
)

TODAY = date(2026, 9, 10)


@pytest.mark.parametrize(
    ("name", "title", "year"),
    [
        ("Lost.in.Translation.2003.1080p.6CH.GROUP", "Lost in Translation", 2003),
        ("Coherence (2013) (1080p BluRay x265 GROUP)", "Coherence", 2013),
        ("Breathless_1960_CRITERION_COLLECTION_10bit_1080p_x265_BrRip", "Breathless", 1960),
        ("The French Dispatch 2021 BluRay 1080p.H264 Ita Eng AC3 5.1", "The French Dispatch", 2021),
        ("Leila's Brothers (2022) GROUP", "Leila's Brothers", 2022),
        ("Kill.Bill.Vol.1-2003-720p", "Kill Bill Vol 1", 2003),
        ("2001_A_Space_Odyssey.1968.720p", "2001 A Space Odyssey", 1968),
        ("Se7en.1995.m-HD.x264.GROUP", "Se7en", 1995),
        ("Mulholland.Dr..2001.1080p.BluRay.x264.GROUP", "Mulholland Dr", 2001),
        ("Memoirs of a Geisha.2005.720p", "Memoirs of a Geisha", 2005),
        ("Memento.2000.1080p.Dubbed(GROUP)", "Memento", 2000),
        ("A.Clockwork.Orange.1971.720p.x264.aac.mp4.GROUP", "A Clockwork Orange", 1971),
        ("Blade Runner 2049 (2017) 1080p", "Blade Runner 2049", 2017),
        ("Blade Runner 2049 1080p", "Blade Runner 2049", None),
        ("1917.2019.1080p", "1917", 2019),
        ("1917", "1917", None),
        ("Holy.Spider.1080p", "Holy Spider", None),
        ("My.Favourite.Cake.1080", "My Favourite Cake", None),
        ("Leon_The_Professional.1080p", "Leon The Professional", None),
        ("The.Fountian.Bluray.x264.GROUP", "The Fountian", None),
        ("Fargo.720p", "Fargo", None),
        ("2012 - Dark Knight Rises", "Dark Knight Rises", 2012),
        ("1968 - 2001 Space Odyssey", "2001 Space Odyssey", 1968),
        ("Bohemian Rhapsody (2018)", "Bohemian Rhapsody", 2018),
        ("Dune 2021", "Dune", 2021),
        ("Christopher Nolan", "Christopher Nolan", None),
        ("Love.2015.1080p.BluRay.6CH.GROUP", "Love", 2015),
        ("Movie.Title.1080p.2019", "Movie Title", 2019),
    ],
)
def test_parse_name(name: str, title: str, year: int | None) -> None:
    parsed = parse_name(name, today=TODAY)

    assert parsed.title == title
    assert parsed.year == year


@pytest.mark.parametrize(
    ("name", "title", "year", "season", "episode"),
    [
        ("Chernobyl.S01E01.720p.WEBRip.x265-GROUP", "Chernobyl", None, 1, 1),
        ("Silicon Valley S06 E01 720p x265 [GROUP]", "Silicon Valley", None, 6, 1),
        ("Stromberg.S01.E03.example.com", "Stromberg", None, 1, 3),
        ("peaky.blinders.1e02.bdrip.x264-GROUP", "peaky blinders", None, 1, 2),
        ("The.Good.Place.S01E01-E2.720.pHDTV", "The Good Place", None, 1, 1),
        ("Dekalog.1989.S01E02.720p.BluRay", "Dekalog", 1989, 1, 2),
        ("Show.Name.2x07.HDTV", "Show Name", None, 2, 7),
        ("Black.Mirror.S02.Special.White.Christmas.480p", "Black Mirror", None, 2, None),
        ("Episode 3", "", None, None, 3),
        ("Brave.New.World.US.S01E01.1080p.WEB-DL", "Brave New World US", None, 1, 1),
    ],
)
def test_parse_episode_name(
    name: str, title: str, year: int | None, season: int | None, episode: int | None
) -> None:
    parsed = parse_episode_name(name, today=TODAY)

    assert parsed.title == title
    assert parsed.year == year
    assert parsed.season == season
    assert parsed.episode == episode


@pytest.mark.parametrize(
    ("folder", "season"),
    [
        ("S01", 1),
        ("s06", 6),
        ("Season 1", 1),
        ("season 4", 4),
        ("Staffel 2", 2),
        ("Stromberg.S02.All.example.com", 2),
        ("Specials", None),
        ("Christopher Nolan", None),
    ],
)
def test_season_from_folder(folder: str, season: int | None) -> None:
    assert season_from_folder(folder) == season


def test_video_detection_is_case_insensitive() -> None:
    assert is_video_file("Film.MKV")
    assert is_video_file("clip.mp4")
    assert not is_video_file("subtitles.srt")
    assert not is_video_file("archive.rar")

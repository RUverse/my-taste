from __future__ import annotations

from pathlib import Path

import pytest

from mytaste.library.scanner import LibraryUnavailableError, scan_directory


def touch(path: Path, size: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_scan_groups_movies_by_folder_and_filename(tmp_path: Path) -> None:
    root = tmp_path / "Movies"
    touch(root / "Lost.in.Translation.2003.1080p.mkv")
    touch(root / "Lost.in.Translation.2003.srt")
    touch(root / "Christopher Nolan" / "2010 - Inception" / "Inception.2010.1080p.mkv")
    touch(root / "Christopher Nolan" / "2010 - Inception" / "._Inception.2010.1080p.mkv")
    touch(root / "Coen Brothers" / "1996 - Fargo" / "Fargo.720p.mkv")
    touch(root / "Roman Polanski" / "2002 - Pianist" / "The.Pianist.2002.1080p.mkv")
    touch(root / "Dune 2021" / "Dune.2021.1080p.mkv")
    touch(root / "$RECYCLE.BIN" / "Deleted.2001.mkv")
    touch(root / ".hidden" / "Secret.2001.mkv")
    touch(root / "Extras" / "sample.mkv")

    result = scan_directory(root, "movie")
    by_key = {file.group_key: file for file in result.files}

    assert len(result.files) == 5
    assert by_key["movie:lost in translation:2003"].titles == ("Lost in Translation",)
    assert by_key["movie:inception:2010"].year == 2010
    assert by_key["movie:fargo:1996"].titles == ("Fargo",)
    assert by_key["movie:the pianist:2002"].titles == ("The Pianist", "Pianist")
    assert by_key["movie:dune:2021"].titles == ("Dune",)
    assert result.skipped_directories == 2


def test_scan_groups_episodes_by_show_folder(tmp_path: Path) -> None:
    root = tmp_path / "TV Shows"
    touch(root / "Sillicon Valley" / "s06" / "Silicon Valley S06 E01 720p.mkv")
    touch(root / "Sillicon Valley" / "s06" / "Silicon Valley S06 E02 720p.mkv")
    touch(root / "Dark" / "S01" / "DARK.S01E01.720p.mkv")
    touch(root / "Dark" / "S03" / "DARK.S03E01.720p.mkv")
    touch(root / "King the Land S01" / "King.the.Land.S01E01.mkv")
    touch(root / "Chernobyl.S01E01.720p.mkv")
    touch(root / "Dekalog" / "Dekalog.1989.S01E01.1080p.mkv")
    touch(root / "Black mirror" / "S02" / "Black.Mirror.S02.Special.White.Christmas.mkv")

    result = scan_directory(root, "tv")
    groups: dict[str, list] = {}
    for file in result.files:
        groups.setdefault(file.group_key, []).append(file)

    assert set(groups) == {
        "tv:folder:sillicon valley",
        "tv:folder:dark",
        "tv:folder:king the land s01",
        "tv:chernobyl:",
        "tv:folder:dekalog",
        "tv:folder:black mirror",
    }
    silicon = groups["tv:folder:sillicon valley"][0]
    assert silicon.titles == ("Sillicon Valley", "Silicon Valley")
    assert silicon.season == 6
    assert silicon.episode == 1
    assert groups["tv:folder:king the land s01"][0].titles == ("King the Land",)
    assert groups["tv:folder:king the land s01"][0].season == 1
    assert groups["tv:chernobyl:"][0].titles == ("Chernobyl",)
    assert groups["tv:folder:dekalog"][0].year == 1989
    special = groups["tv:folder:black mirror"][0]
    assert special.season == 2
    assert special.episode is None


def test_missing_folder_raises(tmp_path: Path) -> None:
    with pytest.raises(LibraryUnavailableError):
        scan_directory(tmp_path / "missing", "movie")

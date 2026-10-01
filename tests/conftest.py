from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
requires_ffmpeg = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None, reason="ffmpeg and ffprobe are not installed"
)

SRT = """1
00:00:01,000 --> 00:00:03,000
Hello <b>there</b>

2
00:00:05,500 --> 00:00:07,250
Second line
"""


def _ffmpeg(*args: str) -> None:
    assert FFMPEG is not None
    subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        check=True,
        timeout=120,
    )


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Twenty-second synthetic clips with a keyframe every two seconds."""

    if FFMPEG is None or FFPROBE is None:
        pytest.skip("ffmpeg and ffprobe are not installed")
    folder = tmp_path_factory.mktemp("media")
    (folder / "subs.srt").write_text(SRT, encoding="utf-8")
    sources = [
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=24",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
    ]
    video = ["-c:v", "libx264", "-preset", "ultrafast", "-g", "48", "-keyint_min", "48"]
    video += ["-sc_threshold", "0", "-pix_fmt", "yuv420p"]
    _ffmpeg(*sources, "-t", "20", *video, "-c:a", "aac", "-b:a", "64k", str(folder / "Clip.mp4"))
    _ffmpeg(
        *sources,
        "-i",
        str(folder / "subs.srt"),
        "-t",
        "20",
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:s",
        *video,
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-c:s",
        "srt",
        "-metadata:s:s:0",
        "language=per",
        str(folder / "Clip.mkv"),
    )
    (folder / "subs.srt").unlink()
    return folder

from pathlib import Path

import pytest

from mytaste.config import ConfigurationError, load_app_settings


def test_load_app_settings_from_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MYTASTE_TMDB_TOKEN", "secret-token")
    monkeypatch.setenv("MYTASTE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MYTASTE_HOST", "0.0.0.0")
    monkeypatch.setenv("MYTASTE_PORT", "9000")

    settings = load_app_settings()

    assert settings.require_tmdb_token() == "secret-token"
    assert settings.database_path == tmp_path / "mytaste.db"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000


def test_tmdb_token_is_required(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MYTASTE_TMDB_TOKEN", raising=False)
    monkeypatch.setenv("MYTASTE_DATA_DIR", str(tmp_path))

    with pytest.raises(ConfigurationError, match="MYTASTE_TMDB_TOKEN"):
        load_app_settings().require_tmdb_token()


def test_library_settings_are_parsed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MYTASTE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MYTASTE_LIBRARY_ROOTS", f"{tmp_path / 'media'}:{tmp_path / 'other'}")
    monkeypatch.setenv("MYTASTE_LIBRARY_RESCAN_MINUTES", "0")

    settings = load_app_settings()

    assert settings.library_roots == (
        (tmp_path / "media").resolve(),
        (tmp_path / "other").resolve(),
    )
    assert settings.library_rescan_minutes == 0

    monkeypatch.setenv("MYTASTE_LIBRARY_ROOTS", "relative/path")
    with pytest.raises(ConfigurationError, match="absolute"):
        load_app_settings()
    monkeypatch.setenv("MYTASTE_LIBRARY_ROOTS", str(tmp_path))
    monkeypatch.setenv("MYTASTE_LIBRARY_RESCAN_MINUTES", "-1")
    with pytest.raises(ConfigurationError, match="zero or greater"):
        load_app_settings()


def test_playback_settings_are_parsed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MYTASTE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MYTASTE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("MYTASTE_FFMPEG", "/opt/ffmpeg/bin/ffmpeg")
    monkeypatch.setenv("MYTASTE_MAX_TRANSCODES", "2")
    monkeypatch.setenv("MYTASTE_HWACCEL", "None")

    settings = load_app_settings()

    assert settings.cache_dir == tmp_path / "cache"
    assert settings.ffmpeg == "/opt/ffmpeg/bin/ffmpeg"
    assert settings.ffprobe == "ffprobe"
    assert settings.max_transcodes == 2
    assert settings.hwaccel == "none"

    monkeypatch.setenv("MYTASTE_HWACCEL", "cuda")
    with pytest.raises(ConfigurationError, match="MYTASTE_HWACCEL"):
        load_app_settings()
    monkeypatch.setenv("MYTASTE_HWACCEL", "auto")
    monkeypatch.setenv("MYTASTE_MAX_TRANSCODES", "-1")
    with pytest.raises(ConfigurationError, match="zero or greater"):
        load_app_settings()

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_HWACCEL_MODES = frozenset({"auto", "none", "drm"})


class ConfigurationError(ValueError):
    """Raised when application configuration is absent or invalid."""


def default_database_path() -> Path:
    configured = os.environ.get("MYTASTE_DATA_DIR")
    if configured:
        root = Path(configured).expanduser()
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        root = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
        root /= "mytaste"
    return root / "mytaste.db"


def default_cache_dir() -> Path:
    configured = os.environ.get("MYTASTE_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg_cache).expanduser() if xdg_cache else Path.home() / ".cache"
    return root / "mytaste"


def _parse_port(value: str | None) -> int:
    try:
        port = int(value or "8000")
    except ValueError as exc:
        raise ConfigurationError("MYTASTE_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("MYTASTE_PORT must be between 1 and 65535")
    return port


@dataclass(frozen=True, slots=True)
class AppSettings:
    tmdb_token: str | None
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    language: str = "en-US"
    request_timeout: float = 10.0
    library_roots: tuple[Path, ...] = ()
    library_rescan_minutes: int = 60
    cache_dir: Path | None = None
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    max_transcodes: int = 1
    hwaccel: str = "auto"

    def require_tmdb_token(self) -> str:
        token = (self.tmdb_token or "").strip()
        if not token:
            raise ConfigurationError(
                "MYTASTE_TMDB_TOKEN is required; configure a TMDB API key or read access token"
            )
        return token


def load_app_settings() -> AppSettings:
    timeout_value = os.environ.get("MYTASTE_REQUEST_TIMEOUT", "10")
    try:
        timeout = float(timeout_value)
    except ValueError as exc:
        raise ConfigurationError("MYTASTE_REQUEST_TIMEOUT must be a number") from exc
    if timeout <= 0:
        raise ConfigurationError("MYTASTE_REQUEST_TIMEOUT must be greater than zero")

    rescan_value = os.environ.get("MYTASTE_LIBRARY_RESCAN_MINUTES", "60")
    try:
        rescan_minutes = int(rescan_value)
    except ValueError as exc:
        raise ConfigurationError("MYTASTE_LIBRARY_RESCAN_MINUTES must be an integer") from exc
    if rescan_minutes < 0:
        raise ConfigurationError("MYTASTE_LIBRARY_RESCAN_MINUTES must be zero or greater")

    transcodes_value = os.environ.get("MYTASTE_MAX_TRANSCODES", "1")
    try:
        max_transcodes = int(transcodes_value)
    except ValueError as exc:
        raise ConfigurationError("MYTASTE_MAX_TRANSCODES must be an integer") from exc
    if max_transcodes < 0:
        raise ConfigurationError("MYTASTE_MAX_TRANSCODES must be zero or greater")

    hwaccel = os.environ.get("MYTASTE_HWACCEL", "auto").strip().lower()
    if hwaccel not in _HWACCEL_MODES:
        choices = ", ".join(sorted(_HWACCEL_MODES))
        raise ConfigurationError(f"MYTASTE_HWACCEL must be one of: {choices}")

    return AppSettings(
        tmdb_token=os.environ.get("MYTASTE_TMDB_TOKEN"),
        database_path=default_database_path(),
        host=os.environ.get("MYTASTE_HOST", "127.0.0.1"),
        port=_parse_port(os.environ.get("MYTASTE_PORT")),
        language=os.environ.get("MYTASTE_LANGUAGE", "en-US"),
        request_timeout=timeout,
        library_roots=_parse_library_roots(os.environ.get("MYTASTE_LIBRARY_ROOTS")),
        library_rescan_minutes=rescan_minutes,
        cache_dir=default_cache_dir(),
        ffmpeg=os.environ.get("MYTASTE_FFMPEG", "ffmpeg"),
        ffprobe=os.environ.get("MYTASTE_FFPROBE", "ffprobe"),
        max_transcodes=max_transcodes,
        hwaccel=hwaccel,
    )


def _parse_library_roots(value: str | None) -> tuple[Path, ...]:
    if not value:
        return ()
    roots: list[Path] = []
    for raw in value.split(os.pathsep):
        cleaned = raw.strip()
        if not cleaned:
            continue
        root = Path(cleaned).expanduser()
        if not root.is_absolute():
            raise ConfigurationError("MYTASTE_LIBRARY_ROOTS entries must be absolute paths")
        roots.append(root.resolve(strict=False))
    return tuple(roots)

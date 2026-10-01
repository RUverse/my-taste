from __future__ import annotations

from typing import Annotated

import typer

from mytaste import __version__
from mytaste.config import ConfigurationError, load_app_settings

app = typer.Typer(
    name="mytaste",
    help="A self-hosted guide to what's new on your streaming services.",
    no_args_is_help=True,
)


@app.command()
def serve(
    host: Annotated[
        str | None,
        typer.Option(help="Interface to bind; use 0.0.0.0 for containers or a LAN."),
    ] = None,
    port: Annotated[int | None, typer.Option(min=1, max=65535, help="TCP port.")] = None,
    reload: Annotated[
        bool, typer.Option(help="Reload when source files change (development only).")
    ] = False,
) -> None:
    """Run the MyTaste web server."""

    try:
        settings = load_app_settings()
        settings.require_tmdb_token()
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc

    import uvicorn

    uvicorn.run(
        "mytaste.web.app:create_app",
        factory=True,
        host=host or settings.host,
        port=port or settings.port,
        reload=reload,
        # Segment requests can wait for ffmpeg; do not let them hold up a restart.
        timeout_graceful_shutdown=5,
    )


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """Browse new releases from the streaming services you already have."""

"""Limit library queries to the libraries the signed-in user may see."""

from __future__ import annotations

from mytaste.accounts.context import visible_libraries


def visible_library_clause(column: str) -> tuple[str, tuple[int, ...]]:
    """An SQL condition limiting ``column`` (a library id) to the user's libraries."""

    allowed = visible_libraries()
    if allowed is None:
        return "1", ()
    if not allowed:
        return "0", ()
    return f"{column} IN ({','.join('?' for _ in allowed)})", tuple(sorted(allowed))

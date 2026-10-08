"""Whose data the current request works with.

The sign-in middleware sets these for every request, and everything the request starts
(including background tasks, which copy the context) sees the same values. Per-user storage
reads :func:`current_user_id`; library storage reads :func:`visible_libraries`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_user_id: ContextVar[int | None] = ContextVar("mytaste_user_id", default=None)
# ``None`` means every library: the owner and admins, and work no user started (scans).
_libraries: ContextVar[frozenset[int] | None] = ContextVar("mytaste_libraries", default=None)


class NoUserError(RuntimeError):
    """Per-user data was used outside a signed-in request."""


def current_user_id() -> int:
    user_id = _user_id.get()
    if user_id is None:
        raise NoUserError("No signed-in user for this operation")
    return user_id


def visible_libraries() -> frozenset[int] | None:
    """The libraries the current user may see, or ``None`` for all of them."""

    return _libraries.get()


def bind(user_id: int | None, libraries: frozenset[int] | None = None) -> None:
    """Set the user for the rest of the current context (a request, a test)."""

    _user_id.set(user_id)
    _libraries.set(libraries)


@contextmanager
def acting_as(user_id: int, libraries: frozenset[int] | None = None) -> Iterator[None]:
    """Work as ``user_id`` inside the block, then restore whoever was set before."""

    user_token = _user_id.set(user_id)
    library_token = _libraries.set(libraries)
    try:
        yield
    finally:
        _libraries.reset(library_token)
        _user_id.reset(user_token)

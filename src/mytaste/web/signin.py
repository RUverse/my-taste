"""Who is signed in: every request is bound to its user before any route runs.

Signed-out visitors only reach the sign-in and setup pages and static files. Changing requests
must come from this site (``Origin`` and ``Sec-Fetch-Site`` checks), so another site can't act
for a signed-in browser.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from mytaste.accounts.context import bind
from mytaste.accounts.models import User
from mytaste.storage.users import SESSION_IDLE, UserRepository

COOKIE = "mytaste_session"
_COOKIE_AGE = int(SESSION_IDLE.total_seconds())
_PUBLIC = ("/static/", "/healthz", "/login", "/setup", "/favicon.ico")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Server-wide changes: libraries and their folders (which also lists the server's disks), the
# site's name, how games are matched across stores, and the people on this server.
_ADMIN_PREFIXES = (
    "/settings/libraries",
    "/api/libraries/folders",
    "/api/preferences/site-title",
    "/settings/people",
)


def _admin_only(path: str) -> bool:
    if path.startswith(_ADMIN_PREFIXES):
        return True
    return path.startswith("/api/games/") and path.endswith("/unlink")


class SignInMiddleware:
    def __init__(self, app: ASGIApp, users: UserRepository) -> None:
        self.app = app
        self.users = users

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path
        user = self.users.session_user(request.cookies.get(COOKIE, ""))
        libraries = None if user is None or user.is_admin else self.users.libraries_for(user.id)
        bind(user.id if user else None, libraries)
        scope.setdefault("state", {})["user"] = user

        response = self._gate(request, path, user)
        if response is not None:
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _gate(self, request: Request, path: str, user: User | None) -> Response | None:
        if request.method not in _SAFE_METHODS and not _same_site(request):
            return JSONResponse({"error": "This request came from another site."}, 403)
        if user is None:
            if path.startswith(_PUBLIC) or path == "/static":
                return None
            if path.startswith("/api/"):
                return JSONResponse({"error": "Sign in first."}, status_code=401)
            if self.users.count() == 0:
                return RedirectResponse("/setup", status_code=303)
            target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            suffix = f"?next={quote(target, safe='')}" if target != "/" else ""
            return RedirectResponse(f"/login{suffix}", status_code=303)
        if _admin_only(path) and not user.is_admin:
            if path.startswith("/api/"):
                return JSONResponse({"error": "Only an admin can do that."}, status_code=403)
            return Response("Only an admin can do that.", status_code=403)
        return None


def _same_site(request: Request) -> bool:
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "same-site", "none")
    # Older browsers: compare Origin with the address the browser used, which a reverse
    # proxy may pass on as X-Forwarded-Host. Tools and tests send neither header.
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return origin != "null" and urlsplit(origin).netloc == host


def sign_in(request: Request, response: Response, user: User, users: UserRepository) -> None:
    """Start a session for ``user`` on this browser, replacing the one it had."""

    previous = request.cookies.get(COOKIE)
    if previous:
        users.end_session(previous)
    token = users.start_session(user.id, request.headers.get("user-agent", ""))
    response.set_cookie(
        COOKIE,
        token,
        max_age=_COOKIE_AGE,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def sign_out(request: Request, response: Response, users: UserRepository) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        users.end_session(token)
    response.delete_cookie(COOKIE, path="/")


def safe_next(value: Any) -> str:
    """A same-site path to return to after signing in, or ``/``."""

    target = str(value or "")
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


class Throttle:
    """Slow down guessing: after a few wrong tries, a profile is locked for a growing while."""

    FREE_TRIES = 5

    def __init__(self) -> None:
        self._failures: dict[int, tuple[int, float]] = {}

    def wait(self, user_id: int) -> int:
        """Seconds until ``user_id`` may try again; 0 when allowed now."""

        _count, until = self._failures.get(user_id, (0, 0.0))
        return max(0, int(until - time.monotonic() + 0.999))

    def failed(self, user_id: int) -> None:
        count, _until = self._failures.get(user_id, (0, 0.0))
        count += 1
        lock = 0.0
        if count >= self.FREE_TRIES:
            lock = min(30.0 * 2 ** (count - self.FREE_TRIES), 15 * 60.0)
        self._failures[user_id] = (count, time.monotonic() + lock)

    def succeeded(self, user_id: int) -> None:
        self._failures.pop(user_id, None)

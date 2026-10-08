"""Sign in with MyTaste: connecting this server to the Hub, invites, and the sign-in round trip.

The owner connects the server once (it registers with the Hub). After that, anyone whose
MyTaste account is linked to an account here can sign in with it, and people the owner invites
by MyTaste username get a member account the first time they sign in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from mytaste.accounts.hub import HubClient, HubError
from mytaste.accounts.models import User
from mytaste.storage.users import UserRepository
from mytaste.web.signin import safe_next, sign_in

_CLIENT_ID, _SECRET, _REDIRECT, _ISSUER = (
    "hub_client_id",
    "hub_client_secret",
    "hub_redirect_uri",
    "hub_issuer",
)
_USERNAME = re.compile(r"[^a-z0-9._-]")

# Messages for ``?hub=`` on the sign-in, account, and people pages; codes keep the text ours.
HUB_MESSAGES = {
    "cancelled": "Signing in with MyTaste was cancelled.",
    "expired": "That sign-in took too long or was already used. Try again.",
    "unreachable": "The MyTaste Hub can't be reached right now. Try again in a moment.",
    "failed": "MyTaste couldn't confirm who you are. Try again.",
    "taken": "That MyTaste account is already linked to someone else here.",
    "only": "You sign in only with MyTaste. Set a password or PIN before unlinking it.",
    "linked": "Your MyTaste account is linked.",
    "connected": "This server is connected to MyTaste.",
}


@dataclass(frozen=True, slots=True)
class HubConnection:
    client_id: str
    secret: str
    redirect_uri: str


def hub_client(request: Request) -> HubClient | None:
    return getattr(request.app.state, "hub", None)


def hub_connection(request: Request) -> HubConnection | None:
    """This server's registration with the configured Hub, if it has one."""

    client = hub_client(request)
    if client is None:
        return None
    settings = request.app.state.instance_settings
    if settings.get(_ISSUER) != client.base_url or not settings.get(_CLIENT_ID):
        return None
    return HubConnection(settings.get(_CLIENT_ID), settings.get(_SECRET), settings.get(_REDIRECT))


def not_invited_message(username: str) -> str:
    who = f"@{username}" if username else "your MyTaste account"
    return f"There's no account here for {who} yet. Ask the owner to invite your MyTaste username."


def create_hub_router() -> APIRouter:
    router = APIRouter()

    def users_of(request: Request) -> UserRepository:
        return request.app.state.users

    def back(path: str, **params: str) -> RedirectResponse:
        return RedirectResponse(f"{path}?{urlencode(params)}", status_code=303)

    # Connecting this server (admins) ----------------------------------------------------

    @router.post("/settings/people/hub/connect")
    async def connect(request: Request) -> Response:
        client = hub_client(request)
        if client is None:
            return Response("MYTASTE_HUB_URL isn't set on this server.", status_code=404)
        owner = next((user for user in users_of(request).list() if user.is_owner), None)
        name = f"{owner.name}'s MyTaste" if owner else "MyTaste"
        redirect_uri = str(request.url_for("hub_callback"))
        try:
            client_id, secret = await client.register(name, redirect_uri)
        except HubError:
            return back("/settings/people", hub="unreachable")
        settings = request.app.state.instance_settings
        settings.save(_ISSUER, client.base_url)
        settings.save(_CLIENT_ID, client_id)
        settings.save(_SECRET, secret)
        settings.save(_REDIRECT, redirect_uri)
        return back("/settings/people", hub="connected")

    @router.post("/settings/people/hub/disconnect")
    async def disconnect(request: Request) -> Response:
        settings = request.app.state.instance_settings
        for key in (_ISSUER, _CLIENT_ID, _SECRET, _REDIRECT):
            settings.save(key, "")
        return RedirectResponse("/settings/people", status_code=303)

    # Signing in -------------------------------------------------------------------------

    @router.get("/auth/hub/start")
    async def start(request: Request) -> Response:
        purpose = "link" if request.query_params.get("purpose") == "link" else "signin"
        me: User | None = request.state.user
        failure_page = "/account" if purpose == "link" else "/login"
        client, connection = hub_client(request), hub_connection(request)
        if client is None or connection is None or (purpose == "link" and me is None):
            return RedirectResponse("/login", status_code=303)
        try:
            target = await client.start(
                client_id=connection.client_id,
                redirect_uri=connection.redirect_uri,
                purpose=purpose,
                next_path=safe_next(request.query_params.get("next")),
                user_id=me.id if me else None,
            )
        except HubError:
            return back(failure_page, hub="unreachable")
        return RedirectResponse(target, status_code=303)

    @router.get("/auth/hub/callback", name="hub_callback")
    async def callback(request: Request) -> Response:
        client, connection = hub_client(request), hub_connection(request)
        pending = client.take(request.query_params.get("state", "")) if client else None
        if client is None or connection is None or pending is None:
            return back("/login", hub="expired")
        failure_page = "/account" if pending.purpose == "link" else "/login"
        if request.query_params.get("error") or not request.query_params.get("code"):
            return back(failure_page, hub="cancelled")
        try:
            identity = await client.finish(
                pending,
                code=request.query_params["code"],
                client_id=connection.client_id,
                secret=connection.secret,
                redirect_uri=connection.redirect_uri,
            )
        except HubError:
            return back(failure_page, hub="failed")
        users = users_of(request)

        if pending.purpose == "link":
            me: User | None = request.state.user
            if me is None or me.id != pending.user_id:
                return back("/login", hub="expired")
            try:
                users.link_hub(me.id, identity.subject, identity.username)
            except ValueError:
                return back("/account", hub="taken")
            return back("/account", hub="linked")

        user = users.user_for_subject(identity.subject)
        target = pending.next_path
        if user is None:
            libraries = users.take_invite(identity.username) if identity.username else None
            if libraries is None:
                username = _USERNAME.sub("", identity.username.lower())[:32]
                return back("/login", hub="not_invited", who=username)
            user = users.create(
                name=identity.name[:40],
                username=users.free_username(identity.username or identity.name),
                role="member",
                hub_only=True,
            )
            users.set_libraries(user.id, libraries)
            users.link_hub(user.id, identity.subject, identity.username)
            target = "/settings"  # Their first stop: choosing their streaming services.
        response = RedirectResponse(target, status_code=303)
        sign_in(request, response, user, users)
        return response

    @router.post("/account/hub/unlink")
    async def unlink(request: Request) -> Response:
        me: User = request.state.user
        try:
            users_of(request).unlink_hub(me.id)
        except ValueError:
            return back("/account", hub="only")
        return RedirectResponse("/account", status_code=303)

    return router

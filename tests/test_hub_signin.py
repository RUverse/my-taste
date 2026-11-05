from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import owner_client
from fastapi.testclient import TestClient
from test_web import FakeCatalog, FakeLibrary

from mytaste.accounts.hub import HubClient
from mytaste.config import AppSettings
from mytaste.web.app import create_app

HUB = "https://hub.test"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeHub:
    """The parts of the MyTaste Hub a server talks to, with people who can 'sign in'."""

    def __init__(self) -> None:
        self.clients: dict[str, tuple[str, list[str]]] = {}
        self.codes: dict[str, dict] = {}
        self.issuer = HUB
        self.signed_in = {"sub": "acct_sara", "preferred_username": "sara", "name": "Sara"}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def authorize(self, location: str) -> str:
        """Play the browser at the hub: approve and return the callback URL with a code."""

        query = {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}
        assert query["code_challenge_method"] == "S256"
        code = f"code-{len(self.codes)}"
        self.codes[code] = {**query, "identity": dict(self.signed_in)}
        return f"{query['redirect_uri']}?code={code}&state={query['state']}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": f"{HUB}/oauth/authorize",
                    "token_endpoint": f"{HUB}/oauth/token",
                    "registration_endpoint": f"{HUB}/oauth/register",
                },
            )
        if path == "/oauth/register":
            body = json.loads(request.content)
            client_id = f"srv_{len(self.clients)}"
            self.clients[client_id] = (f"secret-{client_id}", body["redirect_uris"])
            return httpx.Response(
                201, json={"client_id": client_id, "client_secret": f"secret-{client_id}"}
            )
        if path == "/oauth/token":
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            grant = self.codes.pop(form["code"], None)
            secret, uris = self.clients[form["client_id"]]
            verifier = _b64(hashlib.sha256(form["code_verifier"].encode()).digest())
            if (
                grant is None
                or form["client_secret"] != secret
                or form["redirect_uri"] not in uris
                or verifier != grant["code_challenge"]
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            claims = {
                "iss": HUB,
                "aud": form["client_id"],
                "exp": int(time.time()) + 600,
                "nonce": grant["nonce"],
                **grant["identity"],
            }
            header = _b64(json.dumps({"alg": "HS256"}).encode())
            payload = _b64(json.dumps(claims).encode())
            signature = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256)
            token = f"{header}.{payload}.{_b64(signature.digest())}"
            return httpx.Response(200, json={"id_token": token, "access_token": "a"})
        return httpx.Response(404)


@pytest.fixture
def hub() -> FakeHub:
    return FakeHub()


def make_app(tmp_path: Path, hub: FakeHub):
    settings = AppSettings(
        tmdb_token="test-token", database_path=tmp_path / "mytaste.db", hub_url=HUB
    )
    app = create_app(settings, catalog=FakeCatalog(), library=FakeLibrary())
    app.state.hub = HubClient(HUB, transport=hub.transport())
    return app


def sign_in_with_hub(client: TestClient, hub: FakeHub, purpose: str = "signin"):
    start = client.get(f"/auth/hub/start?purpose={purpose}", follow_redirects=False)
    assert start.status_code == 303 and start.headers["location"].startswith(HUB)
    return client.get(hub.authorize(start.headers["location"]), follow_redirects=False)


def test_connecting_linking_and_signing_in(tmp_path: Path, hub: FakeHub) -> None:
    owner = owner_client(make_app(tmp_path, hub))
    assert "Connect to MyTaste" in owner.get("/settings/people").text
    owner.post("/settings/people/hub/connect")
    ((client_id, (_secret, uris)),) = hub.clients.items()
    assert uris == ["http://testserver/auth/hub/callback"]
    assert "Connected to" in owner.get("/settings/people").text

    # The owner links their MyTaste account, then signs in with it on another device.
    hub.signed_in = {"sub": "acct_owner", "preferred_username": "alireza", "name": "Alireza"}
    linked = sign_in_with_hub(owner, hub, purpose="link")
    assert linked.headers["location"] == "/account?hub=linked"
    assert "@alireza" in owner.get("/account").text

    device = TestClient(owner.app)
    assert "Sign in with MyTaste" in device.get("/login").text
    signed_in = sign_in_with_hub(device, hub)
    assert signed_in.status_code == 303 and signed_in.headers["location"] == "/"
    assert "Owner" in device.get("/account").text


def test_invited_friends_join_and_others_are_turned_away(tmp_path: Path, hub: FakeHub) -> None:
    owner = owner_client(make_app(tmp_path, hub))
    owner.post("/settings/people/hub/connect")

    stranger = TestClient(owner.app)
    refused = sign_in_with_hub(stranger, hub)
    assert refused.headers["location"] == "/login?hub=not_invited&who=sara"
    assert "no account here for @sara" in stranger.get(refused.headers["location"]).text

    owner.post("/settings/people/invites", data={"username": "@Sara"})
    assert "@sara" in owner.get("/settings/people").text
    joined = sign_in_with_hub(stranger, hub)
    assert joined.headers["location"] == "/settings"
    users = owner.app.state.users
    sara = users.by_username("sara")
    assert sara is not None and sara.hub_only and sara.name == "Sara"
    assert users.invites() == ()

    # Friends who joined by invite stay off the profile screen and can't sign in locally.
    assert "Sara" not in TestClient(owner.app).get("/login").text
    local = TestClient(owner.app).post("/login", data={"user": sara.id})
    assert local.status_code == 401
    # Signing in again finds the same account.
    assert sign_in_with_hub(TestClient(owner.app), hub).headers["location"] == "/"
    assert len(users.list()) == 2


def test_sign_ins_are_checked(tmp_path: Path, hub: FakeHub) -> None:
    owner = owner_client(make_app(tmp_path, hub))
    owner.post("/settings/people/hub/connect")
    owner.post("/settings/people/invites", data={"username": "sara"})
    device = TestClient(owner.app)

    # A state the server didn't start, a second use of a state, and a cancelled sign-in.
    assert (
        device.get("/auth/hub/callback?code=x&state=unknown", follow_redirects=False).headers[
            "location"
        ]
        == "/login?hub=expired"
    )
    start = device.get("/auth/hub/start", follow_redirects=False).headers["location"]
    callback = hub.authorize(start)
    device.get(callback, follow_redirects=False)
    device.post("/logout")
    assert device.get(callback, follow_redirects=False).headers["location"] == "/login?hub=expired"
    start = device.get("/auth/hub/start", follow_redirects=False).headers["location"]
    state = parse_qs(urlsplit(start).query)["state"][0]
    cancelled = device.get(
        f"/auth/hub/callback?error=access_denied&state={state}", follow_redirects=False
    )
    assert cancelled.headers["location"] == "/login?hub=cancelled"

    # A hub whose configuration names another issuer is refused.
    hub.issuer = "https://evil.example"
    hub_client = owner.app.state.hub
    hub_client._config = None
    assert device.get("/auth/hub/start", follow_redirects=False).headers["location"] == (
        "/login?hub=unreachable"
    )


def test_without_a_hub_nothing_changes(tmp_path: Path) -> None:
    settings = AppSettings(tmdb_token="test-token", database_path=tmp_path / "mytaste.db")
    owner = owner_client(create_app(settings, catalog=FakeCatalog(), library=FakeLibrary()))
    assert "MyTaste accounts" not in owner.get("/settings/people").text
    assert "Sign in with MyTaste" not in TestClient(owner.app).get("/login").text
    assert owner.get("/auth/hub/start", follow_redirects=False).headers["location"] == "/login"
    assert owner.post("/settings/people/hub/connect").status_code == 404

"""Talking to the MyTaste Hub: registering this server and "Sign in with MyTaste".

The hub is an OpenID Connect provider. This server registers itself once and gets a client id
and secret, then signs people in with the authorization code flow and PKCE. The ID token comes
straight from the hub's token endpoint over TLS; it is still checked: signature (HS256 with the
client secret), issuer, audience, expiry, and nonce.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx


class HubError(Exception):
    """The hub could not be reached or answered with something unusable."""


@dataclass(frozen=True, slots=True)
class HubIdentity:
    subject: str
    username: str
    name: str
    email: str


@dataclass(frozen=True, slots=True)
class PendingSignIn:
    """What a sign-in started here needs when the browser comes back from the hub."""

    verifier: str
    nonce: str
    purpose: str  # "signin" or "link"
    next_path: str
    user_id: int | None
    started: float


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class HubClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport
        self._config: dict[str, Any] | None = None
        self._pending: dict[str, PendingSignIn] = {}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self.timeout, transport=self.transport)

    async def configuration(self) -> dict[str, Any]:
        if self._config is None:
            try:
                async with self._client() as client:
                    response = await client.get(f"{self.base_url}/.well-known/openid-configuration")
                    response.raise_for_status()
                    config = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise HubError("The MyTaste Hub can't be reached right now.") from exc
            if config.get("issuer") != self.base_url:
                raise HubError("The MyTaste Hub's address doesn't match its configuration.")
            self._config = config
        return self._config

    async def register(self, name: str, redirect_uri: str) -> tuple[str, str]:
        """Register this server; returns its client id and secret."""

        config = await self.configuration()
        try:
            async with self._client() as client:
                response = await client.post(
                    config["registration_endpoint"],
                    json={"client_name": name, "redirect_uris": [redirect_uri]},
                )
                response.raise_for_status()
                body = response.json()
            return str(body["client_id"]), str(body["client_secret"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HubError("The MyTaste Hub didn't accept this server.") from exc

    # Sign-in ----------------------------------------------------------------------------

    async def start(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        purpose: str,
        next_path: str,
        user_id: int | None,
    ) -> str:
        """Remember a new sign-in and return the hub address to send the browser to."""

        config = await self.configuration()
        self._forget_old()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        self._pending[state] = PendingSignIn(
            verifier, nonce, purpose, next_path, user_id, time.monotonic()
        )
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{config['authorization_endpoint']}?{query}"

    def take(self, state: str) -> PendingSignIn | None:
        """The sign-in ``state`` belongs to, once; ``None`` if unknown or older than 10 min."""

        self._forget_old()
        return self._pending.pop(state, None)

    async def finish(
        self, pending: PendingSignIn, *, code: str, client_id: str, secret: str, redirect_uri: str
    ) -> HubIdentity:
        config = await self.configuration()
        try:
            async with self._client() as client:
                response = await client.post(
                    config["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "code_verifier": pending.verifier,
                        "client_id": client_id,
                        "client_secret": secret,
                    },
                )
                response.raise_for_status()
                token = str(response.json()["id_token"])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise HubError("The MyTaste Hub didn't confirm the sign-in.") from exc
        claims = self._verified(token, client_id=client_id, secret=secret, nonce=pending.nonce)
        return HubIdentity(
            subject=str(claims["sub"]),
            username=str(claims.get("preferred_username") or ""),
            name=str(claims.get("name") or claims.get("preferred_username") or "MyTaste user"),
            email=str(claims.get("email") or ""),
        )

    def _verified(self, token: str, *, client_id: str, secret: str, nonce: str) -> dict[str, Any]:
        try:
            header, payload, signature = token.split(".")
            if json.loads(_unb64(header)).get("alg") != "HS256":
                raise ValueError("unexpected algorithm")
            expected = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256)
            if not hmac.compare_digest(_unb64(signature), expected.digest()):
                raise ValueError("bad signature")
            claims = json.loads(_unb64(payload))
        except (ValueError, TypeError) as exc:
            raise HubError("The MyTaste Hub's answer couldn't be verified.") from exc
        if (
            claims.get("iss") != self.base_url
            or claims.get("aud") != client_id
            or claims.get("nonce") != nonce
            or not isinstance(claims.get("exp"), int)
            or claims["exp"] < time.time()
            or not claims.get("sub")
        ):
            raise HubError("The MyTaste Hub's answer couldn't be verified.")
        return claims

    def _forget_old(self) -> None:
        cutoff = time.monotonic() - 600
        for state in [key for key, value in self._pending.items() if value.started < cutoff]:
            del self._pending[state]

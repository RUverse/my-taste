"""SQLite persistence for accounts, sign-in sessions, and who can see which library."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mytaste.accounts.models import (
    ROLES,
    Role,
    SecretKind,
    User,
    check_secret,
    clean_name,
    clean_username,
    username_from,
)
from mytaste.accounts.passwords import hash_secret, verify_secret
from mytaste.storage.collections import create_default_collections
from mytaste.storage.migrations import FIRST_USER_ID

# A session stays valid while it is used at least this often, like a TV that stays signed in.
SESSION_IDLE = timedelta(days=180)
# How stale ``last_seen`` may get before a request refreshes it.
_TOUCH_AFTER = timedelta(hours=1)

# Tables holding one user's data, cleared when that user is removed. Collection entries go with
# their collections.
_PER_USER_TABLES = (
    "preferences",
    "subscriptions",
    "display_preferences",
    "collections",
    "playstate",
    "game_preferences",
    "steam_account",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class UserRepository:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def initialize(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
                    secret_kind TEXT NOT NULL DEFAULT ''
                        CHECK (secret_kind IN ('', 'pin', 'password')),
                    secret_hash TEXT,
                    created_at TEXT NOT NULL,
                    hub_only INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    user_agent TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
                CREATE TABLE IF NOT EXISTS library_access (
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    library_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, library_id)
                );
                CREATE TABLE IF NOT EXISTS hub_links (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                    subject TEXT NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    linked_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_invites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    libraries TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(users)")}
            if "hub_only" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN hub_only INTEGER NOT NULL DEFAULT 0"
                )

    # Users ------------------------------------------------------------------------------

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def list(self) -> tuple[User, ...]:
        with self._connect() as connection:
            rows = connection.execute(f"{_USER_SELECT} ORDER BY id").fetchall()
        return tuple(_user_from_row(row) for row in rows)

    def get(self, user_id: int) -> User | None:
        with self._connect() as connection:
            row = connection.execute(f"{_USER_SELECT} WHERE id = ?", (user_id,)).fetchone()
        return _user_from_row(row) if row else None

    def by_username(self, username: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                f"{_USER_SELECT} WHERE username = ?", (username.strip().lower(),)
            ).fetchone()
        return _user_from_row(row) if row else None

    def create(
        self,
        *,
        name: str,
        username: str,
        role: Role,
        secret_kind: SecretKind = "",
        secret: str = "",
        hub_only: bool = False,
    ) -> User:
        """Add an account. The first one must be the owner and gets the data from before
        accounts existed (``FIRST_USER_ID``); later ones start with empty collections."""

        if role not in ROLES:
            raise ValueError("Choose a role")
        check_secret(secret_kind, secret, role)
        values = (
            clean_username(username),
            clean_name(name),
            role,
            secret_kind,
            hash_secret(secret) if secret_kind else None,
            utc_now().isoformat(timespec="seconds"),
            int(hub_only),
        )
        with self._connect() as connection:
            first = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            if first and role != "owner":
                raise ValueError("The first account is the owner")
            if not first and role == "owner":
                raise ValueError("There is already an owner")
            if connection.execute(
                "SELECT 1 FROM users WHERE username = ?", (values[0],)
            ).fetchone():
                raise ValueError("That username is taken")
            cursor = connection.execute(
                "INSERT INTO users (id, username, name, role, secret_kind, secret_hash, "
                "created_at, hub_only) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (FIRST_USER_ID if first else None, *values),
            )
            user_id = int(cursor.lastrowid or 0)
            create_default_collections(connection, user_id)
        user = self.get(user_id)
        assert user is not None
        return user

    def update(self, user_id: int, *, name: str, role: Role | None = None) -> User | None:
        current = self.get(user_id)
        if current is None:
            return None
        new_role = role or current.role
        if new_role not in ROLES:
            raise ValueError("Choose a role")
        if current.is_owner and new_role != "owner":
            raise ValueError("Hand ownership to someone else before changing the owner's role")
        if new_role == "owner" and not current.is_owner:
            raise ValueError("Use “Make owner” to hand over ownership")
        if new_role != "member" and current.secret_kind != "password":
            raise ValueError("Give this person a password before making them an admin")
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET name = ?, role = ? WHERE id = ?",
                (clean_name(name), new_role, user_id),
            )
        return self.get(user_id)

    def make_owner(self, user_id: int) -> None:
        """Hand ownership to an admin; the previous owner becomes an admin."""

        target = self.get(user_id)
        if target is None or target.role != "admin":
            raise ValueError("Only an admin can become the owner")
        with self._connect() as connection:
            connection.execute("UPDATE users SET role = 'admin' WHERE role = 'owner'")
            connection.execute("UPDATE users SET role = 'owner' WHERE id = ?", (user_id,))

    def set_secret(self, user_id: int, kind: SecretKind, secret: str) -> None:
        user = self.get(user_id)
        if user is None:
            raise ValueError("Unknown account")
        check_secret(kind, secret, user.role)
        with self._connect() as connection:
            # A local password or PIN also lets a MyTaste-only account sign in here directly.
            connection.execute(
                "UPDATE users SET secret_kind = ?, secret_hash = ?, "
                "hub_only = CASE WHEN ? = '' THEN hub_only ELSE 0 END WHERE id = ?",
                (kind, hash_secret(secret) if kind else None, kind, user_id),
            )

    def verify(self, user_id: int, secret: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT secret_kind, secret_hash, hub_only FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None or row[2]:
            return False
        if not row[0]:
            return True
        return bool(row[1]) and verify_secret(secret, str(row[1]))

    def delete(self, user_id: int) -> bool:
        """Remove an account and everything that was only theirs. The owner can't be removed."""

        user = self.get(user_id)
        if user is None:
            return False
        if user.is_owner:
            raise ValueError("The owner can't be removed")
        with self._connect() as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table in _PER_USER_TABLES:
                if table in tables:
                    connection.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return True

    # Sessions ---------------------------------------------------------------------------

    def start_session(self, user_id: int, user_agent: str = "") -> str:
        """Create a session and return its token; only a hash of it is stored."""

        token = secrets.token_urlsafe(32)
        now = utc_now().isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, last_seen, user_agent) "
                "VALUES (?, ?, ?, ?, ?)",
                (_token_hash(token), user_id, now, now, user_agent[:200]),
            )
        return token

    def session_user(self, token: str) -> User | None:
        """The user a session token belongs to, while the session is valid."""

        if not token:
            return None
        key = _token_hash(token)
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id, last_seen FROM sessions WHERE token_hash = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            last_seen = datetime.fromisoformat(str(row[1]))
            if now - last_seen > SESSION_IDLE:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (key,))
                return None
            if now - last_seen > _TOUCH_AFTER:
                connection.execute(
                    "UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                    (now.isoformat(timespec="seconds"), key),
                )
        return self.get(int(row[0]))

    def end_session(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def end_sessions(self, user_id: int) -> None:
        """Sign someone out on every device."""

        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # Library access ---------------------------------------------------------------------

    def libraries_for(self, user_id: int) -> frozenset[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT library_id FROM library_access WHERE user_id = ?", (user_id,)
            ).fetchall()
        return frozenset(int(row[0]) for row in rows)

    def set_libraries(self, user_id: int, library_ids: Iterable[int]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM library_access WHERE user_id = ?", (user_id,))
            connection.executemany(
                "INSERT INTO library_access (user_id, library_id) VALUES (?, ?)",
                ((user_id, library_id) for library_id in sorted(set(library_ids))),
            )

    def free_username(self, wanted: str) -> str:
        """``wanted`` as a username, numbered if someone already has it (sara, sara2, …)."""

        base = username_from(wanted)
        candidate, number = base, 1
        while self.by_username(candidate) is not None:
            number += 1
            candidate = f"{base[: 32 - len(str(number))]}{number}"
        return candidate

    # MyTaste accounts ---------------------------------------------------------------------

    def link_hub(self, user_id: int, subject: str, username: str) -> None:
        """Link a MyTaste account to an account here; each can be linked only once."""

        with self._connect() as connection:
            other = connection.execute(
                "SELECT user_id FROM hub_links WHERE subject = ?", (subject,)
            ).fetchone()
            if other and int(other[0]) != user_id:
                raise ValueError("That MyTaste account is already linked to someone else here")
            connection.execute(
                "INSERT INTO hub_links (user_id, subject, username, linked_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET subject = excluded.subject, "
                "username = excluded.username, linked_at = excluded.linked_at",
                (user_id, subject, username, utc_now().isoformat(timespec="seconds")),
            )

    def unlink_hub(self, user_id: int) -> None:
        user = self.get(user_id)
        if user is not None and user.hub_only:
            raise ValueError("Set a password or PIN first; you sign in only with MyTaste")
        with self._connect() as connection:
            connection.execute("DELETE FROM hub_links WHERE user_id = ?", (user_id,))

    def user_for_subject(self, subject: str) -> User | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT user_id FROM hub_links WHERE subject = ?", (subject,)
            ).fetchone()
        return self.get(int(row[0])) if row else None

    def hub_usernames(self) -> dict[int, str]:
        """The MyTaste username each linked account here goes by."""

        with self._connect() as connection:
            rows = connection.execute("SELECT user_id, username FROM hub_links").fetchall()
        return {int(row[0]): str(row[1]) for row in rows}

    def invite(self, username: str, library_ids: Iterable[int]) -> None:
        """Let the MyTaste account ``username`` join as a member, seeing these libraries."""

        cleaned = username.strip().lstrip("@").lower()
        if not cleaned:
            raise ValueError("Enter their MyTaste username")
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM hub_links WHERE username = ?", (cleaned,)
            ).fetchone():
                raise ValueError("That MyTaste account is already here")
            connection.execute(
                "INSERT INTO hub_invites (username, libraries, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO UPDATE SET libraries = excluded.libraries",
                (
                    cleaned,
                    json.dumps(sorted(set(library_ids))),
                    utc_now().isoformat(timespec="seconds"),
                ),
            )

    def invites(self) -> tuple[tuple[int, str, tuple[int, ...]], ...]:
        """Open invites: ``(id, MyTaste username, library ids)``."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, username, libraries FROM hub_invites ORDER BY id"
            ).fetchall()
        return tuple((int(row[0]), str(row[1]), tuple(json.loads(row[2]))) for row in rows)

    def cancel_invite(self, invite_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM hub_invites WHERE id = ?", (invite_id,))

    def take_invite(self, username: str) -> tuple[int, ...] | None:
        """Use up the invite for ``username``; its library ids, or ``None`` if there is none."""

        with self._connect() as connection:
            row = connection.execute(
                "DELETE FROM hub_invites WHERE username = ? RETURNING libraries",
                (username.strip().lower(),),
            ).fetchone()
        return tuple(json.loads(row[0])) if row else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


_USER_SELECT = "SELECT id, username, name, role, secret_kind, created_at, hub_only FROM users"


def _user_from_row(row: sqlite3.Row | tuple[object, ...]) -> User:
    return User(
        id=int(row[0]),
        username=str(row[1]),
        name=str(row[2]),
        role=str(row[3]),  # type: ignore[arg-type]
        secret_kind=str(row[4]),  # type: ignore[arg-type]
        created_at=str(row[5]),
        hub_only=bool(row[6]),
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

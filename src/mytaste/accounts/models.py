from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Role = Literal["owner", "admin", "member"]
# How someone proves who they are: a password, a short numeric PIN (members only), or nothing,
# for a member whose profile anyone at home may open from the profile picker.
SecretKind = Literal["password", "pin", ""]

ROLES: tuple[Role, ...] = ("owner", "admin", "member")
ROLE_LABELS: dict[Role, str] = {"owner": "Owner", "admin": "Admin", "member": "Member"}
# Profile colours, picked by id so a profile keeps its colour.
COLORS = ("#e5487b", "#8b5cf6", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#14b8a6", "#6366f1")

MIN_PASSWORD_LENGTH = 8
_PIN = re.compile(r"^\d{4,8}$")
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_MAX_NAME = 40


@dataclass(frozen=True, slots=True)
class User:
    id: int
    username: str
    name: str
    role: Role
    secret_kind: SecretKind
    created_at: str

    @property
    def is_admin(self) -> bool:
        return self.role in ("owner", "admin")

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"

    @property
    def initial(self) -> str:
        return (self.name or self.username)[:1].upper()

    @property
    def color(self) -> str:
        return COLORS[(self.id - 1) % len(COLORS)]

    @property
    def role_label(self) -> str:
        return ROLE_LABELS[self.role]


def clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise ValueError("Enter a name")
    if len(cleaned) > _MAX_NAME:
        raise ValueError(f"Keep the name under {_MAX_NAME} characters")
    return cleaned


def clean_username(username: str) -> str:
    cleaned = username.strip().lower()
    if not _USERNAME.fullmatch(cleaned):
        raise ValueError(
            "Usernames are up to 32 lowercase letters, digits, dots, dashes, or underscores"
        )
    return cleaned


def username_from(name: str) -> str:
    """A username suggested from a display name, such as ``sara`` from ``Sara``."""

    base = re.sub(r"[^a-z0-9._-]+", "", name.strip().lower().replace(" ", "."))
    return base.strip("._-")[:32] or "user"


def check_secret(kind: SecretKind, secret: str, role: Role) -> None:
    """Raise ``ValueError`` unless ``secret`` is acceptable for ``kind`` and ``role``."""

    if role != "member" and kind != "password":
        raise ValueError("The owner and admins need a password")
    if kind == "password" and len(secret) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters")
    if kind == "pin" and not _PIN.fullmatch(secret):
        raise ValueError("A PIN is 4 to 8 digits")

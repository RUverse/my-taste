from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import OWNER_FORM, OWNER_PASSWORD, owner_client
from fastapi.testclient import TestClient
from test_library_repository import make_file
from test_web import FakeCatalog, FakeLibrary

from mytaste.accounts.context import NoUserError, acting_as, bind
from mytaste.accounts.passwords import hash_secret, verify_secret
from mytaste.config import AppSettings
from mytaste.storage.collections import CollectionRepository
from mytaste.storage.library import ItemDraft, LibraryRepository
from mytaste.storage.playback import PlaybackRepository, PlayState
from mytaste.storage.preferences import PreferenceRepository
from mytaste.storage.users import UserRepository
from mytaste.web.app import create_app


def make_app(tmp_path: Path):
    settings = AppSettings(tmdb_token="test-token", database_path=tmp_path / "mytaste.db")
    return create_app(settings, catalog=FakeCatalog(), library=FakeLibrary())


def add_person(owner: TestClient, name: str, kind: str = "", secret: str = "", **extra) -> int:
    response = owner.post(
        "/settings/people",
        data={"name": name, "role": "member", "kind": kind, "secret": secret, **extra},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return int(response.headers["location"].split("saved=")[1])


def test_passwords_are_salted_and_verified() -> None:
    first, second = hash_secret("correct horse"), hash_secret("correct horse")
    assert first != second
    assert verify_secret("correct horse", first)
    assert not verify_secret("wrong horse", first)
    assert not verify_secret("anything", "not-a-hash")


def test_per_user_storage_needs_a_user(tmp_path: Path) -> None:
    repository = PreferenceRepository(tmp_path / "mytaste.db")
    repository.initialize()
    bind(None)
    with pytest.raises(NoUserError):
        repository.get()


def test_first_visit_sets_up_the_owner(tmp_path: Path) -> None:
    client = TestClient(make_app(tmp_path))

    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    assert client.get("/api/collections").status_code == 401
    rejected = client.post("/setup", data={**OWNER_FORM, "confirm": "something else"})
    assert rejected.status_code == 422 and "match" in rejected.text
    weak = client.post("/setup", data={**OWNER_FORM, "password": "short", "confirm": "short"})
    assert weak.status_code == 422

    created = client.post("/setup", data=OWNER_FORM, follow_redirects=False)
    assert created.status_code == 303
    users = client.app.state.users
    owner = users.get(1)
    assert owner is not None and owner.role == "owner" and owner.username == "owner"
    assert client.get("/api/collections").status_code == 200

    # Setup happens once; later visits go to sign-in.
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    assert client.post("/setup", data=OWNER_FORM, follow_redirects=False).status_code == 303
    assert users.count() == 1

    signed_out = TestClient(client.app)
    page = signed_out.get("/collections/popular?media=movie", follow_redirects=False)
    assert page.headers["location"] == "/login?next=%2Fcollections%2Fpopular%3Fmedia%3Dmovie"


def test_profile_picker_pins_and_passwords(tmp_path: Path) -> None:
    owner = owner_client(make_app(tmp_path))
    kid = add_person(owner, "Kid")
    partner = add_person(owner, "Partner", kind="pin", secret="2468")

    device = TestClient(owner.app)
    picker = device.get("/login")
    assert "Who's watching?" in picker.text
    assert "Owner" in picker.text and "Kid" in picker.text and "Partner" in picker.text

    # A profile without a password opens straight away.
    opened = device.post("/login", data={"user": kid, "next": "/account"}, follow_redirects=False)
    assert opened.headers["location"] == "/account"
    assert "Kid" in device.get("/account").text

    # One with a PIN asks for it; a wrong one is refused.
    assert 'inputmode="numeric"' in device.get(f"/login?user={partner}").text
    assert device.post("/login", data={"user": partner, "secret": "1111"}).status_code == 401
    switched = device.post(
        "/login", data={"user": partner, "secret": "2468"}, follow_redirects=False
    )
    assert switched.status_code == 303
    assert "Partner" in device.get("/account").text

    # The owner's password works from the picker and with a username.
    assert device.post("/login", data={"user": 1, "secret": "nope"}).status_code == 401
    named = device.post(
        "/login",
        data={"username": "OWNER", "secret": OWNER_PASSWORD},
        follow_redirects=False,
    )
    assert named.status_code == 303


def test_wrong_guesses_lock_a_profile_for_a_while(tmp_path: Path) -> None:
    owner = owner_client(make_app(tmp_path))
    partner = add_person(owner, "Partner", kind="pin", secret="2468")
    device = TestClient(owner.app)

    for _ in range(5):
        assert device.post("/login", data={"user": partner, "secret": "0000"}).status_code == 401
    locked = device.post("/login", data={"user": partner, "secret": "2468"})
    assert locked.status_code == 429 and "Too many" in locked.text


def test_without_the_picker_everyone_signs_in_by_name(tmp_path: Path) -> None:
    owner = owner_client(make_app(tmp_path))
    kid = add_person(owner, "Kid")
    owner.post("/settings/people/picker", data={})

    device = TestClient(owner.app)
    page = device.get("/login")
    assert "Who's watching?" not in page.text and 'name="username"' in page.text
    assert device.post("/login", data={"user": kid}).status_code == 401
    assert device.post("/login", data={"username": "kid", "secret": ""}).status_code == 401


def test_each_person_has_their_own_services_display_and_collections(tmp_path: Path) -> None:
    owner = owner_client(make_app(tmp_path))
    add_person(owner, "Kid")
    kid = TestClient(owner.app)
    kid.post("/login", data={"user": 2})

    owner.post("/settings/services", data={"region": "US", "provider_ids": ["8"]})
    owner.post("/api/preferences/display", json={"show_genres": True})
    created = owner.post("/api/collections", json={"name": "Owner's picks"})
    owner_collection = created.json()["collection"]["id"]

    kid_collections = kid.get("/api/collections").json()["collections"]
    assert [item["name"] for item in kid_collections] == ["Watchlist", "My favourites"]
    assert kid.patch(f"/api/collections/{owner_collection}", json={"name": "Mine"}).status_code == (
        404
    )
    assert kid.delete(f"/api/collections/{owner_collection}").status_code in (404, 422)
    assert "Owner's picks" in [
        item["name"] for item in owner.get("/api/collections").json()["collections"]
    ]

    preferences = owner.app.state.preferences
    with acting_as(1):
        assert preferences.get().provider_ids == (8,)
        assert preferences.get_display().show_genres is True
    with acting_as(2):
        assert preferences.get().provider_ids == ()
        assert preferences.get_display().show_genres is False


def test_members_cant_change_the_server(tmp_path: Path) -> None:
    owner = owner_client(make_app(tmp_path))
    add_person(owner, "Kid")
    kid = TestClient(owner.app)
    kid.post("/login", data={"user": 2})

    assert kid.get("/api/libraries/folders").status_code == 403
    assert kid.post("/settings/libraries", data={"name": "Mine"}).status_code == 403
    assert kid.post("/api/preferences/site-title", json={"title": "Kid TV"}).status_code == 403
    assert kid.get("/settings/people").status_code == 403
    assert kid.post("/api/games/steam-1/unlink").status_code == 403
    page = kid.get("/settings").text
    assert "data-rename-site" not in page and 'href="/settings/people"' not in page
    assert 'href="/settings/people"' in owner.get("/settings").text


def test_changes_from_other_sites_are_refused(tmp_path: Path) -> None:
    owner = owner_client(make_app(tmp_path))
    cross = owner.post(
        "/api/collections", json={"name": "Sneaky"}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    assert cross.status_code == 403
    foreign = owner.post(
        "/api/collections", json={"name": "Sneaky"}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    same = owner.post(
        "/api/collections", json={"name": "Fine"}, headers={"Origin": "http://testserver"}
    )
    assert same.status_code == 201


def test_signing_out_and_changing_a_pin_end_sessions(tmp_path: Path) -> None:
    owner = owner_client(make_app(tmp_path))
    partner = add_person(owner, "Partner", kind="pin", secret="2468")
    device = TestClient(owner.app)
    device.post("/login", data={"user": partner, "secret": "2468"})
    assert device.get("/api/collections").status_code == 200

    owner.post(f"/settings/people/{partner}/secret", data={"kind": "pin", "secret": "1357"})
    assert device.get("/api/collections").status_code == 401

    device.post("/login", data={"user": partner, "secret": "1357"})
    device.post("/logout")
    assert device.get("/api/collections").status_code == 401


def test_roles_and_removing_people(tmp_path: Path) -> None:
    owner = owner_client(make_app(tmp_path))
    kid = add_person(owner, "Kid")
    users: UserRepository = owner.app.state.users

    # Admins need a password.
    refused = owner.post(f"/settings/people/{kid}", data={"name": "Kid", "role": "admin"})
    assert refused.status_code == 422 and "password" in refused.text
    owner.post(f"/settings/people/{kid}/secret", data={"kind": "password", "secret": "kid-secret"})
    owner.post(f"/settings/people/{kid}", data={"name": "Kid", "role": "admin"})
    assert users.get(kid).role == "admin"  # type: ignore[union-attr]

    # The owner can't be removed, and an admin can't manage the owner.
    with pytest.raises(ValueError):
        users.delete(1)
    admin = TestClient(owner.app)
    admin.post("/login", data={"username": "kid", "secret": "kid-secret"})
    assert admin.post("/settings/people/1/remove").status_code == 403

    # Removing someone deletes their data and nobody else's.
    member = add_person(owner, "Guest")
    with acting_as(member):
        owner.app.state.collections.create("Guest list")
    assert owner.post(f"/settings/people/{member}/remove").status_code == 200
    assert users.get(member) is None
    with sqlite3.connect(tmp_path / "mytaste.db") as connection:
        owners = connection.execute(
            "SELECT DISTINCT user_id FROM collections ORDER BY user_id"
        ).fetchall()
    assert owners == [(1,), (kid,)]


def test_members_see_only_the_libraries_they_are_given(tmp_path: Path) -> None:
    database = tmp_path / "mytaste.db"
    libraries = LibraryRepository(database)
    libraries.initialize()
    shared = libraries.add_library("Family", [("/media/family", "movie")])
    private = libraries.add_library("Private", [("/media/private", "movie")])
    for library, title, tmdb_id in ((shared, "Up", 14160), (private, "Alien", 348)):
        libraries.replace_items(
            library.id,
            [
                ItemDraft(
                    group_key=f"movie:{title}",
                    media_type="movie",
                    title=title,
                    files=(make_file(f"/media/{title}.mkv"),),
                    tmdb_id=tmdb_id,
                )
            ],
        )
    hidden_file = libraries.movie_files(348)[0]
    playback = PlaybackRepository(database)
    playback.initialize()

    with acting_as(2, frozenset({shared.id})):
        assert [library.name for library in libraries.list_libraries()] == ["Family"]
        assert libraries.get_library(private.id) is None
        assert libraries.get_file(hidden_file.id) is None
        assert libraries.movie_files(348) == ()
        assert libraries.matched_keys() == {("movie", 14160)}
        assert {row.tmdb_id for row in playback.library_files()} == {14160}
    with acting_as(3, frozenset()):
        assert libraries.list_libraries() == ()
    # The owner, and work nobody started (scans), see everything.
    assert len(libraries.list_libraries()) == 2


def test_watch_progress_is_per_person(tmp_path: Path) -> None:
    playback = PlaybackRepository(tmp_path / "mytaste.db")
    playback.initialize()
    playback.save_state(PlayState(key="movie:1", position=600, duration=6000))
    with acting_as(2):
        assert playback.state("movie:1") is None
        playback.save_state(PlayState(key="movie:1", position=60, duration=6000))
    assert playback.state("movie:1").position == 600  # type: ignore[union-attr]
    assert [state.position for state in playback.recent_states()] == [600]


def test_one_profile_data_becomes_the_owners(tmp_path: Path) -> None:
    database = tmp_path / "mytaste.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE preferences (id INTEGER PRIMARY KEY CHECK (id = 1), region TEXT NOT NULL);
            INSERT INTO preferences VALUES (1, 'DE');
            CREATE TABLE subscriptions (provider_id INTEGER PRIMARY KEY CHECK (provider_id > 0));
            INSERT INTO subscriptions VALUES (8), (337);
            CREATE TABLE display_preferences (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                show_year INTEGER NOT NULL DEFAULT 1,
                show_rating INTEGER NOT NULL DEFAULT 1,
                show_media_type INTEGER NOT NULL DEFAULT 1,
                show_genres INTEGER NOT NULL DEFAULT 0,
                show_people INTEGER NOT NULL DEFAULT 0,
                card_size TEXT NOT NULL DEFAULT 'comfortable',
                site_title TEXT NOT NULL DEFAULT 'MyTaste'
            );
            INSERT INTO display_preferences (id, show_genres, card_size, site_title)
                VALUES (1, 1, 'compact', 'Home Cinema');
            CREATE TABLE collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                icon TEXT NOT NULL DEFAULT '',
                default_sort TEXT NOT NULL DEFAULT 'added',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            INSERT INTO collections (name, created_at) VALUES ('Watchlist', '2026-01-01'),
                ('Sci-fi', '2026-01-02');
            CREATE TABLE playstate (
                key TEXT PRIMARY KEY,
                file_id INTEGER,
                position REAL NOT NULL DEFAULT 0,
                duration REAL NOT NULL DEFAULT 0,
                watched INTEGER NOT NULL DEFAULT 0,
                play_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX playstate_updated ON playstate (updated_at);
            INSERT INTO playstate (key, position, duration, updated_at)
                VALUES ('movie:603', 1200, 8160, '2026-09-01');
            CREATE TABLE game_preferences (
                id INTEGER PRIMARY KEY CHECK (id=1), plan TEXT NOT NULL, platform TEXT NOT NULL
            );
            INSERT INTO game_preferences VALUES (1, 'premium', 'console');
            CREATE TABLE steam_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                steam_id TEXT NOT NULL,
                persona TEXT NOT NULL DEFAULT '',
                avatar_url TEXT NOT NULL DEFAULT '',
                profile_url TEXT NOT NULL DEFAULT '',
                owned TEXT NOT NULL DEFAULT '[]',
                owned_checked_at REAL,
                owned_status TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO steam_account (id, steam_id, persona) VALUES (1, '7656', 'Player');
            """
        )

    app = make_app(tmp_path)
    owner = owner_client(app)
    preferences = app.state.preferences
    with acting_as(1):
        assert preferences.get().region == "DE"
        assert preferences.get().provider_ids == (8, 337)
        display = preferences.get_display()
        assert display.show_genres is True and display.card_size == "compact"
        assert display.site_title == "Home Cinema"
        assert app.state.game_preferences.get().plan == "premium"
        assert PlaybackRepository(database).state("movie:603").position == 1200  # type: ignore[union-attr]
    names = [item["name"] for item in owner.get("/api/collections").json()["collections"]]
    assert names == ["Watchlist", "Sci-fi"], "the owner keeps the collections and gets no copies"

    with sqlite3.connect(database) as connection:
        steam = connection.execute("SELECT user_id, steam_id FROM steam_account").fetchall()
        leftovers = connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE '%one_profile%'"
        ).fetchall()
    assert steam == [(1, "7656")]
    assert leftovers == []

    # Someone added later starts from scratch, and the site keeps its name for everyone.
    add_person(owner, "Kid")
    with acting_as(2):
        assert preferences.get().provider_ids == ()
        assert preferences.get_display().site_title == "Home Cinema"
        assert [item.name for item in CollectionRepository(database).list()] == [
            "Watchlist",
            "My favourites",
        ]
    # Starting again changes nothing.
    second = make_app(tmp_path)
    with acting_as(1):
        assert second.state.preferences.get().provider_ids == (8, 337)

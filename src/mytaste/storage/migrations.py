"""Helpers for changing existing tables safely."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

# Data from before accounts existed belongs to the first account, which the setup page creates.
FIRST_USER_ID = 1


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def give_to_first_user(
    database_path: Path,
    table: str,
    create_sql: str,
    columns: Sequence[str],
    *,
    after: Sequence[str] = (),
) -> None:
    """Rebuild a one-profile ``table`` as ``create_sql`` with a ``user_id``, in one transaction.

    Tables from before accounts held one profile's data, often in a single row with ``id = 1``.
    If ``table`` has no ``user_id`` column yet, its rows (``columns`` of them) are copied into
    the new shape as the first user's. ``after`` runs inside the same transaction, for indexes.
    Nothing happens when the table is missing or already has ``user_id``.
    """

    connection = sqlite3.connect(database_path, timeout=5, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = table_columns(connection, table)
        if existing and "user_id" not in existing:
            kept = [column for column in columns if column in existing]
            listed = ", ".join(kept)
            connection.execute(f"ALTER TABLE {table} RENAME TO {table}_one_profile")
            connection.execute(create_sql)
            connection.execute(
                f"INSERT INTO {table} (user_id{', ' if kept else ''}{listed}) "
                f"SELECT {FIRST_USER_ID}{', ' if kept else ''}{listed} FROM {table}_one_profile"
            )
            connection.execute(f"DROP TABLE {table}_one_profile")
            for statement in after:
                connection.execute(statement)
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

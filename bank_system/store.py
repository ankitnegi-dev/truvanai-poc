# bank_system/store.py - bank-side SQLite loan store.
#
# This package plays the role of the CUSTOMER. It does NOT import from
# shared/, tools/, agents/, hitl/, or ingest/.
#
# Uses only stdlib: sqlite3, pathlib, datetime.

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The database file lives inside the bank_system directory. It is gitignored.
_DB_PATH = Path(__file__).parent / "bank.db"


def _connect() -> sqlite3.Connection:
    """Return a sqlite3 connection with row factory set."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_store() -> None:
    """Create the loans table if it does not already exist."""
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS loans (
                application_id TEXT PRIMARY KEY,
                amount         REAL NOT NULL,
                status         TEXT NOT NULL,
                decided_at     TEXT
            )
            """
        )
        conn.commit()


def insert_loan(application_id: str, amount: float) -> None:
    """Insert a new loan in RECEIVED status."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO loans (application_id, amount, status, decided_at) "
            "VALUES (?, ?, 'RECEIVED', NULL)",
            (application_id, amount),
        )
        conn.commit()


def update_loan_status(application_id: str, status: str) -> None:
    """Update a loan's status and set decided_at to now.

    A loan that has been moved to ON_HOLD_FOR_REVIEW by a containment
    callback is not overwritten by a later agent status update.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        current = conn.execute(
            "SELECT status FROM loans WHERE application_id = ?",
            (application_id,),
        ).fetchone()
        if current is not None and current["status"] == "ON_HOLD_FOR_REVIEW":
            return
        conn.execute(
            "UPDATE loans SET status = ?, decided_at = ? "
            "WHERE application_id = ?",
            (status, now, application_id),
        )
        conn.commit()


def get_loan(application_id: str) -> dict[str, Any] | None:
    """Return a loan record as a dict or None when not found."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT application_id, amount, status, decided_at "
            "FROM loans WHERE application_id = ?",
            (application_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(row)

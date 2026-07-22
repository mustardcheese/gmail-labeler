"""SQLite tracking of processed messages so nothing is classified twice."""

import sqlite3

from .config import STATE_DB


def connect() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed (
            message_id   TEXT PRIMARY KEY,
            category     TEXT,
            subcategory  TEXT,
            confidence   TEXT,
            processed_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is not None


def mark_processed(
    conn: sqlite3.Connection,
    message_id: str,
    category: str | None,
    subcategory: str | None,
    confidence: str | None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO processed (message_id, category, subcategory, confidence)"
        " VALUES (?, ?, ?, ?)",
        (message_id, category, subcategory, confidence),
    )
    conn.commit()

"""SQLite tracking of processed messages, plus a cache of model answers.

Three tables, with different jobs:

  * processed - "this message is done, never look at it again". Only written
    on a real (non-dry) run.
  * llm_cache - "this is what the model said about this message". Written on
    every run, dry or not, so the tokens a dry run spends aren't thrown away:
    the follow-up real run reuses the answer instead of re-billing for it.
  * meta - small key/value settings; holds "last_checked", the start time of
    the last run that got through everything it searched (see poller.py).

A cache entry is keyed by (message_id, kind) and only reused when its
fingerprint still matches, so editing a prompt or switching provider/model
silently invalidates the old answers rather than replaying stale ones.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from .config import STATE_DB

KIND_CLASSIFY = "classify"
KIND_EXTRACT = "extract"


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_cache (
            message_id  TEXT NOT NULL,
            kind        TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            result      TEXT NOT NULL,
            cached_at   TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (message_id, kind)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
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


# --- Last-checked point -----------------------------------------------------

def get_last_checked(conn: sqlite3.Connection) -> int | None:
    """Epoch seconds of the last complete run, or None if there never was one.

    Installs from before this was recorded fall back to their first processed
    message, which is when the labeler started sorting mail.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_checked'").fetchone()
    if row:
        return int(row[0])
    row = conn.execute("SELECT MIN(processed_at) FROM processed").fetchone()
    if row and row[0]:
        # processed_at is SQLite datetime('now'): UTC, no zone suffix.
        first = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
        return int(first.timestamp())
    return None


def set_last_checked(conn: sqlite3.Connection, ts: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_checked', ?)", (str(ts),)
    )
    conn.commit()


# --- Model-answer cache ----------------------------------------------------

def fingerprint(*parts) -> str:
    """Stable hash of everything that determines the model's answer.

    Pass the prompt, the JSON schema, and the provider/model names. The email
    body isn't included: a Gmail message id already identifies immutable
    content, so the message id in the cache key covers it.
    """
    h = hashlib.sha256()
    for part in parts:
        blob = part if isinstance(part, str) else json.dumps(part, sort_keys=True)
        h.update(blob.encode())
        h.update(b"\0")  # delimiter, so ("ab","c") != ("a","bc")
    return h.hexdigest()


def cache_get(conn: sqlite3.Connection, message_id: str, kind: str,
              fp: str) -> dict | None:
    """The cached answer, or None on a miss or a fingerprint change.

    None is unambiguous: cache_put never stores a null result.
    """
    row = conn.execute(
        "SELECT result FROM llm_cache WHERE message_id = ? AND kind = ? AND fingerprint = ?",
        (message_id, kind, fp),
    ).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(conn: sqlite3.Connection, message_id: str, kind: str,
              fp: str, result: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache (message_id, kind, fingerprint, result)"
        " VALUES (?, ?, ?, ?)",
        (message_id, kind, fp, json.dumps(result)),
    )
    conn.commit()


def get_or_compute(
    conn: sqlite3.Connection,
    message_id: str,
    kind: str,
    fp: str,
    use_cache: bool,
    compute: Callable[[], dict | None],
) -> tuple[dict | None, bool]:
    """Return (result, came_from_cache), calling the model only on a miss.

    use_cache=False (the --no-cache flag) skips the lookup but still stores the
    fresh answer, so forcing one re-classification doesn't leave the cache cold.
    Unusable answers (None) are not cached, so they're retried next pass.
    Exceptions from `compute` propagate untouched for the caller to handle.
    """
    if use_cache:
        hit = cache_get(conn, message_id, kind, fp)
        if hit is not None:
            return hit, True
    result = compute()
    if result is not None:
        cache_put(conn, message_id, kind, fp, result)
    return result, False

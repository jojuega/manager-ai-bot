"""SQLite connection helpers and lightweight schema migrations.

The original monolith opened a sqlite3 connection inline in two places
(`llm_agent._get_srs_conn` / `_get_state_conn` and the `source_id` migration
inside `task_bot.main`).  This module centralises both concerns so every
domain module gets the same connection shape and migrations are idempotent.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from core.config import DATA, SRS_DB, STATE_DB

log = logging.getLogger("core.db")


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------
def get_srs_conn() -> sqlite3.Connection:
    """Return a connection to the SRS database.

    The parent directory is created on first use, mirroring the original
    `llm_agent._get_srs_conn` behaviour."""
    SRS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SRS_DB))
    conn.row_factory = sqlite3.Row
    return conn


def get_state_conn() -> sqlite3.Connection:
    """Return a connection to the state/migration database."""
    conn = sqlite3.connect(str(STATE_DB))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------
def _migrate_state_source_id() -> None:
    """Ensure ``course_flashcards.source_id`` exists.

    Added during the Notion-sync milestone; the column lets the bot correlate
    a flashcard with its Notion page id so deletions propagate.  The migration
    is idempotent and safe to run on every startup."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with get_state_conn() as conn:
            cols = [
                row[1]
                for row in conn.execute("PRAGMA table_info(course_flashcards)").fetchall()
            ]
            if "source_id" not in cols:
                conn.execute(
                    "ALTER TABLE course_flashcards ADD COLUMN source_id TEXT DEFAULT ''"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cf_source_id "
                    "ON course_flashcards(source_id)"
                )
                log.info("DB migration: added source_id column")
    except Exception as exc:  # pragma: no cover — defensive logging only
        log.warning("DB migration (source_id) failed: %s", exc)


def init_db() -> None:
    """Run every idempotent schema migration.

    Safe to call at every startup; cheap when nothing needs to change."""
    _migrate_state_source_id()


__all__ = [
    "get_srs_conn",
    "get_state_conn",
    "init_db",
]

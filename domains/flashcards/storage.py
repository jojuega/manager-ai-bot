"""
domains.flashcards.storage — SQLite access for ``course_flashcards``.

Extracted from the original ``task_bot.py`` monolith.  Wraps the two
helpers the rest of the flashcards domain relies on:

* :func:`courses` — list the distinct course names.
* :func:`getc` — fetch cards filtered by ``course`` and/or ``ctype``.

Plus three SRS write paths that the handlers call after a user grades a
card (:func:`save_conc`, :func:`save_fact`, :func:`save_notion`).  All
three use the SM-2 algorithm (see :func:`sm2` / :func:`sm2_self`).

State storage
-------------
The original monolith opened a fresh ``sqlite3`` connection inline for
every read/write.  This module centralises that behind
:func:`core.db.get_state_conn` so every domain module shares the same
connection shape and we don't fight over the same SQLite file.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from core.config import DATA

log = logging.getLogger("domains.flashcards.storage")

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
# `state.db` lives under <repo>/data/, same layout as the monolith.
DB_PATH: Path = DATA / "state.db"


# --------------------------------------------------------------------------- #
# SM-2 algorithm (Anki-style spaced repetition)
# --------------------------------------------------------------------------- #
def sm2(q: int, ef: float, intv: float, reps: int) -> tuple[float, float, int]:
    """Pure SM-2 implementation.

    Returns ``(new_ef, new_interval, new_repetitions)``.

    * ``q < 3`` resets repetitions to 0 and interval to 1.
    * ``q >= 3`` grows the interval using the existing ease factor.
    * EF is clamped to a minimum of 1.3 (Anki convention).

    Note: ``intv`` is typed as ``float`` because in the general case it
    is ``round(previous_interval * ease_factor)``; the helper coerces
    via ``int()`` at the SQLite boundary (see :func:`_update_srs`).
    """
    if q < 3:
        reps, intv = 0, 1
    else:
        if reps == 0:
            intv = 1
        elif reps == 1:
            intv = 6
        else:
            intv = round(intv * ef)
        reps += 1
    ef = max(1.3, ef + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))
    return ef, intv, reps


def sm2_self(btn: str, ef: float, intv: float, reps: int) -> tuple[float, float, int]:
    """Button → SM-2 helper used by the factual / Notion self-evaluation.

    Maps the four buttons (again/hard/good/easy) onto the 0-5 SM-2
    quality scale and delegates to :func:`sm2`.
    """
    qm = {"again": 0, "hard": 2, "good": 3, "easy": 5}
    return sm2(qm.get(btn, 3), ef, intv, reps)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def courses() -> list[str]:
    """Distinct list of courses present in ``course_flashcards``."""
    if not DB_PATH.exists():
        return []
    try:
        c = sqlite3.connect(str(DB_PATH))
        rows = c.execute(
            "SELECT DISTINCT course FROM course_flashcards ORDER BY course"
        ).fetchall()
        c.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def getc(
    course: Optional[str] = None,
    ctype: Optional[str | Iterable[str]] = None,
) -> list[dict]:
    """Fetch cards, optionally filtered by ``course`` and/or ``ctype``.

    ``ctype`` accepts a single string, a list/tuple, or ``None`` (no
    filter).  Mirrors the original ``getc`` helper that both handlers
    and tools relied on.
    """
    if not DB_PATH.exists():
        return []
    try:
        c = sqlite3.connect(str(DB_PATH))
        c.row_factory = sqlite3.Row
        q = (
            "SELECT id, front, back, course, card_type, ease, interval, "
            "repetitions, next_review FROM course_flashcards"
        )
        p: list = []
        wh: list[str] = []
        if course:
            wh.append("course=?")
            p.append(course)
        if ctype is not None:
            if isinstance(ctype, (list, tuple)):
                ph = ",".join("?" for _ in ctype)
                wh.append(f"card_type IN ({ph})")
                p.extend(ctype)
            else:
                wh.append("card_type=?")
                p.append(ctype)
        if wh:
            q += " WHERE " + " AND ".join(wh)
        q += " ORDER BY next_review ASC"
        rows = c.execute(q, p).fetchall()
        c.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def is_due(card: dict) -> bool:
    """Return True when ``card``'s next_review is today or earlier."""
    return str(card.get("next_review", "2000-01-01")) <= date.today().isoformat()


# --------------------------------------------------------------------------- #
# Writes (SRS updates)
# --------------------------------------------------------------------------- #
def _update_srs(cid: int, ef2: float, int2: float, rep2: int) -> None:
    """Persist the new SM-2 state for a single card.

    ``int2`` is accepted as a float because :func:`sm2` may return a
    rounded float (e.g. ``round(3 * 2.5) == 8`` which is an int but
    ``round(2.6 * 2.5) == 6``); we coerce at the boundary to keep the
    SQLite column types clean.

    Failures are swallowed (logged) so a write error never blocks the
    user-facing message — matches the original monolith's tolerance.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        nr = (date.today() + timedelta(days=int(int2))).isoformat()
        conn.execute(
            "UPDATE course_flashcards "
            "SET ease=?, interval=?, repetitions=?, next_review=? WHERE id=?",
            (ef2, int(int2), rep2, nr, cid),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"flashcards _update_srs: {e}")


def _load_srs_state(cid: int) -> Optional[tuple[float, float, int]]:
    """Return ``(ease, interval, repetitions)`` for a card, or ``None``."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ease, interval, repetitions FROM course_flashcards WHERE id=?",
            (cid,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return row["ease"], row["interval"], row["repetitions"]
    except Exception:
        return None


def save_conc(cid: int, q: int, resp: str) -> None:
    """Update the SRS state for a conceptual card.

    ``q`` is the SM-2 quality (0-5) — handlers pass 0 for both "fail"
    and "skip" since conceptual evaluation is text-input driven.
    """
    state = _load_srs_state(cid)
    if not state:
        return
    ef2, int2, rep2 = sm2(q, *state)
    _update_srs(cid, ef2, int2, rep2)


def save_fact(cid: int, btn: str) -> None:
    """Update the SRS state for a factual card from a self-eval button."""
    state = _load_srs_state(cid)
    if not state:
        return
    ef2, int2, rep2 = sm2_self(btn, *state)
    _update_srs(cid, ef2, int2, rep2)


def save_notion(cid: int, btn: str) -> None:
    """Update the SRS state for a Notion-sourced card from a self-eval button.

    Notion cards only emit 3 buttons (again / good / easy), so we map
    them directly onto the SM-2 quality scale rather than going through
    :func:`sm2_self` (which also has a "hard" branch the Notion UI
    doesn't surface).
    """
    qm = {"again": 0, "good": 3, "easy": 5}
    state = _load_srs_state(cid)
    if not state:
        return
    ef2, int2, rep2 = sm2(qm.get(btn, 3), *state)
    _update_srs(cid, ef2, int2, rep2)


__all__ = [
    "DB_PATH",
    "sm2",
    "sm2_self",
    "courses",
    "getc",
    "is_due",
    "save_conc",
    "save_fact",
    "save_notion",
]

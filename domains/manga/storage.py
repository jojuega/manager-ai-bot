"""
domains.manga.storage — Manga deck hierarchy + card storage layer.

Extracted from the original monolith's ``scripts/srs.py`` (deck hierarchy,
card persistence, review grading) and ``scripts/task_bot.py`` (per-user
``manga_defaults.json`` cache for the P3 default mode).

Public surface
--------------
Image / filesystem helpers
* :func:`ensure_manga_dirs` — create ``data/manga_images/`` and
  ``data/manga_tmp/`` if missing.
* :func:`clean_manga_tmp` — wipe ``data/manga_tmp/`` (stale session files).
* :func:`manga_images_dir`, :func:`manga_tmp_dir`, :func:`manga_defaults_path`
  — centralised path accessors.

Defaults store (per-user, JSON file)
* :func:`load_manga_defaults` — read ``data/manga_defaults.json`` as a dict.
* :func:`save_manga_defaults` — atomically write the defaults dict.
* :func:`get_manga_default`, :func:`set_manga_default`,
  :func:`clear_manga_default` — per-user helpers keyed by ``user_<id>``.

Manga cards (the ``manga_cards`` table)
* :func:`get_manga_card` — fetch a single card by id.
* :func:`get_manga_cards_for_practice` — list due cards for a deck.
* :func:`mark_manga_card_review` — apply SM-2 grading to a card.
* :func:`create_manga_cards_in_deck` — bulk-insert bubbles into a deck.

Deck hierarchy (``Manga`` root → ``Serie`` → ``Volume``)
* :func:`get_manga_deck_hierarchy` — full nested hierarchy as a dict.
* :func:`get_manga_serie_by_name` — find a level-1 deck under Manga.
* :func:`get_manga_volume_by_number` — find a level-2 deck under a serie.
* :func:`create_manga_serie`, :func:`create_manga_volume` — idempotent creators.
* :func:`get_manga_cards_in_deck_tree` — recursive card fetch under any deck.
* :func:`get_or_create_deck_by_name` — generic helper used by manga practice.
* :func:`delete_manga_card`, :func:`update_manga_card` — card mutations.

Constants
* :data:`MANGA_ROOT_ID` — the seeded id of the ``Manga`` root deck.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from core.config import (
    MANGA_DEFAULTS_PATH,
    MANGA_IMAGES_DIR,
    MANGA_TMP_DIR,
    SRS_DB,
)

# Reuse the SRS connection helper from the vocabulary domain — it already
# creates the manga_cards table on first connect. We open our own
# connection here so we don't depend on vocabulary's per-call state.
try:
    # New layout: vocabulary exposes get_db()
    from domains.vocabulary.storage import get_db as _vocab_get_db  # noqa: F401
except Exception:  # pragma: no cover — defensive
    _vocab_get_db = None


# ==============================================================================
# EXCEPTIONS
# ==============================================================================
class SrsError(ValueError):
    """Raised on invalid input / not found in the manga storage API.

    Carries an error dict that the CLI layer (or a tool wrapper) prints.
    """
    def __init__(self, error_dict: dict) -> None:
        super().__init__(error_dict.get("error", "unknown error"))
        self.error_dict = error_dict


# ==============================================================================
# PATH ACCESSORS
# ==============================================================================
def manga_images_dir() -> Path:
    """Return the absolute path of the ``manga_images`` directory."""
    return MANGA_IMAGES_DIR


def manga_tmp_dir() -> Path:
    """Return the absolute path of the ``manga_tmp`` directory."""
    return MANGA_TMP_DIR


def manga_defaults_path() -> Path:
    """Return the absolute path of the ``manga_defaults.json`` file."""
    return MANGA_DEFAULTS_PATH


# ==============================================================================
# IMAGE / FILESYSTEM HELPERS
# ==============================================================================
def ensure_manga_dirs() -> None:
    """Create ``data/manga_images/`` and ``data/manga_tmp/`` if missing."""
    os.makedirs(MANGA_IMAGES_DIR, exist_ok=True)
    os.makedirs(MANGA_TMP_DIR, exist_ok=True)


def clean_manga_tmp() -> None:
    """Wipe stale session files from ``data/manga_tmp/``.

    Removes every regular file in the directory; ignores subdirectories
    and silently swallows per-file errors (logged via the caller).
    """
    if not os.path.isdir(MANGA_TMP_DIR):
        return
    for name in os.listdir(MANGA_TMP_DIR):
        path = os.path.join(MANGA_TMP_DIR, name)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


# ==============================================================================
# DEFAULTS STORE
# ==============================================================================
def load_manga_defaults() -> dict:
    """Read ``data/manga_defaults.json`` and return the dict.

    Returns an empty dict if the file does not exist (without creating it —
    creating the file is :func:`save_manga_defaults`'s job).
    """
    if not MANGA_DEFAULTS_PATH.exists():
        return {}
    try:
        with open(MANGA_DEFAULTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def save_manga_defaults(data: dict) -> None:
    """Atomically write ``data`` to ``data/manga_defaults.json``.

    Uses a ``.tmp`` + ``os.replace`` so partial writes never corrupt the
    real file. Creates the parent directory if needed.
    """
    MANGA_DEFAULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = MANGA_DEFAULTS_PATH.with_suffix(MANGA_DEFAULTS_PATH.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, MANGA_DEFAULTS_PATH)


def _user_key(user_id: int | str) -> str:
    return f"user_{user_id}"


def get_manga_default(user_id: int | str) -> dict | None:
    """Return the default dict for ``user_id`` (or ``None`` if missing)."""
    defaults = load_manga_defaults()
    return defaults.get(_user_key(user_id))


def set_manga_default(user_id: int | str, payload: dict) -> None:
    """Set the default for ``user_id`` to ``payload`` (must include
    ``serie_id``, ``serie_name``, ``volume_id``, ``volume_name``)."""
    defaults = load_manga_defaults()
    defaults[_user_key(user_id)] = payload
    save_manga_defaults(defaults)


def clear_manga_default(user_id: int | str) -> bool:
    """Remove the default for ``user_id``. Returns ``True`` if there was
    one to remove."""
    defaults = load_manga_defaults()
    key = _user_key(user_id)
    if key not in defaults:
        return False
    del defaults[key]
    save_manga_defaults(defaults)
    return True


# ==============================================================================
# DATABASE CONNECTION (raw sqlite3 — same DB as vocabulary/storage.py)
# ==============================================================================
def get_db():
    """Open (and migrate) the SRS database. Returns a sqlite3 Row connection.

    Mirrors the helper in the original ``srs.py`` and the vocabulary
    domain's :func:`get_db`. The ``manga_cards`` table is created by
    :mod:`domains.vocabulary.storage` on its first connect; we ensure it
    exists here too so this module can be used independently.
    """
    SRS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SRS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS manga_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deck_id INTEGER NOT NULL REFERENCES decks(id) ON DELETE CASCADE,
            image_path TEXT NOT NULL,
            bubble_index INTEGER NOT NULL,
            original_text TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'en',
            translation TEXT NOT NULL DEFAULT '',
            smart_explanation TEXT NOT NULL DEFAULT '',
            easiness_factor REAL DEFAULT 2.5,
            interval INTEGER DEFAULT 0,
            repetitions INTEGER DEFAULT 0,
            next_review DATE DEFAULT (date('now')),
            last_reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_manga_deck ON manga_cards(deck_id);
        CREATE INDEX IF NOT EXISTS idx_manga_next ON manga_cards(next_review);
    """)
    return conn


# ==============================================================================
# MANGA ROOT CONSTANT
# ==============================================================================
# The seeded ``Manga`` root deck id (created by vocabulary/storage.py's
# bootstrap). Exposed as a constant so tools can refer to it without
# re-querying on every call.
MANGA_ROOT_ID: int = 8


# ==============================================================================
# CARDS
# ==============================================================================
def get_manga_card(card_id: int) -> dict | None:
    """Return a single manga card by id, or ``None`` if not found."""
    if card_id is None:
        return None
    conn = get_db()
    row = conn.execute(
        """SELECT id, deck_id, image_path, bubble_index, original_text, language,
                  translation, smart_explanation, easiness_factor, interval,
                  repetitions, next_review, last_reviewed_at, created_at
           FROM manga_cards WHERE id = ?""",
        (int(card_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_manga_cards_for_practice(deck_id: int, limit: int = 20) -> list:
    """List up to ``limit`` manga cards due for review under ``deck_id``."""
    conn = get_db()
    today = date.today().isoformat()
    rows = conn.execute(
        """SELECT id, deck_id, image_path, bubble_index, original_text, language,
                  translation, smart_explanation
           FROM manga_cards
           WHERE deck_id = ?
             AND (next_review <= ? OR repetitions = 0)
           ORDER BY next_review ASC, id ASC
           LIMIT ?""",
        (deck_id, today, int(limit) if limit else 20),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_manga_card_review(card_id: int, grade: int) -> dict:
    """Record a review for a manga card, applying SM-2 to scheduling.

    ``grade`` is 0-5. Mirrors the same mapping as vocabulary reviews.
    """
    if grade < 0 or grade > 5:
        raise SrsError({"error": "grade must be 0-5"})

    conn = get_db()
    card = conn.execute(
        "SELECT * FROM manga_cards WHERE id = ?", (int(card_id),)
    ).fetchone()
    if not card:
        conn.close()
        raise SrsError({"error": f"manga card {card_id} not found"})

    # Inline SM-2 (same as srs.py: ef, interval, reps update)
    ef = float(card["easiness_factor"] or 2.5)
    interval = int(card["interval"] or 0)
    reps = int(card["repetitions"] or 0)

    if grade < 3:
        reps = 0
        interval = 0
    else:
        reps += 1
        if reps == 1:
            interval = 1
        elif reps == 2:
            interval = 6
        else:
            interval = max(1, int(round(interval * ef)))
    ef = max(1.3, ef + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)))

    next_review = (date.today() + timedelta(days=interval)).isoformat()
    conn.execute(
        """UPDATE manga_cards
           SET easiness_factor=?, interval=?, repetitions=?, next_review=?,
               last_reviewed_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (ef, interval, reps, next_review, int(card_id)),
    )
    conn.commit()
    conn.close()
    return {
        "status": "reviewed",
        "card_id": int(card_id),
        "grade": grade,
        "new_easiness_factor": ef,
        "new_interval": interval,
        "repetitions": reps,
        "next_review": next_review,
    }


def create_manga_cards_in_deck(deck_id: int, image_path: str, bubbles: list) -> dict:
    """Bulk-insert ``bubbles`` (list of dicts with at least ``original_text``)
    into the ``manga_cards`` table, all attached to ``deck_id`` and
    ``image_path``. Returns a small summary dict."""
    if not bubbles:
        return {"status": "ok", "card_count": 0, "card_ids": []}
    conn = get_db()
    ids: list[int] = []
    for i, b in enumerate(bubbles, start=1):
        original = (b.get("original_text") or "").strip()
        if not original:
            continue
        cur = conn.execute(
            """INSERT INTO manga_cards
               (deck_id, image_path, bubble_index, original_text, language,
                translation, smart_explanation)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                int(deck_id),
                str(image_path),
                int(b.get("index", i)),
                original,
                (b.get("language") or "en").strip()[:8],
                (b.get("translation") or "").strip(),
                (b.get("smart_explanation") or "").strip(),
            ),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return {"status": "ok", "card_count": len(ids), "card_ids": ids}


def delete_manga_card(card_id: int) -> bool:
    """Delete a single manga card. Returns ``True`` if it existed."""
    conn = get_db()
    cur = conn.execute("DELETE FROM manga_cards WHERE id = ?", (int(card_id),))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def update_manga_card(card_id: int, **fields) -> dict | None:
    """Edit the editable fields of a manga card.

    Allowed fields: ``original_text``, ``translation``, ``smart_explanation``,
    ``language``. Returns the updated card dict, or ``None`` if missing.
    """
    allowed = {"original_text", "translation", "smart_explanation", "language"}
    clean = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not clean:
        raise SrsError({
            "error": ("Proporciona al menos un campo a editar: "
                      "original_text, translation, smart_explanation, language"),
        })
    conn = get_db()
    sets = ", ".join(f"{k} = ?" for k in clean)
    params = list(clean.values()) + [int(card_id)]
    cur = conn.execute(f"UPDATE manga_cards SET {sets} WHERE id = ?", params)
    conn.commit()
    if cur.rowcount == 0:
        # Could be that the row exists but values are unchanged; verify.
        row = conn.execute(
            "SELECT id FROM manga_cards WHERE id = ?", (int(card_id),)
        ).fetchone()
        if not row:
            conn.close()
            return None
    row = conn.execute(
        """SELECT id, deck_id, image_path, bubble_index, original_text, language,
                  translation, smart_explanation, easiness_factor, interval,
                  repetitions, next_review, last_reviewed_at, created_at
           FROM manga_cards WHERE id = ?""",
        (int(card_id),),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ==============================================================================
# DECK HIERARCHY (Manga root → Serie → Volume)
# ==============================================================================
def get_or_create_deck_by_name(name: str, emoji: str = "📁") -> dict:
    """Find a top-level deck by name (case-insensitive) or create it.

    Returns the deck dict. Idempotent.
    """
    name = (name or "").strip()
    if not name:
        raise SrsError({"error": "deck name cannot be empty"})
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM decks WHERE lower(name) = lower(?) AND parent_id IS NULL",
        (name,),
    ).fetchone()
    if row:
        conn.close()
        return dict(row)
    cur = conn.execute(
        "INSERT INTO decks (name, emoji) VALUES (?, ?)", (name, emoji)
    )
    conn.commit()
    deck_id = cur.lastrowid
    row = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    conn.close()
    return dict(row)


def create_manga_serie(serie_name: str) -> dict:
    """Create a level-1 deck under the Manga root. Idempotent."""
    serie_name = (serie_name or "").strip()
    if not serie_name:
        raise SrsError({"error": "serie_name cannot be empty"})

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM decks WHERE parent_id = ? AND lower(name) = lower(?)",
        (MANGA_ROOT_ID, serie_name),
    ).fetchone()
    if existing:
        conn.close()
        d = dict(existing)
        d["status"] = "exists"
        return d
    cur = conn.execute(
        "INSERT INTO decks (name, emoji, parent_id) VALUES (?, ?, ?)",
        (serie_name, "📖", MANGA_ROOT_ID),
    )
    conn.commit()
    deck_id = cur.lastrowid
    row = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    conn.close()
    d = dict(row)
    d["status"] = "created"
    return d


def create_manga_volume(serie_name: str, volume_number) -> dict:
    """Create a level-2 deck under the named serie. Idempotent.

    ``volume_number`` may be an int (saved as ``"Volumen {N}"``) or a string
    (used as-is, e.g. ``"1"``, ``"2.5"``, ``"Vol 3"``).
    """
    serie_name = (serie_name or "").strip()
    if not serie_name:
        raise SrsError({"error": "serie_name cannot be empty"})

    # Resolve the serie (create it if missing — keeps the call idempotent)
    serie = get_manga_serie_by_name(serie_name)
    if not serie:
        serie = create_manga_serie(serie_name)
    serie_id = serie["id"]

    if isinstance(volume_number, bool):
        raise SrsError({"error": "volume_number must be int, float or str"})
    if isinstance(volume_number, (int, float)):
        if isinstance(volume_number, float) and not volume_number.is_integer():
            vol_name = f"Volumen {volume_number}"
        else:
            vol_name = f"Volumen {int(volume_number)}"
    else:
        vol_name = str(volume_number).strip()
        if not vol_name:
            raise SrsError({"error": "volume_number cannot be empty"})

    conn = get_db()
    existing = conn.execute(
        "SELECT * FROM decks WHERE parent_id = ? AND lower(name) = lower(?)",
        (serie_id, vol_name),
    ).fetchone()
    if existing:
        conn.close()
        d = dict(existing)
        d["status"] = "exists"
        return d
    cur = conn.execute(
        "INSERT INTO decks (name, emoji, parent_id) VALUES (?, ?, ?)",
        (vol_name, "📘", serie_id),
    )
    conn.commit()
    deck_id = cur.lastrowid
    row = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    conn.close()
    d = dict(row)
    d["status"] = "created"
    return d


def get_manga_serie_by_name(serie_name: str) -> dict | None:
    """Return the level-1 deck under ``MANGA_ROOT_ID`` whose name matches
    ``serie_name`` (case-insensitive), or ``None``."""
    if not serie_name:
        return None
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM decks WHERE parent_id = ? AND lower(name) = lower(?)",
        (MANGA_ROOT_ID, serie_name.strip()),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_manga_volume_by_number(serie_id: int, volume_number) -> dict | None:
    """Return the level-2 deck under ``serie_id`` matching ``volume_number``,
    or ``None``. ``volume_number`` follows the same coercion as
    :func:`create_manga_volume`."""
    if isinstance(volume_number, bool):
        return None
    if isinstance(volume_number, (int, float)):
        if isinstance(volume_number, float) and not volume_number.is_integer():
            vol_name = f"Volumen {volume_number}"
        else:
            vol_name = f"Volumen {int(volume_number)}"
    else:
        vol_name = str(volume_number).strip()
        if not vol_name:
            return None
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM decks WHERE parent_id = ? AND lower(name) = lower(?)",
        (int(serie_id), vol_name),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_manga_cards_in_deck_tree(root_deck_id: int) -> list[dict]:
    """Return every manga card under ``root_deck_id`` and all its descendants.

    The result includes the SM-2 internals (so the practice loop has all
    state it needs); trim before sending to the LLM if desired.
    """
    conn = get_db()
    # Walk the descendant tree
    seen: set[int] = set()
    queue: list[int] = [int(root_deck_id)]
    all_ids: list[int] = []
    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        all_ids.append(cur)
        kids = conn.execute(
            "SELECT id FROM decks WHERE parent_id = ?", (cur,)
        ).fetchall()
        for k in kids:
            queue.append(int(k["id"]))
    if not all_ids:
        conn.close()
        return []
    placeholders = ",".join("?" * len(all_ids))
    rows = conn.execute(
        f"""SELECT mc.id, mc.deck_id, mc.image_path, mc.bubble_index,
                   mc.original_text, mc.language, mc.translation,
                   mc.smart_explanation, mc.easiness_factor, mc.interval,
                   mc.repetitions, mc.next_review, mc.last_reviewed_at,
                   mc.created_at, d.name AS deck_name
            FROM manga_cards mc
            LEFT JOIN decks d ON d.id = mc.deck_id
            WHERE mc.deck_id IN ({placeholders})
            ORDER BY mc.id ASC""",
        all_ids,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_manga_deck_hierarchy() -> dict:
    """Return the nested hierarchy rooted at ``MANGA_ROOT_ID``.

    Shape::

        {
          "root_id": 8,
          "root_name": "Manga",
          "series": [
              {
                  "id": ..., "name": ..., "emoji": ...,
                  "card_count": <recursive>,
                  "volumes": [
                      {"id": ..., "name": ..., "emoji": ...,
                       "card_count": <direct>},
                      ...
                  ],
              },
              ...
          ],
        }
    """
    conn = get_db()
    root = conn.execute(
        "SELECT * FROM decks WHERE id = ?", (MANGA_ROOT_ID,)
    ).fetchone()
    if not root:
        conn.close()
        return {"root_id": MANGA_ROOT_ID, "root_name": "Manga", "series": []}

    series_rows = conn.execute(
        "SELECT * FROM decks WHERE parent_id = ? ORDER BY sort_order, id",
        (MANGA_ROOT_ID,),
    ).fetchall()

    out_series: list[dict] = []
    for s in series_rows:
        s_dict = dict(s)
        volume_rows = conn.execute(
            "SELECT * FROM decks WHERE parent_id = ? ORDER BY sort_order, id",
            (s["id"],),
        ).fetchall()
        s_card_count = 0
        vols: list[dict] = []
        for v in volume_rows:
            v_dict = dict(v)
            count = conn.execute(
                "SELECT COUNT(*) FROM manga_cards WHERE deck_id = ?", (v["id"],)
            ).fetchone()[0]
            v_dict["card_count"] = int(count or 0)
            s_card_count += v_dict["card_count"]
            vols.append(v_dict)
        s_dict["volumes"] = vols
        s_dict["card_count"] = s_card_count
        out_series.append(s_dict)

    conn.close()
    return {
        "root_id": MANGA_ROOT_ID,
        "root_name": root["name"] if root else "Manga",
        "series": out_series,
    }


# ==============================================================================
# P2 SAVE HELPER (moves image from tmp to images and creates cards)
# ==============================================================================
def save_pending_to_deck(pending: dict, deck_id: int, deck_name: str = "") -> dict:
    """Move the pending image from ``data/manga_tmp/`` to
    ``data/manga_images/`` and bulk-insert its bubbles into ``deck_id``.

    ``pending`` is the dict stored in ``context.user_data["pending_manga"]``
    by :mod:`domains.manga.handlers`.

    Returns the result of :func:`create_manga_cards_in_deck`. Raises
    Exception on filesystem or DB errors.
    """
    tmp_path = pending["image_path"]
    bubbles = pending.get("bubbles", []) or []

    ensure_manga_dirs()
    final_id = uuid.uuid4().hex
    final_path = os.path.join(MANGA_IMAGES_DIR, f"{final_id}.jpg")
    try:
        shutil.move(tmp_path, final_path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise

    return create_manga_cards_in_deck(
        deck_id=deck_id, image_path=final_path, bubbles=bubbles,
    )


__all__ = [
    # exceptions
    "SrsError",
    # path accessors
    "manga_images_dir", "manga_tmp_dir", "manga_defaults_path",
    # filesystem
    "ensure_manga_dirs", "clean_manga_tmp",
    # defaults
    "load_manga_defaults", "save_manga_defaults",
    "get_manga_default", "set_manga_default", "clear_manga_default",
    # db
    "get_db", "MANGA_ROOT_ID",
    # cards
    "get_manga_card", "get_manga_cards_for_practice",
    "mark_manga_card_review", "create_manga_cards_in_deck",
    "delete_manga_card", "update_manga_card",
    # hierarchy
    "get_or_create_deck_by_name",
    "create_manga_serie", "create_manga_volume",
    "get_manga_serie_by_name", "get_manga_volume_by_number",
    "get_manga_cards_in_deck_tree", "get_manga_deck_hierarchy",
    # p2 helper
    "save_pending_to_deck",
]

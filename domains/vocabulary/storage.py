"""
domains.vocabulary.storage — SRS storage layer: DB access, due lists, deck tree.

Extracted from the original monolith's `scripts/srs.py` (pure-Python API +
schema bootstrap) and `scripts/task_bot.py` (caches + deck-tree helpers +
``due`` / ``decks`` / ``_srs_review_word``).

Public surface
--------------
DB bootstrap & errors
* :class:`SrsError` — value error with an ``error_dict`` payload.
* :func:`get_db` — open (and migrate) the SRS database, return a Row-conn.

Words
* :func:`get_due_words` — words due for review, optionally filtered by deck.
* :func:`get_stats` — aggregated SRS stats (total, due, by language, by deck).
* :func:`list_words` — all words (or words in a single deck).
* :func:`add_word` — add a new word; raises :class:`SrsError`.
* :func:`review_word` — apply SM-2 review; raises :class:`SrsError`.

Decks
* :func:`get_deck_tree` — nested deck tree.
* :func:`list_decks_flat` — flat list with depth.
* :func:`create_deck` / :func:`rename_deck` / :func:`delete_deck` / :func:`move_deck`
* :func:`get_deck_stats` — alias for ``get_stats()['by_deck']``.

Bot-side helpers (cache + flat lookups, originally in task_bot.py)
* :func:`due` — cached due-word list (per-user TTL).
* :func:`decks` — deck list at a level, with due_count filled from stats.
* :func:`sts` — cached stats.
* :func:`_srs_review_word` — wrapper around :func:`review_word` that swallows errors.
* :func:`_flatten_deck_tree` / :func:`_all_decks_flat` / :func:`_deck_name_from_tree` /
  :func:`_deck_parent_id_from_tree` / :func:`_deck_due_count_from_stats` /
  :func:`due_count_in_deck` / :func:`due_in_deck` / :func:`deck_name`.

Cache invalidation
* :func:`_invalidate_srs_caches` — drop the in-memory deck-tree + stats caches.
* :func:`_invalidate_due_cache` — drop a per-user due cache.
"""
from __future__ import annotations

import sqlite3
import time as _time
from datetime import date, timedelta
from pathlib import Path

from core.config import DATA, SRS_DB

MAX_DECK_DEPTH = 3  # 0=root, 1=child, 2=grandchild, 3=great-grandchild (REJECTED)


# ==============================================================================
# EXCEPTIONS
# ==============================================================================
class SrsError(ValueError):
    """Raised by pure-Python API on invalid input / not found.

    Carries an error dict that the CLI layer (or a tool wrapper) prints.
    """
    def __init__(self, error_dict: dict) -> None:
        super().__init__(error_dict.get("error", "unknown error"))
        self.error_dict = error_dict


# ==============================================================================
# DATABASE
# ==============================================================================
def get_db():
    """Open (and migrate) the SRS database. Returns a sqlite3 Row connection."""
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SRS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL,
            lang TEXT NOT NULL CHECK(lang IN ('de', 'en')),
            sentence TEXT,
            source TEXT,
            definition TEXT,
            easiness_factor REAL DEFAULT 2.5,
            interval INTEGER DEFAULT 0,
            repetitions INTEGER DEFAULT 0,
            next_review DATE DEFAULT (date('now')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id INTEGER NOT NULL REFERENCES words(id),
            quality INTEGER NOT NULL CHECK(quality >= 0 AND quality <= 5),
            response_text TEXT,
            reviewed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            emoji TEXT DEFAULT '📁',
            parent_id INTEGER REFERENCES decks(id),
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
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
        CREATE INDEX IF NOT EXISTS idx_words_next ON words(next_review);
        CREATE INDEX IF NOT EXISTS idx_reviews_word ON reviews(word_id);
        CREATE INDEX IF NOT EXISTS idx_decks_parent ON decks(parent_id);
        CREATE INDEX IF NOT EXISTS idx_manga_deck ON manga_cards(deck_id);
        CREATE INDEX IF NOT EXISTS idx_manga_next ON manga_cards(next_review);
    """)
    # Migration: add deck_id to words if not exists
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(words)").fetchall()]
    if "deck_id" not in cols:
        conn.execute("ALTER TABLE words ADD COLUMN deck_id INTEGER DEFAULT 1")
        conn.execute("UPDATE words SET deck_id = 1 WHERE deck_id IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_words_deck ON words(deck_id)")
    # Seed default deck (id=1)
    conn.execute(
        "INSERT OR IGNORE INTO decks (id, name, emoji) VALUES (1, 'Palabra del Día', '📅')"
    )
    conn.commit()
    return conn


def get_deck_depth(conn, deck_id):
    """Compute depth of a deck by walking parent chain. Root (no parent) = 0."""
    if deck_id is None:
        return -1  # nonexistent
    depth = 0
    current_id = deck_id
    seen = set()
    while True:
        if current_id in seen:
            return depth  # cycle
        seen.add(current_id)
        row = conn.execute(
            "SELECT parent_id FROM decks WHERE id = ?", (current_id,)
        ).fetchone()
        if row is None:
            return None  # deck not found
        parent_id = row["parent_id"]
        if parent_id is None:
            return depth
        current_id = parent_id
        depth += 1


def deck_exists(conn, deck_id) -> bool:
    row = conn.execute("SELECT 1 FROM decks WHERE id = ?", (deck_id,)).fetchone()
    return row is not None


# Lazy import to break the circular import srs_algorithm ↔ storage (sm2_next
# is used by review_word; storage imports the algorithm too).
from domains.vocabulary.srs_algorithm import sm2_next  # noqa: E402


# ==============================================================================
# PURE-PYTHON API
# ==============================================================================
def get_due_words(deck_id=None):
    """Return words due for review. Optionally filtered by ``deck_id``."""
    conn = get_db()
    today = date.today().isoformat()
    if deck_id is not None:
        rows = conn.execute(
            "SELECT id, word, lang, sentence, source, definition, deck_id, "
            "easiness_factor, interval, repetitions "
            "FROM words WHERE (next_review <= ? OR repetitions = 0) "
            "AND deck_id = ? ORDER BY next_review ASC, id ASC",
            (today, deck_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, word, lang, sentence, source, definition, deck_id, "
            "easiness_factor, interval, repetitions "
            "FROM words WHERE next_review <= ? OR repetitions = 0 "
            "ORDER BY next_review ASC, id ASC",
            (today,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Return aggregated SRS statistics."""
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    due = conn.execute(
        "SELECT COUNT(*) FROM words WHERE next_review <= date('now')"
    ).fetchone()[0]
    reviewed = conn.execute("SELECT COUNT(DISTINCT word_id) FROM reviews").fetchone()[0]
    by_lang = conn.execute(
        "SELECT lang, COUNT(*) as cnt FROM words GROUP BY lang"
    ).fetchall()
    lang_stats = {r["lang"]: r["cnt"] for r in by_lang}

    by_deck = conn.execute("""
        SELECT d.id, d.name, d.emoji, d.parent_id,
               COUNT(w.id) as total,
               SUM(CASE WHEN w.next_review <= date('now') THEN 1 ELSE 0 END) as due_count
        FROM decks d
        LEFT JOIN words w ON w.deck_id = d.id
        GROUP BY d.id
        ORDER BY d.sort_order, d.id
    """).fetchall()
    deck_stats = [
        {
            "id": r["id"],
            "name": r["name"],
            "emoji": r["emoji"],
            "parent_id": r["parent_id"],
            "total_words": r["total"] or 0,
            "due_words": r["due_count"] or 0,
        }
        for r in by_deck
    ]
    conn.close()
    return {
        "total_words": total,
        "due_today": due,
        "words_reviewed_at_least_once": reviewed,
        "by_language": lang_stats,
        "by_deck": deck_stats,
    }


def list_words(deck_id=None):
    """Return all words (or words in a single deck)."""
    conn = get_db()
    if deck_id is not None:
        rows = conn.execute(
            "SELECT id, word, lang, source, deck_id, interval, repetitions, next_review "
            "FROM words WHERE deck_id = ? ORDER BY created_at DESC",
            (deck_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, word, lang, source, deck_id, interval, repetitions, next_review "
            "FROM words ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_word(word: str, lang: str, sentence: str = "", source: str = "",
             definition: str = "", deck_id: int = 1) -> dict:
    """Add a new word. Raises :class:`SrsError` on invalid input / missing deck."""
    conn = get_db()
    if lang not in ("de", "en"):
        conn.close()
        raise SrsError({"error": f"lang must be 'de' or 'en' (got {lang!r})"})
    if not deck_exists(conn, deck_id):
        conn.close()
        raise SrsError({"error": f"deck_id {deck_id} no existe"})
    today = date.today().isoformat()
    cur = conn.execute(
        """INSERT INTO words (word, lang, sentence, source, definition, next_review, deck_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (word, lang, sentence, source, definition, today, deck_id),
    )
    conn.commit()
    word_id = cur.lastrowid
    conn.close()
    return {
        "status": "added",
        "id": word_id,
        "word": word,
        "lang": lang,
        "deck_id": deck_id,
    }


def review_word(word_id: int, quality: int, response: str = "") -> dict:
    """Apply SM-2 review to a word. Raises :class:`SrsError` on bad input."""
    conn = get_db()
    if quality < 0 or quality > 5:
        conn.close()
        raise SrsError({"error": "quality must be 0-5"})

    word = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    if not word:
        conn.close()
        raise SrsError({"error": f"word id {word_id} not found"})

    ef, interval, reps = sm2_next(
        quality, word["easiness_factor"], word["interval"], word["repetitions"]
    )
    next_review = (date.today() + timedelta(days=interval)).isoformat()

    conn.execute(
        """UPDATE words SET easiness_factor=?, interval=?, repetitions=?, next_review=?
           WHERE id=?""",
        (ef, interval, reps, next_review, word_id),
    )
    conn.execute(
        "INSERT INTO reviews (word_id, quality, response_text) VALUES (?, ?, ?)",
        (word_id, quality, response),
    )
    conn.commit()
    conn.close()
    return {
        "status": "reviewed",
        "word_id": word_id,
        "quality": quality,
        "new_easiness_factor": ef,
        "new_interval": interval,
        "repetitions": reps,
        "next_review": next_review,
    }


# --- DECKS ---
def list_decks_flat():
    """Flat list of all decks with computed depth, ordered hierarchically."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, emoji, parent_id, sort_order FROM decks "
        "ORDER BY sort_order, id"
    ).fetchall()
    decks = []
    for r in rows:
        d = dict(r)
        d["depth"] = get_deck_depth(conn, d["id"])
        decks.append(d)
    conn.close()
    return decks


def get_deck_tree():
    """Nested deck tree as a list of root nodes."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, name, emoji, parent_id, sort_order FROM decks "
        "ORDER BY sort_order, id"
    ).fetchall()
    decks = []
    for r in rows:
        d = dict(r)
        d["depth"] = get_deck_depth(conn, d["id"])
        decks.append(d)
    conn.close()
    nodes = {d["id"]: {**d, "children": []} for d in decks}
    roots = []
    for d in decks:
        node = nodes[d["id"]]
        if d["parent_id"] is None or d["parent_id"] not in nodes:
            roots.append(node)
        else:
            nodes[d["parent_id"]]["children"].append(node)
    return roots


def get_deck_stats() -> list:
    """Return per-deck stats (alias for ``get_stats()['by_deck']``)."""
    return get_stats()["by_deck"]


def create_deck(name: str, emoji: str = "📁", parent_id=None) -> dict:
    """Create a new deck. Raises :class:`SrsError` on invalid input / depth violation."""
    name = (name or "").strip()
    if not name:
        raise SrsError({"error": "name cannot be empty"})

    conn = get_db()
    if parent_id is not None:
        if not deck_exists(conn, parent_id):
            conn.close()
            raise SrsError({"error": f"parent deck {parent_id} no existe"})
        parent_depth = get_deck_depth(conn, parent_id)
        if parent_depth is None:
            conn.close()
            raise SrsError({"error": f"parent deck {parent_id} no existe"})
        if parent_depth + 1 >= MAX_DECK_DEPTH:
            conn.close()
            raise SrsError({
                "error": (f"max depth {MAX_DECK_DEPTH} alcanzado. "
                          f"parent tiene depth {parent_depth}")
            })

    cur = conn.execute(
        "INSERT INTO decks (name, emoji, parent_id) VALUES (?, ?, ?)",
        (name, emoji, parent_id),
    )
    conn.commit()
    deck_id = cur.lastrowid
    depth = get_deck_depth(conn, deck_id)
    conn.close()
    return {
        "status": "created",
        "id": deck_id,
        "name": name,
        "emoji": emoji,
        "parent_id": parent_id,
        "depth": depth,
    }


def rename_deck(deck_id: int, name: str = None, emoji: str = None) -> dict:
    """Rename a deck and/or change its emoji."""
    conn = get_db()
    if not deck_exists(conn, deck_id):
        conn.close()
        raise SrsError({"error": f"deck {deck_id} no existe"})
    updates = []
    params = []
    if name is not None:
        name = name.strip()
        if not name:
            conn.close()
            raise SrsError({"error": "name cannot be empty"})
        updates.append("name = ?")
        params.append(name)
    if emoji is not None:
        updates.append("emoji = ?")
        params.append(emoji)
    if not updates:
        conn.close()
        raise SrsError({"error": "nada que editar (provee --name o --emoji)"})
    params.append(deck_id)
    conn.execute(f"UPDATE decks SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    row = conn.execute("SELECT * FROM decks WHERE id = ?", (deck_id,)).fetchone()
    conn.close()
    return {
        "status": "renamed",
        **{k: row[k] for k in ("id", "name", "emoji", "parent_id", "sort_order")},
    }


def delete_deck(deck_id: int) -> dict:
    """Delete a deck. Raises :class:`SrsError` if it has children or words."""
    conn = get_db()
    if not deck_exists(conn, deck_id):
        conn.close()
        raise SrsError({"error": f"deck {deck_id} no existe"})
    children = conn.execute(
        "SELECT COUNT(*) FROM decks WHERE parent_id = ?", (deck_id,)
    ).fetchone()[0]
    if children > 0:
        conn.close()
        raise SrsError({
            "error": (f"deck {deck_id} tiene {children} deck(s) hijo(s). "
                      "Elimínalos o muévelos primero.")
        })
    words = conn.execute(
        "SELECT COUNT(*) FROM words WHERE deck_id = ?", (deck_id,)
    ).fetchone()[0]
    if words > 0:
        conn.close()
        raise SrsError({
            "error": (f"deck {deck_id} tiene {words} palabra(s). "
                      "Muévelas o elimínalas primero.")
        })
    conn.execute("DELETE FROM decks WHERE id = ?", (deck_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": deck_id}


def move_deck(deck_id: int, parent_id: int, sort_order: int = None) -> dict:
    """Re-parent a deck. Raises :class:`SrsError` on invalid input / depth / cycles."""
    conn = get_db()
    if not deck_exists(conn, deck_id):
        conn.close()
        raise SrsError({"error": f"deck {deck_id} no existe"})
    if not deck_exists(conn, parent_id):
        conn.close()
        raise SrsError({"error": f"parent deck {parent_id} no existe"})
    if deck_id == parent_id:
        conn.close()
        raise SrsError({"error": "un deck no puede ser su propio padre"})

    # Reject if new parent is a descendant of this deck (cycle)
    descendants = set()

    def collect_descendants(root_id):
        kids = conn.execute(
            "SELECT id FROM decks WHERE parent_id = ?", (root_id,)
        ).fetchall()
        for k in kids:
            descendants.add(k["id"])
            collect_descendants(k["id"])
    collect_descendants(deck_id)
    if parent_id in descendants:
        conn.close()
        raise SrsError({
            "error": (f"parent {parent_id} es descendiente de {deck_id}; "
                      "causaría ciclo")
        })

    new_parent_depth = get_deck_depth(conn, parent_id)
    if new_parent_depth is None:
        conn.close()
        raise SrsError({"error": f"parent deck {parent_id} no existe"})

    def subtree_max_relative(root_id, current_max):
        kids = conn.execute(
            "SELECT id FROM decks WHERE parent_id = ?", (root_id,)
        ).fetchall()
        for k in kids:
            current_max = subtree_max_relative(k["id"], current_max + 1)
        return current_max
    subtree_depth = subtree_max_relative(deck_id, 0)
    new_deepest = new_parent_depth + 1 + subtree_depth
    if new_deepest >= MAX_DECK_DEPTH:
        conn.close()
        raise SrsError({
            "error": (f"max depth {MAX_DECK_DEPTH} alcanzado. "
                      f"subárbol tiene profundidad relativa {subtree_depth}, "
                      f"parent depth {new_parent_depth}, "
                      f"deepest sería {new_deepest}")
        })

    sort_order = sort_order if sort_order is not None else 0
    conn.execute(
        "UPDATE decks SET parent_id = ?, sort_order = ? WHERE id = ?",
        (parent_id, sort_order, deck_id),
    )
    conn.commit()
    new_depth = get_deck_depth(conn, deck_id)
    conn.close()
    return {
        "status": "moved",
        "id": deck_id,
        "parent_id": parent_id,
        "sort_order": sort_order,
        "new_depth": new_depth,
    }


# ==============================================================================
# BOT-SIDE HELPERS (caches, deck-tree flatten, due/decks wrappers)
# ==============================================================================
# These were originally inlined in task_bot.py.  They wrap the pure API in
# short-TTL in-memory caches to avoid hammering the DB on rapid in-session
# clicks.  Same TTL numbers as the monolith: 10s for deck-tree + stats,
# 5s for the per-user due list.

_CACHE_TTL = 10.0
_DUE_TTL = 5.0
_DECK_TREE_CACHE = {"data": None, "ts": 0.0}
_STATS_CACHE = {"data": None, "ts": 0.0}


def _invalidate_srs_caches() -> None:
    """Drop the deck-tree + stats caches. Call after any SRS write."""
    _DECK_TREE_CACHE["data"] = None
    _DECK_TREE_CACHE["ts"] = 0.0
    _STATS_CACHE["data"] = None
    _STATS_CACHE["ts"] = 0.0


def _invalidate_due_cache(cache) -> None:
    """Drop a per-user due cache. Safe to call with ``None``."""
    if cache is not None:
        cache["data"] = None
        cache["ts"] = 0.0


def _get_cached_deck_tree() -> list:
    """Return deck tree (cached for ``_CACHE_TTL`` seconds)."""
    now = _time.time()
    if (_DECK_TREE_CACHE["data"] is not None
            and (now - _DECK_TREE_CACHE["ts"]) < _CACHE_TTL):
        return _DECK_TREE_CACHE["data"]
    try:
        data = get_deck_tree()
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    _DECK_TREE_CACHE["data"] = data
    _DECK_TREE_CACHE["ts"] = now
    return data


def _get_cached_stats() -> dict:
    """Return SRS stats (cached for ``_CACHE_TTL`` seconds)."""
    now = _time.time()
    if (_STATS_CACHE["data"] is not None
            and (now - _STATS_CACHE["ts"]) < _CACHE_TTL):
        return _STATS_CACHE["data"]
    try:
        data = get_stats()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    _STATS_CACHE["data"] = data
    _STATS_CACHE["ts"] = now
    return data


def due(cache=None) -> list:
    """Return list of due words. Optionally cached for ``_DUE_TTL`` seconds.

    Pass a per-user dict (stored in ``context.user_data['due_cache']``) to
    enable caching.  Call :func:`_invalidate_due_cache` after any review /
    add / delete that changes the due list.
    """
    if cache is not None:
        now = _time.time()
        if cache.get("data") is not None and (now - cache.get("ts", 0.0)) < _DUE_TTL:
            return cache["data"]
    try:
        words = get_due_words()
    except Exception:
        words = []
    if cache is not None:
        cache["data"] = words
        cache["ts"] = _time.time()
    return words


def _srs_review_word(word_id: int, quality: int, response: str = ""):
    """In-process wrapper around :func:`review_word`.

    Swallows :class:`SrsError` (and any other exception) so callers don't
    need to wrap (matches the old subprocess behaviour which discarded
    stderr via ``capture_output=True``).
    """
    try:
        return review_word(word_id, quality, response)
    except SrsError:
        return None
    except Exception:
        return None


def _flatten_deck_tree(nodes, depth: int = 0):
    """Walk a nested deck tree and yield each deck as a flat dict with depth.

    The ``children`` list is preserved so callers can do ``has_children`` checks.
    """
    for n in nodes:
        flat = {k: v for k, v in n.items() if k != "children"}
        flat["has_children"] = bool(n.get("children"))
        flat["depth"] = depth
        yield flat
        children = n.get("children") or []
        if children:
            yield from _flatten_deck_tree(children, depth + 1)


def _all_decks_flat() -> list:
    """Return the flat list of all decks from the cached tree."""
    return list(_flatten_deck_tree(_get_cached_deck_tree()))


def _deck_name_from_tree(deck_id):
    """Look up a deck name in the cached tree; return ``None`` if not found."""
    for d in _all_decks_flat():
        if d.get("id") == deck_id:
            return d.get("name")
    return None


def _deck_parent_id_from_tree(deck_id):
    """Look up a deck's ``parent_id`` in the cached tree."""
    for d in _all_decks_flat():
        if d.get("id") == deck_id:
            return d.get("parent_id")
    return None


def _deck_due_count_from_stats(deck_id):
    """Look up a deck's due count from cached stats; ``None`` if not found."""
    stats = _get_cached_stats()
    by_deck = stats.get("by_deck") or []
    if not isinstance(by_deck, list):
        return None
    for d in by_deck:
        if d.get("id") == deck_id:
            try:
                return int(d.get("due_words", 0))
            except Exception:
                return 0
    return None


def decks(parent_id=None) -> list:
    """Return deck list at a given level from the cached tree.

    Each entry: ``{id, name, emoji, parent_id, has_children, depth, due_count}``.
    """
    flat = _all_decks_flat()
    if parent_id is None:
        level = [d for d in flat if d.get("parent_id") is None]
    else:
        level = [d for d in flat if d.get("parent_id") == parent_id]
    for d in level:
        dc = _deck_due_count_from_stats(d["id"])
        d["due_count"] = dc
    return level


def due_count_in_deck(deck_id: int) -> int:
    """Return count of due words in a given deck.

    Uses the cached ``stats.by_deck`` first; falls back to a filtered
    :func:`get_due_words` call if the stats cache is empty.
    """
    dc = _deck_due_count_from_stats(deck_id)
    if dc is not None:
        return dc
    for d in _all_decks_flat():
        if d.get("id") == deck_id and "due_count" in d and d["due_count"] is not None:
            try:
                return int(d["due_count"])
            except Exception:
                return 0
    try:
        return len(get_due_words(deck_id=deck_id))
    except Exception:
        return 0


def due_in_deck(deck_id: int) -> list:
    """Return due words filtered to a specific deck.

    Falls back to the global :func:`due` list if the deck_id column is
    missing (older DB schema), so callers always get *some* words to review.
    """
    try:
        words = get_due_words(deck_id=deck_id)
    except Exception:
        words = []
    if words:
        return words
    return due()


def deck_name(deck_id) -> str:
    """Return the display name for a deck, or ``'Deck N'`` fallback."""
    name = _deck_name_from_tree(deck_id)
    return name or f"Deck {deck_id}"


def sts() -> dict:
    """Return stats dict (cached). Alias for :func:`_get_cached_stats`."""
    return _get_cached_stats()

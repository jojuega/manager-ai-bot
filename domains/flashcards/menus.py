"""
domains.flashcards.menus — keyboard builders for the flashcards domain.

Extracted from the original ``task_bot.py`` (per-course review menu)
and ``flashcard_menu.py`` (Notion tree navigation).  These helpers
build the ``InlineKeyboardMarkup`` instances the handlers render; they
do **not** themselves talk to Telegram — they only return ``(text, kb)``
tuples that the handlers then ``edit_message_text`` with.

Three sub-menus live here:

* :func:`build_course_menu` — top-level "Por Curso" picker (the
  ``rev:c:<course>`` callback family).
* :func:`build_course_review_menu` — within a course: list the
  conceptual / factual / Notion card counts and the "Repasar" buttons
  (``rev:conc:<course>``, ``rev:fact:<course>``, ``rev:notion:<course>``).
* :func:`build_notion_tree_menu` — Notion tree navigation (delegated
  to the original ``flashcard_menu`` logic, kept here for cohesion).

The Notion tree helpers read the cached JSON file
``<DATA>/notion_tree.json`` produced by the Notion sync job — this is
the same on-disk contract the monolith had.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import DATA

log = logging.getLogger("domains.flashcards.menus")

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
TREE_CACHE: Path = DATA / "notion_tree.json"
STATE_DB: Path = DATA / "state.db"


# --------------------------------------------------------------------------- #
# Per-course review menu
# --------------------------------------------------------------------------- #
def build_course_menu(by_course: dict[str, dict[str, int]]) -> tuple[str, InlineKeyboardMarkup]:
    """Build the "Por Curso" picker.

    ``by_course`` is a ``{course: {conceptual, factual, notion, notion_reversed: int}}``
    mapping — same shape produced by the original ``s_courses``
    handler in the monolith.  Falls back to a "no courses" screen if
    the mapping is empty.
    """
    if not by_course:
        return (
            "No hay cursos con flashcards.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]]
            ),
        )

    text = "📚 **Por Curso**\nSelecciona:"
    rows: list[list[InlineKeyboardButton]] = []
    for c in sorted(by_course.keys()):
        nc = by_course[c]["conceptual"]
        nf = by_course[c]["factual"]
        rows.append([
            InlineKeyboardButton(
                f"{c} (❓{nc} 📝{nf})",
                callback_data=f"rev:c:{c}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")])
    return text, InlineKeyboardMarkup(rows)


def build_course_review_menu(
    course: str,
    conc_cards: list[dict],
    fact_cards: list[dict],
    ntn_cards: list[dict],
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the review menu for a single course.

    Shows card counts + due counts, plus a "Repasar" button for each
    type that has at least one due card.
    """
    from datetime import date

    def due(cards: list[dict]) -> int:
        today = date.today().isoformat()
        return sum(
            1 for c in cards if str(c.get("next_review", "2000-01-01")) <= today
        )

    dc = due(conc_cards)
    df = due(fact_cards)
    dn = due(ntn_cards)

    text = (
        f"📚 **{course}**\n\n"
        f"💭 Conceptuales: {len(conc_cards)} ({dc} pend)\n"
        f"📝 Factuales: {len(fact_cards)} ({df} pend)\n"
        f"🎴 Notion: {len(ntn_cards)} ({dn} pend)"
    )
    rows: list[list[InlineKeyboardButton]] = []
    if dc:
        rows.append([
            InlineKeyboardButton(
                f"💭 Repasar Conceptuales ({dc})",
                callback_data=f"rev:conc:{course}",
            )
        ])
    if df:
        rows.append([
            InlineKeyboardButton(
                f"📝 Repasar Factuales ({df})",
                callback_data=f"rev:fact:{course}",
            )
        ])
    if dn:
        rows.append([
            InlineKeyboardButton(
                f"🎴 Repasar Notion ({dn})",
                callback_data=f"rev:notion:{course}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")])
    return text, InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Notion tree menu
# --------------------------------------------------------------------------- #
def get_tree() -> dict:
    """Read the cached Notion tree from disk.

    Returns ``{"sources": {}}`` (no sources) if the cache file is
    missing.  Mirrors the original ``flashcard_menu.get_tree``.
    """
    if TREE_CACHE.exists():
        try:
            return json.loads(TREE_CACHE.read_text())
        except Exception as e:
            log.error(f"get_tree: {e}")
    return {"sources": {}}


def _get_node_by_path(tree: dict, path_parts: list[int]) -> Optional[dict]:
    """Walk the tree by index path (e.g. ``[0, 2, 1]``)."""
    sources = list(tree.get("sources", {}).values())
    if not sources:
        return None
    try:
        idx = path_parts[0]
        if idx >= len(sources):
            return None
        node = sources[idx]
    except (ValueError, IndexError):
        return None
    for part in path_parts[1:]:
        try:
            if part >= len(node.get("children", [])):
                return None
            node = node["children"][part]
        except (ValueError, IndexError):
            return None
    return node


def build_notion_tree_menu(path_parts: Optional[list[int]] = None) -> tuple[str, InlineKeyboardMarkup]:
    """Build the Notion tree navigation menu.

    * ``path_parts is None`` (or empty) — show the source list.
    * ``path_parts = [i]`` — show the root of source ``i``.
    * ``path_parts = [i, j, k, ...]`` — show the node at that path.

    Each child button carries its full path in the callback so the
    handler can re-render any node without recomputing paths.
    """
    if path_parts is None:
        path_parts = []

    tree = get_tree()
    sources = list(tree.get("sources", {}).values())

    # Source list (root menu)
    if not path_parts:
        if not sources:
            return (
                "🗂️ **Flashcards**\n\n"
                "No hay fuentes de Notion configuradas.\n"
                "Usa `/sync add-source <page_id>` para agregar.",
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬅️ Volver", callback_data="menu")]]
                ),
            )
        if len(sources) == 1:
            # Auto-skip to the only source's children.
            return build_notion_tree_menu([0])

        lines = ["🗂️ **Flashcards**\n"]
        kb: list[list[InlineKeyboardButton]] = []
        for i, src in enumerate(sources):
            t = src.get("title", "?")
            fc = src.get("factual_count", 0)
            cc = src.get("conceptual_count", 0)
            lines.append(f"  {t}: {fc} factuales, {cc} conceptuales")
            kb.append([
                InlineKeyboardButton(
                    f"{t} ({fc + cc})",
                    callback_data=f"fm:n:{i}",
                )
            ])
        kb.append([InlineKeyboardButton("🔁 Practice All", callback_data="fm:p:all")])
        kb.append([InlineKeyboardButton("⬅️ Volver", callback_data="menu")])
        return "\n".join(lines), InlineKeyboardMarkup(kb)

    # Node menu
    node = _get_node_by_path(tree, path_parts)
    if not node:
        return (
            "Nodo no encontrado.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Volver", callback_data="menu")]]
            ),
        )

    title = node.get("title", "?")
    fc = node.get("factual_count", 0)
    cc = node.get("conceptual_count", 0)
    has_conc = node.get("has_conceptual", False)

    lines = [f"🗂️ **{title}**\n"]
    if has_conc:
        lines.append("_Título marcado con 🎴 — se generarán flashcards conceptuales_\n")
    lines.append(f"📊 {fc} factuales · {cc} conceptuales\n")

    rows: list[list[InlineKeyboardButton]] = []
    # Children
    for i, child in enumerate(node.get("children", [])):
        ct = child.get("title", "?")
        cfc = child.get("factual_count", 0)
        ccc = child.get("conceptual_count", 0)
        total = cfc + ccc
        emoji = "🎴" if child.get("has_conceptual") else "📄"
        label = f"{emoji} {ct} ({total})" if total else f"{emoji} {ct}"
        rows.append([
            InlineKeyboardButton(
                label,
                callback_data=f"fm:n:{':'.join(str(p) for p in path_parts)}:{i}",
            )
        ])

    # Practice All for this node
    total = fc + cc
    if total > 0:
        path_str = ":".join(str(p) for p in path_parts)
        rows.append([
            InlineKeyboardButton(
                f"🔁 Practice All ({total})",
                callback_data=f"fm:s:{path_str}",
            )
        ])

    # Navigation
    rows.append([])
    nav: list[InlineKeyboardButton] = []
    if len(path_parts) > 1:
        parent_parts = path_parts[:-1]
        nav.append(
            InlineKeyboardButton(
                "⬅️ Atrás",
                callback_data=f"fm:n:{':'.join(str(p) for p in parent_parts)}",
            )
        )
    else:
        # At the root of a single-source tree, "home" would re-render
        # the same screen; send the user to the main menu instead.
        nav.append(InlineKeyboardButton("⬅️ Volver", callback_data="menu"))
    nav.append(InlineKeyboardButton("🏠 Home", callback_data="menu"))
    rows.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(rows)


__all__ = [
    "TREE_CACHE",
    "STATE_DB",
    "get_tree",
    "build_course_menu",
    "build_course_review_menu",
    "build_notion_tree_menu",
]

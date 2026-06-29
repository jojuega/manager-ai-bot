"""
domains.manga.menus — Telegram keyboards / ``InlineKeyboardMarkup`` builders
for the manga domain.

Extracted from the original monolith's ``scripts/task_bot.py`` (the inline
``InlineKeyboardMarkup([...])`` calls inside the manga handler functions;
this module centralises them so handlers stay short and the button
layout is easy to tweak).

Public surface
--------------
* :func:`kbd_done` — "Volver al menú" used after processing an image.
* :func:`kbd_destination` — main P2 menu: list of series + "Crear nueva" +
  "Cancelar".
* :func:`kbd_volume` — per-serie volume picker + "Atrás" / "Cancelar".
* :func:`kbd_practice_front` — flashcard front (Ver respuesta / Salir).
* :func:`kbd_practice_back` — flashcard back (Again / Good / Easy / Salir).
* :func:`kbd_practice_done` — "Practice completo / Volver".
* :func:`text_no_pending` — message shown when the user hits a manga
  callback without a ``pending_manga`` in ``context.user_data``.
"""
from __future__ import annotations

from typing import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ==============================================================================
# COMMON MESSAGES
# ==============================================================================
def text_no_pending() -> str:
    """Message shown when there's no ``pending_manga`` in context."""
    return "⚠️ No hay imagen pendiente. Envíame una imagen de manga."


# ==============================================================================
# DONE / BACK KEYBOARDS
# ==============================================================================
def kbd_done() -> InlineKeyboardMarkup:
    """'Volver al menú' keyboard used after a manga image is processed."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Volver al menú", callback_data="menu")],
    ])


# ==============================================================================
# DESTINATION MENU (P2)
# ==============================================================================
def kbd_destination(series: Iterable[dict], n_bubbles: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the main P2 destination menu.

    Parameters
    ----------
    series:
        Iterable of dicts ``{"id", "name", "volumes": [...]}`` — typically
        :func:`domains.manga.storage.get_manga_deck_hierarchy`()``["series"]``.
    n_bubbles:
        Number of detected bubbles (used in the prompt text).

    Returns
    -------
    (text, InlineKeyboardMarkup) — caller edits/sends a Telegram message
    with these.
    """
    series = list(series or [])
    rows: list[list[InlineKeyboardButton]] = []
    if series:
        text = f"📚 ¿En qué serie guardamos las {n_bubbles} burbuja(s)?"
        for s in series:
            n_vol = len(s.get("volumes", []) or [])
            label = f"📖 {s['name']} ({n_vol} vol)"
            rows.append([
                InlineKeyboardButton(label, callback_data=f"mselect:{s['id']}"),
            ])
        rows.append([
            InlineKeyboardButton("➕ Crear nueva serie + volumen", callback_data="mnew"),
        ])
    else:
        text = "📚 No hay series todavía. Crea la primera:"

    if not series:
        rows.append([
            InlineKeyboardButton("➕ Crear nueva serie + volumen", callback_data="mnew"),
        ])

    rows.append([InlineKeyboardButton("❌ Cancelar", callback_data="mcancel")])
    return text, InlineKeyboardMarkup(rows)


def kbd_volume(serie: dict, n_bubbles: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build the per-serie volume picker.

    Parameters
    ----------
    serie:
        Dict ``{"id", "name", "volumes": [{"id", "name", "card_count"}, ...]}``
        — a single series from the hierarchy.
    n_bubbles:
        Number of detected bubbles (used in the prompt text).
    """
    volumes = serie.get("volumes", []) or []
    rows: list[list[InlineKeyboardButton]] = []
    if volumes:
        text = f"📚 Serie: {serie['name']}. ¿En qué volumen? ({n_bubbles} burbuja(s))"
        for v in volumes:
            cc = v.get("card_count", 0)
            label = f"📘 {v['name']} ({cc} cards)"
            rows.append([
                InlineKeyboardButton(label, callback_data=f"mvol:{v['id']}"),
            ])
        rows.append([
            InlineKeyboardButton("➕ Crear nuevo volumen",
                                 callback_data=f"mnewvol:{serie['id']}"),
        ])
    else:
        text = (
            f"📚 Serie: {serie['name']} (sin volúmenes todavía). "
            f"Crea el primero para guardar las {n_bubbles} burbuja(s):"
        )
        rows.append([
            InlineKeyboardButton("➕ Crear nuevo volumen",
                                 callback_data=f"mnewvol:{serie['id']}"),
        ])

    rows.append([
        InlineKeyboardButton("⬅️ Atrás", callback_data="mback:dest"),
        InlineKeyboardButton("❌ Cancelar", callback_data="mcancel"),
    ])
    return text, InlineKeyboardMarkup(rows)


# ==============================================================================
# PRACTICE KEYBOARDS
# ==============================================================================
def kbd_practice_front(card_id: int) -> InlineKeyboardMarkup:
    """Inline kb for the front of a manga flashcard (Ver respuesta / Salir)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👀 Ver respuesta", callback_data=f"mgflip:{card_id}")],
        [InlineKeyboardButton("❌ Salir", callback_data="mgquit")],
    ])


def kbd_practice_back(card_id: int) -> InlineKeyboardMarkup:
    """Inline kb for the back of a manga flashcard (Again / Good / Easy / Salir)."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔁 Again", callback_data=f"mgsave:{card_id}:again"),
            InlineKeyboardButton("✅ Good", callback_data=f"mgsave:{card_id}:good"),
            InlineKeyboardButton("⭐ Easy", callback_data=f"mgsave:{card_id}:easy"),
        ],
        [InlineKeyboardButton("❌ Salir", callback_data="mgquit")],
    ])


def kbd_practice_done() -> InlineKeyboardMarkup:
    """Inline kb shown at the end of a manga practice session."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")],
    ])


__all__ = [
    "text_no_pending",
    "kbd_done",
    "kbd_destination",
    "kbd_volume",
    "kbd_practice_front",
    "kbd_practice_back",
    "kbd_practice_done",
]

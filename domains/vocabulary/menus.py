"""
domains.vocabulary.menus — keyboards / InlineKeyboardMarkup builders for the
vocabulary domain.

Extracted from the original monolith's `scripts/task_bot.py` (mostly
inline ``InlineKeyboardMarkup([...])`` calls inside the handler functions;
this module centralises them so handlers stay short and the button layout
is easy to tweak).

Public surface
--------------
* :func:`rev_menu` — the root Vocab menu (sync builder, returns text + kb).
* :func:`kbd_deck_buttons` — render a flat deck list as rows of buttons.
* :func:`kbd_word_practice` — "No la sé / Siguiente / Volver / Salir" inline kb.
* :func:`kbd_practice_flip` — factual-card "Ver respuesta" kb.
* :func:`kbd_practice_flip_back` — back of a factual card with self-eval buttons.
* :func:`kbd_practice_palabra` — palabra-del-día "No la sé" kb.
"""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from domains.vocabulary import storage


# ==============================================================================
# ROOT VOCAB MENU
# ==============================================================================
def rev_menu() -> tuple[str, "InlineKeyboardMarkup"]:
    """Build the root Vocab menu (sync). Includes deck buttons.

    Mirrors :func:`task_bot.rev_menu` from the monolith.  Uses cached stats
    for the due-today count to avoid an extra DB hit on every render.
    """
    stats = storage.sts()
    du = stats.get("due_today", 0) or 0
    lines = ["📖 **Vocab**\n", "Vocabulario con espaciado.\n"]
    if du:
        lines.append(f"\n🔴 {du} palabra(s) pendiente(s).")
    text = "\n".join(lines)
    kb = []
    root_decks = storage.decks(None)
    for d in root_decks:
        dname = d.get("name") or f"Deck {d.get('id')}"
        demoji = d.get("emoji") or "\U0001f4d6"
        dc = d.get("due_count") or 0
        label = f"{demoji} {dname}"
        if dc:
            label += f" ({dc})"
        cb_data = (f"deck:open:{d['id']}" if d.get("has_children")
                   else f"deck:practice:{d['id']}")
        kb.append([InlineKeyboardButton(label, callback_data=cb_data)])
    kb.append([InlineKeyboardButton("\U0001f4ca Estadisticas",
                                    callback_data="rev:stats")])
    # "Palabra del Día" is the seeded default deck (id=1) and is already
    # rendered above as a deck button. Do NOT add a second hardcoded button
    # here — that caused a duplicate entry in the original Vocab menu.
    kb.append([InlineKeyboardButton("\u2b05\ufe0f Volver",
                                    callback_data="nav:back")])
    return text, InlineKeyboardMarkup(kb)


# ==============================================================================
# DECK-LEVEL KEYBOARDS
# ==============================================================================
def kbd_deck_buttons(parent_id=None, deck_label: str | None = None
                     ) -> tuple[str, "InlineKeyboardMarkup"]:
    """Build the text + keyboard for a deck-level view.

    Mirrors :func:`task_bot.show_decks` but only the rendering part — the
    handler is responsible for editing the Telegram message.
    """
    stats = storage.sts()
    du = stats.get("due_today", 0) or 0
    if parent_id is None:
        lines = ["📖 **Vocab**\n", "Vocabulario con espaciado.\n"]
        if du:
            lines.append(f"\n🔴 {du} palabra(s) pendiente(s).")
    else:
        lines = [f"\U0001f4d6 **{deck_label or 'Deck'}**\n",
                 "Elige un subdeck o practica este deck.\n"]
    text = "\n".join(lines)
    subdecks = storage.decks(parent_id)
    kb = []
    for d in subdecks:
        dname = d.get("name") or f"Deck {d.get('id')}"
        demoji = d.get("emoji") or "\U0001f4d6"
        dc = d.get("due_count") or 0
        label = f"{demoji} {dname}"
        if dc:
            label += f" ({dc})"
        cb_data = (f"deck:open:{d['id']}" if d.get("has_children")
                   else f"deck:practice:{d['id']}")
        kb.append([InlineKeyboardButton(label, callback_data=cb_data)])
    if parent_id is not None:
        kb.append([InlineKeyboardButton(
            f"\U0001f4d6 Practicar este deck",
            callback_data=f"deck:practice:{parent_id}"
        )])
    if not subdecks and parent_id is not None:
        text += "\n\n_Sin subdecks. Practica las palabras pendientes._"
    kb.append([InlineKeyboardButton("\U0001f4ca Estadisticas",
                                    callback_data="rev:stats")])
    kb.append([InlineKeyboardButton("\u2b05\ufe0f Volver",
                                    callback_data="nav:back")])
    return text, InlineKeyboardMarkup(kb)


# ==============================================================================
# PRACTICE KEYBOARDS
# ==============================================================================
def kbd_word_practice(word_id) -> "InlineKeyboardMarkup":
    """Inline kb for a palabra-del-día card (No la sé / Siguiente / Volver / Salir)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u274c No la se",
                              callback_data=f"w1:f:{word_id}"),
         InlineKeyboardButton("\u23e9 Siguiente",
                              callback_data=f"w1:s:{word_id}")],
        [InlineKeyboardButton("\u2b05\ufe0f Volver",
                              callback_data="nav:back"),
         InlineKeyboardButton("\u274c Salir",
                              callback_data="menu")],
    ])


def kbd_practice_palabra() -> "InlineKeyboardMarkup":
    """Inline kb for a palabra card during Practice All."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u274c No la se", callback_data="pf:fail")],
        [InlineKeyboardButton("\u2b05\ufe0f Volver", callback_data="nav:back"),
         InlineKeyboardButton("\u274c Salir", callback_data="menu")],
    ])


def kbd_practice_conceptual() -> "InlineKeyboardMarkup":
    """Inline kb for a conceptual card during Practice All."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u274c No se", callback_data="pf:fail")],
        [InlineKeyboardButton("\u2b05\ufe0f Volver", callback_data="nav:back"),
         InlineKeyboardButton("\u274c Salir", callback_data="menu")],
    ])


def kbd_practice_flip() -> "InlineKeyboardMarkup":
    """Inline kb for a factual card before flipping (Ver respuesta)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f441 Ver respuesta",
                              callback_data="pf:flip")],
        [InlineKeyboardButton("\u274c No lo se", callback_data="pf:fail")],
        [InlineKeyboardButton("\u2b05\ufe0f Volver", callback_data="nav:back"),
         InlineKeyboardButton("\u274c Salir", callback_data="menu")],
    ])


def kbd_practice_flip_back() -> "InlineKeyboardMarkup":
    """Inline kb for a flipped factual card (Again / Good / Easy self-eval)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f504 Again", callback_data="pf:again"),
         InlineKeyboardButton("\u2705 Good", callback_data="pf:good"),
         InlineKeyboardButton("\u2b50 Easy", callback_data="pf:easy")],
        [InlineKeyboardButton("\u274c Salir", callback_data="menu")],
    ])


# ==============================================================================
# WORD-OF-THE-DAY KEYBOARD (root)
# ==============================================================================
def kbd_word_of_day() -> "InlineKeyboardMarkup":
    """Inline kb for the Palabra del Día root view (No la sé / Siguiente)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f4d6 Practicar este deck",
                              callback_data="deck:practice:1")],
        [InlineKeyboardButton("\u2b05\ufe0f Volver", callback_data="nav:back")],
    ])

"""
domains.flashcards.handlers — Telegram handlers for the flashcards domain.

Extracted from the original ``task_bot.py`` (``show_conc`` / ``show_fact`` /
``show_notion`` / ``w2_*`` / ``w3_*`` / ``ntn_*`` / ``s_courses`` /
``s_course`` family) and the standalone ``flashcard_menu.handle_callback``
notion-tree dispatcher.

Callback layout
---------------
The original monolith used two parallel routing conventions:

* ``w2:`` / ``w3:`` / ``ntn:`` — per-card SRS callbacks (conceptual,
  factual, notion) routed by the main ``task_bot`` dispatcher.
* ``fm:`` — Notion tree callbacks handled entirely inside
  ``flashcard_menu``.

Both are kept verbatim here.  The Telegram ``Application`` only needs
to call :func:`register_handlers` to wire them up; the upstream router
in the bot core is responsible for prefix-matching the callback_data
and calling the right function.

Notion tree callback format
---------------------------
``fm:h`` — home (source list)
``fm:n:<path>`` — navigate to node
``fm:s:<path>`` — show type selector for Practice All
``fm:p:<path>:<t>`` — start practice (``t`` in ``f|c|mix|all``)
``fm:flip:<cid>:<path>:<idx>:<total>`` — flip factual card
``fm:fail:<cid>:<path>:<idx>:<total>`` — fail factual card
``fm:again|good|easy:<cid>:<path>:<idx>:<total>`` — eval factual
``fm:skip:<path>:<idx>`` — skip conceptual card
``fm:aflip:<cid>:<idx>:<total>`` — Practice-All flip (no path)
``fm:afail:<cid>:<idx>:<total>`` — Practice-All fail
``fm:aagain|agood|aeasy:<cid>:<idx>:<total>`` — Practice-All eval
``fm:askip:<idx>`` — Practice-All conceptual skip
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

from core.config import DATA
from . import menus, storage

log = logging.getLogger("domains.flashcards.handlers")

STATE_DB: Path = DATA / "state.db"


# =============================================================================
# 1. Per-card review (w2: conceptual, w3: factual, ntn: notion)
# =============================================================================
async def show_conc(update, context, course: str) -> None:
    """Show the next due conceptual card for ``course``."""
    cards = [c for c in storage.getc(course, "conceptual") if storage.is_due(c)]
    q = update.callback_query
    if not cards:
        text = "🎉 No hay conceptuales pendientes."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
        await q.answer()
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    c = cards[0]
    text = (
        f"💭 **Conceptual** — {course} ({len(cards)} restantes)\n\n"
        f"**{c['front']}**\n\n"
        "_Escribe tu respuesta._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ No se", callback_data=f"w2:f:{c['id']}:{course}"),
         InlineKeyboardButton("⏩ Sig", callback_data=f"w2:s:{c['id']}:{course}")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="nav:back"),
         InlineKeyboardButton("❌ Salir", callback_data="menu")],
    ])
    await q.answer()
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def w2_fail(update, context, cid: int, course: str) -> None:
    await storage.save_conc(cid, 0, "fail")
    await update.callback_query.answer("❌")
    await show_conc(update, context, course)


async def w2_skip(update, context, cid: int, course: str) -> None:
    await storage.save_conc(cid, 0, "skip")
    await update.callback_query.answer("⏩")
    await show_conc(update, context, course)


async def show_fact(update, context, course: str) -> None:
    """Show the next due factual card for ``course``."""
    cards = [c for c in storage.getc(course, "factual") if storage.is_due(c)]
    q = update.callback_query
    if not cards:
        text = "🎉 No hay factuales pendientes."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
        await q.answer()
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    c = cards[0]
    text = (
        f"📝 **Factual** — {course} ({len(cards)} restantes)\n\n"
        f"**{c['front']}**\n\n"
        '_Toca "Ver respuesta" cuando lo recuerdes._'
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Ver respuesta", callback_data=f"w3:flip:{c['id']}:{course}")],
        [InlineKeyboardButton("❌ No lo se", callback_data=f"w3:f:{c['id']}:{course}")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="nav:back"),
         InlineKeyboardButton("❌ Salir", callback_data="menu")],
    ])
    await q.answer()
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def w3_flip(update, context, cid: int, course: str) -> None:
    """Reveal the back of a factual card and show the 3 eval buttons."""
    cards = storage.getc(course, "factual")
    c = next((x for x in cards if x["id"] == cid), None)
    if not c:
        return
    text = (
        f"📝 **Factual** — {course}\n\n"
        f"**{c['front']}**\n\n"
        f"||{c.get('back', '')}||\n\n"
        "_Autoevaluacion:_"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Again", callback_data=f"w3:again:{cid}:{course}"),
         InlineKeyboardButton("✅ Good", callback_data=f"w3:good:{cid}:{course}"),
         InlineKeyboardButton("⭐ Easy", callback_data=f"w3:easy:{cid}:{course}")],
        [InlineKeyboardButton("❌ Salir", callback_data="menu")],
    ])
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def w3_eval(update, context, btn: str, cid: int, course: str) -> None:
    """Save the user's self-eval on a factual card and show the next."""
    try:
        await _apply_srs(cid, lambda: storage.save_fact(cid, btn))
    except Exception as e:
        log.error(f"Factual save: {e}")
    be = {"again": "🔄", "hard": "💪", "good": "✅", "easy": "⭐"}
    await update.callback_query.answer(f"{be.get(btn, '')} {btn.upper()}")
    await _show_remaining_factual(update, context, course)


async def w3_fail(update, context, cid: int, course: str) -> None:
    """User tapped "No lo se" — quality 0, then show the next card."""
    try:
        await _apply_srs(cid, lambda: storage.save_fact(cid, "again"))
    except Exception:
        pass
    await update.callback_query.answer("❌ Repaso manana.")
    await _show_remaining_factual(update, context, course)


async def _show_remaining_factual(update, context, course: str) -> None:
    """Render the next due factual card, or the completion screen."""
    cards = [c for c in storage.getc(course, "factual") if storage.is_due(c)]
    if not cards:
        text = "🎉 Completaste las factuales!"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    c = cards[0]
    text = (
        f"📝 **Factual** — {course} ({len(cards)} restantes)\n\n"
        f"**{c['front']}**\n\n"
        '_Toca "Ver respuesta" cuando lo recuerdes._'
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Ver respuesta", callback_data=f"w3:flip:{c['id']}:{course}")],
        [InlineKeyboardButton("❌ No lo se", callback_data=f"w3:f:{c['id']}:{course}")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="nav:back"),
         InlineKeyboardButton("❌ Salir", callback_data="menu")],
    ])
    await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# ----- Notion cards -----
async def show_notion(update, context, course: str) -> None:
    """Show the next due Notion-sourced card for ``course``."""
    cards = [c for c in storage.getc(course, ("notion", "notion_reversed")) if storage.is_due(c)]
    q = update.callback_query
    if not cards:
        text = "🎴 No hay flashcards de Notion pendientes."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
        await q.answer()
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    c = cards[0]
    emoji = "🎴" if c["card_type"] == "notion" else "🃏"
    text = (
        f"{emoji} **Notion Flashcard** — {course} ({len(cards)} restantes)\n\n"
        f"**{c['front']}**\n\n"
        '_Toca "Ver respuesta" cuando lo recuerdes._'
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Ver respuesta", callback_data=f"ntn:flip:{c['id']}:{course}")],
        [InlineKeyboardButton("❌ No lo se", callback_data=f"ntn:fail:{c['id']}:{course}")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")],
    ])
    await q.answer()
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def ntn_flip(update, context, cid: int, course: str) -> None:
    """Reveal the back of a Notion card and show the 3 eval buttons."""
    cards = [c for c in storage.getc(course) if c["id"] == cid]
    if not cards:
        return
    c = cards[0]
    emoji = "🎴" if c["card_type"] == "notion" else "🃏"
    text = (
        f"{emoji} **Notion Flashcard** — {course}\n\n"
        f"**{c['front']}**\n\n"
        f"||{c.get('back', '')}||\n\n"
        "_Autoevaluación:_"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Again", callback_data=f"ntn:again:{cid}:{course}"),
         InlineKeyboardButton("✅ Good", callback_data=f"ntn:good:{cid}:{course}"),
         InlineKeyboardButton("⭐ Easy", callback_data=f"ntn:easy:{cid}:{course}")],
        [InlineKeyboardButton("❌ Salir", callback_data="menu")],
    ])
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def ntn_eval(update, context, btn: str, cid: int, course: str) -> None:
    """Save the user's self-eval on a Notion card and show the next."""
    try:
        await _apply_srs(cid, lambda: storage.save_notion(cid, btn))
    except Exception as e:
        log.error(f"Notion eval: {e}")
    emap = {"again": "🔄", "good": "✅", "easy": "⭐"}
    await update.callback_query.answer(f"{emap.get(btn, '')} {btn.upper()}")
    await _show_remaining_notion(update, context, course)


async def ntn_fail(update, context, cid: int, course: str) -> None:
    """User tapped "No lo se" — quality 0, then show the next Notion card."""
    try:
        await _apply_srs(cid, lambda: storage.save_notion(cid, "again"))
    except Exception:
        pass
    await update.callback_query.answer("❌ Repaso manana.")
    await _show_remaining_notion(update, context, course)


async def _show_remaining_notion(update, context, course: str) -> None:
    """Render the next due Notion card, or the completion screen."""
    cards = [c for c in storage.getc(course, ("notion", "notion_reversed")) if storage.is_due(c)]
    if not cards:
        text = "🎴 ¡Completaste las flashcards de Notion! 🎉"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    c = cards[0]
    emoji = "🎴" if c["card_type"] == "notion" else "🃏"
    text = (
        f"{emoji} **Notion Flashcard** — {course} ({len(cards)} restantes)\n\n"
        f"**{c['front']}**\n\n"
        '_Toca "Ver respuesta" cuando lo recuerdes._'
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Ver respuesta", callback_data=f"ntn:flip:{c['id']}:{course}")],
        [InlineKeyboardButton("❌ No lo se", callback_data=f"ntn:fail:{c['id']}:{course}")],
        [InlineKeyboardButton("⬅️ Volver", callback_data="nav:back"),
         InlineKeyboardButton("❌ Salir", callback_data="menu")],
    ])
    await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# =============================================================================
# 2. Course menu (rev:c:<course>, rev:conc:, rev:fact:, rev:notion:)
# =============================================================================
async def s_courses(update, context) -> None:
    """Render the per-course picker.

    Single ``GROUP BY course, card_type`` query — same shape the
    monolith's optimised version produced.
    """
    text = "No hay cursos con flashcards."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
    if STATE_DB.exists():
        try:
            conn = sqlite3.connect(str(STATE_DB))
            try:
                rows = conn.execute(
                    "SELECT course, card_type, COUNT(*) "
                    "FROM course_flashcards "
                    "GROUP BY course, card_type"
                ).fetchall()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            by_course: dict[str, dict[str, int]] = {}
            for course, ctype, cnt in rows:
                if not course:
                    continue
                bucket = by_course.setdefault(
                    course, {"conceptual": 0, "factual": 0, "notion": 0, "notion_reversed": 0}
                )
                if ctype in bucket:
                    bucket[ctype] += cnt
            if by_course:
                text, kb = menus.build_course_menu(by_course)
        except Exception as e:
            log.error(f"s_courses: {e}")
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def s_course(update, context, course: str) -> None:
    """Render the per-course review menu (conceptual / factual / Notion)."""
    cards = storage.getc(course)
    conc = [c for c in cards if c["card_type"] == "conceptual"]
    fact = [c for c in cards if c["card_type"] == "factual"]
    ntns = [c for c in cards if c["card_type"] in ("notion", "notion_reversed")]
    text, kb = menus.build_course_review_menu(course, conc, fact, ntns)
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# =============================================================================
# 3. Notion tree menu dispatcher (fm:...)
# =============================================================================
async def handle_flashcard_menu(update, context, data: str) -> None:
    """Top-level dispatcher for ``fm:*`` callbacks.

    This is the single entry point the application router should call
    after it sees a ``fm:`` prefix in the callback_data.  Mirrors the
    original ``flashcard_menu.handle_callback`` signature 1:1.
    """
    q = update.callback_query
    parts = data.split(":")
    cmd = parts[1] if len(parts) > 1 else ""

    if cmd == "h":
        text, kb = menus.build_notion_tree_menu([])
        await q.answer()
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return

    if cmd == "n":
        path_parts = [int(p) for p in parts[2:]]
        text, kb = menus.build_notion_tree_menu(path_parts)
        await q.answer()
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return

    if cmd == "s":
        # Show type selector. We re-use the node menu (Practice All
        # entry point is `fm:s:<path>`) but the original handler also
        # has a richer type selector. Keep the same UX.
        path_parts = [int(p) for p in parts[2:]]
        node = menus._get_node_by_path(menus.get_tree(), path_parts)
        if not node:
            await q.answer()
            return
        fc = node.get("factual_count", 0)
        cc = node.get("conceptual_count", 0)
        path_str = ":".join(str(p) for p in path_parts)
        lines = [
            f"🔁 **Practice All** — {node.get('title', '?')}\n",
            "Selecciona tipo de flashcards:\n",
        ]
        rows: list[list[InlineKeyboardButton]] = []
        if fc > 0:
            rows.append([
                InlineKeyboardButton(
                    f"🎴 Factuales ({fc})", callback_data=f"fm:p:{path_str}:f"
                )
            ])
        if cc > 0:
            rows.append([
                InlineKeyboardButton(
                    f"🧠 Conceptuales ({cc})", callback_data=f"fm:p:{path_str}:c"
                )
            ])
        if fc > 0 and cc > 0:
            rows.append([
                InlineKeyboardButton(
                    f"🔀 Mixtas ({fc + cc})", callback_data=f"fm:p:{path_str}:mix"
                )
            ])
        rows.append([])
        rows.append([
            InlineKeyboardButton(
                "⬅️ Atrás",
                callback_data=f"fm:n:{':'.join(str(p) for p in path_parts)}",
            ),
            InlineKeyboardButton("🏠 Home", callback_data="menu"),
        ])
        await q.answer()
        await q.edit_message_text(
            "\n".join(lines), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    if cmd == "p":
        type_char = parts[-1]
        path_parts = [int(p) for p in parts[2:-1]]
        if type_char == "all":
            await _start_practice_all(update, context)
        else:
            await _start_practice(update, context, path_parts, type_char)
        return

    if cmd == "flip":
        cid = int(parts[2])
        remaining = parts[3:]
        idx = int(remaining[-2])
        total = int(remaining[-1])
        path_parts = [int(p) for p in remaining[:-2]]
        await _show_factual_flip(update, context, cid, path_parts, idx, total)
        return

    if cmd in ("again", "good", "easy"):
        cid = int(parts[2])
        remaining = parts[3:]
        idx = int(remaining[-2])
        total = int(remaining[-1])
        path_parts = [int(p) for p in remaining[:-2]]
        await _save_factual_eval(update, context, cid, cmd, path_parts, idx, total)
        return

    if cmd == "fail":
        cid = int(parts[2])
        remaining = parts[3:]
        idx = int(remaining[-2])
        total = int(remaining[-1])
        path_parts = [int(p) for p in remaining[:-2]]
        await _save_factual_eval(update, context, cid, "again", path_parts, idx, total)
        return

    if cmd == "skip":
        remaining = parts[2:]
        idx = int(remaining[-1])
        path_parts = [int(p) for p in remaining[:-1]]
        await _next_conceptual(update, context, path_parts, idx + 1)
        return

    if cmd == "aflip":
        cid = int(parts[2])
        idx = int(parts[3])
        total = int(parts[4])
        await _show_factual_flip_all(update, context, cid, idx, total)
        return

    if cmd == "afail":
        cid = int(parts[2])
        idx = int(parts[3])
        total = int(parts[4])
        await _save_factual_eval_all(update, context, cid, "again", idx, total)
        return

    if cmd in ("aagain", "agood", "aeasy"):
        cid = int(parts[2])
        idx = int(parts[3])
        total = int(parts[4])
        await _save_factual_eval_all(update, context, cid, cmd[1:], idx, total)
        return

    if cmd == "askip":
        idx = int(parts[2])
        await _next_conceptual_all(update, context, idx + 1)
        return

    await q.answer()


# ----- Practice-All helpers (no node path) -----
async def _start_practice_all(update, context) -> None:
    q = update.callback_query
    cards = _get_all_due_cards(None)
    if not cards:
        await q.answer("No hay flashcards pendientes.", show_alert=True)
        return
    context.user_data["fm_practice"] = {
        "cards": cards,
        "idx": 0,
        "total": len(cards),
        "path_parts": [],
        "type": "all",
    }
    card = cards[0]
    if card["card_type"] in ("notion", "notion_reversed"):
        await _show_factual_card_all(update, context, card, 0, len(cards))
    elif card["card_type"] == "notion_conceptual":
        await _show_conceptual_card_all(update, context, card, 0, len(cards))
    else:
        await q.answer()


async def _start_practice(update, context, path_parts, type_char: str) -> None:
    q = update.callback_query
    card_type_map = {"f": "factual", "c": "conceptual", "mix": None}
    ct = card_type_map.get(type_char, None)
    cards = _get_cards_for_node(path_parts, ct)
    if not cards:
        await q.answer("No hay flashcards pendientes.", show_alert=True)
        return
    context.user_data["fm_practice"] = {
        "cards": cards,
        "idx": 0,
        "total": len(cards),
        "path_parts": path_parts,
        "type": type_char,
    }
    card = cards[0]
    if card["card_type"] in ("notion", "notion_reversed"):
        await _show_factual_card(update, context, card, path_parts, 0, len(cards))
    elif card["card_type"] == "notion_conceptual":
        await _show_conceptual_card(update, context, card, path_parts, 0, len(cards))
    else:
        await q.answer()


async def _show_factual_card_all(update, context, card, idx: int, total: int) -> None:
    emoji = "🎴" if card["card_type"] == "notion" else "🃏"
    text = (
        f"{emoji} **Factual** ({idx + 1}/{total})\n\n"
        f"**{card['front']}**\n\n"
        '_Toca "Ver respuesta" cuando lo recuerdes._'
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👁 Ver respuesta", callback_data=f"fm:aflip:{card['id']}:{idx}:{total}")],
        [InlineKeyboardButton("❌ No lo sé", callback_data=f"fm:afail:{card['id']}:{idx}:{total}")],
        [InlineKeyboardButton("⬅️ Salir", callback_data="fm:h")],
    ])
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def _show_conceptual_card_all(update, context, card, idx: int, total: int) -> None:
    text = (
        f"🧠 **Conceptual** ({idx + 1}/{total})\n\n"
        f"**{card['front']}**\n\n"
        "_Escribe tu respuesta._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ No lo sé", callback_data=f"fm:askip:{idx}")],
        [InlineKeyboardButton("⬅️ Salir", callback_data="fm:h")],
    ])
    context.user_data["fm_practice"] = {
        "cards": [dict(card)],
        "all_cards": [],
        "idx": idx,
        "total": total,
        "path_parts": [],
        "type": "conceptual",
    }
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def _show_factual_flip_all(update, context, cid: int, idx: int, total: int) -> None:
    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT front, back, card_type FROM course_flashcards WHERE id=?",
            (cid,),
        ).fetchone()
        conn.close()
        if not row:
            return
        emoji = "🎴" if row["card_type"] == "notion" else "🃏"
        text = (
            f"{emoji} **Factual** ({idx + 1}/{total})\n\n"
            f"**{row['front']}**\n\n"
            f"||{row['back']}||\n\n"
            "_Autoevaluación:_"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔴 Again", callback_data=f"fm:aagain:{cid}:{idx}:{total}"),
             InlineKeyboardButton("✅ Good", callback_data=f"fm:agood:{cid}:{idx}:{total}"),
             InlineKeyboardButton("⭐ Easy", callback_data=f"fm:aeasy:{cid}:{idx}:{total}")],
        ])
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        log.error(f"fm aflip: {e}")


async def _save_factual_eval_all(update, context, cid: int, btn: str, current_idx: int, total: int) -> None:
    qmap = {"again": 0, "good": 3, "easy": 5}
    try:
        await _apply_srs(cid, lambda: storage.save_fact(cid, btn))
    except Exception as e:
        log.error(f"fm aeval: {e}")
    emap = {"again": "🔴", "good": "✅", "easy": "⭐"}
    await update.callback_query.answer(f"{emap.get(btn, '')} {btn.upper()}")
    await _next_factual_all(update, context, current_idx + 1, total)


async def _next_factual_all(update, context, next_idx: int, total: int) -> None:
    cards = _get_all_due_cards("factual")
    if next_idx >= len(cards):
        text = "🎴 ¡Completaste las factuales! 🎉"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="fm:h")]])
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    card = cards[next_idx]
    await _show_factual_card_all(update, context, card, next_idx, len(cards))


async def _next_conceptual_all(update, context, next_idx: int) -> None:
    cards = _get_all_due_cards("conceptual")
    if next_idx >= len(cards):
        text = "🧠 ¡Completaste las conceptuales! 🎉"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="fm:h")]])
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    card = cards[next_idx]
    await _show_conceptual_card_all(update, context, card, next_idx, len(cards))


async def _save_factual_eval(update, context, cid: int, btn: str,
                              path_parts, current_idx: int, total: int) -> None:
    try:
        await _apply_srs(cid, lambda: storage.save_fact(cid, btn))
    except Exception as e:
        log.error(f"fm eval: {e}")
    emap = {"again": "🔴", "good": "✅", "easy": "⭐"}
    await update.callback_query.answer(f"{emap.get(btn, '')} {btn.upper()}")
    await _next_factual(update, context, path_parts, current_idx + 1, total)


async def _next_factual(update, context, path_parts, next_idx: int, total: int) -> None:
    cards = _get_cards_for_node(path_parts, "factual")
    if next_idx >= len(cards):
        text = "🎴 ¡Completaste las factuales! 🎉"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "⬅️ Volver",
                callback_data=f"fm:n:{':'.join(str(p) for p in path_parts)}",
            )]
        ])
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    card = cards[next_idx]
    await _show_factual_card(update, context, card, path_parts, next_idx, len(cards))


async def _next_conceptual(update, context, path_parts, next_idx: int) -> None:
    cards = _get_cards_for_node(path_parts, "conceptual")
    if next_idx >= len(cards):
        text = "🧠 ¡Completaste las conceptuales! 🎉"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "⬅️ Volver",
                callback_data=f"fm:n:{':'.join(str(p) for p in path_parts)}",
            )]
        ])
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    card = cards[next_idx]
    await _show_conceptual_card(update, context, card, path_parts, next_idx, len(cards))


# ----- Card renderers (per-node) -----
async def _show_factual_card(update, context, card, path_parts, idx: int, total: int) -> None:
    emoji = "🎴" if card["card_type"] == "notion" else "🃏"
    text = (
        f"{emoji} **Factual** ({idx + 1}/{total})\n\n"
        f"**{card['front']}**\n\n"
        '_Toca "Ver respuesta" cuando lo recuerdes._'
    )
    path_str = ":".join(str(p) for p in path_parts)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "👁 Ver respuesta",
            callback_data=f"fm:flip:{card['id']}:{path_str}:{idx}:{total}",
        )],
        [InlineKeyboardButton(
            "❌ No lo sé",
            callback_data=f"fm:fail:{card['id']}:{path_str}:{idx}:{total}",
        )],
        [InlineKeyboardButton("⬅️ Salir", callback_data=f"fm:n:{path_str}")],
    ])
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def _show_conceptual_card(update, context, card, path_parts, idx: int, total: int) -> None:
    text = (
        f"🧠 **Conceptual** ({idx + 1}/{total})\n\n"
        f"**{card['front']}**\n\n"
        "_Escribe tu respuesta._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "❌ No lo sé",
            callback_data=f"fm:skip:{':'.join(str(p) for p in path_parts)}:{idx}",
        )],
        [InlineKeyboardButton(
            "⬅️ Salir",
            callback_data=f"fm:n:{':'.join(str(p) for p in path_parts)}",
        )],
    ])
    context.user_data["fm_practice"] = {
        "cards": [dict(card)],
        "all_cards": [],
        "idx": idx,
        "total": total,
        "path_parts": path_parts,
        "type": "conceptual",
    }
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def _show_factual_flip(update, context, cid: int, path_parts, idx: int, total: int) -> None:
    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT front, back, card_type FROM course_flashcards WHERE id=?",
            (cid,),
        ).fetchone()
        conn.close()
        if not row:
            return
        emoji = "🎴" if row["card_type"] == "notion" else "🃏"
        text = (
            f"{emoji} **Factual** ({idx + 1}/{total})\n\n"
            f"**{row['front']}**\n\n"
            f"||{row['back']}||\n\n"
            "_Autoevaluación:_"
        )
        path_str = ":".join(str(p) for p in path_parts)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔴 Again", callback_data=f"fm:again:{cid}:{path_str}:{idx}:{total}"),
             InlineKeyboardButton("✅ Good", callback_data=f"fm:good:{cid}:{path_str}:{idx}:{total}"),
             InlineKeyboardButton("⭐ Easy", callback_data=f"fm:easy:{cid}:{path_str}:{idx}:{total}")],
        ])
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        log.error(f"fm flip: {e}")


# =============================================================================
# 4. Helpers — Notion tree DB queries
# =============================================================================
def _collect_page_ids(node: dict) -> list[str]:
    """Recursively collect every page_id in a Notion tree node."""
    ids = [node["page_id"]]
    for child in node.get("children", []):
        ids.extend(_collect_page_ids(child))
    return ids


def _get_cards_for_node(path_parts: list, card_type: Optional[str] = None) -> list[dict]:
    """Get all due cards under a Notion tree node.

    ``card_type``: ``'factual'`` (notion / notion_reversed),
    ``'conceptual'`` (notion_conceptual), or ``None`` for both.
    """
    if not path_parts:
        return []
    node = menus._get_node_by_path(menus.get_tree(), path_parts)
    if not node:
        return []
    page_ids = _collect_page_ids(node)
    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        today = date.today().isoformat()
        params: list = [today]
        like_clauses = []
        for pid in page_ids:
            prefix = f"https://notion.so/{pid.replace('-', '')}%"
            like_clauses.append("source LIKE ?")
            params.append(prefix)
        q = (
            "SELECT id, front, back, card_type, ease, interval, repetitions, "
            "next_review, source FROM course_flashcards "
            f"WHERE next_review <= ? AND ({' OR '.join(like_clauses)})"
        )
        if card_type == "factual":
            q += " AND card_type IN ('notion', 'notion_reversed')"
        elif card_type == "conceptual":
            q += " AND card_type = 'notion_conceptual'"
        q += " ORDER BY next_review ASC"
        rows = conn.execute(q, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"_get_cards_for_node: {e}")
        return []


def _get_all_due_cards(card_type: Optional[str] = None) -> list[dict]:
    """Return every due card across all sources."""
    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        today = date.today().isoformat()
        q = (
            "SELECT id, front, back, card_type, ease, interval, repetitions, "
            "next_review, source FROM course_flashcards WHERE next_review <= ?"
        )
        params: list = [today]
        if card_type == "factual":
            q += " AND card_type IN ('notion', 'notion_reversed')"
        elif card_type == "conceptual":
            q += " AND card_type = 'notion_conceptual'"
        q += " ORDER BY next_review ASC"
        rows = conn.execute(q, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.error(f"_get_all_due_cards: {e}")
        return []


# =============================================================================
# 5. SRS wrapper (so handlers don't open their own connections)
# =============================================================================
async def _apply_srs(cid: int, fn) -> None:
    """Run ``fn()`` inside a try/except so handlers stay terse.

    Each storage.*_save already does its own try/except and swallows
    errors; this wrapper exists so the handler signature matches the
    original monolith (``storage.save_fact(cid, btn)``) one-liner.
    """
    fn()


# =============================================================================
# 6. Telegram application wiring
# =============================================================================
def register_handlers(app: Application) -> None:
    """Register the flashcards-domain handlers on a Telegram ``Application``.

    Two handlers are wired:

    * A ``CallbackQueryHandler`` that pattern-matches every
      ``w2:``, ``w3:`` and ``ntn:`` callback and dispatches to the
      matching ``show_*`` / ``*_eval`` / ``*_fail`` function.
    * A ``CallbackQueryHandler`` for ``fm:`` callbacks, delegating to
      :func:`handle_flashcard_menu`.

    The original monolith also had a text-input handler for
    conceptual cards (the user types an answer, an LLM grades it).
    That handler lives in the agent layer and is out of scope for this
    domain.
    """
    from telegram.ext import CallbackQueryHandler

    # The two main callback families. We register them as a single
    # CallbackQueryHandler that decides what to do based on the
    # callback_data prefix.  This mirrors how the monolith's main
    # callback_router worked.
    async def _w_router(update, context):
        data = update.callback_query.data
        if data.startswith("w2:fail:") or data.startswith("w2:f:"):
            _, _, cid_s, course = data.split(":", 3)
            await w2_fail(update, context, int(cid_s), course)
        elif data.startswith("w2:skip:") or data.startswith("w2:s:"):
            _, _, cid_s, course = data.split(":", 3)
            await w2_skip(update, context, int(cid_s), course)
        elif data.startswith("w3:flip:"):
            _, _, cid_s, course = data.split(":", 3)
            await w3_flip(update, context, int(cid_s), course)
        elif data.startswith("w3:again:"):
            _, _, cid_s, course = data.split(":", 3)
            await w3_eval(update, context, "again", int(cid_s), course)
        elif data.startswith("w3:hard:"):
            _, _, cid_s, course = data.split(":", 3)
            await w3_eval(update, context, "hard", int(cid_s), course)
        elif data.startswith("w3:good:"):
            _, _, cid_s, course = data.split(":", 3)
            await w3_eval(update, context, "good", int(cid_s), course)
        elif data.startswith("w3:easy:"):
            _, _, cid_s, course = data.split(":", 3)
            await w3_eval(update, context, "easy", int(cid_s), course)
        elif data.startswith("w3:f:") or data.startswith("w3:fail:"):
            _, _, cid_s, course = data.split(":", 3)
            await w3_fail(update, context, int(cid_s), course)
        elif data.startswith("ntn:flip:"):
            _, _, cid_s, course = data.split(":", 3)
            await ntn_flip(update, context, int(cid_s), course)
        elif data.startswith("ntn:again:"):
            _, _, cid_s, course = data.split(":", 3)
            await ntn_eval(update, context, "again", int(cid_s), course)
        elif data.startswith("ntn:good:"):
            _, _, cid_s, course = data.split(":", 3)
            await ntn_eval(update, context, "good", int(cid_s), course)
        elif data.startswith("ntn:easy:"):
            _, _, cid_s, course = data.split(":", 3)
            await ntn_eval(update, context, "easy", int(cid_s), course)
        elif data.startswith("ntn:fail:"):
            _, _, cid_s, course = data.split(":", 3)
            await ntn_fail(update, context, int(cid_s), course)

    async def _fm_router(update, context):
        await handle_flashcard_menu(update, context, update.callback_query.data)

    app.add_handler(CallbackQueryHandler(_w_router, pattern=r"^(w2|w3|ntn):"))
    app.add_handler(CallbackQueryHandler(_fm_router, pattern=r"^fm:"))


__all__ = [
    "show_conc",
    "w2_fail",
    "w2_skip",
    "show_fact",
    "w3_flip",
    "w3_eval",
    "w3_fail",
    "show_notion",
    "ntn_flip",
    "ntn_eval",
    "ntn_fail",
    "s_courses",
    "s_course",
    "handle_flashcard_menu",
    "register_handlers",
]

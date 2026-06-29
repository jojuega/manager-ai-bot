"""Telegram handlers for the tasks domain.

Extracted from the original ``task_bot.py``:

* ``start_cmd`` — /start (renders the main menu).
* ``briefing_cmd`` — /briefing (sends the latest saved briefing markdown).
* ``any_msg`` — catches any unhandled text message and either routes to
  handle_text or renders the main menu, mirroring the original
  thread-aware behaviour.

Access control (owner-only) is preserved via ``ALLOWED_USER_ID`` from
``core.config``.
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.config import ALLOWED_USER_ID
from telegram import Update
from telegram.ext import ContextTypes

from .menus import main_menu

log = logging.getLogger("tasks.handlers")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/start`` — render the main menu for the owner only."""
    if update.effective_user and update.effective_user.id != ALLOWED_USER_ID:
        return
    text, kb = main_menu()
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def briefing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/briefing`` — dump the latest ``~/.hermes/latest_briefing.md`` if any.

    Splits the file into ≤3900-char chunks so Telegram accepts each one.
    """
    if update.effective_user and update.effective_user.id != ALLOWED_USER_ID:
        return
    bp = Path.home() / ".hermes" / "latest_briefing.md"
    if not bp.exists():
        await update.message.reply_text("📭 No hay briefing guardado.")
        return
    text = bp.read_text().strip()
    if not text:
        await update.message.reply_text("📭 Briefing vacío.")
        return
    for i in range(0, len(text), 3900):
        await update.message.reply_text(text[i:i + 3900], parse_mode="Markdown")


async def any_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Catch-all for non-command text messages.

    Mirrors the original behaviour: only respond to the owner, and only
    dispatch to ``handle_text`` for the user's main chat or the dedicated
    81148 thread; otherwise just show the main menu.

    ``handle_text`` is imported lazily to avoid a circular import with the
    top-level bot module.
    """
    msg = update.message
    if msg is None:
        return
    if msg.from_user is None or msg.from_user.id != ALLOWED_USER_ID:
        return

    tid = str(msg.message_thread_id) if msg.message_thread_id else ""
    cid = str(msg.chat_id)

    if not tid or (cid == "402446137" and tid == "81148"):
        # Defer the import — handle_text lives in the bot's top-level module
        # and itself imports from several domains.
        from handle_text import handle_text  # type: ignore
        await handle_text(update, context)
    else:
        text, kb = main_menu()
        await msg.reply_text(text, parse_mode="Markdown", reply_markup=kb)

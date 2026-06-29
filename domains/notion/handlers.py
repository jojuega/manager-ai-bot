"""
domains.notion.handlers — Telegram-side handlers for the Notion domain.

Provides:

* :func:`notion_sync_cmd` — ``/sync`` command handler.  Runs an
  in-process Notion flashcard sync (no subprocess) and renders the
  result back to the chat.  Equivalent to the original
  ``task_bot.notion_sync_cmd``.
* :func:`notion_notify_job` — daily 9am ``JobQueue`` callback that
  counts pending Notion flashcards and pings the owner.  Equivalent
  to the original ``task_bot.notion_notify_job``.

Both handlers take the same arguments the original code did, so the
``main`` entry point can register them against the Telegram
``Application`` and ``JobQueue`` exactly as it used to.
"""
from __future__ import annotations

import concurrent.futures
import logging
import sqlite3
from datetime import date
from typing import TYPE_CHECKING

from core.config import ALLOWED_USER_ID, DATA
from domains.notion.sync import run_sync

if TYPE_CHECKING:  # pragma: no cover — typing only
    from telegram import Update
    from telegram.ext import ContextTypes

log = logging.getLogger("domains.notion.handlers")


async def notion_sync_cmd(update: "Update", context: "ContextTypes.DEFAULT_TYPE") -> None:
    """``/sync`` command: sync flashcards from Notion and report back.

    Mirrors the original ``task_bot.notion_sync_cmd``:

    * Silently ignores non-owners (the ``ALLOWED_USER_ID`` gate).
    * Sends a "syncing…" message immediately and edits it in place.
    * Runs :func:`domains.notion.sync.run_sync` in a worker thread so
      the 120s timeout can be enforced without blocking the asyncio
      loop.
    * On success, invalidates any in-memory SRS / Notion-tree caches
      the bot maintains (callbacks supplied via ``context.bot_data``,
      see the notes below for the expected keys).

    Cache invalidation
    ------------------
    The original handler called two private helpers from ``task_bot``:

      * ``_invalidate_srs_caches()`` — drops the SRS / deck-tree / stats
        in-memory caches.
      * ``_invalidate_notion_tree_cache()`` — drops the cached
        ``notion_tree.json`` snapshot.

    To keep this domain module standalone, we look them up on
    ``context.application`` (the ``Application`` instance) under the
    attribute names ``_invalidate_srs_caches`` and
    ``_invalidate_notion_tree_cache``.  The main entry point is
    responsible for attaching them.  If they are not present, we
    silently skip that step (matching the original "best-effort"
    behaviour — the sync itself already succeeded).
    """
    # Only respond to the owner
    if update.effective_user and update.effective_user.id != ALLOWED_USER_ID:
        return
    msg = await update.message.reply_text("\U0001f504 Sincronizando flashcards desde Notion...")
    try:
        # Run in-process: avoids ~0.7-1.5s subprocess startup per call.
        # `run_sync` is the pure-Python entry point in domains.notion.sync.
        def _do_sync() -> dict:
            return run_sync(timeout_sec=120, verbose=False)

        # Run in a thread so we can enforce a wall-clock timeout without
        # blocking the asyncio event loop (matches the old subprocess
        # timeout=120).
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do_sync)
            try:
                result = future.result(timeout=120)
            except concurrent.futures.TimeoutError:
                await msg.edit_text("\u23f0 Timeout: la sincronización tardó más de 2 minutos.")
                return

        if result.get("status") == "ok":
            # Build the same [ok]/[error] summary lines the old subprocess produced.
            summary: list[str] = []
            for r in result.get("sources", []):
                pr_list = r.get("page_results") or [{}]
                title = pr_list[0].get("title") or r.get("page") or ""
                added = r.get("added", 0)
                updated = r.get("updated", 0)
                removed = r.get("removed", 0)
                found = r.get("cards_found", 0)
                pages = r.get("pages_scanned", 1)
                stat = r.get("status", "ok")
                summary.append(
                    f"[{stat}] {title} ({pages} páginas): "
                    f"+{added} ~{updated} -{removed} ({found} cards)"
                )
            if summary:
                text = "\U0001f3b4 **Sync completado**\n\n" + "\n".join(summary)
            else:
                text = "\U0001f3b4 Sync completado."
            await msg.edit_text(text, parse_mode="Markdown")
            # New words may have been added — drop the SRS caches.
            # New Notion cards may have arrived — drop the cached tree.
            app = getattr(context, "application", None)
            for hook_name in ("_invalidate_srs_caches", "_invalidate_notion_tree_cache"):
                hook = getattr(app, hook_name, None)
                if callable(hook):
                    try:
                        hook()
                    except Exception:
                        log.exception("cache invalidator %s failed", hook_name)
        else:
            errs = result.get("errors") or []
            err_text = "\n".join(errs)[:500] or "sin salida"
            await msg.edit_text(f"\u274c Error en sync:\n{err_text}")
    except Exception as e:
        await msg.edit_text(f"\u274c Error: {e}")


async def notion_notify_job(context: "ContextTypes.DEFAULT_TYPE") -> None:
    """Daily 9am notification: count pending Notion flashcards.

    Looks up the owner chat id from ``context.bot_data["srs_chat_id"]``
    (set during ``/start`` in the original monolith) and, if there is
    at least one Notion flashcard due today, sends a Markdown reminder
    pointing the user to the Notion flow in the menu.
    """
    chat_id = context.bot_data.get("srs_chat_id")
    if not chat_id:
        return
    try:
        dbf = DATA / "state.db"
        if not dbf.exists():
            return
        conn = sqlite3.connect(str(dbf))
        today_s = date.today().isoformat()
        count = conn.execute(
            "SELECT COUNT(*) FROM course_flashcards "
            "WHERE card_type IN ('notion','notion_reversed') AND next_review <= ?",
            (today_s,),
        ).fetchone()[0]
        conn.close()
        if count > 0:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"\U0001f3b4 **Flashcards de Notion** \u2014 Tienes **{count}** pendiente(s) hoy.\n"
                    f"Rep\u00e1salas con /start \u2192 Revisi\u00f3n \u2192 Por Curso \u2192 Notion."
                ),
                parse_mode="Markdown",
            )
    except Exception as e:
        log.error(f"Notion notify error: {e}")


__all__ = [
    "notion_sync_cmd",
    "notion_notify_job",
]

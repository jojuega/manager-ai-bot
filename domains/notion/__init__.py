"""
domains.notion — Notion flashcard sync domain.

Owns:

* :mod:`domains.notion.sync` — the Notion → flashcard sync engine
  (page fetching, marker parsing, DB writes, tree cache, source list).
* :mod:`domains.notion.tools` — the LLM-callable tool ``sync_notion``
  and its OpenAI/DeepSeek tool schema.
* :mod:`domains.notion.handlers` — the Telegram ``/sync`` command and
  the daily 9am notification job.

Hook-up
-------
The application entry point (``scripts/start_task_bot.py`` or
equivalent) wires this domain into the rest of the app by calling:

* :func:`register` with the agent's :class:`agent.tool_registry.ToolRegistry`
  to publish the LLM tool.
* :func:`register_handlers` with the Telegram ``Application`` to install
  the command handler and schedule the daily notification job.

Both functions are idempotent: calling them twice does not duplicate
tools or handlers.
"""
from __future__ import annotations

import logging
from datetime import time
from typing import TYPE_CHECKING

from agent.tool_registry import ToolRegistry
from domains.notion.tools import SYNC_NOTION_TOOL
from domains.notion.handlers import notion_notify_job, notion_sync_cmd

if TYPE_CHECKING:  # pragma: no cover — typing only
    from telegram.ext import Application

log = logging.getLogger("domains.notion")


# --------------------------------------------------------------------------- #
# Public surface
# --------------------------------------------------------------------------- #
__all__ = [
    "register",
    "register_handlers",
]


def register(registry: ToolRegistry) -> None:
    """Register this domain's LLM tools into ``registry``.

    Currently publishes a single tool:

    * ``sync_notion`` — trigger a full Notion flashcard sync.  Non-
      destructive (the LLM can call it freely; the underlying sync
      updates ``course_flashcards`` and the ``notion_tree.json``
      cache, but the user can roll those back by re-syncing).

    Idempotent: re-registering is a no-op so a double ``register()``
    call (e.g. during a hot-reload) does not raise.
    """
    if "sync_notion" in registry:
        log.debug("domains.notion: 'sync_notion' already registered, skipping")
        return
    registry.add(
        name=SYNC_NOTION_TOOL["name"],
        fn=SYNC_NOTION_TOOL["fn"],
        schema=SYNC_NOTION_TOOL["schema"],
        destructive=SYNC_NOTION_TOOL["destructive"],
    )
    log.info("domains.notion: registered tool 'sync_notion'")


def register_handlers(app: "Application") -> None:
    """Wire up Telegram command handlers and scheduled jobs for this domain.

    * Adds the ``/sync`` command (delegates to
      :func:`domains.notion.handlers.notion_sync_cmd`).
    * Schedules the daily 9am Notion-flashcard reminder
      (:func:`domains.notion.handlers.notion_notify_job`).

    Safe to call once during application startup.  The Telegram library
    raises if you register two ``CommandHandler``s for the same command
    on the same app, so we guard the registration with a small "already
    wired?" check via ``app.handlers``.
    """
    from telegram.ext import CommandHandler

    # Command handler
    already_wired = any(
        isinstance(h, CommandHandler) and "sync" in getattr(h, "commands", ())
        for h in app.handlers.get(0, [])
    )
    if not already_wired:
        app.add_handler(CommandHandler("sync", notion_sync_cmd))
        log.info("domains.notion: registered /sync command")
    else:
        log.debug("domains.notion: /sync command already registered, skipping")

    # Daily 9am job
    job_q = app.job_queue
    if job_q is None:
        log.warning(
            "domains.notion: app.job_queue is None — daily Notion notification "
            "job NOT scheduled (install pytz/tzdata or pass a non-None job_queue)."
        )
        return

    job_name = "notion_notify_daily"
    # JobQueue deduplication by name: ``run_daily`` will replace an
    # existing job with the same ``name`` argument, so calling this
    # twice is safe.
    job_q.run_daily(notion_notify_job, time=time(9, 0, 0), name=job_name)
    log.info("domains.notion: scheduled daily Notion notification job at 09:00")

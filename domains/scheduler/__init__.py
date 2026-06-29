"""
domains.scheduler — scheduled jobs domain.

Extracted from the monolith.  Owns everything that runs on a schedule
rather than in response to a user message:

* :mod:`.cronjobs` — the on-disk store + natural-language parser for
  user-scheduled one-shot jobs ("a las 3pm avísame…", "en 30 minutos
  recuérdame…", "mañana 9am…").
* :mod:`.jobs` — the actual ``JobQueue`` callbacks:

  - :func:`srs_job` — the daily 14:00 SRS reminder.
  - :func:`_cronjob_callback` — the dispatcher that fires the
    reminder / LLM agent at trigger time.
  - :func:`_restore_pending_cronjobs` — startup re-registration of
    jobs that survived a restart.

The 9am Notion-flashcard reminder (:func:`notion_notify_job`) lives
in :mod:`domains.notion.handlers` because it conceptually belongs to
the Notion domain; we do not duplicate it here.

Public entry points
-------------------
* :func:`register` — register this domain's LLM tools into a
  :class:`agent.tool_registry.ToolRegistry`.  The scheduler does not
  expose any LLM tools (all of its work is scheduled, not prompted),
  so this is a no-op.
* :func:`register_handlers` — wire the domain into the Telegram
  ``Application``:

  - Schedules the daily 14:00 SRS job.
  - Restores any pending one-shot cronjobs from disk into the live
    ``JobQueue``.

The application entry point calls both functions once at startup.
Both are idempotent (PTB's ``run_daily`` deduplicates by name, and
re-running :func:`restore_pending_cronjobs` after the jobs are
already in the queue is harmless: any already-restored job has its
``trigger_at`` unchanged, so the next run will re-schedule it for the
remaining time, with PTB replacing the previous registration).
"""
from __future__ import annotations

import logging
from datetime import time
from typing import TYPE_CHECKING

from domains.scheduler.jobs import (
    restore_pending_cronjobs,
    srs_job,
)

if TYPE_CHECKING:  # pragma: no cover — typing only
    from telegram.ext import Application
    from agent.tool_registry import ToolRegistry

log = logging.getLogger("domains.scheduler")


__all__ = [
    "register",
    "register_handlers",
]


def register(registry: "ToolRegistry | None") -> None:
    """Register this domain's LLM tools into ``registry``.

    The scheduler domain is purely a consumer of ``JobQueue`` callbacks;
    it does not expose any tools to the LLM.  This function is a no-op
    kept for symmetry with the other domains so the application entry
    point can iterate over domains uniformly.

    Parameters
    ----------
    registry:
        A :class:`agent.tool_registry.ToolRegistry` (or ``None``); both
        are accepted so callers can disable a domain without changing
        the bootstrap code.
    """
    if registry is None:
        return
    log.debug("domains.scheduler: no LLM tools to register (purely scheduled)")


def register_handlers(app: "Application | None") -> None:
    """Wire this domain into the Telegram ``Application``.

    Two responsibilities:

    1. Schedule the daily 14:00 SRS job (:func:`srs_job`).
    2. Restore any pending one-shot cronjobs from disk into the live
       ``JobQueue`` (:func:`restore_pending_cronjobs`).

    Both operations require a non-``None`` ``app.job_queue`` (PTB only
    attaches one when its ``JobQueue`` is installed).  We log a warning
    and skip scheduling if it isn't available — the rest of the bot
    keeps running.

    Safe to call once during application startup.  ``run_daily`` is
    idempotent by ``name``, so a double ``register_handlers`` call
    (e.g. during a hot-reload) replaces the previous SRS registration
    instead of stacking a second one.
    """
    if app is None:
        return

    job_q = app.job_queue
    if job_q is None:
        log.warning(
            "domains.scheduler: app.job_queue is None — daily SRS job and "
            "cronjob restore NOT scheduled (install pytz/tzdata or pass a "
            "non-None job_queue)."
        )
        return

    # Daily SRS reminder (14:00) — replaces any prior registration with
    # the same name, so this is safe to call twice.
    srs_job_name = "srs_daily"
    job_q.run_daily(srs_job, time=time(14, 0, 0), name=srs_job_name)
    log.info("domains.scheduler: scheduled daily SRS job at 14:00")

    # Re-register any cronjobs that survived a previous shutdown.
    # Must run after app.job_queue is available.
    restore_pending_cronjobs(app)

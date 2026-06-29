"""
Scheduled jobs domain — JobQueue callbacks + cronjob dispatch.

Extracted from the original ``jogtasksbot/scripts/task_bot.py`` monolith.

This module owns everything that runs *without a user prompt* in the
background: the daily SRS reminder, and the one-shot cronjobs that the
user can schedule via natural language ("a las 3pm avísame…").

Layers
------
* :func:`srs_job` — the daily 14:00 SRS reminder (still owner-gated via
  ``context.bot_data['srs_chat_id']``).
* :func:`_schedule_cronjob_in_jobqueue` / :func:`_cronjob_callback` —
  the bridge between the on-disk cronjob storage (:mod:`.cronjobs`) and
  PTB's in-memory ``JobQueue``.  Persists one-shot jobs to disk so they
  survive restarts; re-registers them on startup via
  :func:`_restore_pending_cronjobs`.
* :func:`_restore_pending_cronjobs` — on bot startup, re-schedule any
  pending cronjob that is not yet more than 1h overdue (later ones are
  marked as missed / done so we never fire a stale reminder hours late).

Public entry points
-------------------
* :func:`schedule_cronjob_in_jobqueue` — public version of the
  JobQueue bridge, used by the text-message dispatcher when the user
  schedules a new cronjob.
* :func:`restore_pending_cronjobs` — public version of the restore
  helper, called from the domain's :func:`register_handlers`.

(The ``notion_notify_job`` daily 9am reminder lives in
:mod:`domains.notion.handlers` — the Notion domain owns its own
notification, so it is *not* duplicated here.)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from telegram.ext import ContextTypes

from domains.scheduler import cronjobs
from domains.vocabulary.storage import get_due_words

if TYPE_CHECKING:  # pragma: no cover — typing only
    from telegram.ext import Application

log = logging.getLogger("domains.scheduler.jobs")


# ---------------------------------------------------------------------------
# Daily SRS reminder (14:00)
# ---------------------------------------------------------------------------
async def srs_job(context: ContextTypes.DEFAULT_TYPE):
    """Daily SRS check at 14:00 — sends reminder to the stored chat_id.

    The chat id is stashed in ``context.bot_data['srs_chat_id']`` by the
    first ``/start`` invocation (owner-gated). If the user never started
    the bot there is no recipient and we silently no-op.
    """
    chat_id = context.bot_data.get("srs_chat_id")
    if not chat_id:
        return
    try:
        # In-process call into the vocabulary domain's SRS storage.
        # No subprocess startup cost on the hot path.
        words = get_due_words()
        count = len(words)
        if count > 0:
            msg = f"📚 **Repaso SRS** — {count} palabra(s) pendiente(s):\n\n"
            for i, w in enumerate(words, 1):
                msg += f"**{i}. {w['word']}** ({w['lang']})\n"
                if w.get("sentence"):
                    msg += f"   💬 _{w['sentence']}_\n"
                if w.get("definition"):
                    msg += f"   📝 ||{w['definition']}||\n"
                msg += "\n"
            msg += "✍️ Escribe tu oración acá y la evalúo."
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        log.info(f"SRS job: {count} words due")
    except Exception as e:
        log.error(f"SRS job error: {e}")


# ---------------------------------------------------------------------------
# One-shot cronjob dispatch
# ---------------------------------------------------------------------------
# PTB's JobQueue rejects delays > 24h with a ValueError. For longer delays
# (e.g. "mañana 3pm" = ~30h away), cap at 24h on the first registration and
# re-schedule the remainder on the next bot start. This is acceptable: a
# cronjob that fires once is "best effort" across restarts; if the user
# needs a hard 30h timer, they keep the bot running.
_MAX_JOBQ_DELAY = 24 * 60 * 60  # seconds


def schedule_cronjob_in_jobqueue(app, job) -> None:
    """Register a one-shot job with PTB's JobQueue. Takes the Application.

    Public wrapper around the internal helper. Exposed so the text-message
    dispatcher (in the tasks domain) can wire newly-parsed cronjobs into
    the live JobQueue after persisting them to disk.
    """
    _schedule_cronjob_in_jobqueue(app, job)


def _schedule_cronjob_in_jobqueue(app, job) -> None:
    """Register a one-shot job with PTB's JobQueue. Takes the Application."""
    try:
        trigger_at = datetime.fromisoformat(job.trigger_at)
    except Exception as e:
        log.error(f"_schedule_cronjob_in_jobqueue: bad trigger_at {job.trigger_at!r}: {e}")
        return
    delay = (trigger_at - datetime.now()).total_seconds()
    if delay < 0:
        delay = 0
    if delay > _MAX_JOBQ_DELAY:
        # Fire on the next 24h boundary. The job stays "pending" on disk
        # with its full trigger_at, so the next start (or a manual re-add)
        # will re-schedule the remainder.
        delay = _MAX_JOBQ_DELAY
    try:
        app.job_queue.run_once(
            _cronjob_callback,
            when=delay,
            data={
                "job_id": job.id,
                "kind": job.kind,
                "payload": job.payload,
                "chat_id": job.chat_id,
            },
            name=f"cronjob_{job.id}",
        )
    except Exception as e:
        log.error(f"job_queue.run_once failed for cronjob {job.id}: {e}")


async def _cronjob_callback(context: ContextTypes.DEFAULT_TYPE):
    """Called by JobQueue at trigger time. Dispatches reminder or agent task."""
    data = context.job.data
    job_id = data.get("job_id")
    kind = data.get("kind")
    payload = data.get("payload") or {}
    chat_id = data.get("chat_id")
    try:
        if kind == "reminder":
            text = payload.get("text", "(sin mensaje)")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ **Recordatorio**\n\n{text}",
                parse_mode="Markdown",
            )
        elif kind == "agent":
            prompt = payload.get("prompt", "")
            if not prompt:
                await context.bot.send_message(
                    chat_id=chat_id, text="⚠️ Tarea programada vacía."
                )
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🤖 Ejecutando: _{prompt}_…",
                    parse_mode="Markdown",
                )
                try:
                    # Lazy import: the LLM agent is heavy and only needed
                    # for the agent-flavored cronjobs (rare vs simple
                    # reminders).
                    from llm_agent import get_agent_with_fallback  # type: ignore
                    agent = get_agent_with_fallback()
                    result = agent.process_message(prompt, history=[])
                    response_text = (
                        result.get("response", "(sin respuesta)")
                        if isinstance(result, dict)
                        else str(result)
                    )
                    # PTB has a 4096-char limit per message — split if needed.
                    for i in range(0, len(response_text), 3500):
                        chunk = response_text[i: i + 3500]
                        await context.bot.send_message(
                            chat_id=chat_id, text=chunk, parse_mode="Markdown"
                        )
                except Exception as e:
                    log.error(f"cronjob agent call failed: {e}")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"❌ Error ejecutando tarea programada: {e}",
                    )
        else:
            log.warning(f"cronjob callback: unknown kind {kind!r} for job {job_id}")
        if job_id is not None:
            cronjobs.mark_done(job_id)
    except Exception as e:
        log.error(f"cronjob callback failed (job {job_id}): {e}")


# ---------------------------------------------------------------------------
# Startup restore
# ---------------------------------------------------------------------------
def restore_pending_cronjobs(app) -> None:
    """Public wrapper around :func:`_restore_pending_cronjobs`.

    Re-registers any pending cronjob that survived the previous shutdown
    into the new JobQueue. Called from :func:`domains.scheduler.register_handlers`
    once ``app.job_queue`` is available.
    """
    _restore_pending_cronjobs(app)


def _restore_pending_cronjobs(app) -> None:
    """On startup, re-register pending jobs from disk into JobQueue.

    Jobs whose trigger time is already > 1h in the past are marked done
    without firing (they're considered "missed" — better than firing a
    stale reminder hours late).
    """
    try:
        all_jobs = cronjobs.load_cronjobs()
    except Exception as e:
        log.warning(f"cronjob restore: failed to load: {e}")
        return
    restored = missed = 0
    for job in all_jobs:
        if job.status != "pending":
            continue
        try:
            trigger_at = datetime.fromisoformat(job.trigger_at)
        except Exception:
            log.warning(f"cronjob restore: bad trigger_at for #{job.id}, marking done")
            cronjobs.mark_done(job.id)
            continue
        if trigger_at < datetime.now() - timedelta(hours=1):
            cronjobs.mark_done(job.id)
            missed += 1
            continue
        _schedule_cronjob_in_jobqueue(app, job)
        restored += 1
    if restored or missed:
        log.info(
            f"cronjob restore: {restored} re-scheduled, "
            f"{missed} marked missed (overdue >1h)"
        )


__all__ = [
    "srs_job",
    "schedule_cronjob_in_jobqueue",
    "restore_pending_cronjobs",
]

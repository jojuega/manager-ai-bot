"""bot.py — entry point for the manager-ai-bot Telegram bot.

This module wires every domain (tasks, vocabulary, flashcards, manga, notion,
scheduler) into a single :class:`telegram.ext.Application` and a single
:class:`agent.tool_registry.ToolRegistry` consumed by an
:class:`agent.agent.LLMAgent`.

Pipeline at startup
-------------------
1. Load secrets & config from :mod:`core.config` (BOT_TOKEN, ALLOWED_USER_ID,
   DEEPSEEK_KEY, …).  ``core.config`` already ``sys.exit(1)``s at import
   time if the bot token is missing.
2. Run idempotent DB migrations via :func:`core.db.init_db`.
3. Build an empty :class:`ToolRegistry` and let every domain register its
   tools into it.
4. Construct the :class:`LLMAgent` (singleton, with OpenCode Go → DeepSeek
   fallback) bound to the populated registry.
5. Build the Telegram ``Application`` and let every domain register its
   handlers (``register_handlers(app)``).
6. Install the global free-text handler that routes everything else through
   ``agent.process_message()``.
7. Install a top-level error handler and start polling.

Per-domain registration
-----------------------
Each domain package (``domains.tasks``, ``domains.vocabulary``, …) exposes
two no-op-safe functions:

* ``register(registry)``       — populates the agent's ``ToolRegistry``.
* ``register_handlers(app)``   — installs Telegram ``CommandHandler``s,
  ``MessageHandler``s, ``CallbackQueryHandler``s, and (for ``notion``) the
  daily 9am job.

A domain whose ``__init__.py`` does not provide one of the hooks is
silently skipped (with a ``DEBUG`` log line) so an incomplete or future
domain doesn't take the whole bot down.  This keeps ``scheduler`` —
currently an empty package — out of the loop without requiring
conditional imports.

Running
-------
::

    python -m bot
    # or
    python bot.py
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Core: config, DB, agent pieces
# --------------------------------------------------------------------------- #
from core.config import ALLOWED_USER_ID, BOT_TOKEN, DATA
from core.db import init_db
from agent.agent import LLMAgent, get_agent_with_fallback
from agent.tool_registry import ToolRegistry

# --------------------------------------------------------------------------- #
# Domain packages
# --------------------------------------------------------------------------- #
# Each domain exposes (or may expose) ``register(registry)`` and
# ``register_handlers(app)``.  An exception during import or a missing
# hook logs a warning and the domain is skipped — the bot must still come
# up if one of the domains is broken.
from domains import tasks, vocabulary, flashcards, manga, notion

log = logging.getLogger("bot")


# --------------------------------------------------------------------------- #
# Domain list — order matters for handler priority inside the Application
# (earlier registered handlers get first crack at an update).  Keep the
# highest-priority, most-specific handlers first.
# --------------------------------------------------------------------------- #
DOMAIN_PACKAGES: tuple[Any, ...] = (
    tasks,        # /start, /briefing, free-text (deferred to agent below)
    vocabulary,   # /srs, deck callbacks
    flashcards,   # course-picker callbacks
    manga,        # /z, photos, manga:* callbacks, awaiting-manga text
    notion,       # /sync, daily 9am job
    # ``scheduler`` is currently an empty package; it gets re-added to
    # this tuple once it exposes ``register`` / ``register_handlers``.
)


# =========================================================================== #
# Domain wiring
# =========================================================================== #
def _safe_register(domain_name: str, hook: str, fn: Callable[..., Any], *args: Any) -> None:
    """Invoke a domain hook defensively.

    Any exception (missing hook, broken tool/handler, …) is caught and
    logged so a single bad domain cannot stop the bot from starting.
    """
    try:
        fn(*args)
        log.debug("domain %s: %s() ok", domain_name, hook)
    except Exception as exc:  # pragma: no cover — defensive logging only
        log.warning(
            "domain %s: %s() failed: %s — domain disabled for this run",
            domain_name, hook, exc,
        )


def build_registry() -> ToolRegistry:
    """Build a :class:`ToolRegistry` populated with every domain's tools."""
    registry = ToolRegistry()
    for pkg in DOMAIN_PACKAGES:
        register_fn = getattr(pkg, "register", None)
        if register_fn is None:
            log.debug("domain %s: no register(registry) — skipping", pkg.__name__)
            continue
        _safe_register(pkg.__name__, "register", register_fn, registry)
    log.info("ToolRegistry populated with %d tools: %s", len(registry), registry.names())
    return registry


def build_agent(registry: ToolRegistry) -> LLMAgent:
    """Build the singleton :class:`LLMAgent` with DeepSeek fallback.

    Uses :func:`agent.agent.get_agent_with_fallback` so the agent is
    resolved against ``OPENCODE_GO_API_KEY`` first, then ``DEEPSEEK_API_KEY``.
    The same singleton is reused across every call to ``process_message``.
    """
    agent = get_agent_with_fallback(registry)
    log.info(
        "LLMAgent ready: base_url=%s model=%s tools=%d",
        agent.base_url, agent.model, len(agent.registry),
    )
    return agent


# =========================================================================== #
# Free-text handler — routes every non-command message to the LLM agent
# =========================================================================== #
def _chat_key(update: Update) -> str:
    """Return the bucket key used to store per-chat conversation history.

    The key is ``"<chat_id>:<thread_id>"`` so the main chat and its forum
    threads each have their own history.  Empty ``message_thread_id`` is
    normalised to ``"main"`` for readability.
    """
    chat = update.effective_chat
    tid = getattr(update.effective_message, "message_thread_id", None)
    return f"{chat.id if chat else 'unknown'}:{tid if tid else 'main'}"


def _get_history(app: Application, key: str) -> list:
    """Return (and lazily create) the history list for ``key``."""
    histories: dict[str, list] = app.bot_data.setdefault("chat_histories", {})
    history = histories.get(key)
    if history is None:
        history = []
        histories[key] = history
    return history


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all for free text.

    Routes the message to :meth:`LLMAgent.process_message` and replies with
    the agent's response.  Per-chat history lives in ``bot_data`` so it
    survives across handler invocations within the same process.
    """
    msg = update.message
    if msg is None or msg.text is None:
        return
    # Owner-only — mirror the tasks domain's behaviour.
    if msg.from_user is None or msg.from_user.id != ALLOWED_USER_ID:
        return

    agent: LLMAgent = context.application.bot_data["agent"]
    history = _get_history(context.application, _chat_key(update))

    try:
        result = agent.process_message(msg.text, history=history)
    except Exception as exc:
        log.exception("agent.process_message failed")
        await msg.reply_text(f"❌ Error procesando el mensaje: {exc}")
        return

    # process_message mutates and returns the same list — write it back
    # so subsequent turns see the updated turns.
    history[:] = result.get("history", history)

    response = result.get("response", "")
    confirmation = result.get("confirmation")

    if confirmation:
        # The agent flagged a destructive action — surface it to the user
        # verbatim.  Wiring the actual Confirm/Cancel buttons is left to
        # the existing per-domain callback routers (see agent.confirm and
        # the conversation-history contract for confirmed actions).
        await msg.reply_text(response)
    elif response:
        await msg.reply_text(response)


# =========================================================================== #
# Top-level error handler
# =========================================================================== #
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the exception and notify the user when possible.

    Mirrors the original ``task_bot.err_h`` behaviour: always log, and
    if the update is a message update, send a short apologetic reply to
    the chat.  Anything else (CallbackQuery, channel post, etc.) gets the
    log entry only — Telegram's API doesn't allow arbitrary "errors" on
    non-message updates.
    """
    log.exception("Unhandled exception in handler", exc_info=context.error)

    err = context.error
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                f"❌ Error interno: {err}",
            )
        except Exception:  # pragma: no cover — never let the error handler raise
            log.exception("error_handler: failed to send error reply")


# =========================================================================== #
# Application assembly
# =========================================================================== #
def build_application(registry: ToolRegistry, agent: LLMAgent) -> Application:
    """Build the fully-wired :class:`telegram.ext.Application`.

    The Application is *not* started here — call :func:`main` (or
    ``app.run_polling()`` yourself) once everything is in place.
    """
    app = Application.builder().token(BOT_TOKEN).build()

    # Stash the agent on bot_data so handlers can reach it without a
    # global.  Same for the DATA directory in case handlers need it.
    app.bot_data["agent"] = agent
    app.bot_data["registry"] = registry
    app.bot_data["data_dir"] = str(DATA)

    # ----- per-domain handlers ---------------------------------------- #
    for pkg in DOMAIN_PACKAGES:
        reg_h = getattr(pkg, "register_handlers", None)
        if reg_h is None:
            log.debug("domain %s: no register_handlers(app) — skipping", pkg.__name__)
            continue
        _safe_register(pkg.__name__, "register_handlers", reg_h, app)

    # ----- global catch-all for free text ----------------------------- #
    # Registered *last* so domain-specific MessageHandlers (e.g. the
    # manga awaiting-input router) get first dibs on text updates.
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_message_handler,
        block=False,  # let other MessageHandlers still see the update
    ))

    # ----- top-level error handler ------------------------------------ #
    app.add_error_handler(error_handler)

    return app


# =========================================================================== #
# main()
# =========================================================================== #
def main() -> None:
    """Entry point: build everything, then start polling."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Starting manager-ai-bot …")

    # 1. Idempotent DB migrations (safe to run on every startup).
    init_db()

    # 2. Wire the LLM side: registry → agent (singleton, with fallback).
    registry = build_registry()
    agent = build_agent(registry)

    # 3. Wire the Telegram side: application → domain handlers → catch-all.
    app = build_application(registry, agent)
    log.info("Application wired; %d handlers registered.", len(list(app.handlers[0])))

    # 4. Run forever.
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()

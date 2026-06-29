"""Tasks domain — public entry point.

Exposes two integration hooks so the rest of the bot can wire the tasks
domain into both the LLM agent and the Telegram dispatcher:

* ``register(registry)`` — adds every ``tasks_*`` callable and its schema to
  the given :class:`agent.tool_registry.ToolRegistry`. The registry is what
  the LLM agent consumes.
* ``register_handlers(app)`` — adds the tasks-domain Telegram handlers
  (``/start``, ``/briefing``, text-message catch-all) to the given
  ``telegram.ext.Application``.

Both functions are no-ops if their argument is ``None`` so callers can
disable a domain without modifying the bot's bootstrap code.
"""
from __future__ import annotations

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .handlers import any_msg, briefing_cmd, start_cmd
from .tools import (
    DESTRUCTIVE_TOOLS,
    TOOL_DISPATCH,
    TOOL_SCHEMAS,
)


def register(registry) -> None:
    """Register every ``tasks_*`` tool on the given ToolRegistry.

    Parameters
    ----------
    registry:
        An :class:`agent.tool_registry.ToolRegistry` instance, or ``None``
        (in which case this function is a no-op).
    """
    if registry is None:
        return
    # Build a schema lookup so we can find the right schema for each
    # callable's tool name.
    schema_by_name = {s["function"]["name"]: s for s in TOOL_SCHEMAS}
    for tool_name, fn in TOOL_DISPATCH.items():
        schema = schema_by_name.get(tool_name)
        if schema is None:
            # Defensive: a callable without a matching schema is a bug, but
            # we don't want a single typo to take down the whole registry.
            continue
        registry.add(
            name=tool_name,
            fn=fn,
            schema=schema,
            destructive=tool_name in DESTRUCTIVE_TOOLS,
        )


def register_handlers(app) -> None:
    """Add the tasks-domain Telegram handlers to ``app``.

    Parameters
    ----------
    app:
        A :class:`telegram.ext.Application` (or compatible), or ``None``
        (in which case this function is a no-op).
    """
    if app is None:
        return
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("briefing", briefing_cmd))
    # Text messages are handled via a MessageHandler that filters out commands.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, any_msg))


__all__ = [
    "register",
    "register_handlers",
    "TOOL_SCHEMAS",
    "TOOL_DISPATCH",
    "DESTRUCTIVE_TOOLS",
]

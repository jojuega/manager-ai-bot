"""
Manga domain — public entry point.

Exposes two integration hooks so the rest of the bot can wire the manga
domain into both the LLM agent and the Telegram dispatcher:

* ``register(registry)`` — adds every ``manga_*`` callable and its schema
  to the given :class:`agent.tool_registry.ToolRegistry`. The registry is
  what the LLM agent consumes.
* ``register_handlers(app)`` — adds the manga-domain Telegram handlers
  (``handle_image`` for incoming photos, ``/z`` for the default mode,
  the ``manga_*`` / ``mg*`` callback dispatchers) to the given
  ``telegram.ext.Application``.

Both functions are no-ops if their argument is ``None`` so callers can
disable a domain without modifying the bot's bootstrap code.
"""
from __future__ import annotations

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .handlers import (
    _manga_dispatch_callback,
    _manga_handle_text_input,
    handle_image,
    z_cmd,
)
from .tools import (
    DESTRUCTIVE_TOOLS,
    TOOL_DISPATCH,
    TOOL_SCHEMAS,
)


# Callback-data prefixes the manga domain owns. Anything starting with one
# of these gets routed to :func:`domains.manga.handlers._manga_dispatch_callback`
# by ``register_callbacks`` below.
MANGA_CALLBACK_PREFIXES = (
    "mgprac",
    "mgflip",
    "mgsave",
    "mgquit",
    "mselect:",
    "mnew",
    "mnewvol:",
    "mvol:",
    "mback:",
    "mcancel",
)


def _is_manga_callback(data: str) -> bool:
    """Return True if ``data`` should be routed to the manga domain."""
    if not data:
        return False
    for prefix in MANGA_CALLBACK_PREFIXES:
        if data == prefix or data.startswith(prefix):
            return True
    return False


async def manga_callback_router(update, context) -> None:
    """Top-level CallbackQueryHandler that fans out to the right
    manga-specific callback based on the callback data prefix."""
    data = update.callback_query.data if update.callback_query else ""
    if not data:
        return
    if _is_manga_callback(data):
        await _manga_dispatch_callback(update, context, data)


async def manga_text_router(update, context) -> None:
    """Top-level MessageHandler that consumes ``awaiting_manga_input``
    actions before the normal text/agent flow takes over."""
    msg = update.message
    if not msg or not msg.text:
        return
    if msg.text.startswith("/"):
        return  # commands go to their own handlers
    await _manga_handle_text_input(update, context, msg.text)


def register(registry) -> None:
    """Register every ``manga_*`` tool on the given ToolRegistry.

    Parameters
    ----------
    registry:
        An :class:`agent.tool_registry.ToolRegistry` instance, or
        ``None`` (in which case this function is a no-op).
    """
    if registry is None:
        return
    schema_by_name = {s["function"]["name"]: s for s in TOOL_SCHEMAS}
    for tool_name, fn in TOOL_DISPATCH.items():
        schema = schema_by_name.get(tool_name)
        if schema is None:
            continue
        registry.add(
            name=tool_name,
            fn=fn,
            schema=schema,
            destructive=tool_name in DESTRUCTIVE_TOOLS,
        )


def register_handlers(app) -> None:
    """Add the manga-domain Telegram handlers to ``app``.

    Parameters
    ----------
    app:
        A :class:`telegram.ext.Application` (or compatible), or
        ``None`` (in which case this function is a no-op).
    """
    if app is None:
        return
    # /z — manga default mode (PTB normalises to lowercase → /Z also works)
    app.add_handler(CommandHandler("z", z_cmd))
    # Incoming photos / image documents → unified image handler
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.IMAGE, handle_image,
    ))
    # Callback router (catches mselect:/mvol:/mgflip:/...; defers to the
    # domain's internal dispatcher)
    app.add_handler(CallbackQueryHandler(manga_callback_router))
    # Text input router (only consumes messages that are part of a P2 flow;
    # otherwise it's a no-op and the regular flow takes over)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, manga_text_router,
    ))


__all__ = [
    "register",
    "register_handlers",
    "TOOL_SCHEMAS",
    "TOOL_DISPATCH",
    "DESTRUCTIVE_TOOLS",
    "MANGA_CALLBACK_PREFIXES",
]

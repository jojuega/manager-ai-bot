"""Telegram-bound helpers that don't pull in the bot framework at import time.

Kept here so the rest of ``core/`` (config, db) stays importable in
non-telegram contexts (tests, cron jobs, CLI tooling).  ``telegram`` is
imported lazily inside the functions that need it, with type hints under
``TYPE_CHECKING`` for static analysers.
"""
from __future__ import annotations

import html as _html_module
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only
    from telegram import Update

log = logging.getLogger("core.telegram_utils")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def html_escape(s: object) -> str:
    """Escape user-controlled text before inserting it into an HTML parse_mode
    message.  Avoids the LLM breaking Telegram's HTML parser with stray
    ``<``, ``>`` and ``&``.

    Exposed as the public ``html_escape`` (without the leading underscore) so
    it can be reused by domain modules; the original ``_html_escape`` was a
    module-private name in the monolith."""
    if s is None:
        return ""
    return _html_module.escape(str(s))


async def safe_reply_markdown(update: "Update", text: str, **kwargs):
    """Send ``text`` with ``Markdown`` parse_mode.

    Falls back to a plain-text message if the LLM produced malformed
    formatting that Telegram can't parse.  Re-raises any other
    :class:`telegram.error.BadRequest`."""
    # Local import keeps ``core.telegram_utils`` importable without
    # ``python-telegram-bot`` installed (e.g. for unit tests).
    from telegram.error import BadRequest

    try:
        return await update.message.reply_text(
            text, parse_mode="Markdown", **kwargs
        )
    except BadRequest as exc:
        msg = str(exc)
        if "can't parse entities" in msg or "Can't parse entities" in msg:
            log.warning(
                "Markdown parse failed, retrying as plain text: %s", msg[:120]
            )
            return await update.message.reply_text(text, **kwargs)
        raise


# ---------------------------------------------------------------------------
# Backwards-compat shims
# ---------------------------------------------------------------------------
# The monolith used ``_html_escape`` / ``_safe_reply_markdown`` everywhere.
# Keep those names available so any other module that still imports them (and
# the call-sites that may be migrated in later steps) keeps working.
_html_escape = html_escape
_safe_reply_markdown = safe_reply_markdown


__all__ = [
    "html_escape",
    "safe_reply_markdown",
    "_html_escape",
    "_safe_reply_markdown",
]

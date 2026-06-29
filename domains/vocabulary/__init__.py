"""Vocabulary domain — SRS-based word learning.

Exposes:
- register(registry) — wires vocabulary/deck tools into the LLM ToolRegistry
- register_handlers(app) — wires SRS Telegram handlers
"""
from __future__ import annotations

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from .handlers import handle_callback, register_handlers as _register_handlers
from .tools import (
    DESTRUCTIVE_TOOLS,
    TOOL_DEFINITIONS,
    TOOL_FUNCTIONS,
)


def register(registry) -> None:
    """Register all vocabulary LLM tools on the given ToolRegistry."""
    if registry is None:
        return
    for name, fn in TOOL_FUNCTIONS.items():
        schema = None
        for s in TOOL_DEFINITIONS:
            if s.get("function", {}).get("name") == name:
                schema = s
                break
        if schema is None:
            continue
        destructive = name in DESTRUCTIVE_TOOLS
        registry.add(name, fn, schema, destructive=destructive)


def register_handlers(app: Application) -> None:
    """Register vocabulary Telegram handlers."""
    if app is None:
        return
    _register_handlers(app)

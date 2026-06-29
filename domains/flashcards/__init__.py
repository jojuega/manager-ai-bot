"""
domains.flashcards — flashcards domain module.

Extracted from the monolith.  Owns everything related to the
``course_flashcards`` table and its Telegram UI surface:

* :mod:`.tools` — LLM-callable tools (``flashcard_*``, ``course_*``).
* :mod:`.storage` — SQLite reads + SM-2 SRS writes.
* :mod:`.menus` — keyboard builders (per-course picker + Notion tree).
* :mod:`.handlers` — Telegram handlers + :func:`register_handlers`.

Public entry points
-------------------
* :func:`register` — register the LLM tools into a
  :class:`agent.tool_registry.ToolRegistry`.
* :func:`register_handlers` — register the Telegram ``CallbackQueryHandler``s
  on an ``Application``.

The agent core remains domain-agnostic; the application entry point
calls :func:`register` (and every other domain's equivalent) once at
startup, then hands the populated registry to ``build_agent``.
"""
from __future__ import annotations

from telegram.ext import Application

from agent.tool_registry import ToolRegistry

from .handlers import register_handlers
from .tools import register


__all__ = ["register", "register_handlers"]

"""
domains.notion.tools — LLM-callable tool for the Notion sync domain.

Defines :func:`tool_sync_notion` (the function the LLM can invoke) and
:data:`SYNC_NOTION_SCHEMA` / :data:`SYNC_NOTION_TOOL` (the OpenAI-style
tool definitions the agent surfaces to the model).

Extracted from the original ``scripts/llm_agent.py`` monolith where it
lived under the "TOOL: SYSTEM" block alongside other system tools.

The tool runs the sync **in-process** (instead of spawning a subprocess
on the ``notion_sync.py`` CLI) which avoids the ~0.7-1.5s interpreter
startup per call.  This is exactly the same in-process call the original
``tool_sync_notion`` performed; the implementation is just relocated here.
"""
from __future__ import annotations

import concurrent.futures
import logging

from domains.notion.sync import run_sync

log = logging.getLogger("domains.notion.tools")


# --------------------------------------------------------------------------- #
# Tool implementation
# --------------------------------------------------------------------------- #
def tool_sync_notion() -> dict:
    """Trigger a Notion flashcard sync (LLM-callable tool).

    Returns
    -------
    dict
        On success: ``{"status": "ok", "message": "Sync completado",
        "details": [<summary lines>]}``.

        On error / timeout: ``{"status": "error", "message": "..."}``.

        The function is intentionally defensive: it never raises — every
        exception is captured and reported back to the LLM as a structured
        error so the agent can show it to the user.
    """
    try:
        # Run in a thread so we can enforce a 120s wall-clock timeout
        # without blocking the asyncio event loop.  Mirrors the
        # behaviour of the original subprocess-based implementation.
        def _do_sync() -> dict:
            return run_sync(timeout_sec=120, verbose=False)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_do_sync)
            try:
                result = future.result(timeout=120)
            except concurrent.futures.TimeoutError:
                return {"status": "error", "message": "Timeout (más de 2 minutos)"}

        if result.get("status") == "ok":
            # Build the same [ok]/[error] summary lines the original
            # subprocess produced, so the LLM (and downstream
            # user-facing messages) keep their familiar shape.
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
            return {
                "status": "ok",
                "message": "Sync completado",
                "details": summary[:5],
            }
        else:
            errs = result.get("errors") or []
            err_text = "\n".join(errs)[:300] or "sin salida"
            return {"status": "error", "message": f"Error sync: {err_text}"}
    except Exception as e:
        log.exception("tool_sync_notion failed")
        return {"status": "error", "message": str(e)}


# --------------------------------------------------------------------------- #
# OpenAI / DeepSeek tool schemas
# --------------------------------------------------------------------------- #
SYNC_NOTION_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "sync_notion",
        "description": "Sincroniza flashcards desde Notion (forzar sync manual)",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


# Map of all tools this domain registers into the agent's
# :class:`agent.tool_registry.ToolRegistry`.  Kept here next to the
# implementations so the agent factory can wire them up with a single
# ``registry.add(**entry)`` loop.
SYNC_NOTION_TOOL: dict = {
    "name": "sync_notion",
    "fn": tool_sync_notion,
    "schema": SYNC_NOTION_SCHEMA,
    "destructive": False,
}


__all__ = [
    "tool_sync_notion",
    "SYNC_NOTION_SCHEMA",
    "SYNC_NOTION_TOOL",
]

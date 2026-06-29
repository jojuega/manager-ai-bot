"""
agent.confirm — parse and execute user-confirmed destructive actions.

In the original monolith, the LLM could emit a textual marker of the form
``__CONFIRM__:tool_name:JSON_params:user_message`` to flag that the
action it was about to take was destructive and required the user's
explicit approval.  The Telegram layer would then show Confirm/Cancel
buttons; on confirm, the bot called ``execute_confirmed(action, params)``
which routed through the two module-level tool registries defined on
the original agent module (one for safe tools, one for destructive ones).

In the refactored codebase those globals are gone: tool dispatch lives
behind a :class:`agent.tool_registry.ToolRegistry`.  This module keeps
the same public surface (``parse_confirm`` / ``execute_confirmed``) but
talks to a registry instead of module-level dicts.  The registry is
expected to be passed in by the caller (the agent factory, the Telegram
callback handler, etc.), so this module remains domain-agnostic.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from agent.prompts import CONFIRM_MARKER
from agent.tool_registry import ToolRegistry

log = logging.getLogger("agent.confirm")


def parse_confirm(text: str) -> Optional[dict]:
    """Extract a confirmation request from the LLM's response text.

    The expected format is::

        __CONFIRM__:tool_name:JSON_params:user_message

    The ``user_message`` portion is ignored here — the caller (the
    Telegram layer) is in charge of re-rendering the full response text
    alongside the Confirm/Cancel buttons.  Only the action name and
    parameters are returned because those are the values needed to
    actually execute the tool on confirmation.

    Returns
    -------
    ``{"action": str, "params": dict}`` on success, or ``None`` if the
    text does not contain the marker, the marker is malformed, or its
    JSON params fail to parse.
    """
    if not text:
        return None

    marker = f"{CONFIRM_MARKER}:"
    if marker not in text:
        return None

    try:
        # Format: __CONFIRM__:tool_name:JSON_params:user_message
        rest = text.split(marker, 1)[1]

        # First part is the tool name
        tool_name = rest.split(":", 1)[0].strip()
        rest2 = rest.split(":", 1)[1] if ":" in rest else ""

        # Second part is JSON params
        params_json = rest2.split(":", 1)[0].strip() if ":" in rest2 else ""
        params = json.loads(params_json) if params_json else {}

        if not tool_name:
            log.warning("parse_confirm: empty tool name in confirm marker")
            return None

        return {"action": tool_name, "params": params}
    except Exception as e:
        log.warning(f"Failed to parse confirm marker: {e}")
        return None


def execute_confirmed(
    tool_name: str,
    tool_args: dict,
    registry: ToolRegistry,
) -> str:
    """Execute a tool that the user has just confirmed via the UI.

    This is the entry point used by the Telegram ``agent:confirm``
    callback handler.  It is a thin wrapper over the registry that
    formats the result into a human-readable string suitable for sending
    back to the user.

    Parameters
    ----------
    tool_name:
        Name of the tool to run (must already be in ``registry``).
    tool_args:
        Arguments to pass to the tool's callable.
    registry:
        The :class:`ToolRegistry` instance used by the running agent.

    Returns
    -------
    A user-facing string:

    * ``result["message"]`` if the tool returned a dict with a ``message``
      field (the standard convention for this project's tools);
    * a pretty-printed JSON dump if the result is a dict without a
      ``message``;
    * an error line starting with ``❌`` if the tool returned
      ``status == "error"``;
    * ``❌ Tool '<name>' no encontrada`` if the tool is unknown.
    """
    fn = registry.get(tool_name)
    if not fn:
        return f"❌ Tool '{tool_name}' no encontrada"

    try:
        result = fn(**(tool_args or {}))
    except TypeError as e:
        log.error(f"execute_confirmed: bad args for {tool_name}: {e}")
        return f"❌ Error: argumentos inválidos para {tool_name}: {e}"
    except Exception as e:
        log.error(f"execute_confirmed: {tool_name} raised: {e}")
        return f"❌ Error ejecutando {tool_name}: {e}"

    if not isinstance(result, dict):
        return json.dumps(result, indent=2, ensure_ascii=False)

    if result.get("status") == "error":
        return f"❌ Error: {result.get('message', 'desconocido')}"
    if "message" in result:
        return str(result["message"])
    return json.dumps(result, indent=2, ensure_ascii=False)


__all__ = ["parse_confirm", "execute_confirmed"]

"""
agent.tool_registry — generic, domain-agnostic registry for LLM-callable tools.

A ``ToolRegistry`` holds:

* a callable for each tool name (``fn``);
* an OpenAI/DeepSeek-compatible schema describing the tool's parameters
  (``schema``);
* a ``destructive`` flag indicating whether the tool requires user
  confirmation before being executed.

The registry intentionally knows nothing about any particular application
domain.  Domain modules register their own tools; ``agent.LLMAgent`` only
consumes the registry through its public surface.

This is the central piece of decoupling that lets us extract the agent core
out of the original monolith without dragging the domain tools along.
"""
from __future__ import annotations

from typing import Callable


class ToolRegistry:
    """Registry of LLM-callable tools.

    Storage layout::

        self._tools: dict[str, dict] = {
            "<tool_name>": {
                "fn": <callable>,
                "schema": {...},   # OpenAI/DeepSeek tool definition
                "destructive": False,
            },
            ...
        }
    """

    def __init__(self) -> None:
        self._tools: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Mutation
    # ------------------------------------------------------------------ #
    def add(
        self,
        name: str,
        fn: Callable,
        schema: dict,
        destructive: bool = False,
    ) -> None:
        """Register a tool under ``name``.

        Parameters
        ----------
        name:
            Unique tool name as it appears in the LLM tool schema and in
            ``function.name`` when the model returns a tool call.
        fn:
            Python callable invoked when the LLM requests this tool.  It is
            called with the model's arguments unpacked (``fn(**fn_args)``),
            so its signature must match the JSON-schema properties.
        schema:
            OpenAI-compatible tool definition, i.e. the dict with keys
            ``type``/``function.name``/``function.description``/
            ``function.parameters``.
        destructive:
            If ``True``, the agent will route calls to this tool through a
            confirmation flow (display Confirm/Cancel buttons) instead of
            executing it immediately.
        """
        if not name or not isinstance(name, str):
            raise ValueError("Tool name must be a non-empty string")
        if not callable(fn):
            raise TypeError(f"Tool '{name}': fn must be callable")
        if not isinstance(schema, dict):
            raise TypeError(f"Tool '{name}': schema must be a dict")
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")

        self._tools[name] = {
            "fn": fn,
            "schema": schema,
            "destructive": bool(destructive),
        }

    def remove(self, name: str) -> None:
        """Unregister a tool.  No-op if it isn't registered."""
        self._tools.pop(name, None)

    def clear(self) -> None:
        """Remove every tool from the registry."""
        self._tools.clear()

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #
    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def names(self) -> list[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())

    def is_destructive(self, name: str) -> bool:
        """Return ``True`` if the named tool requires user confirmation.

        Unknown tools are treated as non-destructive; the caller is
        expected to handle "tool not found" separately via
        :meth:`get_tool_map`.
        """
        entry = self._tools.get(name)
        if not entry:
            return False
        return bool(entry.get("destructive", False))

    # ------------------------------------------------------------------ #
    # LLM-facing serialisation
    # ------------------------------------------------------------------ #
    def get_tool_definitions(self) -> list[dict]:
        """Return tool definitions in the OpenAI/DeepSeek ``tools`` format.

        Each definition is exactly the ``schema`` passed to :meth:`add` —
        the registry does not wrap or rewrite it.  The list is empty if
        no tools are registered.
        """
        return [entry["schema"] for entry in self._tools.values()]

    def get_tool_map(self) -> dict[str, Callable]:
        """Return a mapping ``name -> callable`` for tool execution.

        Useful when an external caller (e.g. the Telegram callback handler
        after a user confirms a destructive action) needs to invoke a tool
        directly without going through the LLM.
        """
        return {name: entry["fn"] for name, entry in self._tools.items()}

    def get(self, name: str) -> Callable | None:
        """Return the callable for ``name``, or ``None`` if unknown."""
        entry = self._tools.get(name)
        return entry["fn"] if entry else None

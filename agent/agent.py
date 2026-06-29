"""
agent.agent — the LLM agent core, extracted from the original monolith.

Responsibilities
----------------
The agent core owns:

* the OpenAI-compatible HTTP client used to talk to the LLM endpoint;
* history compaction (the "summarise old turns" trick);
* the tool-call loop (real tool calls *and* the pseudo tool calls
  deepseek-v4-flash sometimes emits inline as ``<invoke>...</invoke>``
  blocks);
* destructive-action detection (delegated to a
  :class:`agent.tool_registry.ToolRegistry`);
* confirmation-marker parsing (delegated to :mod:`agent.confirm`);
* the rotating tool-call log and the self-improvement log, both stored
  under ``core.config.DATA``;
* the singleton factory + DeepSeek fallback that the original
  ``get_agent_with_fallback`` exposed.

What it does NOT own
--------------------
* The tool implementations themselves.  Those live in the
  ``domains/`` packages and are registered into a :class:`ToolRegistry`
  by the application entry point.  The agent core is intentionally
  domain-agnostic.

Public surface
--------------
* :class:`LLMAgent` — main class, the chat agent.
* :func:`get_agent` — singleton getter (reads API keys from env via
  :mod:`core.config`).
* :func:`get_agent_with_fallback` — like ``get_agent`` but with a
  DeepSeek runtime fallback when the primary endpoint is down.
* :func:`build_agent` — convenience that builds a fully configured
  :class:`LLMAgent` from a :class:`ToolRegistry`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from agent.prompts import (
    CONFIRM_MARKER,
    SELF_IMPROVEMENT,
    SYSTEM_PROMPT,
    TOOL_CALL_LOG,
)
from agent.tool_registry import ToolRegistry
from core.config import DATA, DEEPSEEK_KEY, ENV_PATH

log = logging.getLogger("agent.agent")


# --------------------------------------------------------------------------- #
# API config
# --------------------------------------------------------------------------- #
DEFAULT_BASE_URL: str = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL: str = "deepseek-v4-flash"
DEFAULT_MAX_TOKENS: int = 4096

# Fallback endpoint used when the primary one (OpenCode Go) is down.
DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL: str = "deepseek-v4-flash"

# Where to read the primary API key from in the environment / .env file.
PRIMARY_KEY_ENV: str = "OPENCODE_GO_API_KEY"
FALLBACK_KEY_ENV: str = "DEEPSEEK_API_KEY"


# --------------------------------------------------------------------------- #
# Secret loading
# --------------------------------------------------------------------------- #
def _load_primary_api_key() -> str:
    """Load the primary (OpenCode Go) API key.

    Tries, in order:

    1. The ``$OPENCODE_GO_API_KEY`` environment variable.
    2. The key in ``$HERMES_HOME/.env`` (path resolved by
       :mod:`core.config`).

    Returns an empty string if neither source is set.
    """
    key = os.environ.get(PRIMARY_KEY_ENV, "").strip()
    if key:
        return key

    if ENV_PATH.exists():
        try:
            for ln in ENV_PATH.read_text().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    if k.strip() == PRIMARY_KEY_ENV:
                        return v.strip()
        except Exception:
            pass

    return ""


def _load_fallback_api_key() -> str:
    """Load the fallback (DeepSeek) API key.

    Tries, in order:

    1. The ``$DEEPSEEK_API_KEY`` environment variable.
    2. The :data:`core.config.DEEPSEEK_KEY` constant (which is itself
       loaded from a base64-encoded file under ``DATA/`` or from the
       environment — see :mod:`core.config`).
    3. The key in ``$HERMES_HOME/.env``.
    """
    key = os.environ.get(FALLBACK_KEY_ENV, "").strip()
    if key:
        return key
    if DEEPSEEK_KEY:
        return DEEPSEEK_KEY
    if ENV_PATH.exists():
        try:
            for ln in ENV_PATH.read_text().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    if k.strip() == FALLBACK_KEY_ENV:
                        return v.strip()
        except Exception:
            pass
    return ""


# --------------------------------------------------------------------------- #
# Agent
# --------------------------------------------------------------------------- #
class LLMAgent:
    """LLM chat agent with tool calling and destructive-action guarding.

    The agent takes a :class:`ToolRegistry` so it stays decoupled from
    the domain tools.  The application entry point is expected to
    register every domain tool into the registry before instantiating
    the agent (or by calling :func:`build_agent`).
    """

    def __init__(
        self,
        api_key: str,
        registry: ToolRegistry,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        if not api_key:
            raise ValueError("LLMAgent: api_key is required")
        if registry is None:
            raise ValueError("LLMAgent: registry is required")

        self.api_key = api_key
        self.registry = registry
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self._turn_counter = 0
        self._tool_definitions: list[dict] = registry.get_tool_definitions()

    # ------------------------------------------------------------------ #
    # API plumbing
    # ------------------------------------------------------------------ #
    def _call_api(
        self,
        messages: list,
        tools: list | None = None,
        tool_choice: str = "auto",
        max_retries: int = 3,
    ) -> dict:
        """Call the LLM API (OpenAI-compatible) with exponential backoff retry."""
        url = f"{self.base_url}/chat/completions"
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": 0.7,
            # DeepSeek V4 defaults to thinking mode ENABLED. With tool calls
            # this requires passing reasoning_content back in every subsequent
            # request, which breaks our stateless message history. Disable it.
            "thinking": {"type": "disabled"},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        data = json.dumps(payload).encode()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "HermesAgent/1.0",
        }

        last_error = None
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                resp = urllib.request.urlopen(req, timeout=60)
                return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                err_text = e.read().decode()
                # Don't retry on client errors (4xx)
                if 400 <= e.code < 500:
                    log.error(f"LLM API error {e.code}: {err_text[:500]}")
                    raise RuntimeError(f"API error {e.code}: {err_text[:300]}")
                last_error = e
                log.warning(
                    f"LLM API error {e.code} (attempt {attempt + 1}/{max_retries}): {err_text[:200]}"
                )
            except (urllib.error.URLError, OSError, ConnectionError, TimeoutError) as e:
                last_error = e
                log.warning(
                    f"LLM API connection error (attempt {attempt + 1}/{max_retries}): {e}"
                )

            if attempt < max_retries - 1:
                delay = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(delay)

        # All retries exhausted
        if isinstance(last_error, urllib.error.HTTPError):
            raise RuntimeError(
                f"API error after {max_retries} retries: {last_error.code}"
            )
        log.error(f"LLM API call failed after {max_retries} retries: {last_error}")
        raise RuntimeError(
            f"Error de conexión tras {max_retries} intentos: {last_error}"
        )

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def process_message(self, user_text: str, history: list | None = None) -> dict:
        """
        Process a user message.

        Returns
        -------
        dict
            ``{"response": str, "history": list, "confirmation": dict | None}``

            * ``response`` — text to send to the user.
            * ``history``  — updated conversation history.
            * ``confirmation`` — if the agent needs the user to confirm
              a destructive action, this is
              ``{"action": <tool_name>, "params": {...}, "message": <full_text>}``
              and the application is expected to show Confirm/Cancel
              buttons and call :func:`agent.confirm.execute_confirmed`
              on confirm.
        """
        if history is None:
            history = []

        # ── Inject self-improvement lessons ──
        lessons_text = self._load_lessons_text()

        system = SYSTEM_PROMPT
        if lessons_text:
            system += (
                "\n\n## LECCIONES APRENDIDAS (errores previos — no repetir)\n\n"
                + lessons_text
            )

        # ── Compact history if too long ──
        MAX_HISTORY_ENTRIES = 10  # 5 exchanges
        if len(history) > MAX_HISTORY_ENTRIES:
            history = self._compact_history(history, MAX_HISTORY_ENTRIES)

        self._turn_counter += 1

        # Strip reasoning_content from history entries (OpenCode Go may
        # inject it, and DeepSeek API requires it to be passed back if
        # present — we don't track it).
        clean_history = [
            {k: v for k, v in entry.items() if k != "reasoning_content"}
            for entry in history
        ]

        messages = [
            {"role": "system", "content": system},
            *clean_history,
            {"role": "user", "content": user_text},
        ]

        # Call LLM with tools — fall back to DeepSeek if OpenCode Go fails
        try:
            response = self._call_api(messages, tools=self._tool_definitions)
        except RuntimeError as e:
            if "opencode" in self.base_url:
                log.warning(f"OpenCode Go failed, switching to DeepSeek: {e}")
                self.base_url = DEEPSEEK_BASE_URL
                self.model = DEEPSEEK_MODEL
                response = self._call_api(messages, tools=self._tool_definitions)
            else:
                raise

        choice = response["choices"][0]
        msg = choice["message"]
        # Strip reasoning_content from response (OpenCode Go may inject it)
        msg.pop("reasoning_content", None)

        # Handle tool calls
        if msg.get("tool_calls"):
            return self._handle_tool_calls(msg, messages, history, user_text)

        # Plain text response
        content = msg.get("content", "") or ""
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": content})
        content, _ = self._extract_pseudo_tool_calls(content)

        # Save tool call log (empty for non-tool responses)
        self._save_tool_call_log(user_text, [], content)
        # Check for self-improvement
        self._check_self_improvement(user_text, history, content)

        # Check for confirmation marker in response
        from agent.confirm import parse_confirm  # local import to avoid cycle

        confirm = parse_confirm(content)
        if confirm:
            confirm["message"] = content
            return {"response": content, "history": history, "confirmation": confirm}

        return {"response": content, "history": history, "confirmation": None}

    # ------------------------------------------------------------------ #
    # Tool-call loop
    # ------------------------------------------------------------------ #
    def _handle_tool_calls(
        self,
        msg: dict,
        messages: list,
        history: list,
        user_text: str,
    ) -> dict:
        """Execute tool calls in a loop + parse pseudo-tool-calls from content."""
        MAX_TOOL_TURNS = 10
        all_tool_calls: list = []
        tool_calls = msg.get("tool_calls", [])
        content = msg.get("content", "") or ""

        for turn in range(MAX_TOOL_TURNS):
            assistant_msg: dict = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
                all_tool_calls.extend(tool_calls)
            messages.append(assistant_msg)

            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    fn_args = {}

                if self.registry.is_destructive(fn_name):
                    # Plain text (no markdown) — tool names may contain
                    # underscores that break Telegram's Markdown parser.
                    confirm_text = (
                        f"⚠️ Acción: {fn_name}\n\n"
                        f"Esta acción requiere tu confirmación.\n"
                        f"Usa los botones de abajo para confirmar o cancelar."
                    )
                    messages.append({
                        "role": "tool", "tool_call_id": tc["id"],
                        "content": json.dumps({
                            "status": "rejected",
                            "reason": "destructive_action_needs_confirmation",
                            "message": "Esta acción requiere confirmación del usuario.",
                        }),
                    })
                    history.append({"role": "user", "content": user_text})
                    history.append({"role": "assistant", "content": confirm_text})
                    self._save_tool_call_log(user_text, all_tool_calls, confirm_text)
                    self._check_self_improvement(user_text, history, confirm_text)
                    return {
                        "response": confirm_text, "history": history,
                        "confirmation": {
                            "action": fn_name, "params": fn_args,
                            "message": confirm_text,
                        },
                    }

                result = self._execute_tool(fn_name, fn_args)
                result_str = json.dumps(result, ensure_ascii=False)
                messages.append({
                    "role": "tool", "tool_call_id": tc["id"],
                    "content": result_str,
                })

            final = self._call_api(messages)
            next_msg = final["choices"][0]["message"]
            next_msg.pop("reasoning_content", None)  # avoid history poisoning
            tool_calls = next_msg.get("tool_calls", [])
            content = next_msg.get("content", "") or ""

            # Parse pseudo-tool-calls if no real ones
            if not tool_calls and content:
                cleaned, pseudo = self._extract_pseudo_tool_calls(content)
                if pseudo:
                    assistant_msg = {
                        "role": "assistant", "content": cleaned,
                        "tool_calls": pseudo,
                    }
                    messages.append(assistant_msg)
                    all_tool_calls.extend(pseudo)
                    for tc in pseudo:
                        fn_name = tc["function"]["name"]
                        try:
                            fn_args = json.loads(tc["function"]["arguments"])
                        except json.JSONDecodeError:
                            fn_args = {}
                        result = self._execute_tool(fn_name, fn_args)
                        result_str = json.dumps(result, ensure_ascii=False)
                        messages.append({
                            "role": "tool", "tool_call_id": tc["id"],
                            "content": result_str,
                        })
                    tool_calls = pseudo
                    content = cleaned
                    continue

            if not tool_calls:
                break

        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": content})
        content, _ = self._extract_pseudo_tool_calls(content)

        self._save_tool_call_log(user_text, all_tool_calls, content)
        self._check_self_improvement(user_text, history, content)

        from agent.confirm import parse_confirm  # local import to avoid cycle

        confirm = parse_confirm(content)
        if confirm:
            confirm["message"] = content
            return {"response": content, "history": history, "confirmation": confirm}
        return {"response": content, "history": history, "confirmation": None}

    # ------------------------------------------------------------------ #
    # Tool dispatch
    # ------------------------------------------------------------------ #
    def _execute_tool(self, fn_name: str, fn_args: dict) -> dict:
        """Execute a tool function by name through the registry."""
        fn = self.registry.get(fn_name)
        if not fn:
            return {"status": "error", "message": f"Tool '{fn_name}' no encontrada"}
        try:
            return fn(**(fn_args or {}))
        except TypeError as e:
            log.error(f"_execute_tool: bad args for {fn_name}: {e}")
            return {"status": "error", "message": f"Argumentos inválidos: {e}"}
        except Exception as e:
            log.error(f"_execute_tool: {fn_name} raised: {e}")
            return {"status": "error", "message": str(e)}

    def execute_confirmed_action(self, action: str, params: dict) -> str:
        """Execute a confirmed destructive action via the registry.

        Returns a human-readable string for the user (same format as
        :func:`agent.confirm.execute_confirmed`).
        """
        from agent.confirm import execute_confirmed

        return execute_confirmed(action, params, self.registry)

    # ------------------------------------------------------------------ #
    # Pseudo tool calls (deepseek-v4-flash quirk)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_pseudo_tool_calls(text: str):
        """Parse pseudo-tool-call blocks that deepseek-v4-flash emits in content.

        Returns ``(cleaned_text, tool_calls_list)`` where ``tool_calls_list``
        is a list of proper tool-call dicts suitable for feeding back
        into the loop.
        """
        tool_calls = []
        call_id_counter = 0

        invoke_re = re.compile(
            r'<\s*[^>]*?\b(invoke)\b[^>]*>'
            r'(.*?)'
            r'<\s*/\s*[^>]*?\b(invoke)\b[^>]*>',
            re.DOTALL,
        )

        def _replace_invoke(m):
            nonlocal call_id_counter
            inner = m.group(2)
            name_m = re.search(r'''name\s*=\s*["']([^"']+)["']''', m.group(0))
            if not name_m:
                return ""
            tool_name = name_m.group(1)
            params = {}
            for pm in re.finditer(
                r'<\s*[^>]*?\b(parameter)\b[^>]*>\s*(.*?)\s*'
                r'<\s*/\s*[^>]*?\b(parameter)\b[^>]*>',
                inner, re.DOTALL,
            ):
                param_name_m = re.search(r'''name\s*=\s*["']([^"']+)["']''', pm.group(0))
                if param_name_m:
                    value_raw = pm.group(2).strip()
                    string_m = re.search(
                        r'''string\s*=\s*["'](true|false)["']''', pm.group(0)
                    )
                    if string_m and string_m.group(1) == "false":
                        try:
                            params[param_name_m.group(1)] = json.loads(value_raw)
                        except (json.JSONDecodeError, ValueError):
                            params[param_name_m.group(1)] = value_raw
                    else:
                        params[param_name_m.group(1)] = value_raw
            call_id = f"pseudo_{call_id_counter}"
            call_id_counter += 1
            tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(params, ensure_ascii=False),
                },
            })
            return ""

        cleaned = invoke_re.sub(_replace_invoke, text)
        cleaned = re.sub(
            r'^\s*<[^>]*\b(parameter|tool_calls)\b[^>]*>.*$', '', cleaned, flags=re.MULTILINE
        )
        cleaned = re.sub(
            r'^\s*</[^>]*\b(parameter|tool_calls)\b[^>]*>\s*$', '', cleaned, flags=re.MULTILINE
        )
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip(), tool_calls

    # ------------------------------------------------------------------ #
    # History compaction
    # ------------------------------------------------------------------ #
    def _compact_history(self, history: list, max_entries: int) -> list:
        """Summarise old turns and keep only the last ``max_entries``.

        On any error (summary call failure, malformed response), falls
        back to a plain truncation.
        """
        old_part = history[:-max_entries]
        old_texts = []
        for entry in old_part:
            if entry.get("content"):
                role = "Usuario" if entry["role"] == "user" else "Asistente"
                old_texts.append(f"{role}: {entry['content'][:300]}")

        if not old_texts:
            return history[-max_entries:]

        summary_prompt = (
            "Resume en 2-3 líneas lo ocurrido en esta conversación, "
            "extrayendo solo decisiones tomadas, datos relevantes y órdenes ejecutadas. "
            "Ignora saludos y cortesías.\n\n"
            + "\n".join(old_texts)
        )
        try:
            summary_resp = self._call_api([{"role": "user", "content": summary_prompt}])
            summary_msg = summary_resp["choices"][0]["message"]
            summary_msg.pop("reasoning_content", None)
            summary_text = summary_msg.get("content", "").strip()
            return (
                [{"role": "system",
                  "content": f"[⏳ RESUMEN DE INTERACCIONES ANTERIORES] {summary_text} — Esto ya ocurrió, no actuar sobre esto."}]
                + history[-max_entries:]
            )
        except Exception:
            return history[-max_entries:]

    # ------------------------------------------------------------------ #
    # Self-improvement log
    # ------------------------------------------------------------------ #
    def _load_lessons_text(self) -> str:
        """Return the self-improvement lessons as a single string, or ""."""
        path = Path(SELF_IMPROVEMENT)
        if not path.exists():
            return ""
        try:
            lessons = [
                line.strip()
                for line in path.read_text().splitlines()
                if line.strip().startswith("- **")
            ]
            if lessons and len(lessons) <= 15:
                return "\n".join(lessons)
        except Exception:
            pass
        return ""

    def _check_self_improvement(
        self,
        user_text: str,
        history: list,
        response: str,
    ) -> None:
        """Check if the user just corrected the bot and extract a lesson."""
        correction_keywords = [
            "no", "mal", "error", "quería decir", "eso no",
            "corrige", "rectifica", "al revés", "al reves",
            "equivocado", "incorrecto", "no es así",
        ]
        text_lower = (user_text or "").lower()
        is_correction = any(kw in text_lower for kw in correction_keywords)
        if not is_correction:
            return

        try:
            context_parts = []
            recent = history[-4:] if len(history) >= 4 else history
            for entry in recent:
                role = "Usuario" if entry["role"] == "user" else "Asistente"
                context_parts.append(
                    f"{role}: {entry.get('content', '')[:200]}"
                )

            tool_log = []
            path = Path(TOOL_CALL_LOG)
            if path.exists():
                try:
                    tool_log = json.loads(path.read_text())
                except Exception:
                    pass

            tool_context = ""
            if tool_log:
                tool_context = "\nTool calls recientes:\n"
                for entry in tool_log[-2:]:
                    for tc in entry.get("tool_calls", []):
                        tool_context += (
                            f"  - {tc['name']}"
                            f"({json.dumps(tc.get('args', {}), ensure_ascii=False)})\n"
                        )

            analysis_prompt = (
                "## Análisis de corrección\n"
                f"El usuario corrigió al bot.\n\n"
                f"Corrección del usuario: {user_text[:300]}\n\n"
                f"Contexto de conversación:\n"
                + "\n".join(context_parts) + "\n"
                + tool_context + "\n"
                "## Instrucción\n"
                "Analiza qué aprendiste de este error. Extrae una lección accionable en 1-2 líneas.\n"
                "Responde SOLO con la lección, en el formato:\n"
                "__LEARN__: <descripción de la lección>"
            )

            result = self._call_api([{"role": "user", "content": analysis_prompt}])
            content = result["choices"][0]["message"]["content"].strip()

            if "__LEARN__" in content:
                lesson = content.split("__LEARN__:", 1)[1].strip()
                lesson = lesson.split("```")[0].strip().strip('"').strip("'")
                if lesson:
                    self._add_lesson(lesson)
        except Exception as e:
            log.warning(f"Self-improvement check failed: {e}")

    def _add_lesson(self, lesson: str) -> None:
        """Add a lesson to ``self_improvement.md``, dedup and enforce limit."""
        try:
            path = Path(SELF_IMPROVEMENT)
            today = date.today().isoformat()
            new_entry = f"- **{today}**: {lesson}"

            lines = []
            if path.exists():
                lines = path.read_text().splitlines()

            # Dedup: if the same lesson text already exists, just update date
            for i, line in enumerate(lines):
                if ": " in line and lesson[:40] in line:
                    lines[i] = new_entry
                    path.write_text("\n".join(lines))
                    return

            # Remove header lines, keep only entries
            entries = [l for l in lines if l.strip().startswith("- **")]

            # Add new entry
            entries.append(new_entry)

            # Enforce max 15
            if len(entries) > 15:
                entries = entries[-15:]

            # Rebuild file
            content = "# 💾 Self-Improvement Log\n\n"
            content += f"Última actualización: {today}\n\n"
            content += (
                "<!-- Máximo 15 entradas. Al llegar al límite, consolidar "
                "similares o eliminar la más vieja. -->\n\n"
            )
            content += "\n".join(entries) + "\n"

            path.write_text(content)
            log.info(f"Learned: {lesson}")
        except Exception as e:
            log.warning(f"Failed to add lesson: {e}")

    # ------------------------------------------------------------------ #
    # Tool call log
    # ------------------------------------------------------------------ #
    def _save_tool_call_log(
        self,
        user_text: str,
        tool_calls: list,
        final_response: str,
    ) -> None:
        """Save tool-call info to a rotating buffer (max 3 entries)."""
        try:
            tool_info = []
            for tc in tool_calls:
                try:
                    args = json.loads(tc["function"]["arguments"])
                except Exception:
                    args = {}
                tool_info.append({
                    "name": tc["function"]["name"],
                    "args": args,
                })

            entry = {
                "turn_id": self._turn_counter,
                "user_message": (user_text or "")[:200],
                "tool_calls": tool_info,
                "final_response": (final_response or "")[:300],
                "timestamp": datetime.now().isoformat(),
            }

            path = Path(TOOL_CALL_LOG)
            log_data = []
            if path.exists():
                try:
                    log_data = json.loads(path.read_text())
                except Exception:
                    log_data = []

            log_data.append(entry)
            if len(log_data) > 3:
                log_data = log_data[-3:]

            path.write_text(json.dumps(log_data, indent=2, ensure_ascii=False))
        except Exception as e:
            log.warning(f"Failed to save tool call log: {e}")


# --------------------------------------------------------------------------- #
# Factory / singleton
# --------------------------------------------------------------------------- #
_agent_instance: Optional[LLMAgent] = None


def build_agent(
    registry: ToolRegistry,
    api_key: Optional[str] = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> LLMAgent:
    """Build a fresh :class:`LLMAgent` instance (does not touch the singleton).

    If ``api_key`` is omitted, the primary key is loaded from the
    environment via :func:`_load_primary_api_key`.  Raises
    :class:`ValueError` if no key is available.
    """
    if not api_key:
        api_key = _load_primary_api_key()
    if not api_key:
        raise ValueError("No API key found for LLM agent")
    return LLMAgent(
        api_key=api_key,
        registry=registry,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
    )


def get_agent(registry: ToolRegistry) -> LLMAgent:
    """Get or create the singleton LLM agent.

    Tries the primary (OpenCode Go) key first; if that is unavailable
    but the fallback (DeepSeek) key is set, returns an agent pointed
    at the DeepSeek endpoint.
    """
    global _agent_instance
    if _agent_instance is not None:
        return _agent_instance

    primary = _load_primary_api_key()
    if primary:
        _agent_instance = LLMAgent(api_key=primary, registry=registry)
        return _agent_instance

    fallback = _load_fallback_api_key()
    if fallback:
        log.warning("Using DeepSeek API key instead of OpenCode Go")
        _agent_instance = LLMAgent(
            api_key=fallback,
            registry=registry,
            base_url=DEEPSEEK_BASE_URL,
            model=DEEPSEEK_MODEL,
        )
        return _agent_instance

    raise ValueError("No API key found for LLM agent")


def get_agent_with_fallback(registry: ToolRegistry) -> LLMAgent:
    """Get the singleton, with a DeepSeek fallback when the primary is down.

    Behavioural notes:

    * If the singleton already exists, it is returned as-is.
    * Otherwise the primary key is attempted first; if that fails, the
      DeepSeek key is attempted as a last resort.
    * The runtime fallback inside :meth:`LLMAgent.process_message` (which
      swaps ``base_url`` mid-flight when an OpenCode Go call returns a
      5xx) is preserved.
    """
    global _agent_instance
    if _agent_instance is not None:
        return _agent_instance
    try:
        return get_agent(registry)
    except (RuntimeError, ValueError):
        pass

    fallback = _load_fallback_api_key()
    if fallback:
        log.warning("Fallback: using DeepSeek API key")
        _agent_instance = LLMAgent(
            api_key=fallback,
            registry=registry,
            base_url=DEEPSEEK_BASE_URL,
            model=DEEPSEEK_MODEL,
        )
        return _agent_instance

    raise ValueError("No API key available for LLM agent")


def reset_agent() -> None:
    """Drop the cached singleton (mainly for tests)."""
    global _agent_instance
    _agent_instance = None


# Backwards-compat alias — the original monolith exposed a top-level
# ``execute_confirmed`` function.  The new home for that logic is
# :mod:`agent.confirm`; this thin wrapper keeps the old call sites
# working.
def execute_confirmed(
    tool_name: str,
    tool_args: dict,
    registry: Optional[ToolRegistry] = None,
) -> str:
    """Module-level shim that delegates to :func:`agent.confirm.execute_confirmed`.

    ``registry`` is required; passing ``None`` is a programming error
    because the old module-level ``SAFE_TOOL_MAP`` no longer exists.
    """
    from agent.confirm import execute_confirmed as _execute_confirmed

    if registry is None:
        raise ValueError(
            "execute_confirmed: a ToolRegistry is required "
            "(SAFE_TOOL_MAP/DESTRUCTIVE_TOOLS globals have been removed)"
        )
    return _execute_confirmed(tool_name, tool_args, registry)


__all__ = [
    "LLMAgent",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_MAX_TOKENS",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "PRIMARY_KEY_ENV",
    "FALLBACK_KEY_ENV",
    "build_agent",
    "get_agent",
    "get_agent_with_fallback",
    "reset_agent",
    "execute_confirmed",
]

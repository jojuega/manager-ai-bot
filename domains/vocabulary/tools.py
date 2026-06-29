"""
domains.vocabulary.tools — LLM-callable tools for the vocabulary domain.

Extracted from the original monolith's `scripts/llm_agent.py`
(:func:`_run_srs`, :func:`tool_vocab_*`, :func:`tool_deck_*`, their
``TOOL_DEFINITIONS`` entries, and the slice of :data:`SAFE_TOOL_MAP` /
:data:`DESTRUCTIVE_TOOLS` that mentions them).

Public surface
--------------
* :func:`register` — register all vocabulary tools into a
  :class:`agent.tool_registry.ToolRegistry`.  Returns the list of tool
  names that were registered.
* :data:`DESTRUCTIVE_TOOLS` — set of tool names that should be routed
  through the destructive-action confirmation flow.
* :data:`TOOL_DEFINITIONS` — list of OpenAI-compatible tool schemas.

Tool behaviour
--------------
The original monolith wrapped srs.py as a subprocess (``_run_srs``).  In
the new module layout we call the in-process :mod:`storage` API directly
and translate :class:`storage.SrsError` to the same ``{"status": "error",
"message": ...}`` shape the LLM expects.
"""
from __future__ import annotations

import sqlite3

from domains.vocabulary import storage
from domains.vocabulary.storage import (
    SrsError,
    add_word,
    create_deck,
    delete_deck,
    get_due_words,
    get_stats,
    list_decks_flat,
    move_deck,
    rename_deck,
)

# Same path the original ``llm_agent._get_srs_conn`` used.
from core.config import SRS_DB


# ==============================================================================
# BACKING DB CONNECTION (for tools that need a raw connection)
# ==============================================================================
def _get_srs_conn():
    """Open a connection to the SRS database. Used by tools that need raw SQL."""
    SRS_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(SRS_DB))
    conn.row_factory = sqlite3.Row
    return conn


# ==============================================================================
# TOOL IMPLEMENTATIONS
# ==============================================================================
def tool_vocab_add(word: str, lang: str, sentence: str = "",
                   source: str = "", definition: str = "",
                   deck_id: int = None) -> dict:
    """Add a new vocabulary word for SRS."""
    try:
        return add_word(
            word=word,
            lang=lang,
            sentence=sentence,
            source=source,
            definition=definition,
            deck_id=deck_id if deck_id is not None else 1,
        )
    except SrsError as e:
        return {"status": "error", "message": e.error_dict.get("error", str(e))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_vocab_list() -> dict:
    """List all vocabulary words (recently added first)."""
    return {"words": storage.list_words()}


def tool_vocab_stats() -> dict:
    """Get vocabulary SRS statistics."""
    return get_stats()


def tool_vocab_delete(word_id: int) -> dict:
    """Delete a vocabulary word. DESTRUCTIVE."""
    try:
        conn = _get_srs_conn()
        row = conn.execute(
            "SELECT word, lang FROM words WHERE id=?", (word_id,)
        ).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "message": f"Palabra #{word_id} no encontrada"}
        word_text = row["word"]
        conn.execute("DELETE FROM reviews WHERE word_id=?", (word_id,))
        conn.execute("DELETE FROM words WHERE id=?", (word_id,))
        conn.commit()
        conn.close()
        storage._invalidate_srs_caches()
        return {
            "status": "ok",
            "message": f"🗑️ Palabra eliminada: '{word_text}' (#{word_id})",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_vocab_edit(word_id: int, word: str = None, lang: str = None,
                    sentence: str = None, source: str = None,
                    definition: str = None) -> dict:
    """Edit a vocabulary word's details."""
    try:
        conn = _get_srs_conn()
        row = conn.execute("SELECT * FROM words WHERE id=?", (word_id,)).fetchone()
        if not row:
            conn.close()
            return {"status": "error", "message": f"Palabra #{word_id} no encontrada"}
        updates = []
        params = []
        if word is not None:
            updates.append("word=?")
            params.append(word)
        if lang is not None:
            if lang not in ("de", "en"):
                conn.close()
                return {"status": "error", "message": "Idioma debe ser 'de' o 'en'"}
            updates.append("lang=?")
            params.append(lang)
        if sentence is not None:
            updates.append("sentence=?")
            params.append(sentence)
        if source is not None:
            updates.append("source=?")
            params.append(source)
        if definition is not None:
            updates.append("definition=?")
            params.append(definition)
        if not updates:
            conn.close()
            return {"status": "error", "message": "Nada que editar"}
        params.append(word_id)
        conn.execute(
            f"UPDATE words SET {', '.join(updates)} WHERE id=?", params
        )
        conn.commit()
        conn.close()
        old_word = row["word"]
        return {
            "status": "ok",
            "message": f"✏️ Palabra '{old_word}' (#{word_id}) actualizada",
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# DECK TOOLS
# ==============================================================================
def tool_deck_create(name: str, emoji: str = "📁",
                     parent_id: int = None) -> dict:
    """Create a new vocabulary deck. Max 3 levels of depth."""
    try:
        return create_deck(name=name, emoji=emoji, parent_id=parent_id)
    except SrsError as e:
        return {"status": "error", "message": e.error_dict.get("error", str(e))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_deck_list() -> dict:
    """List all decks as a tree structure."""
    return {"tree": storage.get_deck_tree()}


def tool_deck_delete(deck_id: int) -> dict:
    """Delete a deck. Only if it has no children and no words. DESTRUCTIVE."""
    try:
        result = delete_deck(deck_id)
        storage._invalidate_srs_caches()
        return result
    except SrsError as e:
        return {"status": "error", "message": e.error_dict.get("error", str(e))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_deck_rename(deck_id: int, name: str = None,
                     emoji: str = None) -> dict:
    """Rename a deck or change its emoji."""
    try:
        result = rename_deck(deck_id, name=name, emoji=emoji)
        storage._invalidate_srs_caches()
        return result
    except SrsError as e:
        return {"status": "error", "message": e.error_dict.get("error", str(e))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_deck_move(deck_id: int, parent_id: int,
                   sort_order: int = None) -> dict:
    """Move a deck under a different parent."""
    try:
        result = move_deck(deck_id, parent_id, sort_order=sort_order)
        storage._invalidate_srs_caches()
        return result
    except SrsError as e:
        return {"status": "error", "message": e.error_dict.get("error", str(e))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# TOOL DEFINITIONS (OpenAI / DeepSeek function-calling format)
# ==============================================================================
TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "vocab_add",
            "description": "Añade una palabra nueva al sistema SRS de vocabulario",
            "parameters": {
                "type": "object",
                "properties": {
                    "word": {"type": "string", "description": "La palabra"},
                    "lang": {"type": "string", "enum": ["de", "en"],
                             "description": "Idioma: de=alemán, en=inglés"},
                    "sentence": {"type": "string",
                                 "description": "Frase de ejemplo (opcional)"},
                    "source": {"type": "string",
                               "description": "Fuente (opcional, ej: Tagesschau)"},
                    "definition": {"type": "string",
                                   "description": "Definición en español (opcional)"},
                    "deck_id": {"type": "integer",
                                "description": ("ID del deck donde guardar la "
                                                "palabra, omite para usar "
                                                "Palabra del Día (id=1)")},
                },
                "required": ["word", "lang"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vocab_list",
            "description": "Lista todas las palabras de vocabulario con su estado SRS",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vocab_stats",
            "description": ("Muestra estadísticas del sistema SRS de "
                            "vocabulario, incluye stats por deck"),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vocab_edit",
            "description": ("Edita los detalles de una palabra de vocabulario "
                            "(palabra, idioma, frase, fuente, definición)"),
            "parameters": {
                "type": "object",
                "properties": {
                    "word_id": {"type": "integer", "description": "ID de la palabra"},
                    "word": {"type": "string",
                             "description": "Nuevo texto de la palabra (opcional)"},
                    "lang": {"type": "string", "enum": ["de", "en"],
                             "description": "Nuevo idioma (opcional)"},
                    "sentence": {"type": "string",
                                 "description": "Nueva frase de ejemplo (opcional)"},
                    "source": {"type": "string",
                               "description": "Nueva fuente (opcional)"},
                    "definition": {"type": "string",
                                   "description": "Nueva definición (opcional)"},
                },
                "required": ["word_id"],
            },
        },
    },
    # ── Decks (vocab hierarchy) ──
    {
        "type": "function",
        "function": {
            "name": "deck_create",
            "description": "Crea un nuevo deck de vocabulario. max 3 niveles de profundidad.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nombre del deck"},
                    "emoji": {"type": "string", "description": "Emoji opcional"},
                    "parent_id": {"type": "integer",
                                  "description": ("ID del deck padre "
                                                  "(opcional, omite para raíz)")},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deck_list",
            "description": "Lista todos los decks en estructura de árbol",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deck_delete",
            "description": "Elimina un deck. Solo si está vacío (sin hijos ni palabras).",
            "parameters": {
                "type": "object",
                "properties": {
                    "deck_id": {"type": "integer", "description": "ID del deck a eliminar"},
                },
                "required": ["deck_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deck_rename",
            "description": "Renombra un deck o cambia su emoji",
            "parameters": {
                "type": "object",
                "properties": {
                    "deck_id": {"type": "integer", "description": "ID del deck"},
                    "name": {"type": "string",
                             "description": "Nuevo nombre (opcional)"},
                    "emoji": {"type": "string",
                              "description": "Nuevo emoji (opcional)"},
                },
                "required": ["deck_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deck_move",
            "description": ("Mueve un deck bajo otro padre. "
                            "No permite ciclos ni exceder max depth 3."),
            "parameters": {
                "type": "object",
                "properties": {
                    "deck_id": {"type": "integer", "description": "ID del deck a mover"},
                    "parent_id": {"type": "integer",
                                  "description": "ID del nuevo deck padre"},
                    "sort_order": {"type": "integer", "description": "Orden (opcional)"},
                },
                "required": ["deck_id", "parent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vocab_delete",
            "description": ("⚠️ Elimina una palabra de vocabulario. "
                            "Borra también todas sus reviews. "
                            "Requiere confirmación del usuario."),
            "parameters": {
                "type": "object",
                "properties": {
                    "word_id": {"type": "integer", "description": "ID de la palabra a eliminar"},
                },
                "required": ["word_id"],
            },
        },
    },
]


# ==============================================================================
# DESTRUCTIVE TOOLS
# ==============================================================================
# Subset of the monolith's DESTRUCTIVE_TOOLS that lives in this domain. The
# main :mod:`agent` module merges all domains' destructive sets at startup.
DESTRUCTIVE_TOOLS: set[str] = {"vocab_delete", "deck_delete"}


# ==============================================================================
# TOOL → CALLABLE MAP
# ==============================================================================
TOOL_FUNCTIONS: dict[str, callable] = {
    "vocab_add": tool_vocab_add,
    "vocab_list": tool_vocab_list,
    "vocab_stats": tool_vocab_stats,
    "vocab_edit": tool_vocab_edit,
    "vocab_delete": tool_vocab_delete,
    "deck_create": tool_deck_create,
    "deck_list": tool_deck_list,
    "deck_rename": tool_deck_rename,
    "deck_move": tool_deck_move,
    "deck_delete": tool_deck_delete,
}


# ==============================================================================
# REGISTRATION
# ==============================================================================
def register(registry) -> list[str]:
    """Register every vocabulary tool into ``registry``.

    Parameters
    ----------
    registry:
        An :class:`agent.tool_registry.ToolRegistry` instance.

    Returns
    -------
    list[str]
        The names of the tools that were registered (in registration order).
    """
    registered: list[str] = []
    for tool_def in TOOL_DEFINITIONS:
        name = tool_def["function"]["name"]
        fn = TOOL_FUNCTIONS.get(name)
        if fn is None:
            raise RuntimeError(
                f"tools.register: missing callable for tool {name!r}"
            )
        registry.add(
            name=name,
            fn=fn,
            schema=tool_def,
            destructive=name in DESTRUCTIVE_TOOLS,
        )
        registered.append(name)
    return registered

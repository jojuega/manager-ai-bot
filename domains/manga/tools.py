"""
domains.manga.tools — LLM-callable tools for the manga domain.

Extracted from the original monolith's ``llm_agent.py``
(:func:`tool_manga_*` and the ``SAFE_TOOL_MAP`` / ``DESTRUCTIVE_TOOLS``
entries that mentioned them). Each tool wraps the pure-Python storage
API in :mod:`domains.manga.storage` and returns the same JSON shape
the LLM agent expects.

Public surface
--------------
* :func:`register` — register all ``manga_*`` callables on a
  :class:`agent.tool_registry.ToolRegistry`.
* :data:`TOOL_DISPATCH` — ``tool-name → callable`` mapping.
* :data:`TOOL_SCHEMAS` — OpenAI/DeepSeek tool schemas (one per callable).
* :data:`DESTRUCTIVE_TOOLS` — tool names that need user confirmation.
"""
from __future__ import annotations

import logging

from . import storage
from .storage import (
    SrsError,
    create_manga_serie,
    create_manga_volume,
    delete_manga_card,
    get_manga_card,
    get_manga_cards_in_deck_tree,
    get_manga_deck_hierarchy,
    get_manga_serie_by_name,
    update_manga_card,
)

log = logging.getLogger("manga.tools")


# ==============================================================================
# TOOL IMPLEMENTATIONS
# ==============================================================================
def tool_manga_card_list(deck_name: str = None, language: str = None,
                         limit: int = 20) -> dict:
    """List manga cards, optionally filtered by deck and/or language.

    ``deck_name`` is matched against any level-1 or level-2 deck under
    the Manga root (case-insensitive). Cards are gathered recursively
    from the matched deck AND its children.
    """
    try:
        cards: list = []
        if deck_name:
            serie = get_manga_serie_by_name(deck_name)
            root_deck_id = None
            if serie:
                root_deck_id = serie["id"]
            else:
                # Try matching against any level-2 volume
                tree = get_manga_deck_hierarchy()
                for s in tree.get("series", []):
                    for v in s.get("volumes", []):
                        if v["name"].lower() == deck_name.lower():
                            root_deck_id = v["id"]
                            break
                    if root_deck_id is not None:
                        break
            if root_deck_id is None:
                return {
                    "cards": [],
                    "count": 0,
                    "error": f"deck '{deck_name}' no encontrado bajo Manga",
                }
            cards = get_manga_cards_in_deck_tree(root_deck_id)
        else:
            # No deck filter → all cards under the Manga root
            cards = get_manga_cards_in_deck_tree(storage.MANGA_ROOT_ID)
        if language:
            cards = [c for c in cards if c.get("language") == language]
        if limit and limit > 0:
            cards = cards[: int(limit)]
        # Trim SM-2 internals for a leaner response
        out = []
        for c in cards:
            out.append({
                "id": c["id"],
                "deck_id": c["deck_id"],
                "deck_name": c.get("deck_name"),
                "image_path": c.get("image_path"),
                "bubble_index": c.get("bubble_index"),
                "original_text": c.get("original_text"),
                "language": c.get("language"),
                "translation": c.get("translation"),
                "smart_explanation": c.get("smart_explanation"),
                "created_at": c.get("created_at"),
            })
        return {"cards": out, "count": len(out)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_manga_card_get(card_id: int) -> dict:
    """Get the full details of a single manga card by id, including its
    deck_name for context. Returns an error if the id doesn't exist.
    """
    try:
        card = get_manga_card(card_id)
        if not card:
            return {"status": "error", "message": f"card {card_id} not found"}
        # Attach deck_name
        try:
            conn = storage.get_db()
            row = conn.execute(
                "SELECT name FROM decks WHERE id = ?", (card["deck_id"],)
            ).fetchone()
            conn.close()
            card["deck_name"] = row["name"] if row else None
        except Exception:
            card["deck_name"] = None
        return card
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_manga_card_delete(card_id: int, confirm: bool = False) -> dict:
    """Delete a manga card. DESTRUCTIVE — two-step: first call with
    ``confirm=False`` to get a preview, then call again with
    ``confirm=True`` to actually delete.
    """
    try:
        if not confirm:
            card = get_manga_card(card_id)
            if not card:
                return {"status": "error", "message": f"card {card_id} not found"}
            return {
                "needs_confirmation": True,
                "card": card,
                "message": ("Vas a borrar esta card. "
                            "Llama de nuevo con confirm=True."),
            }
        deleted = delete_manga_card(card_id)
        if not deleted:
            return {"status": "error", "message": f"card {card_id} not found",
                    "deleted": False}
        return {"deleted": True, "card_id": int(card_id)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_manga_card_edit(card_id: int, original_text: str = None,
                         translation: str = None,
                         smart_explanation: str = None,
                         language: str = None) -> dict:
    """Edit the editable fields of a manga card. At least one field must
    be provided. Returns the updated card, or an error if the card
    doesn't exist. SM-2 state, image_path, bubble_index, and deck_id
    cannot be changed through this tool — those are managed by the
    system.
    """
    try:
        fields = {
            k: v for k, v in {
                "original_text": original_text,
                "translation": translation,
                "smart_explanation": smart_explanation,
                "language": language,
            }.items() if v is not None
        }
        if not fields:
            return {
                "status": "error",
                "message": ("Proporciona al menos un campo a editar: "
                            "original_text, translation, smart_explanation, "
                            "language"),
            }
        card = update_manga_card(card_id, **fields)
        if card is None:
            return {"status": "error", "message": f"card {card_id} not found"}
        return {"status": "ok", "card": card}
    except SrsError as e:
        return {"status": "error", "message": e.error_dict.get("error", str(e))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_manga_serie_list() -> dict:
    """List all series (level-1 decks under the Manga root). For each
    serie returns id, name, emoji, volume_count and total card_count
    (recursive — includes all volumes).
    """
    try:
        tree = get_manga_deck_hierarchy()
        out = []
        for s in tree.get("series", []):
            out.append({
                "id": s["id"],
                "name": s["name"],
                "emoji": s.get("emoji"),
                "volume_count": len(s.get("volumes", []) or []),
                "card_count": s.get("card_count", 0),
            })
        return {"series": out, "count": len(out),
                "root_id": tree.get("root_id"),
                "root_name": tree.get("root_name")}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_manga_volume_list(serie_name: str) -> dict:
    """List the volumes (level-2 decks) of a given serie, with
    per-volume card_count. Returns a clear error if the serie doesn't
    exist under Manga.
    """
    try:
        serie = get_manga_serie_by_name(serie_name)
        if not serie:
            return {
                "status": "error",
                "message": (f"serie '{serie_name}' no existe bajo Manga. "
                            f"Usa manga_serie_list para ver las series "
                            f"disponibles, o manga_serie_create para "
                            f"crearla."),
            }
        tree = get_manga_deck_hierarchy()
        match = next(
            (s for s in tree.get("series", [])
             if s["id"] == serie["id"]),
            None,
        )
        volumes = match.get("volumes", []) if match else []
        out = []
        for v in volumes:
            out.append({
                "id": v["id"],
                "name": v["name"],
                "emoji": v.get("emoji"),
                "card_count": v.get("card_count", 0),
            })
        return {
            "serie": {
                "id": serie["id"],
                "name": serie["name"],
                "emoji": serie.get("emoji"),
            },
            "volumes": out,
            "count": len(out),
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_manga_serie_create(serie_name: str) -> dict:
    """Create a new serie (level-1 deck under Manga). Idempotent: if
    the serie already exists (case-insensitive), returns the existing
    one with ``created=False``.
    """
    try:
        result = create_manga_serie(serie_name)
        created = (result.get("status") != "exists")
        return {
            "serie": {
                "id": result["id"],
                "name": result["name"],
                "emoji": result.get("emoji"),
            },
            "created": created,
        }
    except SrsError as e:
        return {"status": "error", "message": e.error_dict.get("error", str(e))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def tool_manga_volume_create(serie_name: str, volume_number) -> dict:
    """Create a new volume (level-2 deck) under a serie. Idempotent.

    ``volume_number`` can be an int (saved as ``"Volumen {N}"``) or a
    string (used as-is, e.g. ``"1"``, ``"2.5"``, ``"Vol 3"``).
    """
    try:
        result = create_manga_volume(serie_name, volume_number)
        created = (result.get("status") != "exists")
        return {
            "volume": {
                "id": result["id"],
                "name": result["name"],
                "emoji": result.get("emoji"),
                "parent_id": result.get("parent_id"),
            },
            "created": created,
        }
    except SrsError as e:
        return {"status": "error", "message": e.error_dict.get("error", str(e))}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==============================================================================
# TOOL SCHEMAS (OpenAI/DeepSeek function calling format)
# ==============================================================================
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "manga_card_list",
            "description": (
                "Lista cards de manga, opcionalmente filtradas por "
                "deck (serie o volumen, case-insensitive) y/o idioma. "
                "Recoge recursivamente todas las cards del deck y sus hijos."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "deck_name": {
                        "type": "string",
                        "description": (
                            "Nombre del deck (serie o volumen). "
                            "Si se omite, devuelve todas las cards bajo Manga."
                        ),
                    },
                    "language": {
                        "type": "string",
                        "description": "Código ISO 639-1 (en, ja, es, de, fr, ...)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Máximo de cards a devolver (default 20).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manga_card_get",
            "description": (
                "Obtiene los detalles completos de una card de manga por id, "
                "incluyendo el nombre del deck. Devuelve error si la card no existe."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer", "description": "ID de la card"},
                },
                "required": ["card_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manga_card_delete",
            "description": (
                "⚠️ Elimina una card de manga (DESTRUCTIVO). Requiere "
                "confirmación en dos pasos: primera llamada con confirm=false "
                "muestra la card a borrar; segunda con confirm=true la borra."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer", "description": "ID de la card a eliminar"},
                    "confirm": {
                        "type": "boolean",
                        "description": "true para confirmar el borrado (default false).",
                    },
                },
                "required": ["card_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manga_card_edit",
            "description": (
                "Edita los campos editables de una card de manga. Proporciona "
                "al menos uno de: original_text, translation, smart_explanation, "
                "language. El estado SM-2, image_path, bubble_index y deck_id "
                "no se pueden cambiar desde esta herramienta."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "integer", "description": "ID de la card a editar"},
                    "original_text": {"type": "string", "description": "Texto original"},
                    "translation": {"type": "string", "description": "Traducción al español"},
                    "smart_explanation": {
                        "type": "string",
                        "description": "Explicación inteligente (phrasal verbs, slang, etc.)",
                    },
                    "language": {
                        "type": "string",
                        "description": "Código ISO 639-1 del idioma de la burbuja",
                    },
                },
                "required": ["card_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manga_serie_list",
            "description": (
                "Lista todas las series (decks nivel 1 bajo el root Manga). "
                "Para cada serie devuelve id, name, emoji, volume_count y "
                "card_count (recursivo, suma de todos los volúmenes)."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manga_volume_list",
            "description": (
                "Lista los volúmenes (decks nivel 2) de una serie, con "
                "card_count por volumen. Devuelve error claro si la serie "
                "no existe bajo Manga."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "serie_name": {
                        "type": "string",
                        "description": "Nombre exacto (case-insensitive) de la serie",
                    },
                },
                "required": ["serie_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manga_serie_create",
            "description": (
                "Crea una nueva serie (deck nivel 1 bajo Manga). "
                "Idempotente: si la serie ya existe, devuelve la existente con created=false."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "serie_name": {
                        "type": "string",
                        "description": "Nombre de la serie a crear (case-insensitive).",
                    },
                },
                "required": ["serie_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manga_volume_create",
            "description": (
                "Crea un nuevo volumen (deck nivel 2) bajo una serie. "
                "Idempotente. volume_number puede ser int (guardado como "
                "'Volumen N') o str (usado tal cual: '1', '2.5', 'Vol 3')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "serie_name": {
                        "type": "string",
                        "description": "Nombre de la serie padre (case-insensitive).",
                    },
                    "volume_number": {
                        "type": ["integer", "number", "string"],
                        "description": (
                            "Número o nombre del volumen. int → 'Volumen N'; "
                            "float con decimales o string → usado tal cual."
                        ),
                    },
                },
                "required": ["serie_name", "volume_number"],
            },
        },
    },
]


# ==============================================================================
# TOOL DISPATCH
# ==============================================================================
TOOL_DISPATCH: dict = {
    "manga_card_list": tool_manga_card_list,
    "manga_card_get": tool_manga_card_get,
    "manga_card_delete": tool_manga_card_delete,
    "manga_card_edit": tool_manga_card_edit,
    "manga_serie_list": tool_manga_serie_list,
    "manga_volume_list": tool_manga_volume_list,
    "manga_serie_create": tool_manga_serie_create,
    "manga_volume_create": tool_manga_volume_create,
}


# Tools the agent should require explicit user confirmation for.
DESTRUCTIVE_TOOLS: set[str] = {
    "manga_card_delete",
}


def register(registry) -> list[str]:
    """Register every ``manga_*`` tool on the given :class:`ToolRegistry`.

    Returns the list of tool names that were registered. No-op (returns
    an empty list) if ``registry`` is ``None``.
    """
    if registry is None:
        return []
    schema_by_name = {s["function"]["name"]: s for s in TOOL_SCHEMAS}
    registered: list[str] = []
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
        registered.append(tool_name)
    return registered


__all__ = [
    "TOOL_DISPATCH",
    "TOOL_SCHEMAS",
    "DESTRUCTIVE_TOOLS",
    "register",
]

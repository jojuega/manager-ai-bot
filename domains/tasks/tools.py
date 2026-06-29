"""Tasks domain — LLM-callable tools + their OpenAI/DeepSeek schemas.

The callable functions are extracted from the original monolith's
``llm_agent.py`` (``tool_tasks_*``). The schema list mirrors the
``TOOL_DEFINITIONS`` block for the ``tasks_*`` entries in the same file.

All callables take/return plain dicts so they can be plugged into the
generic ``agent.tool_registry.ToolRegistry``.
"""
from __future__ import annotations

from datetime import date

from .storage import (
    load_tasks,
    next_task_id,
    save_tasks,
    today_entry,
)

# ─── Default task-list metadata (used when ``_lists_meta`` is missing) ─────
DEFAULT_LISTS_META = {
    "daily": {"name": "Daily Tasks", "emoji": "📋", "order": 0},
    "study": {"name": "Study Session", "emoji": "📚", "order": 1},
    "srs": {"name": "SRS Review", "emoji": "🔁", "order": 2},
    "hausarbeit": {"name": "Hausarbeit", "emoji": "📝", "order": 3},
}


# ==============================================================================
# Tool callables
# ==============================================================================
def tool_tasks_preview() -> dict:
    """Get today's task preview with all lists."""
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    day, _ = today_entry(d)
    items = day.get("tasks", [])

    for k, v in DEFAULT_LISTS_META.items():
        if k not in meta:
            meta[k] = v

    lists = []
    for lid, info in sorted(meta.items(), key=lambda x: x[1].get("order", 99)):
        ti = [t for t in items if t.get("list", "daily") == lid]
        lists.append({
            "id": lid,
            "name": info.get("name", lid),
            "emoji": info.get("emoji", "📋"),
            "total": len(ti),
            "completed": sum(1 for t in ti if t["status"] == "completed"),
            "order": info.get("order", 99),
        })

    return {"today": date.today().isoformat(), "lists": lists}


def tool_tasks_get_list(list_id: str) -> dict:
    """Get all items in a specific task list."""
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    day, _ = today_entry(d)
    items = [t for t in day.get("tasks", []) if t.get("list", "daily") == list_id]
    info = meta.get(list_id, {"name": list_id, "emoji": "📋"})
    return {
        "list_id": list_id,
        "name": info.get("name", list_id),
        "emoji": info.get("emoji", "📋"),
        "total": len(items),
        "completed": sum(1 for t in items if t["status"] == "completed"),
        "items": items,
    }


def tool_tasks_create_list(list_id: str, name: str, emoji: str = "📋", order: int = 99,
                           numbered: bool = True) -> dict:
    """Create a new task list category."""
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    if list_id in meta:
        return {"status": "error", "message": f"La lista '{list_id}' ya existe"}
    meta[list_id] = {"name": name, "emoji": emoji, "order": order, "numbered": numbered}
    d["_lists_meta"] = meta
    save_tasks(d)
    return {"status": "ok", "message": f"✅ Lista '{name}' creada", "list_id": list_id}


def tool_tasks_add_item(list_id: str, text: str = "", content: str = "") -> dict:
    """Add a new task to today's list."""
    # Accept both 'text' (LLM's preferred name) and 'content' (legacy).
    task_text = text or content
    if not task_text:
        return {"status": "error", "message": "El texto de la tarea está vacío"}
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    if list_id not in meta:
        return {"status": "error", "message": f"La lista '{list_id}' no existe. Créala primero."}
    day, _ = today_entry(d)
    tasks = day.get("tasks", [])
    tid = next_task_id(tasks)
    tasks.append({"id": tid, "content": task_text, "status": "pending", "list": list_id})
    day["tasks"] = tasks
    save_tasks(d)
    name = meta[list_id]["name"]
    return {"status": "ok", "message": f"✅ Tarea añadida a '{name}': {task_text}", "task_id": tid}


def tool_tasks_toggle_item(task_id: str) -> dict:
    """Toggle a task between pending and completed."""
    d = load_tasks()
    day, _ = today_entry(d)
    for t in day.get("tasks", []):
        if t["id"] == task_id:
            t["status"] = "completed" if t["status"] == "pending" else "pending"
            save_tasks(d)
            return {"status": "ok", "task_id": task_id, "new_status": t["status"], "content": t["content"]}
    return {"status": "error", "message": f"Tarea {task_id} no encontrada"}


def tool_tasks_toggle_by_text(list_id: str, text: str) -> dict:
    """Toggle a task by matching its text content (fuzzy). Returns matched tasks."""
    d = load_tasks()
    day, _ = today_entry(d)
    tl = text.strip().lower()
    tasks = [t for t in day.get("tasks", []) if t.get("list", "daily") == list_id]
    # Try exact match first
    exact = [t for t in tasks if t["content"].strip().lower() == tl]
    if exact:
        t = exact[0]
        t["status"] = "completed" if t["status"] == "pending" else "pending"
        save_tasks(d)
        return {"status": "ok", "task_id": t["id"], "new_status": t["status"],
                "content": t["content"], "match": "exact"}
    # Try contains match
    contains = [t for t in tasks if tl in t["content"].strip().lower()]
    if len(contains) == 1:
        t = contains[0]
        t["status"] = "completed" if t["status"] == "pending" else "pending"
        save_tasks(d)
        return {"status": "ok", "task_id": t["id"], "new_status": t["status"],
                "content": t["content"], "match": "contains"}
    elif len(contains) > 1:
        return {"status": "multiple", "matches": [t["content"] for t in contains],
                "message": f"Múltiples coincidencias para '{text}'"}
    return {"status": "error", "message": f"No se encontró '{text}' en la lista"}


def tool_tasks_delete_item(task_id: str) -> dict:
    """Delete a task permanently. DESTRUCTIVE."""
    d = load_tasks()
    day, _ = today_entry(d)
    tasks = day.get("tasks", [])
    for i, t in enumerate(tasks):
        if t["id"] == task_id:
            removed = tasks.pop(i)
            day["tasks"] = tasks
            save_tasks(d)
            meta = d.get("_lists_meta", {})
            list_name = meta.get(t.get("list", "daily"), {}).get("name", t.get("list", "daily"))
            return {"status": "ok", "message": f"🗑️ Tarea eliminada de '{list_name}': {removed['content']}"}
    return {"status": "error", "message": f"Tarea {task_id} no encontrada"}


def tool_tasks_edit_item(task_id: str, new_content: str) -> dict:
    """Edit a task's content."""
    d = load_tasks()
    day, _ = today_entry(d)
    for t in day.get("tasks", []):
        if t["id"] == task_id:
            old = t["content"]
            t["content"] = new_content
            save_tasks(d)
            return {"status": "ok", "message": f"✏️ Tarea editada: '{old}' → '{new_content}'"}
    return {"status": "error", "message": f"Tarea {task_id} no encontrada"}


def tool_tasks_delete_list(list_id: str) -> dict:
    """Delete a task list category and ALL its tasks. DESTRUCTIVE."""
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    if list_id not in meta:
        return {"status": "error", "message": f"La lista '{list_id}' no existe"}
    name = meta[list_id]["name"]
    del meta[list_id]
    d["_lists_meta"] = meta
    day, _ = today_entry(d)
    day["tasks"] = [t for t in day.get("tasks", []) if t.get("list", "daily") != list_id]
    save_tasks(d)
    return {"status": "ok", "message": f"🗑️ Lista '{name}' eliminada con todas sus tareas"}


def tool_tasks_edit_list(list_id: str, name: str = None, emoji: str = None, order: int = None) -> dict:
    """Edit a task list's metadata (name, emoji, order)."""
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    if list_id not in meta:
        return {"status": "error", "message": f"La lista '{list_id}' no existe"}
    changed = []
    if name is not None:
        meta[list_id]["name"] = name
        changed.append(f"nombre → '{name}'")
    if emoji is not None:
        meta[list_id]["emoji"] = emoji
        changed.append(f"emoji → {emoji}")
    if order is not None:
        meta[list_id]["order"] = order
        changed.append(f"orden → {order}")
    if not changed:
        return {"status": "error", "message": "Nada que editar"}
    d["_lists_meta"] = meta
    save_tasks(d)
    return {"status": "ok", "message": f"✏️ Lista '{list_id}' actualizada: {', '.join(changed)}"}


def tool_tasks_clear_completed(list_id: str) -> dict:
    """Remove ALL completed tasks from a list. Cannot be undone."""
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    info = meta.get(list_id, {"name": list_id})
    day, _ = today_entry(d)
    tasks = day.get("tasks", [])
    completed = [t for t in tasks if t.get("list") == list_id and t["status"] == "completed"]
    removed_count = len(completed)
    if not removed_count:
        return {"status": "ok", "message": f"No hay tareas completadas en '{info['name']}'"}
    day["tasks"] = [t for t in tasks if not (t.get("list") == list_id and t["status"] == "completed")]
    save_tasks(d)
    return {"status": "ok", "message": f"🧹 {removed_count} tarea(s) completada(s) eliminada(s) de '{info['name']}'"}


def tool_tasks_batch_add_items(list_id: str, items: list) -> dict:
    """Add MULTIPLE tasks to a list at once. items is a list of strings."""
    if not items or not isinstance(items, list):
        return {"status": "error", "message": "Debes proporcionar una lista de textos (items)"}
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    if list_id not in meta:
        return {"status": "error", "message": f"La lista '{list_id}' no existe. Créala primero."}
    day, _ = today_entry(d)
    tasks = day.get("tasks", [])
    added = []
    for txt in items:
        txt = txt.strip()
        if not txt:
            continue
        tid = next_task_id(tasks)
        tasks.append({"id": tid, "content": txt, "status": "pending", "list": list_id})
        added.append(txt)
    day["tasks"] = tasks
    save_tasks(d)
    name = meta[list_id]["name"]
    if not added:
        return {"status": "error", "message": "No se añadió ninguna tarea (lista vacía)"}
    return {"status": "ok", "message": f"✅ {len(added)} tarea(s) añadida(s) a '{name}'", "added": added}


def tool_tasks_move_item(task_id: str, to_list_id: str) -> dict:
    """Move a task to a different list."""
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    if to_list_id not in meta:
        return {"status": "error", "message": f"La lista destino '{to_list_id}' no existe"}
    day, _ = today_entry(d)
    for t in day.get("tasks", []):
        if t["id"] == task_id:
            old_list = t.get("list", "daily")
            t["list"] = to_list_id
            save_tasks(d)
            from_name = meta.get(old_list, {}).get("name", old_list)
            to_name = meta[to_list_id]["name"]
            return {"status": "ok", "message": f"↪️ Tarea movida de '{from_name}' a '{to_name}': {t['content']}"}
    return {"status": "error", "message": f"Tarea {task_id} no encontrada"}


# ==============================================================================
# OpenAI/DeepSeek-compatible tool schemas (one per callable above)
# ==============================================================================
TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "tasks_preview",
            "description": "Muestra el resumen de todas las listas de tareas de hoy con su progreso",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_get_list",
            "description": "Muestra todas las tareas de una lista específica",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string",
                                "description": "ID de la lista (daily, study, hausarbeit, o personalizada)"},
                },
                "required": ["list_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_add_item",
            "description": "Añade una nueva tarea a una lista para hoy",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "ID de la lista donde añadir la tarea"},
                    "text": {"type": "string", "description": "Descripción de la tarea"},
                },
                "required": ["list_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_toggle_item",
            "description": "Marca/desmarca una tarea como completada",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID de la tarea a cambiar estado"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_toggle_by_text",
            "description": "Marca/desmarca una tarea buscando por su texto (para listas SIN números como compras). Usa coincidencia exacta o parcial.",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "ID de la lista donde buscar"},
                    "text": {"type": "string", "description": "Texto a buscar (coincidencia exacta o parcial)"},
                },
                "required": ["list_id", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_edit_item",
            "description": "Edita el contenido de una tarea",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID de la tarea"},
                    "new_content": {"type": "string", "description": "Nuevo contenido"},
                },
                "required": ["task_id", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_create_list",
            "description": "Crea una nueva categoría de lista de tareas",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "ID único (ej: compras, proyectos)"},
                    "name": {"type": "string", "description": "Nombre visible (ej: Lista de Compras)"},
                    "emoji": {"type": "string", "description": "Emoji (default: 📋)"},
                    "order": {"type": "integer", "description": "Orden en menú (menor=primero, default: 99)"},
                    "numbered": {"type": "boolean", "description": "True=lista numerada (estudio/tareas), False=lista sin números (compras)"},
                },
                "required": ["list_id", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_edit_list",
            "description": "Edita el nombre, emoji u orden de una lista de tareas",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "ID de la lista a editar"},
                    "name": {"type": "string", "description": "Nuevo nombre visible (opcional)"},
                    "emoji": {"type": "string", "description": "Nuevo emoji (opcional)"},
                    "order": {"type": "integer", "description": "Nuevo orden (opcional, menor=primero)"},
                },
                "required": ["list_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_batch_add_items",
            "description": "Añade MULTIPLES tareas a una lista de una sola vez. Útil para 'crea una lista con X, Y, Z'",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "ID de la lista destino"},
                    "items": {"type": "array", "items": {"type": "string"},
                             "description": "Lista de textos de las tareas a añadir"},
                },
                "required": ["list_id", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_move_item",
            "description": "Mueve una tarea a otra lista",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "ID de la tarea a mover"},
                    "to_list_id": {"type": "string", "description": "ID de la lista destino"},
                },
                "required": ["task_id", "to_list_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tasks_clear_completed",
            "description": "⚠️ Elimina TODAS las tareas completadas de una lista (irreversible)",
            "parameters": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "description": "ID de la lista a limpiar"},
                },
                "required": ["list_id"],
            },
        },
    },
]


# ==============================================================================
# Tool dispatch table: tool-name → callable (for ToolRegistry.add)
# ==============================================================================
TOOL_DISPATCH: dict = {
    "tasks_preview": tool_tasks_preview,
    "tasks_get_list": tool_tasks_get_list,
    "tasks_create_list": tool_tasks_create_list,
    "tasks_add_item": tool_tasks_add_item,
    "tasks_toggle_item": tool_tasks_toggle_item,
    "tasks_toggle_by_text": tool_tasks_toggle_by_text,
    "tasks_delete_item": tool_tasks_delete_item,
    "tasks_edit_item": tool_tasks_edit_item,
    "tasks_delete_list": tool_tasks_delete_list,
    "tasks_edit_list": tool_tasks_edit_list,
    "tasks_clear_completed": tool_tasks_clear_completed,
    "tasks_batch_add_items": tool_tasks_batch_add_items,
    "tasks_move_item": tool_tasks_move_item,
}

# Tools the agent should require explicit user confirmation for.
DESTRUCTIVE_TOOLS: set[str] = {
    "tasks_delete_item",
    "tasks_delete_list",
    "tasks_clear_completed",
}

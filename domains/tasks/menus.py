"""Telegram keyboards for the tasks domain.

Builders extracted from the original ``task_bot.py``:

* ``main_menu`` — root menu (Tasks / Vocab / Flashcards / Stats).
* ``task_lists_keyboard`` — list-of-lists view (one button per task list).
* ``task_list_keyboard`` — single list with one button per task + "Volver".
"""
from __future__ import annotations

from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .storage import load_tasks, today_entry

# Default list meta (also referenced from tools.DEFAULT_LISTS_META). Kept in
# sync locally so this module can be imported without triggering tools.py
# side-effects.
DEFAULT_LISTS_META = {
    "daily": {"name": "Daily Tasks", "emoji": "📋", "order": 0},
    "study": {"name": "Study Session", "emoji": "📚", "order": 1},
    "srs": {"name": "SRS Review", "emoji": "🔁", "order": 2},
    "hausarbeit": {"name": "Hausarbeit", "emoji": "📝", "order": 3},
}


def _task_lists(d: dict) -> list[dict]:
    """Return the list-of-lists summary used by both ``main_menu`` and
    ``task_lists_keyboard``."""
    meta = d.get("_lists_meta", {})
    for k, v in DEFAULT_LISTS_META.items():
        if k not in meta:
            meta[k] = v
    day, _ = today_entry(d)
    items = day.get("tasks", [])
    out = []
    for lid, info in sorted(meta.items(), key=lambda x: x[1].get("order", 99)):
        ti = [t for t in items if t.get("list", "daily") == lid]
        out.append({
            "id": lid,
            "name": info.get("name", lid),
            "emoji": info.get("emoji", "📋"),
            "total": len(ti),
            "completed": sum(1 for t in ti if t["status"] == "completed"),
        })
    return out


def main_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Root Telegram menu: today's task progress + global navigation.

    Mirrors the original ``task_bot.main_menu`` body (the cross-domain buttons
    — Vocab, Flashcards, Stats — are added by the caller since they belong to
    other domains). The keyboard returned here ONLY carries the Tasks button;
    downstream code can append extra rows if it has access to those domains.
    """
    d = load_tasks()
    ls = _task_lists(d)
    ts = date.today().isoformat()
    lines = [f"📋 **Task Lists — {ts}**\n"]
    total_all = sum(l["total"] for l in ls)
    done_all = sum(l["completed"] for l in ls)
    if total_all:
        pct = round(done_all / total_all * 100)
        lines.append(f"{'█' * (pct // 10)}{'░' * (10 - pct // 10)}  {done_all}/{total_all} ({pct}%)\n")
    for l in ls:
        if l["total"]:
            lines.append(f"{l['emoji']} **{l['name']}**: {l['completed']}/{l['total']}")
        else:
            lines.append(f"{l['emoji']} **{l['name']}**: —")
    text = "\n".join(lines)
    kb = [
        [InlineKeyboardButton("📋 Tasks", callback_data="menu:tasks")],
    ]
    return text, InlineKeyboardMarkup(kb)


def task_lists_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    """Build the "Task Lists" sub-menu: one button per list + "Volver"."""
    d = load_tasks()
    ls = _task_lists(d)
    text = "📋 **Task Lists**\n"
    rows = []
    for l in ls:
        if l["total"]:
            label = f"{l['emoji']} {l['name']}: {l['completed']}/{l['total']}"
        else:
            label = f"{l['emoji']} {l['name']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"list:{l['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")])
    return text, InlineKeyboardMarkup(rows)


def task_list_keyboard(list_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build the per-list view: numbered tasks + "Volver" + tip line.

    Sets ``context.user_data['viewing_list']`` is the caller's job — this
    builder only renders the keyboard. Returns ``(text, keyboard)``.
    """
    d = load_tasks()
    meta = d.get("_lists_meta", {})
    info = meta.get(list_id, {"name": list_id, "emoji": "📋"})
    day, _ = today_entry(d)
    tasks = [t for t in day.get("tasks", []) if t.get("list", "daily") == list_id]
    ts = date.today().isoformat()
    emoji, name = info.get("emoji", "📋"), info.get("name", "Tasks")
    total, done = len(tasks), sum(1 for t in tasks if t["status"] == "completed")
    numbered = info.get("numbered", True)  # default: numbered

    lines = [f"{emoji} **{name}** — {ts}\n"]
    if total:
        pct = round(done / total * 100)
        lines.append(f"{'█' * (pct // 10)}{'░' * (10 - pct // 10)}  {done}/{total} ({pct}%)\n")
        for i, t in enumerate(tasks, 1):
            check = "✅" if t["status"] == "completed" else "⬜"
            if numbered:
                lines.append(f"**{i}.** {check} {t['content']}")
            else:
                lines.append(f"{check} {t['content']}")
    else:
        lines.append("_No hay tareas._")

    # Shortcut hint
    if numbered and total:
        lines.append("\n_Consejo: escribe el número para marcar/desmarcar, añade 'u' para desmarcar._")
    else:
        lines.append("\n_Consejo: escribe el texto del ítem y el LLM lo marcará._")

    text = "\n".join(lines)
    rows = [
        [InlineKeyboardButton(
            f"{'✅' if t['status'] == 'completed' else '⬜'} {t['content']}",
            callback_data=f"task:{list_id}:{t['id']}",
        )]
        for t in tasks
    ]
    rows.append([InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")])
    return text, InlineKeyboardMarkup(rows)

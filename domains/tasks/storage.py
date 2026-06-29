"""Storage helpers for the tasks domain.

Read/write helpers around ``data/tasks.json`` plus the small in-memory cache
(1.5s TTL + mtime check) that the original monolith kept in ``task_bot.py``.
Keeping these here means tools and menus share the same on-disk format and
the same cache invalidation rules.
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

# Re-use the centralised DATA path from core.config so we don't hardcode
# /root/projects/jogtasksbot anywhere.
from core.config import DATA

TASKS_FILE: Path = DATA / "tasks.json"

# In-memory cache (mtime + 1.5s TTL). Tasks are read on every menu render and
# on every task toggle, so caching eliminates redundant disk reads and JSON
# parses on rapid navigation.
_TASKS_CACHE: dict[str, Any] = {"data": None, "ts": 0.0, "mtime": 0.0}
_TASKS_TTL = 1.5


def load_tasks() -> dict:
    """Load ``tasks.json`` with a 1.5s TTL + mtime-based cache.

    Returns an empty dict if the file doesn't exist or fails to parse.
    """
    try:
        mtime = TASKS_FILE.stat().st_mtime if TASKS_FILE.exists() else 0
    except Exception:
        mtime = 0
    now = time.time()
    if (
        _TASKS_CACHE["data"] is not None
        and (now - _TASKS_CACHE["ts"]) < _TASKS_TTL
        and mtime == _TASKS_CACHE["mtime"]
    ):
        return _TASKS_CACHE["data"]
    if not TASKS_FILE.exists():
        _TASKS_CACHE["data"] = {}
    else:
        try:
            _TASKS_CACHE["data"] = json.loads(TASKS_FILE.read_text())
        except Exception:
            _TASKS_CACHE["data"] = {}
    _TASKS_CACHE["ts"] = now
    _TASKS_CACHE["mtime"] = mtime
    return _TASKS_CACHE["data"]


def save_tasks(d: dict) -> None:
    """Write ``tasks.json`` and invalidate the in-memory cache so the next
    ``load_tasks()`` re-reads from disk.
    """
    TASKS_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False))
    # Force a re-read on next load_tasks(): the new mtime will differ anyway,
    # but bumping ts here too is belt-and-suspenders and avoids a stale-hit
    # race within the same millisecond.
    _TASKS_CACHE["mtime"] = -1.0
    _TASKS_CACHE["ts"] = 0.0


def today_entry(d: dict) -> tuple[dict, str]:
    """Return ``(day_dict, iso_date)`` for today, creating the entry if missing.

    The day dict is the same object as ``d[iso_date]`` so the caller can mutate
    it in place before ``save_tasks()``.
    """
    t = date.today().isoformat()
    if t not in d:
        d[t] = {"tasks": []}
    return d[t], t


def next_task_id(tasks: list) -> str:
    """Return the next free integer-ish task id as a string.

    Tries to avoid collisions by scanning existing numeric ids and adding 1.
    """
    existing = [int(t["id"]) for t in tasks if str(t["id"]).isdigit()]
    return str(max(existing) + 1 if existing else 1)

"""
Cronjob system for the TaskBot.

Extracted from the original ``jogtasksbot/scripts/cronjobs.py`` monolith.
Adapted to use ``core.config`` for paths (no hardcoded absolute paths) so
the scheduler domain is portable across installations.

Responsibilities
----------------
* Parse natural-language schedules ("a las 2pm", "en 30 minutos",
  "mañana 9am") and classify them as either a simple ``reminder`` or an
  ``agent`` task (LLM will run at trigger time).
* Persist scheduled jobs to ``<DATA>/cronjobs.json`` so they survive
  bot restarts.
* List / cancel / mark-done pending jobs and surface the ones whose
  trigger time has passed.

The actual dispatch (running the LLM agent or sending the reminder
text) lives in :mod:`domains.scheduler.jobs`; this module is the
durable storage + parser layer.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.config import DATA

# Storage file lives inside the project data dir (resolved via core.config
# so it follows the repo, not an absolute hardcoded path).
CRONJOBS_FILE: Path = DATA / "cronjobs.json"

log = logging.getLogger("domains.scheduler.cronjobs")


class Cronjob:
    """A scheduled action."""

    def __init__(self, id, trigger_at, kind, payload, chat_id, created_at=None):
        self.id = id
        self.trigger_at = trigger_at  # ISO datetime string
        self.kind = kind  # "reminder" or "agent"
        self.payload = payload  # {"text": "..."} for reminder, {"prompt": "..."} for agent
        self.chat_id = chat_id
        self.created_at = created_at or datetime.now().isoformat()
        self.status = "pending"  # pending | done | cancelled

    def to_dict(self):
        return {
            "id": self.id,
            "trigger_at": self.trigger_at,
            "kind": self.kind,
            "payload": self.payload,
            "chat_id": self.chat_id,
            "created_at": self.created_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d):
        c = cls(
            id=d["id"], trigger_at=d["trigger_at"], kind=d["kind"],
            payload=d["payload"], chat_id=d["chat_id"],
            created_at=d.get("created_at"),
        )
        c.status = d.get("status", "pending")
        return c


def load_cronjobs() -> list:
    if not CRONJOBS_FILE.exists():
        return []
    try:
        data = json.loads(CRONJOBS_FILE.read_text())
        return [Cronjob.from_dict(d) for d in data]
    except Exception as e:
        log.error(f"load_cronjobs: {e}")
        return []


def save_cronjobs(jobs: list):
    DATA.mkdir(parents=True, exist_ok=True)
    data = [j.to_dict() for j in jobs]
    CRONJOBS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ---- Parsing natural language schedules ----

# Verbs that imply an "agent" action (LLM will run with the rest of the text
# as a prompt) vs a simple "reminder" (just echo the text back at trigger time).
_AGENT_VERBS = (
    "haz", "lee", "busca", "analiza", "crea", "agrega",
    "envía", "envia", "dame", "dime", "revisa", "prepara",
    "genera", "manda", "envíale", "escribe", "muéstrame",
    "calcula", "compara", "resume", "sincroniza", "agenda",
)


def _classify_kind(rest: str) -> str:
    """Decide if the post-schedule text implies an agent action or a simple reminder."""
    rest_lower = rest.lower().lstrip(" ,.;:-¡!¿?")
    for v in _AGENT_VERBS:
        if rest_lower.startswith(v + " ") or rest_lower.startswith(v + ",") or rest_lower == v:
            return "agent"
    return "reminder"


def _strip_leading_punct(text: str) -> str:
    return re.sub(r"^[\s,.;:\-¡!¿?]+", "", text)


def parse_schedule(text: str) -> Optional[tuple]:
    """
    Try to extract a schedule from user text. Returns (trigger_at, kind, payload_text) or None.

    Examples:
    - "a las 2pm avísame" → trigger 14:00 today, kind="reminder", text="avísame"
    - "a las 3pm lee mis tareas y dame una lista" → trigger 15:00 today, kind="agent", text=full prompt
    - "en 30 minutos recuérdame X" → trigger now+30m, kind="reminder", text="recuérdame X"
    - "mañana 9am X" → trigger tomorrow 09:00, kind="agent" or "reminder" based on verbs
    """
    text_lower = text.lower().strip()
    now = datetime.now()

    # Pattern: "a las HHam/pm" or "a las H:MMam/pm"
    m = re.search(r"\ba las (\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text_lower)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        rest = _strip_leading_punct(text[m.end():])
        return target, _classify_kind(rest), rest

    # Pattern: "en N minutos" / "en N horas"
    m = re.search(r"\ben (\d+)\s*(minuto|hora|minutos|horas|min|hr|h)\b", text_lower)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("hora") or unit in ("hr", "h"):
            target = now + timedelta(hours=amount)
        else:
            target = now + timedelta(minutes=amount)
        rest = _strip_leading_punct(text[m.end():])
        return target, _classify_kind(rest), rest

    # Pattern: "mañana a las H" or "mañana HHam"
    m = re.search(r"\bmañana\s+(?:a las )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text_lower)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3)
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        target = (now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        rest = _strip_leading_punct(text[m.end():])
        return target, _classify_kind(rest), rest

    return None


def schedule_cronjob(trigger_at: datetime, kind: str, payload_text: str, chat_id: int):
    """Add a cronjob to disk and return it."""
    jobs = load_cronjobs()
    new_id = max([j.id for j in jobs], default=0) + 1
    payload = {"prompt" if kind == "agent" else "text": payload_text}
    job = Cronjob(
        id=new_id,
        trigger_at=trigger_at.isoformat(),
        kind=kind,
        payload=payload,
        chat_id=chat_id,
    )
    jobs.append(job)
    save_cronjobs(jobs)
    return job


def cancel_cronjob(job_id: int) -> bool:
    """Mark a pending job as cancelled. Returns True only if the job was
    actually pending (and got cancelled). Returns False if the id is unknown
    OR if the job is already in a terminal state (done / cancelled)."""
    jobs = load_cronjobs()
    for j in jobs:
        if j.id == job_id:
            if j.status != "pending":
                return False
            j.status = "cancelled"
            save_cronjobs(jobs)
            return True
    return False


def list_cronjobs(chat_id: int = None) -> list:
    jobs = load_cronjobs()
    if chat_id is not None:
        jobs = [j for j in jobs if j.chat_id == chat_id]
    return [j for j in jobs if j.status == "pending"]


def mark_done(job_id: int):
    jobs = load_cronjobs()
    for j in jobs:
        if j.id == job_id:
            j.status = "done"
    save_cronjobs(jobs)


def get_due_jobs() -> list:
    """Return pending jobs whose trigger time has passed."""
    jobs = load_cronjobs()
    now = datetime.now()
    due = []
    for j in jobs:
        if j.status != "pending":
            continue
        try:
            trig = datetime.fromisoformat(j.trigger_at)
        except Exception:
            continue
        if trig <= now:
            due.append(j)
    return due


__all__ = [
    "Cronjob",
    "CRONJOBS_FILE",
    "load_cronjobs",
    "save_cronjobs",
    "parse_schedule",
    "schedule_cronjob",
    "cancel_cronjob",
    "list_cronjobs",
    "mark_done",
    "get_due_jobs",
]

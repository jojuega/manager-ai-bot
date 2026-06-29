"""Project configuration: paths, secrets, and constants.

Centralises everything that was previously hardcoded or scattered at the top
of the original task_bot.py monolith.  Other modules should import constants
from here (PROJ, DATA, *_PATH, BOT_TOKEN, DEEPSEEK_KEY, ALLOWED_USER_ID, …)
instead of hardcoding paths.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------
# <repo>/core/config.py → <repo>/.  Works regardless of the absolute install
# location (jogtasksbot clone, manager-ai-bot clone, anywhere else).
PROJ: Path = Path(__file__).resolve().parent.parent
SCRIPTS: Path = PROJ / "scripts"
DATA: Path = PROJ / "data"

# Make sibling scripts importable when launched as a module.  This mirrors the
# defensive sys.path tweak the old monolith had, but only applies when
# `core/` is being executed directly (rare).
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))


# ---------------------------------------------------------------------------
# Secret loading
# ---------------------------------------------------------------------------
# Optional .env injection from $HERMES_HOME/.env (matches the original
# behaviour).  Values only set the environment if not already defined, so the
# real shell env always wins.
ENV_PATH: Path = Path(
    os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
) / ".env"
if ENV_PATH.exists():
    with open(ENV_PATH) as f:
        for ln in f:
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k, v)


def _load_secret(env_var: str, b64_filename: str) -> str:
    """Return a secret from `<DATA>/<b64_filename>` (base64-encoded) or,
    failing that, from the environment variable.  Empty string when neither
    source is available."""
    b64_path = DATA / b64_filename
    if b64_path.exists():
        try:
            return base64.b64decode(b64_path.read_text().strip()).decode()
        except Exception:
            # Corrupt file — fall through to env var.
            pass
    return os.environ.get(env_var, "")


BOT_TOKEN: str = _load_secret("TASK_BOT_TOKEN", "bot_token.b64")
if not BOT_TOKEN:
    # Mirror the old FATAL exit so a misconfigured deployment is loud.
    print("FATAL: no bot token", file=sys.stderr)
    sys.exit(1)

DEEPSEEK_KEY: str = _load_secret("DEEPSEEK_API_KEY", "deepseek_key.b64")


# ---------------------------------------------------------------------------
# Storage paths
# ---------------------------------------------------------------------------
STATE_DB: Path = DATA / "state.db"
SRS_DB: Path = DATA / "srs.db"

PRACTICE: Path = DATA / "practice_session.json"
MANGA_IMAGES_DIR: Path = DATA / "manga_images"
MANGA_TMP_DIR: Path = DATA / "manga_tmp"
MANGA_DEFAULTS_PATH: Path = DATA / "manga_defaults.json"
TTS_CACHE: Path = PROJ / "tts"

# Ensure the TTS cache exists up-front (matches the original behaviour).
TTS_CACHE.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Access control / TTS
# ---------------------------------------------------------------------------
ALLOWED_USER_ID: int = 402446137

TTS_VOICES: dict[str, str] = {
    "de": "de-DE-KatjaNeural",
    "en": "en-GB-SoniaNeural",
}


# ---------------------------------------------------------------------------
# LLM endpoint and prompt library
# ---------------------------------------------------------------------------
DEEP_URL: str = "https://api.deepseek.com/v1/chat/completions"

_PROMPTS_FILE: Path = DATA / "prompts.json"
PROMPTS: dict = json.loads(_PROMPTS_FILE.read_text()) if _PROMPTS_FILE.exists() else {}


__all__ = [
    "PROJ",
    "SCRIPTS",
    "DATA",
    "BOT_TOKEN",
    "DEEPSEEK_KEY",
    "ALLOWED_USER_ID",
    "STATE_DB",
    "SRS_DB",
    "PRACTICE",
    "MANGA_IMAGES_DIR",
    "MANGA_TMP_DIR",
    "MANGA_DEFAULTS_PATH",
    "TTS_CACHE",
    "TTS_VOICES",
    "DEEP_URL",
    "PROMPTS",
]

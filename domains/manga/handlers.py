"""
Telegram handlers for the manga domain.

Extracted from the original ``task_bot.py``:

* ``handle_image`` — the unified image handler (manga vs non-manga).
* ``_manga_save_to_deck`` — move image from ``manga_tmp`` to
  ``manga_images`` and bulk-create cards (now lives in
  :mod:`domains.manga.storage` as :func:`save_pending_to_deck`).
* ``_manga_show_destination_menu`` / ``_manga_show_volume_menu`` —
  render the P2 destination and volume pickers.
* ``cb_manga_select_serie`` / ``cb_manga_new`` / ``cb_manga_newvol`` /
  ``cb_manga_vol`` / ``cb_manga_back`` / ``cb_manga_cancel`` — callback
  handlers for ``mselect:`` / ``mnew`` / ``mnewvol:`` / ``mvol:`` /
  ``mback:`` / ``mcancel``.
* ``parse_manga_serie_volume`` / ``_parse_volume_number_local`` — LLM
  and local parsers used by the text-input flow.
* ``_manga_handle_text_input`` — text-input dispatcher for the P2 flow.
* ``z_cmd`` / ``_z_show_default`` / ``_z_clear_default`` /
  ``_z_set_default`` — ``/z`` command + sub-handlers (P3 default mode).
* ``manga_practice_start`` / ``_manga_show_card`` / ``cb_manga_flip`` /
  ``cb_manga_save`` / ``cb_manga_quit`` — Anki-style practice mode.

Access control (owner-only) is preserved via ``ALLOWED_USER_ID`` from
``core.config``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.config import (
    ALLOWED_USER_ID,
    DATA,
    DEEP_URL,
    DEEPSEEK_KEY,
    MANGA_DEFAULTS_PATH,
    MANGA_IMAGES_DIR,
    MANGA_TMP_DIR,
    SRS_DB,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import menus, storage
from .storage import (
    SrsError,
    create_manga_cards_in_deck,
    create_manga_serie,
    create_manga_volume,
    get_manga_card,
    get_manga_cards_for_practice,
    get_manga_deck_hierarchy,
    get_manga_serie_by_name,
    get_or_create_deck_by_name,
    mark_manga_card_review,
    save_pending_to_deck,
)

log = logging.getLogger("manga.handlers")


# ==============================================================================
# SHARED KEYBOARDS (thin wrappers around the menu module so the rest of this
# file mirrors the original monolith naming — `_manga_done_keyboard` etc.).
# ==============================================================================
def _manga_done_keyboard():
    return menus.kbd_done()


# ==============================================================================
# DEFAULT MODE: load/save manga_defaults.json
# ==============================================================================
def load_manga_defaults() -> dict:
    return storage.load_manga_defaults()


def save_manga_defaults(data: dict) -> None:
    storage.save_manga_defaults(data)


# ==============================================================================
# IMAGE HANDLER (unified: manga vs non-manga)
# ==============================================================================
async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unified image handler: detect manga vs non-manga and dispatch.

    Manga flow (P2):
      1. Download image to ``data/manga_tmp/``.
      2. Call :func:`manga_extract` (vision model) for bubbles.
      3. If a P3 default exists and the volume is still alive, save directly.
      4. Otherwise stash the result in ``context.user_data['pending_manga']``
         and show the destination menu.

    Non-manga flow:
      Buffer the text in ``context.user_data['pending_ocr']`` and show a
      preview. (No further action wired up yet.)
    """
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    photo = update.message.photo
    if photo:
        file = await photo[-1].get_file()
    elif (update.message.document and update.message.document.mime_type
          and update.message.document.mime_type.startswith("image/")):
        file = await update.message.document.get_file()
    else:
        return

    storage.ensure_manga_dirs()
    tmp_id = uuid.uuid4().hex
    tmp_path = os.path.join(MANGA_TMP_DIR, f"{tmp_id}.jpg")
    await file.download_to_drive(tmp_path)

    await update.message.reply_text("🖼️ Procesando imagen…")

    # Lazy import to avoid an import cycle with agent/llm_agent at module load.
    try:
        from agent.llm_agent import manga_extract  # type: ignore
    except Exception:
        try:
            from llm_agent import manga_extract  # type: ignore
        except Exception:
            log.error("manga_extract import failed")
            await update.message.reply_text(
                "❌ El extractor de manga no está disponible."
            )
            return
    try:
        result = await asyncio.to_thread(manga_extract, tmp_path)
    except Exception as e:
        log.error("manga_extract failed: %s", e)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        await update.message.reply_text(f"❌ Error procesando la imagen: {e}")
        return

    is_manga = bool(result.get("is_manga"))

    if is_manga:
        bubbles = result.get("bubbles", [])
        if not bubbles:
            await update.message.reply_text(
                "⚠️ Detecté que es manga pero no encontré texto. La imagen no se guardó."
            )
            return

        # P2: don't create cards automatically. Stash and show the menu.
        try:
            language_set = sorted(set((b.get("language") or "en") for b in bubbles))
        except Exception:
            language_set = ["en"]
        context.user_data["pending_manga"] = {
            "image_path": str(tmp_path),
            "bubbles": bubbles,
            "language_set": language_set,
            "original_image_path": str(tmp_path),  # alias: P3 default
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        n = len(bubbles)
        await update.message.reply_text(
            f"📥 Imagen procesada. Detecté {n} burbuja(s). ¿Dónde la guardamos?",
            parse_mode=None,
        )

        # P3: check for a saved default
        try:
            defaults = load_manga_defaults()
            user_key = f"user_{update.effective_user.id}"
            default = defaults.get(user_key)
        except Exception as e:
            log.error("handle_image(manga): load_manga_defaults failed: %s", e)
            default = None

        if default and default.get("volume_id"):
            vol_id = default["volume_id"]
            vol_name = default.get("volume_name") or "Volumen"
            try:
                conn = storage.get_db()
                row = conn.execute(
                    "SELECT id, name FROM decks WHERE id = ?", (vol_id,)
                ).fetchone()
                conn.close()
            except Exception as e:
                log.error("handle_image(manga): default check failed: %s", e)
                row = None

            if row:
                # Default is valid → save and reply
                actual_vol_name = row["name"]
                serie_name = default.get("serie_name") or "Manga"
                try:
                    pending = context.user_data["pending_manga"]
                    save_result = save_pending_to_deck(
                        pending, deck_id=row["id"], deck_name=actual_vol_name,
                    )
                except Exception as e:
                    log.error("handle_image(manga): default save failed: %s", e)
                    await update.message.reply_text(
                        f"❌ Error guardando en el default {serie_name} / {actual_vol_name}: {e}",
                        parse_mode=None,
                    )
                    await _manga_show_destination_menu(update, context)
                    return
                context.user_data.pop("pending_manga", None)
                card_count = save_result.get("card_count", len(pending["bubbles"]))
                await update.message.reply_text(
                    f"✅ Guardadas {card_count} card(s) en {serie_name} / {actual_vol_name} (default).",
                    parse_mode=None,
                    reply_markup=_manga_done_keyboard(),
                )
                return
            else:
                # Default is broken (volume deleted) — warn and fall through
                serie_name = default.get("serie_name") or "Manga"
                vol_name_disp = default.get("volume_name") or "(volumen)"
                await update.message.reply_text(
                    f"⚠️ Tu default '{serie_name} / {vol_name_disp}' ya no existe. "
                    f"Elige destino:",
                    parse_mode=None,
                )
                # No return — fall through to the menu

        # No default (or broken default) → show the P2 menu
        await _manga_show_destination_menu(update, context)
    else:
        # Not manga: buffer the OCR text and ask the user what to do.
        text = result.get("text", "").strip()
        language = result.get("language", "unknown")
        if not text:
            await update.message.reply_text(
                "⚠️ No detecté manga ni texto legible en la imagen."
            )
            return
        context.user_data["pending_ocr"] = {
            "text": text,
            "language": language,
            "image_path": tmp_path,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        preview = text if len(text) <= 500 else text[:500] + "…"
        await update.message.reply_text(
            f"📄 Esto no parece manga. Detecté texto en *{language}*:\n\n"
            f"```\n{preview}\n```\n\n"
            "¿Qué quieres hacer con él? (de momento solo te lo muestro)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_manga_done_keyboard(),
        )


# ==============================================================================
# P2: DESTINATION / VOLUME MENUS
# ==============================================================================
async def _manga_show_destination_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the main P2 menu: existing series + 'Crear nueva' + 'Cancelar'."""
    pending = context.user_data.get("pending_manga")
    if not pending:
        text = menus.text_no_pending()
        kb = _manga_done_keyboard()
        if update.callback_query:
            await update.callback_query.answer()
            try:
                await update.callback_query.edit_message_text(
                    text, parse_mode=None, reply_markup=kb,
                )
            except Exception:
                await update.callback_query.message.reply_text(
                    text, parse_mode=None, reply_markup=kb,
                )
        else:
            await update.message.reply_text(text, parse_mode=None, reply_markup=kb)
        return

    n = len(pending.get("bubbles", []) or [])
    try:
        tree = get_manga_deck_hierarchy()
    except Exception as e:
        log.error("_manga_show_destination_menu: get_hierarchy failed: %s", e)
        tree = {"series": []}

    text, kb = menus.kbd_destination(tree.get("series", []), n_bubbles=n)

    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=None, reply_markup=kb,
            )
        except Exception:
            await update.callback_query.message.reply_text(
                text, parse_mode=None, reply_markup=kb,
            )
    else:
        await update.message.reply_text(text, parse_mode=None, reply_markup=kb)


async def _manga_show_volume_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  serie_id: int) -> None:
    """Show the volume picker for a given serie."""
    pending = context.user_data.get("pending_manga")
    if not pending:
        await _manga_show_destination_menu(update, context)
        return

    try:
        tree = get_manga_deck_hierarchy()
    except Exception as e:
        log.error("_manga_show_volume_menu: get_hierarchy failed: %s", e)
        tree = {"series": []}

    serie = None
    for s in tree.get("series", []):
        if int(s["id"]) == int(serie_id):
            serie = s
            break
    if not serie:
        text = "⚠️ La serie ya no existe. Volviendo al menú principal."
        await update.callback_query.answer(text)
        await _manga_show_destination_menu(update, context)
        return

    n = len(pending.get("bubbles", []) or [])
    text, kb = menus.kbd_volume(serie, n_bubbles=n)

    await update.callback_query.answer()
    try:
        await update.callback_query.edit_message_text(
            text, parse_mode=None, reply_markup=kb,
        )
    except Exception:
        await update.callback_query.message.reply_text(
            text, parse_mode=None, reply_markup=kb,
        )


async def cb_manga_select_serie(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback ``mselect:<serie_id>`` — show the volume menu."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    try:
        serie_id = int(update.callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await update.callback_query.answer("Callback inválido.", show_alert=True)
        return
    await _manga_show_volume_menu(update, context, serie_id=serie_id)


async def cb_manga_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback ``mnew`` — ask the user for serie+volume via text input."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    pending = context.user_data.get("pending_manga")
    if not pending:
        await update.callback_query.answer("No hay imagen pendiente.", show_alert=True)
        return

    context.user_data["awaiting_manga_input"] = {
        "action": "new_serie_volume",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    text = (
        "📝 Escribe el nombre de la serie y el volumen. Ejemplos:\n"
        "• Naruto 3\n"
        "• Dragon Ball tomo 5\n"
        "• Bleach vol. 10.5"
    )
    await update.callback_query.answer()
    try:
        await update.callback_query.edit_message_text(text, parse_mode=None)
    except Exception:
        await update.callback_query.message.reply_text(text, parse_mode=None)


async def cb_manga_newvol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback ``mnewvol:<serie_id>`` — ask for a volume number via text."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    pending = context.user_data.get("pending_manga")
    if not pending:
        await update.callback_query.answer("No hay imagen pendiente.", show_alert=True)
        return
    try:
        serie_id = int(update.callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await update.callback_query.answer("Callback inválido.", show_alert=True)
        return

    context.user_data["awaiting_manga_input"] = {
        "action": "new_volume",
        "serie_id": serie_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    text = "📝 ¿Qué número de volumen? (ej. '3', '10.5', 'tomo 7')"
    await update.callback_query.answer()
    try:
        await update.callback_query.edit_message_text(text, parse_mode=None)
    except Exception:
        await update.callback_query.message.reply_text(text, parse_mode=None)


async def cb_manga_vol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback ``mvol:<vol_id>`` — save cards into an existing volume."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    try:
        vol_id = int(update.callback_query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await update.callback_query.answer("Callback inválido.", show_alert=True)
        return

    pending = context.user_data.get("pending_manga")
    if not pending:
        await update.callback_query.answer("No hay imagen pendiente.", show_alert=True)
        return

    # Drop input state so a partial failure doesn't leave garbage around
    context.user_data.pop("awaiting_manga_input", None)

    # Resolve the volume name for the success message
    vol_name = None
    try:
        tree = get_manga_deck_hierarchy()
        for s in tree.get("series", []):
            for v in s.get("volumes", []):
                if int(v["id"]) == int(vol_id):
                    vol_name = v["name"]
                    break
            if vol_name:
                break
    except Exception as e:
        log.warning("cb_manga_vol: hierarchy lookup failed: %s", e)

    try:
        result = save_pending_to_deck(pending, deck_id=vol_id, deck_name=vol_name or "")
    except Exception as e:
        log.error("cb_manga_vol: save failed: %s", e)
        context.user_data.pop("pending_manga", None)
        try:
            await update.callback_query.edit_message_text(
                f"❌ Error guardando: {e}", parse_mode=None,
                reply_markup=_manga_done_keyboard(),
            )
        except Exception:
            await update.callback_query.message.reply_text(
                f"❌ Error guardando: {e}", parse_mode=None,
            )
        return

    n = result.get("card_count", 0)
    final_label = vol_name or f"deck #{vol_id}"
    context.user_data.pop("pending_manga", None)
    try:
        await update.callback_query.edit_message_text(
            f"✅ Guardadas {n} card(s) en {final_label}.",
            parse_mode=None,
            reply_markup=_manga_done_keyboard(),
        )
    except Exception:
        await update.callback_query.message.reply_text(
            f"✅ Guardadas {n} card(s) en {final_label}.",
            parse_mode=None,
            reply_markup=_manga_done_keyboard(),
        )


async def cb_manga_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback ``mback:<dest>`` — currently only ``mback:dest`` (main menu)."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    dest = (update.callback_query.data.split(":", 1)[1]
            if ":" in update.callback_query.data else "dest")
    if dest == "dest":
        await _manga_show_destination_menu(update, context)
    else:
        await update.callback_query.answer()
        await _manga_show_destination_menu(update, context)


async def cb_manga_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback ``mcancel`` — clear P2 state and ack."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    pending = context.user_data.get("pending_manga")
    tmp_path = (pending or {}).get("image_path")
    context.user_data.pop("pending_manga", None)
    context.user_data.pop("awaiting_manga_input", None)
    if tmp_path and os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    await update.callback_query.answer("Cancelado")
    try:
        await update.callback_query.edit_message_text(
            "❌ Cancelado.", parse_mode=None,
            reply_markup=_manga_done_keyboard(),
        )
    except Exception:
        await update.callback_query.message.reply_text(
            "❌ Cancelado.", parse_mode=None,
            reply_markup=_manga_done_keyboard(),
        )


# ==============================================================================
# P2: LLM PARSER + TEXT HANDLER
# ==============================================================================
def parse_manga_serie_volume(text: str) -> dict | None:
    """Use deepseek-chat to extract ``{serie, volumen}`` from a free-text
    user input.

    Returns a dict with ``serie`` (str) and ``volumen`` (int or str), or
    ``None`` on failure. Validates:

    * JSON parseable
    * ``serie`` is a non-empty string
    * ``volumen`` is int or str
    """
    if not text or not text.strip():
        return None
    if not DEEPSEEK_KEY:
        log.warning("parse_manga_serie_volume: no DEEPSEEK_KEY")
        return None
    prompt = (
        "Extrae el nombre de la serie y el número de volumen del siguiente texto. "
        'Responde SOLO JSON válido: {"serie": "nombre", "volumen": N o "string"}. '
        f'Texto: "{text.strip()}"'
    )
    payload = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 80,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        DEEP_URL, data=payload,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = json.loads(resp.read())
        content = body["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.warning(f"parse_manga_serie_volume: API call failed: {e}")
        return None
    # Strip ```json ... ``` fences
    if content.startswith("```"):
        try:
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        except Exception:
            pass
    try:
        data = json.loads(content)
    except Exception as e:
        log.warning(f"parse_manga_serie_volume: json parse failed: {e} — content: {content!r}")
        return None
    if not isinstance(data, dict):
        return None
    serie = data.get("serie")
    volumen = data.get("volumen")
    if not isinstance(serie, str) or not serie.strip():
        return None
    if isinstance(volumen, bool):
        return None
    if isinstance(volumen, (int, float)):
        if isinstance(volumen, float) and not volumen.is_integer():
            volumen = str(volumen)
        else:
            volumen = int(volumen)
    elif isinstance(volumen, str):
        volumen = volumen.strip()
        if not volumen:
            return None
    else:
        return None
    return {"serie": serie.strip(), "volumen": volumen}


def _parse_volume_number_local(text: str):
    """Local fallback for the ``new_volume`` flow: just a number under
    an existing serie. Accepts ``'3'``, ``'10.5'``, ``'tomo 7'``,
    ``'vol 2'``, ``'capitulo 4'``."""
    t = (text or "").strip()
    if not t:
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        f = float(t)
        if f.is_integer():
            return int(f)
        return str(f)
    except ValueError:
        pass
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if m:
        s = m.group(1)
        if "." in s:
            return s
        try:
            return int(s)
        except ValueError:
            return s
    return None


async def _manga_handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    text: str) -> bool:
    """Process a ``awaiting_manga_input`` action from the user.

    Returns ``True`` if it consumed the message (handler should stop),
    ``False`` to let the normal text/agent flow take over.

    Supported actions:

    * ``new_serie_volume`` — LLM-parse the text, create serie+volume,
      save cards.
    * ``new_volume`` — local-parse the volume number, create volume,
      save cards.
    """
    state = context.user_data.get("awaiting_manga_input")
    if not state:
        return False
    pending = context.user_data.get("pending_manga")
    action = state.get("action")

    # Drop input state up front so a partial failure doesn't leave garbage
    context.user_data.pop("awaiting_manga_input", None)

    if not pending:
        await update.message.reply_text(
            "⚠️ Ya no hay imagen pendiente. Envíame otra imagen.",
            parse_mode=None,
        )
        return True

    if action == "new_serie_volume":
        await update.message.reply_text("⏳ Parseando con el modelo…", parse_mode=None)
        parsed = parse_manga_serie_volume(text)
        if not parsed:
            await update.message.reply_text(
                "❌ No pude entender la serie/volumen. "
                "Inténtalo de nuevo con el formato: 'Naruto 3', "
                "'Dragon Ball tomo 5', 'Bleach vol. 10.5'.\n\n"
                "Pulsa ❌ Cancelar en el menú para salir.",
                parse_mode=None,
            )
            return True
        serie_name = parsed["serie"]
        volumen = parsed["volumen"]
        try:
            serie = create_manga_serie(serie_name)
            serie_id = serie["id"]
        except Exception as e:
            log.error("new_serie_volume: create_manga_serie failed: %s", e)
            await update.message.reply_text(
                f"❌ Error creando la serie: {e}", parse_mode=None,
            )
            context.user_data.pop("pending_manga", None)
            return True
        try:
            vol = create_manga_volume(serie_name, volumen)
            vol_id = vol["id"]
        except Exception as e:
            log.error("new_serie_volume: create_manga_volume failed: %s", e)
            await update.message.reply_text(
                f"❌ Error creando el volumen: {e}", parse_mode=None,
            )
            context.user_data.pop("pending_manga", None)
            return True
        try:
            result = save_pending_to_deck(pending, deck_id=vol_id,
                                          deck_name=vol.get("name"))
        except Exception as e:
            log.error("new_serie_volume: save_pending_to_deck failed: %s", e)
            await update.message.reply_text(
                f"❌ Error guardando: {e}", parse_mode=None,
            )
            context.user_data.pop("pending_manga", None)
            return True
        n = result.get("card_count", 0)
        context.user_data.pop("pending_manga", None)
        await update.message.reply_text(
            f"✅ Serie '{serie_name}' → {vol.get('name')}. "
            f"Guardadas {n} card(s).",
            parse_mode=None,
            reply_markup=_manga_done_keyboard(),
        )
        return True

    if action == "new_volume":
        serie_id = state.get("serie_id")
        if not serie_id:
            await update.message.reply_text(
                "❌ Falta la serie. Vuelve a empezar.", parse_mode=None,
            )
            context.user_data.pop("pending_manga", None)
            return True
        serie_name = None
        try:
            tree = get_manga_deck_hierarchy()
            for s in tree.get("series", []):
                if int(s["id"]) == int(serie_id):
                    serie_name = s["name"]
                    break
        except Exception as e:
            log.warning("new_volume: hierarchy lookup failed: %s", e)
        if not serie_name:
            await update.message.reply_text(
                "❌ La serie ya no existe. Vuelve a empezar.", parse_mode=None,
            )
            context.user_data.pop("pending_manga", None)
            return True
        volumen = _parse_volume_number_local(text)
        if volumen is None:
            await update.message.reply_text(
                "❌ No detecté un número de volumen. "
                "Prueba con: '3', '10.5', 'tomo 7'.",
                parse_mode=None,
            )
            return True
        try:
            vol = create_manga_volume(serie_name, volumen)
            vol_id = vol["id"]
        except Exception as e:
            log.error("new_volume: create_manga_volume failed: %s", e)
            await update.message.reply_text(
                f"❌ Error creando el volumen: {e}", parse_mode=None,
            )
            context.user_data.pop("pending_manga", None)
            return True
        try:
            result = save_pending_to_deck(pending, deck_id=vol_id,
                                          deck_name=vol.get("name"))
        except Exception as e:
            log.error("new_volume: save_pending_to_deck failed: %s", e)
            await update.message.reply_text(
                f"❌ Error guardando: {e}", parse_mode=None,
            )
            context.user_data.pop("pending_manga", None)
            return True
        n = result.get("card_count", 0)
        context.user_data.pop("pending_manga", None)
        await update.message.reply_text(
            f"✅ {serie_name} → {vol.get('name')}. "
            f"Guardadas {n} card(s).",
            parse_mode=None,
            reply_markup=_manga_done_keyboard(),
        )
        return True

    log.warning(f"_manga_handle_text_input: unknown action {action!r}")
    return True


# ==============================================================================
# P3: /z DEFAULT MODE
# ==============================================================================
async def z_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler for ``/z`` (case-insensitive — PTB normalises to lowercase)."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    if not update.message or not update.message.text:
        return
    raw = update.message.text.strip()
    parts = raw.split(maxsplit=1)
    if len(parts) == 1:
        await _z_show_default(update, context)
        return
    rest = parts[1].strip()
    if not rest:
        await _z_show_default(update, context)
        return
    rest_lower = rest.lower()
    if rest_lower in ("off", "clear", "reset"):
        await _z_clear_default(update, context)
        return
    if rest_lower in ("show", "status", "info", "list"):
        await _z_show_default(update, context)
        return
    if rest_lower.startswith("manga"):
        text = rest[5:].strip()  # strip "manga" or "manga "
        if not text:
            await update.message.reply_text(
                "❌ Falta el texto de la serie/volumen.\n"
                "Uso: /z manga <serie> <volumen>\n"
                "Ejemplos: 'Naruto 3', 'Dragon Ball tomo 5', 'Bleach vol. 10'",
                parse_mode=None,
            )
            return
        await _z_set_default(update, context, text)
        return
    await update.message.reply_text(
        "Uso: /z manga <serie> <volumen>\n"
        "     /z show (ver default)\n"
        "     /z off (limpiar default)\n\n"
        "Ejemplos:\n"
        "  /z manga Naruto 3\n"
        "  /z manga Dragon Ball tomo 5\n"
        "  /Z (muestra el default actual)",
        parse_mode=None,
    )


async def _z_show_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current user's manga default (or 'no hay default')."""
    user_key = f"user_{update.effective_user.id}"
    try:
        defaults = load_manga_defaults()
    except Exception as e:
        log.error("_z_show_default: load failed: %s", e)
        defaults = {}
    default = defaults.get(user_key)
    if not default or not default.get("volume_id"):
        await update.message.reply_text(
            "ℹ️ No hay default de manga configurado.\n"
            "Configura uno con: /z manga <serie> <volumen>\n"
            "Limpia con: /z off",
            parse_mode=None,
        )
        return

    serie_name = default.get("serie_name") or "(serie)"
    vol_name = default.get("volume_name") or "(volumen)"
    set_at = default.get("set_at") or "?"
    vol_id = default.get("volume_id")
    exists = False
    actual_vol_name = vol_name
    try:
        conn = storage.get_db()
        row = conn.execute(
            "SELECT id, name FROM decks WHERE id = ?", (vol_id,)
        ).fetchone()
        conn.close()
        if row:
            exists = True
            actual_vol_name = row["name"]
    except Exception as e:
        log.warning("_z_show_default: deck check failed: %s", e)

    if exists:
        await update.message.reply_text(
            f"📌 Default actual: Manga / {serie_name} / {actual_vol_name}\n"
            f"   (set_at: {set_at})\n\n"
            f"Las próximas imágenes de manga se guardarán ahí automáticamente.\n"
            f"Cambia con: /z manga <otra> <vol>\n"
            f"Limpia con: /z off",
            parse_mode=None,
        )
    else:
        await update.message.reply_text(
            f"⚠️ Default roto: Manga / {serie_name} / {vol_name} "
            f"(volume_id={vol_id} ya no existe).\n"
            f"Será ignorado hasta que lo actualices con: /z manga <serie> <vol>",
            parse_mode=None,
        )


async def _z_clear_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove the current user's manga default."""
    user_key = f"user_{update.effective_user.id}"
    try:
        defaults = load_manga_defaults()
    except Exception as e:
        log.error("_z_clear_default: load failed: %s", e)
        defaults = {}
    if user_key not in defaults:
        await update.message.reply_text(
            "ℹ️ No había default configurado.", parse_mode=None,
        )
        return
    del defaults[user_key]
    try:
        save_manga_defaults(defaults)
    except Exception as e:
        log.error("_z_clear_default: save failed: %s", e)
        await update.message.reply_text(
            f"❌ Error guardando: {e}", parse_mode=None,
        )
        return
    await update.message.reply_text(
        "🗑️ Default de manga limpiado. Las próximas imágenes mostrarán "
        "el menú de destino.",
        parse_mode=None,
    )


async def _z_set_default(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Parse ``text``, create serie+volume (idempotent) and save as the
    current user's default."""
    await update.message.reply_text("⏳ Parseando con el modelo…", parse_mode=None)
    parsed = parse_manga_serie_volume(text)
    if not parsed:
        await update.message.reply_text(
            "❌ No pude entender la serie/volumen. "
            "Inténtalo de nuevo con el formato: 'Naruto 3', "
            "'Dragon Ball tomo 5', 'Bleach vol. 10'.\n\n"
            "Reformula: /z manga <serie> <volumen>",
            parse_mode=None,
        )
        return
    serie_name = parsed["serie"]
    volumen = parsed["volumen"]
    try:
        serie = create_manga_serie(serie_name)
    except Exception as e:
        log.error("_z_set_default: create_manga_serie failed: %s", e)
        await update.message.reply_text(
            f"❌ Error creando la serie '{serie_name}': {e}",
            parse_mode=None,
        )
        return
    try:
        vol = create_manga_volume(serie_name, volumen)
    except Exception as e:
        log.error("_z_set_default: create_manga_volume failed: %s", e)
        await update.message.reply_text(
            f"❌ Error creando el volumen {volumen} de '{serie_name}': {e}",
            parse_mode=None,
        )
        return
    vol_id = vol["id"]
    serie_id = serie["id"]
    try:
        conn = storage.get_db()
        row = conn.execute(
            "SELECT id, name FROM decks WHERE id = ?", (vol_id,)
        ).fetchone()
        conn.close()
        vol_db_name = row["name"] if row else vol.get("name") or f"Volumen {volumen}"
    except Exception as e:
        log.warning("_z_set_default: deck reload failed: %s", e)
        vol_db_name = vol.get("name") or f"Volumen {volumen}"
    try:
        defaults = load_manga_defaults()
    except Exception as e:
        log.error("_z_set_default: load defaults failed: %s", e)
        defaults = {}
    user_key = f"user_{update.effective_user.id}"
    defaults[user_key] = {
        "serie_id": serie_id,
        "serie_name": serie_name,
        "volume_id": vol_id,
        "volume_name": vol_db_name,
        "set_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        save_manga_defaults(defaults)
    except Exception as e:
        log.error("_z_set_default: save defaults failed: %s", e)
        await update.message.reply_text(
            f"❌ Error guardando el default: {e}", parse_mode=None,
        )
        return
    await update.message.reply_text(
        f"✅ Default establecido: Manga / {serie_name} / {vol_db_name}. "
        f"Las próximas imágenes se guardarán ahí.",
        parse_mode=None,
    )


# ==============================================================================
# PRACTICE MODE (Anki-style 3 buttons: Again / Good / Easy)
# ==============================================================================
async def manga_practice_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start a manga practice session from the Manga deck menu."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    try:
        deck = get_or_create_deck_by_name("Manga", "📖")
        deck_id = deck["id"] if isinstance(deck, dict) else int(deck)
    except Exception as e:
        log.error(f"manga_practice_start: deck lookup failed: {e}")
        text = "❌ No pude localizar el deck 'Manga'."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, parse_mode=None, reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode=None, reply_markup=kb)
        return
    try:
        cards = get_manga_cards_for_practice(deck_id, limit=20)
    except Exception as e:
        log.error(f"manga_practice_start: list cards failed: {e}")
        cards = []
    if not cards:
        text = "🎉 No hay flashcards de manga pendientes."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, parse_mode=None, reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode=None, reply_markup=kb)
        return
    context.user_data["manga_practice"] = {
        "card_ids": [c["id"] for c in cards],
        "current_idx": 0,
        "deck_id": deck_id,
    }
    await _manga_show_card(update, context)


async def _manga_show_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the front of the current practice card (or the done screen)."""
    session = context.user_data.get("manga_practice") or {}
    card_ids = session.get("card_ids") or []
    idx = session.get("current_idx", 0)
    if idx >= len(card_ids):
        text = "🎉 Completaste las flashcards de manga!"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(text, parse_mode=None, reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode=None, reply_markup=kb)
        return
    try:
        card = get_manga_card(card_ids[idx])
    except Exception as e:
        log.error(f"_manga_show_card: get_manga_card({card_ids[idx]}) failed: {e}")
        card = None
    if not card:
        # Skip missing card and try the next one
        session["current_idx"] = idx + 1
        context.user_data["manga_practice"] = session
        await _manga_show_card(update, context)
        return
    # Front: original text + language + (if exists) full image
    text = (
        f"📖 **Manga flashcard {idx + 1}/{len(card_ids)}**\n\n"
        f"_{card.get('language','')}_\n\n"
        f"**{card.get('original_text','')}**"
    )
    img = card.get("image_path")
    kb = menus.kbd_practice_front(card["id"])
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(text, parse_mode=None, reply_markup=kb)
        except Exception:
            await update.callback_query.message.reply_text(text, parse_mode=None, reply_markup=kb)
        if img and Path(img).exists():
            try:
                with open(img, "rb") as f:
                    await update.callback_query.message.reply_photo(
                        photo=f, caption="🖼 Imagen completa", reply_markup=kb,
                    )
            except Exception as e:
                log.warning(f"_manga_show_card: send photo failed: {e}")
    else:
        if img and Path(img).exists():
            try:
                with open(img, "rb") as f:
                    await update.message.reply_photo(
                        photo=f, caption=text, parse_mode=None, reply_markup=kb,
                    )
            except Exception:
                await update.message.reply_text(text, parse_mode=None, reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode=None, reply_markup=kb)


async def cb_manga_flip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback 'Ver respuesta': show the back of a card."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.callback_query.answer()
    try:
        card_id = int(update.callback_query.data.split(":")[1])
    except (ValueError, IndexError):
        await update.callback_query.edit_message_text("❌ Callback inválido.")
        return
    try:
        card = get_manga_card(card_id)
    except Exception as e:
        log.error(f"cb_manga_flip: get_manga_card({card_id}) failed: {e}")
        card = None
    if not card:
        await update.callback_query.edit_message_text("❌ Card no encontrada.")
        return
    text = (
        f"📖 **Back**\n\n"
        f"🌐 **Traducción:**\n{card.get('translation','')}\n\n"
        f"💡 **Explicación inteligente:**\n{card.get('smart_explanation','')}"
    )
    kb = menus.kbd_practice_back(card_id)
    await update.callback_query.edit_message_text(text, parse_mode=None, reply_markup=kb)


async def cb_manga_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grade callback: apply SM-2 via mark_manga_card_review and advance."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.callback_query.answer()
    parts = update.callback_query.data.split(":")
    try:
        card_id = int(parts[1])
        grade_str = parts[2]
    except (ValueError, IndexError):
        await update.callback_query.edit_message_text("❌ Callback inválido.")
        return
    qm = {"again": 0, "good": 3, "easy": 5}  # same mapping as w3_eval
    grade = qm.get(grade_str)
    if grade is None:
        await update.callback_query.edit_message_text("❌ Grade inválido.")
        return
    try:
        mark_manga_card_review(card_id, grade)
    except SrsError as e:
        log.error(f"cb_manga_save: SrsError {e}")
    except Exception as e:
        log.error(f"cb_manga_save: unexpected {e}")
    # Advance to the next card
    session = context.user_data.get("manga_practice") or {}
    session["current_idx"] = session.get("current_idx", 0) + 1
    context.user_data["manga_practice"] = session
    await _manga_show_card(update, context)


async def cb_manga_quit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback 'Salir': cancel the manga practice session."""
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    await update.callback_query.answer("Sesión cancelada")
    context.user_data.pop("manga_practice", None)
    text = "👋 Practice de manga cancelado."
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Volver", callback_data="nav:back")]])
    await update.callback_query.edit_message_text(text, parse_mode=None, reply_markup=kb)


# ==============================================================================
# CALLBACK DISPATCH HELPER
# ==============================================================================
# Convenience for the bot's main callback dispatcher: a single function that
# routes the ``manga_*`` callback data prefixes to the appropriate handler.
# Returns ``True`` if it handled the callback, ``False`` otherwise.
MANGA_CALLBACK_PREFIXES = (
    "mgprac:", "mgflip:", "mgsave:", "mgquit:",
    "mselect:", "mnew", "mnewvol:", "mvol:", "mback:", "mcancel",
)
# Note: "mgquit" (no colon) is the cancel callback, "mnew" too. Keep them
# as exact-match keys alongside the prefix patterns above.


async def _manga_dispatch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   data: str) -> bool:
    """Route a manga-related callback to its handler. Returns True if handled."""
    if data == "mgprac":
        await manga_practice_start(update, context)
        return True
    if data.startswith("mgflip:"):
        await cb_manga_flip(update, context)
        return True
    if data.startswith("mgsave:"):
        await cb_manga_save(update, context)
        return True
    if data == "mgquit":
        await cb_manga_quit(update, context)
        return True
    if data.startswith("mselect:"):
        await cb_manga_select_serie(update, context)
        return True
    if data == "mnew":
        await cb_manga_new(update, context)
        return True
    if data.startswith("mnewvol:"):
        await cb_manga_newvol(update, context)
        return True
    if data.startswith("mvol:"):
        await cb_manga_vol(update, context)
        return True
    if data.startswith("mback:"):
        await cb_manga_back(update, context)
        return True
    if data == "mcancel":
        await cb_manga_cancel(update, context)
        return True
    return False

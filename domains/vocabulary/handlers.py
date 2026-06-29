"""
domains.vocabulary.handlers — Telegram handlers for the vocabulary domain.

Extracted from the original monolith's `scripts/task_bot.py` and adapted to
the new module layout (paths / caches / keyboards come from
:mod:`domains.vocabulary.storage` and :mod:`domains.vocabulary.menus`).

Public surface
--------------
Handlers
* :func:`sr` — root Vocab menu callback (``revision``).
* :func:`show_decks` — render a deck-level menu.
* :func:`show_deck_practice` — render the first due word in a specific deck.
* :func:`show_wd` — show the first due palabra-del-día card.
* :func:`w1_fail` / :func:`w1_skip` — grade a palabra card as 0 and advance.
* :func:`show_next_wd` — advance to the next palabra-del-día card.
* :func:`s_practice` — start "Practice All" (mix all due cards).
* :func:`show_practice_card` — render one practice card.
* :func:`pf_flip` / :func:`pf_fail` / :func:`pf_self_eval` / :func:`pf_done` — Practice All actions.
* :func:`s_stats` — render the Vocab stats view (mirrors :func:`task_bot.s_stats`).

Helpers
* :func:`save_practice_review` — persist a review for a practice card.

Design notes
------------
The handlers depend on three pieces of context state:

* ``context.user_data['due_cache']`` — per-user due-list cache.
* ``context.user_data['practice']`` — current Practice All session.
* ``context.user_data['nav_stack']`` — global nav stack (managed elsewhere).

TTS is delegated to a callback :func:`say_word` injected via
:func:`register_handlers` so this module stays free of bot-token / voice
configuration.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import DATA, STATE_DB
from domains.vocabulary import menus, storage
from domains.vocabulary.menus import (
    kbd_practice_conceptual,
    kbd_practice_flip,
    kbd_practice_flip_back,
    kbd_practice_palabra,
    kbd_word_practice,
)
from domains.vocabulary.srs_algorithm import sm2_self

log = logging.getLogger("vocab.handlers")


# -------------------------------------------------------------------------- #
# TTS hook
# -------------------------------------------------------------------------- #
# Injected by :func:`register_handlers` so the domain doesn't need to know
# about edge-tts / bot configuration.  Signature: ``async (word, lang, update, context) -> None``.
_say_word = None


def _say(word: str, lang: str, update, context) -> None:
    """Send a TTS voice for ``word``. Falls back to a no-op if no hook is set."""
    if _say_word is None:
        return
    try:
        import asyncio
        coro = _say_word(word, lang, update, context)
        if coro is not None:
            # We may be called from a sync context (callbacks always async);
            # create_task is the right primitive if there's a running loop.
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(coro)
                else:
                    loop.run_until_complete(coro)
            except RuntimeError:
                pass
    except Exception as e:
        log.debug(f"_say failed for {word!r}: {e}")


# ==============================================================================
# ROOT VOCAB MENU
# ==============================================================================
async def sr(update, context) -> None:
    """Render the root Vocab menu (callback ``revision``)."""
    q = update.callback_query
    text, kb = menus.rev_menu()
    await q.answer()
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


# ==============================================================================
# DECK NAVIGATION
# ==============================================================================
async def show_decks(update, context, parent_id=None, deck_label=None) -> None:
    """Show decks at a given level. ``parent_id=None`` = root Vocab menu."""
    q = update.callback_query
    await q.answer()
    text, kb = menus.kbd_deck_buttons(parent_id=parent_id, deck_label=deck_label)
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)


async def show_deck_practice(update, context, deck_id, deck_label=None) -> None:
    """Show Palabra-del-Día-style cards filtered to a specific deck."""
    q = update.callback_query
    await q.answer()
    wl = storage.due_in_deck(deck_id)
    if not wl:
        text = (f"\U0001f4d6 **{deck_label or 'Deck'}** \u2014 "
                "\U0001f389 Sin palabras pendientes.")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u2b05\ufe0f Volver", callback_data="nav:back")
        ]])
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        return
    w = wl[0]
    if deck_label:
        header = (f"\U0001f4d6 **{deck_label}** \u2014 "
                  f"Palabra #{w['id']} ({len(wl)} restantes)")
    else:
        header = f"\U0001f4d6 **Palabra #{w['id']}** ({len(wl)} restantes)"
    text = (
        f"{header}\n\n"
        f"**{w['word']}** ({w['lang']})\n"
        f"\U0001f4ac *Contexto:* _{w.get('sentence','')}_\n\n"
        f"||{w.get('definition','')}||\n\n"
        f"_Escribe una oración usando esta palabra._"
    )
    await q.edit_message_text(text, parse_mode="Markdown",
                              reply_markup=kbd_word_practice(w["id"]))
    _say(w["word"], w["lang"], update, context)


# ==============================================================================
# TYPE 1: PALABRA DEL DÍA
# ==============================================================================
async def show_wd(update, context) -> None:
    """Show the first due palabra-del-día card."""
    wl = storage.due()
    if not wl:
        text, kb = menus.rev_menu()
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
        return
    w = wl[0]
    text = (
        f"\U0001f4d6 **Palabra #{w['id']}** ({len(wl)} restantes)\n\n"
        f"**{w['word']}** ({w['lang']})\n"
        f"\U0001f4ac *Contexto:* _{w.get('sentence','')}_\n\n"
        f"||{w.get('definition','')}||\n\n"
        f"_Escribe una oración usando esta palabra._"
    )
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=kbd_word_practice(w["id"])
    )
    _say(w["word"], w["lang"], update, context)


async def w1_fail(update, context, wid) -> None:
    """User didn't know the word → quality=0, save, advance."""
    storage._srs_review_word(wid, 0, "fail")
    storage._invalidate_srs_caches()
    await update.callback_query.answer("\u274c Repaso manana.")
    await show_next_wd(update, context)


async def w1_skip(update, context, wid) -> None:
    """User skipped the word → quality=0 ("skip"), advance."""
    storage._srs_review_word(wid, 0, "skip")
    storage._invalidate_srs_caches()
    await update.callback_query.answer("\u23e9 Saltada.")
    await show_next_wd(update, context)


async def show_next_wd(update, context) -> None:
    """Advance to the next due palabra-del-día card."""
    wl = storage.due()
    if not wl:
        text, kb = menus.rev_menu()
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
        return
    w = wl[0]
    text = (
        f"\U0001f4d6 **Palabra #{w['id']}** ({len(wl)} restantes)\n\n"
        f"**{w['word']}** ({w['lang']})\n"
        f"\U0001f4ac *Contexto:* _{w.get('sentence','')}_\n\n"
        f"||{w.get('definition','')}||\n\n"
        f"_Escribe una oración._"
    )
    await update.callback_query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=kbd_word_practice(w["id"])
    )
    _say(w["word"], w["lang"], update, context)


# ==============================================================================
# PRACTICE ALL
# ==============================================================================
def _load_practice() -> dict:
    """Load the persisted Practice All session (if any)."""
    practice_path = DATA / "practice_session.json"
    try:
        if practice_path.exists():
            return json.loads(practice_path.read_text())
    except Exception:
        pass
    return {}


def _save_practice(data: dict) -> None:
    """Persist the Practice All session."""
    practice_path = DATA / "practice_session.json"
    try:
        practice_path.write_text(json.dumps(data, ensure_ascii=False))
    except Exception as e:
        log.debug(f"_save_practice: {e}")


def _clear_practice() -> None:
    """Delete the persisted Practice All session."""
    practice_path = DATA / "practice_session.json"
    try:
        if practice_path.exists():
            practice_path.unlink()
    except Exception:
        pass


async def s_practice(update, context) -> None:
    """Start Practice All: mix all due cards, show one at a time in order."""
    # Per-user due cache
    if "due_cache" not in context.user_data:
        context.user_data["due_cache"] = {"data": None, "ts": 0.0}
    due_cache = context.user_data["due_cache"]
    storage._invalidate_due_cache(due_cache)  # always fetch fresh on start

    cards: list = []

    # 1) Palabra del Día
    for w in storage.due(cache=due_cache):
        cards.append({"type": "palabra", "id": w["id"], "word": w["word"],
                      "lang": w["lang"], "sentence": w.get("sentence", ""),
                      "definition": w.get("definition", "")})

    # 2) Course cards: ONE query for all due rows.
    today_s = date.today().isoformat()
    db = STATE_DB
    course_query_ok = False
    if db.exists():
        try:
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, front, back, course, card_type, "
                    "       ease, interval, repetitions "
                    "FROM course_flashcards "
                    "WHERE next_review <= ? AND card_type IN "
                    "      ('conceptual','factual','notion','notion_reversed')",
                    (today_s,),
                ).fetchall()
                course_query_ok = True
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            for r in rows:
                d = dict(r)
                ct = d.get("card_type")
                if ct == "conceptual":
                    cards.append({"type": "conceptual", "id": d["id"],
                                  "front": d["front"],
                                  "back": d.get("back", ""),
                                  "course": d.get("course", "")})
                elif ct == "factual":
                    cards.append({"type": "factual", "id": d["id"],
                                  "front": d["front"],
                                  "back": d.get("back", ""),
                                  "course": d.get("course", ""),
                                  "ease": d.get("ease", 2.5),
                                  "interval": d.get("interval", 0),
                                  "repetitions": d.get("repetitions", 0)})
        except Exception as e:
            log.error(f"s_practice course query failed: {e}")
            course_query_ok = False

    if not course_query_ok:
        # Fallback N+1 path (course-by-course).
        for c in _courses():
            for cc in _getc(c, "conceptual"):
                if str(cc.get("next_review", "2000-01-01")) <= today_s:
                    cards.append({"type": "conceptual", "id": cc["id"],
                                  "front": cc["front"],
                                  "back": cc.get("back", ""),
                                  "course": cc["course"]})
            for fc in _getc(c, "factual"):
                if str(fc.get("next_review", "2000-01-01")) <= today_s:
                    cards.append({"type": "factual", "id": fc["id"],
                                  "front": fc["front"],
                                  "back": fc.get("back", ""),
                                  "course": fc["course"],
                                  "ease": fc.get("ease", 2.5),
                                  "interval": fc.get("interval", 0),
                                  "repetitions": fc.get("repetitions", 0)})

    if not cards:
        text = "\U0001f389 No hay nada pendiente. \u00a1Completaste!"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u2b05\ufe0f Volver", callback_data="nav:back")
        ]])
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, parse_mode="Markdown", reply_markup=kb
        )
        context.user_data.pop("due_cache", None)
        return

    session_data = {"cards": cards, "idx": 0, "total": len(cards)}
    context.user_data["practice"] = session_data
    _save_practice(session_data)

    await show_practice_card(update, context, cards[0], 0, len(cards))


async def show_practice_card(update, context, card, idx, total) -> None:
    """Show one practice card with the appropriate UI per type."""
    t = card["type"]
    if t == "palabra":
        text = (
            f"\U0001f501 **Practice All** ({idx + 1}/{total})\n\n"
            f"\U0001f4d6 **{card['word']}** ({card['lang']})\n"
            f"\U0001f4ac *Contexto:* _{card.get('sentence','')}_\n\n"
            f"||{card.get('definition','')}||\n\n"
            f"_Escribe una oración._"
        )
        kb = kbd_practice_palabra()
    elif t == "conceptual":
        text = (
            f"\U0001f501 **Practice All** ({idx + 1}/{total})\n\n"
            f"\U0001f4ad *{card['course']}* \n**{card['front']}**\n\n"
            f"_Escribe tu respuesta._"
        )
        kb = kbd_practice_conceptual()
    else:  # factual
        text = (
            f"\U0001f501 **Practice All** ({idx + 1}/{total})\n\n"
            f"\U0001f4dd *{card['course']}* \n**{card['front']}**\n\n"
            f'_Toca "Ver respuesta" cuando lo recuerdes._'
        )
        kb = kbd_practice_flip()

    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=kb
    )
    if t == "palabra":
        _say(card["word"], card["lang"], update, context)


async def pf_flip(update, context) -> None:
    """Show the back of a factual card + self-eval buttons."""
    session = context.user_data.get("practice", {})
    cards = session.get("cards", [])
    idx = session.get("idx", 0)
    if idx >= len(cards):
        return
    card = cards[idx]
    text = (
        f"\U0001f501 **Practice All** ({idx + 1}/{session.get('total', 1)})\n\n"
        f"\U0001f4dd *{card['course']}* \n**{card['front']}**\n\n"
        f"||{card.get('back','')}||\n\n"
        f"_Autoevaluación:_"
    )
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=kbd_practice_flip_back()
    )
    session["flipped"] = True
    context.user_data["practice"] = session


async def pf_fail(update, context) -> None:
    """User didn't know → quality=0, save, next card."""
    session = context.user_data.get("practice", {})
    cards = session.get("cards", [])
    idx = session.get("idx", 0)
    if idx >= len(cards):
        await pf_done(update, context)
        return
    card = cards[idx]
    await _save_practice_review(card, 0, "no_se")
    storage._invalidate_due_cache(context.user_data.get("due_cache"))
    await update.callback_query.answer("\u274c Repaso manana.")
    idx += 1
    session["idx"] = idx
    context.user_data["practice"] = session
    _save_practice(session)

    if idx >= len(cards):
        await pf_done(update, context)
    else:
        await show_practice_card(update, context, cards[idx], idx,
                                 session["total"])


async def pf_self_eval(update, context, btn: str) -> None:
    """Self-eval for factual cards (Again / Hard / Good / Easy)."""
    session = context.user_data.get("practice", {})
    cards = session.get("cards", [])
    idx = session.get("idx", 0)
    if idx >= len(cards):
        await pf_done(update, context)
        return
    card = cards[idx]
    await _save_practice_self_eval(card, btn)
    be = {"again": "\U0001f504", "hard": "\U0001f4aa",
          "good": "\u2705", "easy": "\u2b50"}
    await update.callback_query.answer(f"{be.get(btn, '')} {btn.upper()}")
    idx += 1
    session["idx"] = idx
    context.user_data["practice"] = session
    _save_practice(session)

    if idx >= len(cards):
        await pf_done(update, context)
    else:
        await show_practice_card(update, context, cards[idx], idx,
                                 session["total"])


async def pf_done(update, context) -> None:
    """Practice completed."""
    context.user_data.pop("practice", None)
    context.user_data.pop("due_cache", None)
    _clear_practice()
    text, kb = menus.rev_menu()
    await update.callback_query.edit_message_text(
        "\U0001f389 \u00a1Practice All completado!\n\n" + text,
        parse_mode="Markdown", reply_markup=kb,
    )
    # The user is now back at the Vocab root menu; if they press "back"
    # from here, they should return to whatever was on the stack BEFORE
    # they started Practice All. The application-level nav router owns
    # the nav_stack; we just leave a hint for it via user_data so a
    # future refactor can pick it up. For now the "back" button is
    # handled by the global ``nav:back`` callback, which is enough.
    context.user_data["last_vocab_screen"] = "revision"


# ==============================================================================
# INTERNAL REVIEW PERSISTENCE
# ==============================================================================
async def _save_practice_review(card, quality, response) -> None:
    """Save a review based on the card type."""
    if card["type"] == "palabra":
        storage._srs_review_word(card["id"], quality, response)
        storage._invalidate_srs_caches()


async def _save_practice_self_eval(card, btn: str) -> None:
    """Persist a self-eval grade for a factual card."""
    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT ease, interval, repetitions FROM course_flashcards "
            "WHERE id=?", (card["id"],)
        ).fetchone()
        if row:
            ef, intv, reps = row["ease"], row["interval"], row["repetitions"]
            ef2, intv2, rep2 = sm2_self(btn, ef, intv, reps)
            nr = (date.today() + timedelta(days=intv2)).isoformat()
            conn.execute(
                "UPDATE course_flashcards SET ease=?, interval=?, "
                "repetitions=?, next_review=? WHERE id=?",
                (ef2, intv2, rep2, nr, card["id"]),
            )
            conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"PF eval: {e}")


# ==============================================================================
# COURSE HELPERS (fallback for the old N+1 path)
# ==============================================================================
def _courses() -> list:
    """List distinct course names from ``course_flashcards``."""
    if not STATE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(STATE_DB))
        rows = conn.execute(
            "SELECT DISTINCT course FROM course_flashcards ORDER BY course"
        ).fetchall()
        conn.close()
        return [r[0] for r in rows if r[0]]
    except Exception:
        return []


def _getc(course=None, ctype=None) -> list:
    """Fetch course flashcards, optionally filtered by course / card_type."""
    if not STATE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        q = ("SELECT id, front, back, course, card_type, ease, interval, "
             "repetitions, next_review FROM course_flashcards")
        p = []
        wh = []
        if course:
            wh.append("course=?")
            p.append(course)
        if ctype is not None:
            if isinstance(ctype, (list, tuple)):
                ph = ",".join("?" for _ in ctype)
                wh.append(f"card_type IN ({ph})")
                p.extend(ctype)
            else:
                wh.append("card_type=?")
                p.append(ctype)
        if wh:
            q += " WHERE " + " AND ".join(wh)
        q += " ORDER BY next_review ASC"
        rows = conn.execute(q, p).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ==============================================================================
# STATS VIEW
# ==============================================================================
async def s_stats(update, context) -> None:
    """Render the Vocab stats view (text + back button)."""
    s = storage.sts()
    lines = ["\U0001f4ca **Estadisticas**\n"]
    lines.append("**\U0001f4d6 Vocabulario**")
    lines.append(
        f"\u2502 Total: **{s.get('total_words', 0)}** | "
        f"Repasadas: **{s.get('words_reviewed_at_least_once', 0)}** | "
        f"Pendientes: **{s.get('due_today', 0)}**"
    )
    bl = s.get("by_language", {})
    if bl:
        for lang, cnt in bl.items():
            flag = "\U0001f1e9\U0001f1ea" if lang == "de" else "\U0001f1ec\U0001f1e7"
            lines.append(f"\u2502 {flag} {lang.upper()}: {cnt}")
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("\u2b05\ufe0f Volver", callback_data="nav:back")
    ]])
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        text, parse_mode="Markdown", reply_markup=kb
    )


# ==============================================================================
# CALLBACK ROUTER
# ==============================================================================
async def handle_callback(update, context) -> None:
    """Body of the vocabulary callback router.

    Handles ``revision``, ``deck:open:``, ``deck:practice:``, ``deck:back:``,
    ``rev:stats``, ``rev:wordday``, ``rev:practice``, ``pf:*``, and
    ``w1:f:``/``w1:s:``.  Returns ``True`` if the callback was handled,
    ``False`` otherwise (so the application-level router can fall through
    to other domain handlers).
    """
    q = update.callback_query
    d = q.data or ""

    if d == "revision":
        await sr(update, context)
        return True

    if d.startswith("deck:open:"):
        try:
            deck_id = int(d.split(":")[2])
        except (ValueError, IndexError):
            await q.answer("Deck invalido.")
            return True
        await show_decks(update, context, parent_id=deck_id,
                         deck_label=storage.deck_name(deck_id))
        return True

    if d.startswith("deck:practice:"):
        try:
            deck_id = int(d.split(":")[2])
        except (ValueError, IndexError):
            await q.answer("Deck invalido.")
            return True
        await show_deck_practice(update, context, deck_id=deck_id,
                                 deck_label=storage.deck_name(deck_id))
        return True

    if d.startswith("deck:back:"):
        try:
            parent_id = int(d.split(":")[2])
        except (ValueError, IndexError):
            parent_id = None
        if parent_id:
            await show_decks(update, context, parent_id=parent_id,
                             deck_label=storage.deck_name(parent_id))
        else:
            await sr(update, context)
        return True

    if d == "rev:stats":
        await s_stats(update, context)
        return True

    if d == "rev:wordday":
        await show_wd(update, context)
        return True

    if d == "rev:practice":
        await s_practice(update, context)
        return True

    if d == "pf:fail":
        await pf_fail(update, context)
        return True
    if d == "pf:flip":
        await pf_flip(update, context)
        return True
    if d == "pf:again":
        await pf_self_eval(update, context, "again")
        return True
    if d == "pf:hard":
        await pf_self_eval(update, context, "hard")
        return True
    if d == "pf:good":
        await pf_self_eval(update, context, "good")
        return True
    if d == "pf:easy":
        await pf_self_eval(update, context, "easy")
        return True

    if d.startswith("w1:f:"):
        try:
            wid = int(d.split(":")[2])
        except (ValueError, IndexError):
            await q.answer("ID invalido.")
            return True
        await w1_fail(update, context, wid)
        return True

    if d.startswith("w1:s:"):
        try:
            wid = int(d.split(":")[2])
        except (ValueError, IndexError):
            await q.answer("ID invalido.")
            return True
        await w1_skip(update, context, wid)
        return True

    return False


# ==============================================================================
# REGISTRATION
# ==============================================================================
def register_handlers(application, say_word=None) -> list[str]:
    """Register all vocabulary handlers into a Telegram ``Application``.

    Parameters
    ----------
    application:
        A :class:`telegram.ext.Application` instance.
    say_word:
        Optional async callable ``async (word, lang, update, context) -> None``
        used for TTS. If ``None``, TTS is disabled.

    Returns
    -------
    list[str]
        The callback-data prefixes this domain handles.
    """
    global _say_word
    _say_word = say_word

    from telegram.ext import CallbackQueryHandler

    application.add_handler(CallbackQueryHandler(handle_callback))
    return [
        "revision", "deck:open:", "deck:practice:", "deck:back:",
        "rev:stats", "rev:wordday", "rev:practice",
        "pf:fail", "pf:flip", "pf:again", "pf:hard", "pf:good", "pf:easy",
        "w1:f:", "w1:s:",
    ]

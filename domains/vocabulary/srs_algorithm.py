"""
domains.vocabulary.srs_algorithm — SM-2 spaced-repetition algorithm.

Extracted from the original monolith's `scripts/srs.py` and `scripts/task_bot.py`.

Public surface
--------------
* :func:`sm2_next` — pure SM-2 step (canonical, with full quality 0-5).
* :func:`sm2` — short-arg-name alias used by the bot UI.
* :func:`sm2_self` — map a self-eval button label ("again"/"hard"/"good"/"easy")
  to a quality grade and call :func:`sm2`.

All three return ``(easiness_factor, interval, repetitions)``.
"""
from __future__ import annotations


def sm2_next(quality: int, easiness_factor: float,
             interval: int, repetitions: int) -> tuple[float, int, int]:
    """SM-2 algorithm step. ``quality`` is 0-5.

    Returns ``(new_easiness_factor, new_interval, new_repetitions)``.

    Mirrors the canonical implementation in ``srs.sm2_next``.
    """
    if quality < 3:
        repetitions = 0
        interval = 1
    else:
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = round(interval * easiness_factor)
        repetitions += 1

    ef = easiness_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
    if ef < 1.3:
        ef = 1.3

    return ef, interval, repetitions


def sm2(q: int, ef: float, intv: int, reps: int) -> tuple[float, int, int]:
    """Short-argument alias for :func:`sm2_next` (matches the bot UI's style)."""
    return sm2_next(q, ef, intv, reps)


def sm2_self(btn: str, ef: float, intv: int, reps: int) -> tuple[float, int, int]:
    """Self-eval button → SM-2 quality grade, then call :func:`sm2`.

    Mapping: ``again``=0, ``hard``=2, ``good``=3, ``easy``=5.
    Unknown buttons fall back to ``good`` (3).
    """
    qm = {"again": 0, "hard": 2, "good": 3, "easy": 5}
    return sm2(qm.get(btn, 3), ef, intv, reps)

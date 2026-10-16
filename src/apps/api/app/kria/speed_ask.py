"""Did the creator ask the footage to play faster or slower? (KRI-689)

"Speed the river shots up 2x" lands in the Creative Brief as a ``style`` requirement with no
facts: the Main Creator strategy has no field for clip speed. A phone montage lays every clip
out at its natural speed (`phone_guided_plan` refuses any ``playback_rate`` other than 1:
"the recipe has no retime for story footage"), so the ask is dropped unless something says so.
This module only answers "is this requirement a clip-speed ask?"; `brief_checks` turns the
answer into the heads-up before approval and the "Couldn't" receipt after the render.

Evidence, in order (KRI-667): a recorded fact decides. ``playback_rate`` (or ``speed``) is a
number, 2 = twice as fast, 0.5 = half speed, 1 = no change. The Main Creator is not asked for it
yet (its prompt lives in ``main_creator._BRIEF_PROMPT_SECTION``), so today a requirement almost
never carries it and the creator's words are read instead. That fallback is deliberately narrow
and uses no ``re.*`` (the KRI-667 ratchet): it needs a phrase that can only mean retiming, or a
speed verb aimed at the footage, and it stays silent about pacing, text and music so "fast
pacing" or "speed up the title animation" is never reported as a speed ask that was dropped.
Delete the fallback once the model records ``playback_rate``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.kria.brief_route import loose_text

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement

# The model files "speed the river shots up" under style; a pacing-flavoured wording lands in
# timing or select. Text, order and audio asks have their own checkers.
SPEED_ASK_KINDS = frozenset({"style", "timing", "select"})
RATE_FACT_KEYS = ("playback_rate", "speed")

# Adjacent tokens (diacritics folded) that can only mean retiming footage.
_RETIME_PHRASES: tuple[tuple[str, ...], ...] = (
    ("slow", "motion"),
    ("slow", "mo"),
    ("slo", "mo"),
    ("slowmo",),
    ("slomo",),
    ("timelapse",),
    ("time", "lapse"),
    ("hyperlapse",),
    ("hyper", "lapse"),
    ("fast", "motion"),
    ("fast", "forward"),
    ("playback", "speed"),
    ("playback", "rate"),
    ("double", "speed"),
    ("half", "speed"),
    ("speed", "ramp"),
    ("speed", "ramps"),
    ("speed", "ramping"),
    # Turkish: ağır çekim, yavaş çekim, zaman atlamalı
    ("agir", "cekim"),
    ("yavas", "cekim"),
    ("zaman", "atlamali"),
)
_SPEED_VERBS = frozenset({"speed", "sped", "speeding", "slow", "slowed", "slowing"})
_PARTICLES = frozenset({"up", "down"})
_VERB_PARTICLE_GAP = 4  # "speed the river shots up": up to four words between them
_SPEED_WORDS = frozenset({"speed", "faster", "slower", "quicker", "fast", "slow"})
_FOOTAGE_NOUNS = frozenset(
    ("clip", "clips", "shot", "shots", "footage", "video", "videos", "take", "takes")
    + ("scene", "scenes")
)
# What the verbs also describe without touching the footage: the rhythm of the edit, the text
# on it, the sound under it. Any of these in the same ask means "not clip speed".
_NOT_FOOTAGE = frozenset(
    ("cut", "cuts", "cutting", "pacing", "pace", "paced", "transition", "transitions", "tempo")
    + ("rhythm", "edit", "editing", "text", "title", "titles", "caption", "captions")
    + ("subtitle", "subtitles", "label", "labels", "animation", "animations", "typing")
    + ("music", "song", "audio", "sound")
)
# Turkish stems (hızlandır, yavaşlat) and the footage / non-footage words around them.
_TR_STEMS = ("hizlandir", "yavaslat")
_TR_FOOTAGE = ("klip", "klib", "cekim", "video", "goruntu", "sahne", "kayit")
_TR_NOT_FOOTAGE = (
    "kesim",
    "gecis",
    "tempo",
    "montaj",
    "yazi",
    "baslik",
    "altyazi",
    "animasyon",
) + ("etiket", "muzik", "sarki")
_TR_SPEEDY = ("hizli", "hizlan", "yavas")


def _tokens(text: str) -> list[str]:
    folded = loose_text(text).replace("×", "x")
    return "".join(ch if ch.isalnum() else " " for ch in folded).split()


def _is_multiplier(token: str) -> bool:
    """ "2x" or "x2": a number of times."""
    if token.endswith("x") and token[:-1].isdigit():
        return True
    return token.startswith("x") and token[1:].isdigit()


def _has_phrase(tokens: Sequence[str]) -> bool:
    for phrase in _RETIME_PHRASES:
        width = len(phrase)
        if any(tuple(tokens[i : i + width]) == phrase for i in range(len(tokens) - width + 1)):
            return True
    return False


def _verb_with_particle(tokens: Sequence[str]) -> bool:
    for index, token in enumerate(tokens):
        if token in _SPEED_VERBS and _PARTICLES.intersection(
            tokens[index + 1 : index + 2 + _VERB_PARTICLE_GAP]
        ):
            return True
    return False


def _english_words(tokens: Sequence[str]) -> bool:
    if _has_phrase(tokens):
        return True
    if _NOT_FOOTAGE.intersection(tokens):
        return False
    multiplier = any(_is_multiplier(token) for token in tokens)
    if multiplier and _SPEED_WORDS.intersection(tokens):
        return True  # "2x speed", "2x faster"
    return _verb_with_particle(tokens) and (multiplier or bool(_FOOTAGE_NOUNS.intersection(tokens)))


def _turkish_words(tokens: Sequence[str]) -> bool:
    if any(token.startswith(_TR_NOT_FOOTAGE) for token in tokens):
        return False
    if "kat" in tokens and any(token.startswith(_TR_SPEEDY) for token in tokens):
        return True  # "2 kat hızlı", "iki kat hızlandır"
    return any(token.startswith(_TR_STEMS) for token in tokens) and (
        any(token.startswith(_TR_FOOTAGE) for token in tokens)
        or any(_is_multiplier(token) for token in tokens)
    )


def _recorded_rate(req: BriefRequirement) -> float | None:
    for key in RATE_FACT_KEYS:
        value = req.facts.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value) and value > 0:
            return float(value)
    return None


def asks_clip_speed(req: BriefRequirement) -> bool:
    """True when ``req`` asks some of the footage to play faster or slower."""
    if req.kind not in SPEED_ASK_KINDS or req.scope == "title":
        return False
    rate = _recorded_rate(req)
    if rate is not None:
        return rate != 1.0
    tokens = _tokens(req.description or "")
    return _english_words(tokens) or _turkish_words(tokens)


__all__ = ["RATE_FACT_KEYS", "SPEED_ASK_KINDS", "asks_clip_speed"]

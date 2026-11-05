"""What the creator already asked for, as the five ported editor kinds read it (KRI-129).

"The creator's request wins." The kinds that came over from the KRI-756 chips (captions, music,
sfx, transition, title_animation) used to rely on the agent's skill text for it, which does not
exist on the deterministic path and does not bind a model that picks the kind anyway. A kind now
asks ``spoke(ctx, DIMENSION)`` inside its own ``available``, so every path (the chip set, the
deterministic cards, an agent's pick) goes through the same rule.

The rule is one-sided on purpose. If a live requirement says ANYTHING about the dimension, the
card is withheld: an opposite ask would be undone by the card ("no captions" then "turn the
captions on"), and an equal ask would be restated by it, which a tap later refuses and still
commits brief updates for (GAP-5 / GAP-6). Withholding costs a card; serving one costs the
creator's word.

The loader hands the brief over in ``ctx.kind_inputs["brief"]`` (``suggestion_serve``, with any v2
flag on): a ``CreativeBrief``, or ``None`` when the thread has none. A context without the key was
never loaded (every flag off, or a test context) and is left exactly as it was. Anything under the
key that is not a brief fails closed.

Typed facts decide where the brief writer records them (``captions``, ``audio_intent``,
``animation``). Where it records nothing (transitions, sound effects, music level) the creator's
own words decide, read over the shared Turkish-aware fold: the ``description`` only, never the
``literal``, which is the text the creator wants ON the video and says nothing about its lanes.

Import-light on purpose: no ``app.kria`` import at module level (the brief module pulls the ORM),
the requirements are read by attribute.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.services.suggestions.context import SuggestionContext
from app.text_fold import has_marker

# Equals ``suggestion_serve.BRIEF_KEY`` (a test pins it): serve imports the kinds, not the reverse.
BRIEF_KEY = "brief"


@dataclass(frozen=True)
class Dimension:
    """One thing the creator can have an opinion about, and how an opinion about it reads."""

    name: str
    # A requirement fact whose key starts with one of these is a typed statement.
    fact_prefixes: tuple[str, ...] = ()
    # Typed facts that only count with a specific value: {key: values}.
    fact_values: Mapping[str, frozenset[str]] | None = None
    # Phrases over the folded description (word start, any ending): "caption" hits "captions".
    words: tuple[str, ...] = ()


CAPTIONS = Dimension(
    "captions",
    fact_prefixes=("caption",),
    words=("caption", "subtitle", "altyazi"),
)
MUSIC = Dimension(
    "music",
    fact_prefixes=("music",),
    fact_values={"audio_intent": frozenset({"no_music"})},
    words=(
        "music",
        "song",
        "soundtrack",
        "muzik",
        # "müzik" + a vowel suffix: the k softens to ğ ("müziği", "müziğin", "müziğe"), which the
        # fold writes as "g".
        "muzig",
        "sarki",
        "volume",
        "louder",
        "quieter",
        "ses seviye",
        # "sesi" / "sesini" + the verb: the ask names the sound, not the word "music".
        *(
            f"{noun} {verb}"
            for noun in ("sesi", "sesini")
            for verb in ("kis", "ac", "azalt", "dusur", "artir", "yukselt", "kapat")
        ),
    ),
)
SFX = Dimension(
    "sfx",
    fact_prefixes=("sfx", "sound_effect", "licensed_sfx"),
    words=("sound effect", "sfx", "whoosh", "efekt", "ses efekt"),
)
TRANSITION = Dimension(
    "transition",
    fact_prefixes=("transition",),
    words=(
        "transition",
        "crossfade",
        "cross fade",
        "dissolve",
        "hard cut",
        "jump cut",
        "straight cut",
        "cuts only",
        "gecis",
    ),
)
TITLE_ANIMATION = Dimension(
    "title_animation",
    fact_prefixes=("animation", "text_animation"),
    words=("animat", "typewriter", "animasyon"),
)


class _Unreadable:
    """The brief the loader handed over is not a brief: treated as 'the creator spoke'."""


UNREADABLE = _Unreadable()


def _live(ctx: SuggestionContext) -> list[Any] | _Unreadable | None:
    """The live requirements, ``None`` when the loader never ran, ``UNREADABLE`` when it is junk."""
    inputs = ctx.kind_inputs
    if not isinstance(inputs, Mapping) or BRIEF_KEY not in inputs:
        return None
    brief = inputs[BRIEF_KEY]
    if brief is None:
        return []
    live = getattr(brief, "live", None)
    if not callable(live):
        return UNREADABLE
    try:
        return list(live())
    except Exception:  # noqa: BLE001 - any shape we cannot iterate is not a brief
        return UNREADABLE


def _talks_about(req: Any, dim: Dimension) -> bool:
    facts = getattr(req, "facts", None)
    facts = facts if isinstance(facts, Mapping) else {}
    for key, value in facts.items():
        key = str(key)
        if any(key.startswith(prefix) for prefix in dim.fact_prefixes):
            return True
        wanted = (dim.fact_values or {}).get(key)
        if wanted is not None and str(value).strip().casefold() in wanted:
            return True
    description = getattr(req, "description", None)
    return isinstance(description, str) and has_marker(description, dim.words)


def spoke(ctx: SuggestionContext, dim: Dimension) -> bool:
    """Did the creator already say something about ``dim``? True withholds the card."""
    live = _live(ctx)
    if live is None:
        return False
    if isinstance(live, _Unreadable):
        return True
    return any(_talks_about(req, dim) for req in live)

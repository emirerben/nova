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

KRI-667: the server does not guess what a creator meant from their words (every word list needs
a twin per language and misses a paraphrase: a Turkish verb one word further from its noun than a
list allowed left the music card on screen over a creator who had just asked to turn the sound
down). The brief writer records the meaning and this module reads the field:

* ``kind``: the writer files every volume, music and sound ask as an ``audio`` requirement, in any
  language and any wording, so an ``audio`` requirement is an ask about the soundtrack, unless its
  typed facts place it in the footage's own sound lane: a clip's sound turned on or off
  (``clip_sound``, read by ``clip_sound_toggle_ask``: the very requirement a mute-clip tap writes)
  is about what a clip plays, not about the music or the sound effects, and withholds neither card
  (only while ``KRIA_SUGGESTION_PINNED_TAPS_ENABLED`` is on: with it off, every ``audio``
  requirement speaks, as it always did). A fact that lane's reader cannot parse is not that lane:
  the requirement still speaks;
* ``facts``: ``captions`` / ``caption_*``, ``animation``, ``audio_intent: no_music``, ``music*``.

Two splits have no typed signal, so they read the creator's ``description`` once, over the shared
Turkish-aware fold (``app.text_fold``), never the ``literal`` (the text the creator wants ON the
video, which says nothing about its lanes): music against sound effects (both are ``audio``), and
transitions (the writer records no fact and no kind for them). That one reader,
``_description_says``, is on the raw chat text allow-list (``tests/test_raw_chat_text_readers.py``).
An ``audio`` requirement that names sound effects is a sound-effects ask and not a music ask; any
other is a music ask, however it is worded.

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
    # A requirement of one of these kinds is a statement about the dimension ...
    kinds: tuple[str, ...] = ()
    # ... but only when its description names one of ``kind_needs`` (when set) and none of
    # ``kind_not``: the split between two dimensions that share a kind.
    kind_needs: tuple[str, ...] = ()
    kind_not: tuple[str, ...] = ()
    # A requirement fact whose key starts with one of these is a typed statement.
    fact_prefixes: tuple[str, ...] = ()
    # Typed facts that only count with a specific value: {key: values}.
    fact_values: Mapping[str, frozenset[str]] | None = None
    # Phrases over the folded description (word start, any ending): "gecis" hits "gecisler". Only
    # for a dimension the brief writer records nothing typed about.
    words: tuple[str, ...] = ()


CAPTIONS = Dimension("captions", fact_prefixes=("caption",))
# Sound-effect names: what tells an `audio` requirement about sound effects from one about music.
_SFX_WORDS = ("sound effect", "sfx", "whoosh", "efekt", "ses efekt")
# The writer files volume, music and sound asks as `audio` requirements ("sesi biraz kıs", "tone
# down the sound", "parçayı değiştir"), so the kind is the signal; a sound-effects ask is not one.
MUSIC = Dimension(
    "music",
    kinds=("audio",),
    kind_not=_SFX_WORDS,
    fact_prefixes=("music",),
    fact_values={"audio_intent": frozenset({"no_music"})},
)
SFX = Dimension(
    "sfx",
    kinds=("audio",),
    kind_needs=_SFX_WORDS,
    fact_prefixes=("sfx", "sound_effect", "licensed_sfx"),
)
# No typed fact and no kind of its own: the one dimension that reads the description.
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
TITLE_ANIMATION = Dimension("title_animation", fact_prefixes=("animation", "text_animation"))


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


def _description_says(req: Any, words: tuple[str, ...]) -> bool:
    """The creator's description names one of ``words`` (the shared fold; the registered reader)."""
    description = getattr(req, "description", None)
    return isinstance(description, str) and has_marker(description, words)


def _is_clip_sound_toggle(req: Any) -> bool:
    """The requirement is, by its typed facts, a clip's own sound turned on or off.

    The one reader of that shape (``clip_sound_toggle_ask``); anything it cannot read is not a
    toggle, so a malformed or unrecognised fact leaves the requirement speaking for the card.
    Only while pinned taps are on: that is when a mute-clip tap can write one, and with the flag
    off the legacy chips must read the brief exactly as they did (flag off is byte-identical).
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.clip_sound_toggle_ask import requirement_sound_toggle  # noqa: PLC0415

    if not settings.kria_suggestion_pinned_taps_enabled:
        return False
    try:
        return requirement_sound_toggle(req) is not None
    except Exception:  # noqa: BLE001 - a shape that cannot be read is not the footage lane
        return False


def _kind_says(req: Any, dim: Dimension) -> bool:
    if getattr(req, "kind", None) not in dim.kinds:
        return False
    if _is_clip_sound_toggle(req):
        return False
    if dim.kind_needs and not _description_says(req, dim.kind_needs):
        return False
    return not (dim.kind_not and _description_says(req, dim.kind_not))


def _talks_about(req: Any, dim: Dimension) -> bool:
    if _kind_says(req, dim):
        return True
    facts = getattr(req, "facts", None)
    facts = facts if isinstance(facts, Mapping) else {}
    for key, value in facts.items():
        key = str(key)
        if any(key.startswith(prefix) for prefix in dim.fact_prefixes):
            return True
        wanted = (dim.fact_values or {}).get(key)
        if wanted is not None and str(value).strip().casefold() in wanted:
            return True
    return bool(dim.words) and _description_says(req, dim.words)


def spoke(ctx: SuggestionContext, dim: Dimension) -> bool:
    """Did the creator already say something about ``dim``? True withholds the card."""
    live = _live(ctx)
    if live is None:
        return False
    if isinstance(live, _Unreadable):
        return True
    return any(_talks_about(req, dim) for req in live)

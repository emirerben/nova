"""Did the creator ask for ONE clip's own sound on a Voiceover edit? (KRI-842)

Café N1: "Seslendirme her yerde net duyulsun ama Atakan'ın ısırdığı klipte o klibin kendi sesi
de hafifçe altta kalsın. Seslendirme bitince son klipte Caro'nun "bye bye" demesi duyulsun."
A Voiceover edit mixes every clip's own sound as one bed under the voice, so neither half could
be done and neither was ever said: the Turkish wording had no checker, and the English
"keep the clip sound low on the bite clip" was met by the GLOBAL bed.

The Main Creator records each half as an ``audio`` requirement scoped ``clip:<media id>`` with
``facts.clip_sound`` (KRI-667: the model records the meaning, the server reads the field, no
wording is matched here):

- ``"under_voice"``: this clip's own sound plays softly under the voice; every other clip stays
  silent under it.
- ``"after_voice"``: after the voice ends this clip plays on with its own sound. The line the
  creator wants heard ("bye bye") goes in the usual spoken ``triggers`` list.

The phone Voiceover worker (`app.pipeline.phone_clip_sound`, to build it), `app.kria.brief_checks`
(to answer the requirement before approval and on the finished video) and the editor Save all
read the ask here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.kria.spoken_text_ask import spoken_triggers

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

CLIP_SOUND_FACT = "clip_sound"
UNDER_VOICE = "under_voice"
AFTER_VOICE = "after_voice"
CLIP_SOUND_MODES = frozenset({UNDER_VOICE, AFTER_VOICE})


@dataclass(frozen=True)
class ClipSoundAsk:
    """One clip whose own sound the creator wants kept under (or after) the voiceover."""

    requirement: BriefRequirement
    mode: str
    # Spoken lines the creator wants heard ("bye bye"); only an ``after_voice`` ask has any.
    triggers: tuple[str, ...] = ()

    @property
    def requirement_id(self) -> str:
        return self.requirement.id

    def media_id(self, media_ids: Iterable[str]) -> str | None:
        """The clip among ``media_ids`` the ask's ``clip:<id>`` scope names, else None."""
        from app.kria.brief_checks import _scope_clip_id  # noqa: PLC0415

        return _scope_clip_id(self.requirement, media_ids)


def requirement_clip_sound(req: BriefRequirement) -> ClipSoundAsk | None:
    """``req`` as a clip-sound ask when its recorded facts say exactly that."""
    if not req.live or not req.scope.startswith("clip:"):
        return None
    value = req.facts.get(CLIP_SOUND_FACT)
    mode = " ".join(value.split()).lower() if isinstance(value, str) else ""
    if mode not in CLIP_SOUND_MODES:
        return None
    return ClipSoundAsk(
        requirement=req,
        mode=mode,
        triggers=spoken_triggers(req) if mode == AFTER_VOICE else (),
    )


def clip_sound_asks(requirements: Iterable[BriefRequirement]) -> tuple[ClipSoundAsk, ...]:
    """Every clip-sound ask among ``requirements``, in brief order."""
    return tuple(ask for req in requirements if (ask := requirement_clip_sound(req)) is not None)


def brief_clip_sound_asks(brief: CreativeBrief | None) -> tuple[ClipSoundAsk, ...]:
    """`clip_sound_asks` over a brief's live requirements (empty without one)."""
    return clip_sound_asks(brief.live()) if brief is not None else ()


__all__ = [
    "AFTER_VOICE",
    "CLIP_SOUND_FACT",
    "CLIP_SOUND_MODES",
    "UNDER_VOICE",
    "ClipSoundAsk",
    "brief_clip_sound_asks",
    "clip_sound_asks",
    "requirement_clip_sound",
]

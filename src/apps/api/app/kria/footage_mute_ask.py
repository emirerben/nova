"""Did the creator ask to mute the footage's own sound under their voice? (KRI-821)

"Mute the crowd noise under my voice" / "kamera sesini kapat" lands in the Creative Brief
as an ``audio`` (or ``style``/``select``) requirement. A voiceover edit mixes the footage's
own sound as a bed under the voice (narrated: ~25 % by default), so without this reader the
render quietly kept the bed and the receipt told the creator to "lower it in the editor's
mix", a control narrated edits do not have.

The ask is the brief writer's typed ``audio_intent: source_muted``, corroborated by the
approved requirement's own words (`app.kria.brief_checks.renders_bed_mute`: names the
footage sound, says mute/remove, never keep / lower / raise it). The receipt reads the same
function, so render and receipt can never disagree. "Keep it low", "remove only the wind
noise" or "drop the song, keep the crowd" are not mutes: the format's bed stays.

Only the pinned, approved brief is read (`content_plan_build` dispatch). A model suggestion
(copilot, planner) never reaches this reader, so it can never mute a creator's footage.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

# The bed level an explicit mute renders at: no footage sound under the voice.
MUTED_BED_LEVEL = 0.0


@dataclass(frozen=True)
class FootageMuteAsk:
    """The brief requirements that asked for the footage's own sound to be muted."""

    requirement_ids: tuple[str, ...]


def footage_mute_ask(requirements: Iterable[BriefRequirement]) -> FootageMuteAsk | None:
    """The live requirements asking to mute the footage sound (None when none do)."""
    from app.kria.brief_checks import renders_bed_mute  # noqa: PLC0415

    ids = tuple(req.id for req in requirements if renders_bed_mute(req))
    return FootageMuteAsk(requirement_ids=ids) if ids else None


def brief_footage_mute(brief: CreativeBrief | None) -> FootageMuteAsk | None:
    """`footage_mute_ask` over a brief's live requirements (None without one)."""
    return footage_mute_ask(brief.live()) if brief is not None else None


def approved_bed_level(brief: CreativeBrief | None, item_level: float | None) -> float | None:
    """The voiceover bed level a render of this approved brief plays the footage at.

    Precedence (KRI-821): an explicit mute in the approved brief renders silent footage
    (the brief is what this render was approved for); otherwise the item's own level
    (the web item-page slider), else None (Kria's default). A later editor Save
    (``original_audio_level`` on the variant) wins over both, in the phone compiler.
    """
    if brief_footage_mute(brief) is not None:
        return MUTED_BED_LEVEL
    return item_level

"""Did the creator ask to see clips full screen? (talking show-when-mentioned)

Thread 6fa36e51: "Show the talks when I mention in full screen and use their audio." The brief
kept it as ``r1 [style/global] facts={display_mode: fullscreen}``. A multi-clip Talking edit
reads that one typed fact (KRI-667: the model records the meaning, the server reads the field,
no wording is matched here) to decide whether the clips that are not the creator's own take
cover him full screen instead of playing back to back (`app.pipeline.phone_talking_mentions`).

- ``global`` scope: the ask is about no clip in particular, so the clips' own analysis decides
  which clips are "the talks".
- ``clip:<id>`` scope: the ask names that clip.
- A live ``order`` requirement means the creator also arranged the clips. Playing them in
  that order is what they asked, so nothing is reclassified (``order_asked``).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

DISPLAY_MODE_FACT = "display_mode"
_FULL_SCREEN_KINDS = frozenset({"style", "select", "timing"})


@dataclass(frozen=True)
class FullScreenShowAsk:
    requirement_ids: tuple[str, ...]
    # The ``clip:<id>`` scopes, as the brief wrote them.
    media_refs: tuple[str, ...]
    # An ask with no clip named.
    whole_video: bool
    # The creator also gave an order for the clips.
    order_asked: bool


def _is_full_screen(req: BriefRequirement) -> bool:
    value = req.facts.get(DISPLAY_MODE_FACT)
    if not isinstance(value, str):
        return False
    return "".join(ch for ch in value.casefold() if ch.isalnum()) == "fullscreen"


def full_screen_show_ask(requirements: Iterable[BriefRequirement]) -> FullScreenShowAsk | None:
    """The live full-screen display asks among ``requirements``, else None."""
    reqs = [req for req in requirements if req.live]
    asks = [req for req in reqs if req.kind in _FULL_SCREEN_KINDS and _is_full_screen(req)]
    refs = tuple(
        dict.fromkeys(
            req.scope.split(":", 1)[1].strip()
            for req in asks
            if req.scope.startswith("clip:") and req.scope.split(":", 1)[1].strip()
        )
    )
    whole = any(req.scope == "global" for req in asks)
    if not (refs or whole):
        return None
    return FullScreenShowAsk(
        requirement_ids=tuple(req.id for req in asks),
        media_refs=refs,
        whole_video=whole,
        order_asked=any(req.kind == "order" for req in reqs),
    )


def brief_full_screen_show_ask(brief: CreativeBrief | None) -> FullScreenShowAsk | None:
    return None if brief is None else full_screen_show_ask(brief.requirements)

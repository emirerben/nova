"""What the server can settle about a brief `select` ask without reading its words (KRI-994).

Prod thread 4C536AFE (Don Julio lunch M2, 2026-10-27): the finished video's review listed
three asks under "Have a look at these in the video" that the server already knew the
answer to:

* "Skip the clip where he cuts the cheese": the creator tapped that clip out of the picker
  (a resolved ``exclude`` intent, evidence "creator selected") and the render holds none of
  its footage. The intent had no back-reference to the brief ask, so no checker could say so.
* "Use my steak photo too", withdrawn two turns later by "Forget the steak photo. Make the
  montage from the video clips only.": the brief extractor (a model) added the clips-only ask
  but did not ``remove`` the photo ask.
* "Make the montage from the video clips only (no photos)": every cut of the finished
  timeline is a video, so it is proven.

KRI-667: the model records meaning in typed fields and the server checks the fields.

* ``ClipIntent.requirement_id``: the planner names the ``select`` ask an ``exclude`` carries
  out. It is kept only when it names a LIVE ``select`` requirement. An exclusion left unlinked
  by the model is linked by string identity only: its attribute, case/whitespace-normalised,
  appears verbatim inside exactly ONE live ``select`` requirement's text. No word lists, no
  token overlap, no stemming: zero or several matches leave it unlinked (the ask stays
  "have a look").
* ``facts.only_media_kind == "video"``: the creator wants video clips only (no photos).
* ``facts.include_media_kind == "photo"``: the creator asks to include a photo.

The finished timeline is the proof, never the tap or the model's field: an exclusion is met
only when every clip it left out is absent from the finished edit, "clips only" only when no
cut of it is a photo.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.schemas.clip_intents import CREATOR_EVIDENCE

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

ONLY_KIND_FACT = "only_media_kind"
INCLUDE_KIND_FACT = "include_media_kind"


def only_video_ask(req: Any) -> bool:
    """The typed "video clips only, no photos" ask (a ``select`` requirement)."""
    facts = getattr(req, "facts", None) or {}
    return getattr(req, "kind", None) == "select" and facts.get(ONLY_KIND_FACT) == "video"


def include_photo_ask(req: Any) -> bool:
    """The typed "use this photo" ask (a ``select`` requirement)."""
    facts = getattr(req, "facts", None) or {}
    return getattr(req, "kind", None) == "select" and facts.get(INCLUDE_KIND_FACT) == "photo"


def withdrawn_photo_asks(
    prior_live: Iterable[BriefRequirement],
    restricting: Iterable[BriefRequirement],
    *,
    source_turn_id: str | None,
    same_batch_ids: Iterable[str] = (),
) -> set[str]:
    """Ids of EARLIER live typed photo asks a later typed clips-only ask supersedes.

    Nothing else supersedes anything here: no contradiction detector. Both asks must carry
    their typed fact, the photo ask must come from an earlier turn than the clips-only one
    (never the same message), and a clips-only ask that is not typed (the partial-overlap
    "also use the steak photo, the rest video") withdraws nothing.
    """
    if not any(only_video_ask(req) for req in restricting):
        return set()
    skip = set(same_batch_ids)
    return {
        req.id
        for req in prior_live
        if include_photo_ask(req)
        and req.id not in skip
        and not (source_turn_id is not None and req.source_turn_id == source_turn_id)
    }


def _text_key(value: object) -> str:
    """Case- and whitespace-normalised text, for a verbatim-containment test only."""
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


@dataclass(frozen=True)
class ExclusionFact:
    """The clips the creator's exclusions for one live ``select`` ask left out.

    ``complete`` is False when a linked exclusion is not proven to name the creator's own
    clips (a resolver guess, or it matched nothing): the receipt then claims nothing.
    """

    requirement_id: str
    media_ids: tuple[str, ...]
    complete: bool = True


def _assigned(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = row.get("assignments")
    return [a for a in raw if isinstance(a, Mapping)] if isinstance(raw, list) else []


def exclusion_facts(
    strategy: Mapping[str, Any] | None, brief: CreativeBrief | None
) -> tuple[ExclusionFact, ...]:
    """Each live ``select`` ask a resolved ``exclude`` intent is linked to, with its clips.

    ``strategy`` is the pinned creator strategy (``resolved_clip_intents`` as stored).
    Shared by the render worker (plan-time receipts) and the finished-video review.
    """
    if not isinstance(strategy, Mapping) or brief is None:
        return ()
    rows = [
        row
        for row in strategy.get("resolved_clip_intents") or []
        if isinstance(row, Mapping)
        and row.get("op") == "exclude"
        and row.get("status", "resolved") == "resolved"
    ]
    if not rows:
        return ()
    texts = {req.id: _text_key(req.text()) for req in brief.live() if req.kind == "select"}
    media: dict[str, list[str]] = {}
    complete: dict[str, bool] = {}
    for row in rows:
        declared = row.get("requirement_id")
        linked = declared if isinstance(declared, str) and declared in texts else None
        if linked is None:
            attribute = _text_key(row.get("attribute"))
            found = [rid for rid, text in texts.items() if attribute and attribute in text]
            linked = found[0] if len(found) == 1 else None
        if linked is None:
            continue
        assigned = _assigned(row)
        by_creator = bool(assigned) and all(a.get("evidence") in CREATOR_EVIDENCE for a in assigned)
        complete[linked] = complete.get(linked, True) and by_creator
        media.setdefault(linked, []).extend(
            str(a["media_id"]) for a in assigned if a.get("media_id")
        )
    return tuple(
        ExclusionFact(rid, tuple(dict.fromkeys(ids)), complete[rid]) for rid, ids in media.items()
    )


__all__ = [
    "INCLUDE_KIND_FACT",
    "ONLY_KIND_FACT",
    "ExclusionFact",
    "exclusion_facts",
    "include_photo_ask",
    "only_video_ask",
    "withdrawn_photo_asks",
]

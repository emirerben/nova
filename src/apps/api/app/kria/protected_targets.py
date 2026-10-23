"""What the creator asked to keep text off, and where they marked it (KRI-827).

"No text on her face or on the plate" is a placement ask about every text on the video. The
model that reads the message records it as a structured fact (KRI-667 language policy:
models understand, server code reads fields), on any requirement:

    facts.keep_text_off = [{"kind": "face"}, {"kind": "object", "label": "plate"}]

with the object in the creator's own words. That fact only says WHAT to keep clear. WHERE an
object is comes only from the creator: a box they draw on one frame of one of their clips
(`ProtectedRegionSelection`), sent with a chat turn (`SubmitTurnBody.protected_region`),
checked against the thread's own media and stamped with the clip's generation by the server,
and pinned on the approval binding (``protected_regions``). So model output can never put an
object region on the video, nor can Gemini metadata: an object the brief names without a
mark is ``no_selection`` (the creator is asked to mark it) and is never called clear, and a
mark is protected even when no fact names it (drawing the box is the ask). A face target
protects every detected face (the face sampler), not only the dominant one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.text_fold import fold_text

if TYPE_CHECKING:
    from app.kria.brief import CreativeBrief

KEEP_TEXT_OFF_KEY = "keep_text_off"
PROTECTED_REGIONS_FIELD = "protected_regions"
# The variant's receipt of how every protected target was kept clear.
PROTECTED_TEXT_FIELD = "protected_text_regions"


@dataclass(frozen=True)
class ProtectedTarget:
    """Something to keep text off."""

    kind: Literal["face", "object"]
    label: str  # "face", or the creator's noun ("plate", "tabak")
    requirement_ids: tuple[str, ...] = ()


def _fact_entries(value: object) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("kind")
        if kind == "face":
            entries.append(("face", "face"))
        elif kind == "object":
            label = " ".join(str(item.get("label") or "").split())[:60]
            if label:
                entries.append(("object", label))
    return entries


def protected_targets(
    brief: CreativeBrief | None, marks: Iterable[ProtectedRegionSelection] = ()
) -> tuple[ProtectedTarget, ...]:
    """Every face/object to keep text off: the brief's ``keep_text_off`` facts, in brief
    order, then each creator mark no fact names (its own label)."""
    order: list[tuple[str, str]] = []
    labels: dict[tuple[str, str], str] = {}
    req_ids: dict[tuple[str, str], list[str]] = {}
    for req in brief.live() if brief is not None else ():
        for kind, label in _fact_entries(req.facts.get(KEEP_TEXT_OFF_KEY)):
            key = (kind, fold_text(label))
            if key not in labels:
                order.append(key)
                labels[key] = label
            req_ids.setdefault(key, [])
            if req.id not in req_ids[key]:
                req_ids[key].append(req.id)
    for mark in marks:
        key = ("object", fold_text(mark.label))
        if key not in labels:
            order.append(key)
            labels[key] = mark.label
            req_ids[key] = []
    return tuple(
        ProtectedTarget(kind=key[0], label=labels[key], requirement_ids=tuple(req_ids[key]))  # type: ignore[arg-type]
        for key in order
    )


class ProtectedRegionSelection(BaseModel):
    """The creator's mark: a box on one frame of one of their clips.

    ``box`` is ``[left, top, right, bottom]`` normalized to the UPRIGHT source frame (the
    space `KriaCropEditor` edits in). ``generation`` is stamped by the server from the
    thread's media at submit time, never taken from the client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=60)
    media_id: str = Field(min_length=1, max_length=160)
    source_t_s: float = Field(ge=0.0, le=3600.0)
    box: tuple[float, float, float, float]
    generation: str | None = Field(default=None, max_length=200)

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, value: object) -> str:
        return " ".join(str(value or "").split())

    @model_validator(mode="after")
    def _box(self) -> ProtectedRegionSelection:
        left, top, right, bottom = self.box
        if not all(0.0 <= v <= 1.0 for v in self.box):
            raise ValueError("box must be normalized to [0, 1]")
        if right - left < 0.02 or bottom - top < 0.02:
            raise ValueError("box is too small to mark an object")
        return self


def fold_protected_regions(events: Iterable[tuple[str, Any]]) -> list[dict]:
    """The creator's marks replayed from the thread: user turns only (a model or assistant
    event can never author one), last mark per (label, media) wins."""
    marks: dict[tuple[str, str], dict] = {}
    for role, payload in events:
        if role != "user" or not isinstance(payload, Mapping):
            continue
        raw = payload.get("protected_region")
        if not isinstance(raw, Mapping):
            continue
        try:
            mark = ProtectedRegionSelection.model_validate(dict(raw))
        except ValueError:
            continue
        key = (fold_text(mark.label), mark.media_id)
        # Re-marking moves the key to the end, so list order is the creator's mark order.
        marks.pop(key, None)
        marks[key] = mark.model_dump(mode="json")
    return list(marks.values())


def binding_protected_regions(
    raw_binding: Mapping[str, Any] | None,
) -> list[ProtectedRegionSelection]:
    rows = raw_binding.get(PROTECTED_REGIONS_FIELD) if isinstance(raw_binding, Mapping) else None
    out: list[ProtectedRegionSelection] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            out.append(ProtectedRegionSelection.model_validate(row))
        except ValueError:
            continue
    return out


def selection_for(
    target: ProtectedTarget,
    selections: Iterable[ProtectedRegionSelection],
    media_id: str | None = None,
) -> ProtectedRegionSelection | None:
    """The creator's latest mark for ``target``: the same label, folded ("Plate" marks
    "plate"). The app's mark card carries the target's label, so a mark for another thing
    never stands in. With ``media_id`` (the clip being rendered), the latest mark on that
    clip wins over a later mark on another clip."""
    label = fold_text(target.label)
    same = [m for m in selections if fold_text(m.label) == label]
    if media_id is not None:
        on_clip = [m for m in same if m.media_id == media_id]
        if on_clip:
            return on_clip[-1]
    return same[-1] if same else None


__all__ = [
    "PROTECTED_REGIONS_FIELD",
    "PROTECTED_TEXT_FIELD",
    "ProtectedRegionSelection",
    "ProtectedTarget",
    "KEEP_TEXT_OFF_KEY",
    "binding_protected_regions",
    "fold_protected_regions",
    "protected_targets",
    "selection_for",
]

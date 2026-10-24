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


# What a protected-text receipt measured: the text rows, the caption geometry and the crop.
# An edit after the render (editor Save, a text PATCH) that changes any of these makes the
# receipt describe a video the creator no longer has.
_MEASURED_KEYS = (
    "text_elements",
    "caption_editor_style",
    "caption_margin_v",
    "caption_size_px",
    "caption_stroke_width",
    "voiceover_caption_font",
    "captions_enabled",
    "caption_cues",
    "landscape_fit",
)
STALE_STATUSES = frozenset({"clear", "unverified", "collision"})


def protected_text_fingerprint(variant: Mapping[str, Any]) -> str:
    """A digest of what the receipt was measured against (`_MEASURED_KEYS`)."""
    import hashlib  # noqa: PLC0415
    import json  # noqa: PLC0415

    payload = {key: variant.get(key) for key in _MEASURED_KEYS}
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def stale_protected_text(receipt: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """The receipt after an edit it no longer describes: every measured verdict becomes
    ``stale`` (never ``clear``); ``no_selection`` / ``invalidated`` / ``other_source`` keep
    their own, more specific, ask."""
    out = dict(receipt)
    targets = []
    for target in receipt.get("targets") or []:
        row = dict(target) if isinstance(target, Mapping) else target
        if isinstance(row, dict) and row.get("status", "unverified") in STALE_STATUSES:
            row["status"] = "stale"
        targets.append(row)
    out["targets"] = targets
    out["verdict"] = (
        "stale"
        if any(isinstance(t, Mapping) and t.get("status") == "stale" for t in targets)
        else receipt.get("verdict")
    )
    out["stale_reason"] = reason
    return out


def protected_text_after_editor_save(
    receipt: object,
    *,
    keep_segments: Iterable[tuple[float, float]],
    landscape_fit: str | None,
    speaker_position_x: float | None,
) -> object:
    """The stored receipt after a phone editor Save recompiled the edit: unchanged when the
    cut, the crop and the bars are what it was measured with, otherwise ``stale`` -- the
    tracked boxes are not stored, so a changed cut / crop is never judged from the old
    measurement. ``landscape_fit=None`` (a portrait speaker: bars and crop draw the same)
    skips the bars check."""
    if not isinstance(receipt, Mapping):
        return receipt
    measured_fit = receipt.get("landscape_fit")
    measured_x = float(receipt.get("speaker_position_x") or 0.0)
    measured_keep = [
        (float(a), float(b))
        for a, b in (receipt.get("keep_segments") or [])
        if isinstance(a, int | float) and isinstance(b, int | float)
    ]
    keep = [(float(a), float(b)) for a, b in keep_segments]
    same_cut = len(keep) == len(measured_keep) and all(
        abs(a - c) <= 0.02 and abs(b - d) <= 0.02
        for (a, b), (c, d) in zip(keep, measured_keep, strict=True)
    )
    if (
        same_cut
        and (measured_fit is None or landscape_fit is None or measured_fit == landscape_fit)
        and abs(float(speaker_position_x or 0.0) - measured_x) <= 1e-3
    ):
        return receipt
    if receipt.get("stale_reason"):
        return receipt
    return stale_protected_text(receipt, "edited_geometry")


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
    "protected_text_after_editor_save",
    "protected_text_fingerprint",
    "stale_protected_text",
]

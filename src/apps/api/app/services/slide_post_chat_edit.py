"""Chat editing for slide posts, through the video edit copilot (KRI-298 / KRI-301).

The copilot already understands "put them in chronological order", "add each photo's
location" and "same style on every text" for video edits. Rather than build a second
composer, a slide post is PRESENTED to it as a timeline of 1-second slots with text
bars (``build_slide_post_snapshot``), the copilot's parsed ops run over that working
set through the shared text/timeline handlers (``apply_text_lane_ops``), and the
result is projected back onto a ``SlidePostDraft`` (``compile_slide_post_ops``).

Read-only staging, like ``/slide-post/propose``: nothing here writes the database.
The client stages the returned draft and saves it with the normal versioned PUT.

Slide id -> slot ``media_id`` and ``slot_id`` (stable across reorder/remove). Slide i
owns the window ``[i, i+1)``. Text elements become text bars on that window; a
place/time label is the bar ``clip-label-media-{slide.id}`` (the id the video label
lane already links to a clip). A text added over several windows is copied onto each
slide with fresh ids.
"""

from __future__ import annotations

import asyncio
import copy
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import RunContext, TerminalError
from app.agents.edit_copilot import (
    SLIDE_POST_OPS,
    SLIDE_POST_SURFACE,
    EditCopilotAgent,
    EditCopilotInput,
)
from app.config import settings
from app.kria.brief_binding import BriefBinding
from app.schemas.slide_post import (
    MAX_SLIDE_TEXT_LENGTH,
    MAX_SLIDE_TEXTS,
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    SlideTextElement,
    bump_slide_post_version,
)
from app.services.kria_editor_ops import (
    _CLIP_LABEL_MEDIA_PREFIX,
    KriaEditorOpError,
    _safe_slot,
    _seen_text,
    _text_appearance_enabled,
    _text_appearance_inventory,
    apply_text_lane_ops,
)

log = structlog.get_logger()

_MAX_FACT_VALUE_CHARS = 80
_MAX_SLOT_FACTS = 4
_MAX_CAPTION_SHOWN = 300
_FAMILIES = ["text", "clip", "timeline", "slides"]
_POSITION_OUT = {
    "top": "top",
    "middle": "center",
    "center": "center",
    "bottom": "bottom",
    "custom": "custom",
}
_POSITION_IN = {"center": "middle"}
# Fields a patch may carry that a slide text cannot render (reported, never silent).
_UNSUPPORTED_PATCH_FIELDS = frozenset(
    {
        "animation_phases",
        "effect",
        "highlight_color",
        "letter_spacing",
        "line_spacing",
        "rotation_deg",
        "bend",
        "behind_subject",
        "background_color",
    }
)
SlideOutcome = Literal["edited", "clarification", "unsupported", "no_effect", "failed"]


class SlidePostChatEditResponse(BaseModel):
    """What ``POST /plan-items/{id}/slide-post/chat-edit`` returns (never persisted)."""

    outcome: SlideOutcome
    reply: str
    draft: SlidePostDraft | None = None
    # The server's CURRENT draft version: the client's next PUT sends this as
    # `expected_version`, so a staged chat edit saves without a conflict step.
    base_version: int
    changes: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


@dataclass
class CompiledSlideDraft:
    draft: SlidePostDraft
    changes: list[str]
    notes: list[str] = field(default_factory=list)
    unsupported_requested_property: bool = False


# --------------------------------------------------------------------------- facts


def _slide_facts(asset: Any) -> list[dict[str, Any]]:
    """Path-free capture/understanding facts for one slide asset, [] when none exist."""
    from app.services.clip_facts import slide_asset_facts  # noqa: PLC0415

    try:
        return _prompt_facts(slide_asset_facts(asset))
    except Exception:  # noqa: BLE001 - facts are best-effort context, never a failure
        return []


# Analysis sources whose `subject`/`description` were never seen by a model: the
# keyless filename stub (`autoplace._stub_analysis`), the extracted-frame placeholder
# (`autoplace._materialize_extracted_frames`) and probe-only metadata.
_NON_VISION_SOURCES = frozenset({"stub", "extracted_frame", "probe_only"})
_NON_VISION_FIELDS = ("subject", "description", "best_moments")


def _slide_seen(asset: Any) -> str:
    """What a ready slide's photo/video shows (KRI-817), or "" when nothing was seen.

    The same sanitized, capped projection the video copilot uses (``_seen_text``), so a
    legacy ``description``/``subject`` counts like an ``understanding`` block. Not gated
    on CLIP_FACTS, like the video path. Placeholder text a model never produced is
    dropped first, so a filename can't pass as visual evidence.
    """
    try:
        analysis = getattr(asset, "analysis", None)
        if not isinstance(analysis, dict) or not analysis:
            return ""
        if analysis.get("source") in _NON_VISION_SOURCES:
            analysis = {k: v for k, v in analysis.items() if k not in _NON_VISION_FIELDS}
        kind = "image" if getattr(asset, "kind", None) == "image" else "video"
        return _seen_text(analysis, kind=kind)
    except Exception:  # noqa: BLE001 - context is best-effort, never a failure
        return ""


def _prompt_facts(facts: Any) -> list[dict[str, Any]]:
    from app.services.clip_facts import facts_for_prompt  # noqa: PLC0415

    out = [
        {
            "kind": str(fact.get("kind")),
            "value": str(fact.get("value"))[:_MAX_FACT_VALUE_CHARS],
            "provenance": str(fact.get("provenance")),
        }
        for fact in facts_for_prompt(facts)
        if fact.get("kind") and fact.get("value")
    ]
    return out[:_MAX_SLOT_FACTS]


# ------------------------------------------------------------------ working state


def _label_bar_id(slide_id: str) -> str:
    return f"{_CLIP_LABEL_MEDIA_PREFIX}{slide_id}"


def _slide_rows(draft: SlidePostDraft) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, slide in enumerate(draft.slides):
        row: dict[str, Any] = {
            "slot_id": slide.id,
            "media_id": slide.id,
            "media_kind": slide.kind,
            "clip_index": index,
            "in_s": 0.0,
            "duration_s": 1.0,
            "output_start_s": float(index),
            "output_end_s": float(index + 1),
            "removed": False,
            "transition_after": "cut",
            "look_preset": slide.edits.look_preset if slide.edits else "none",
            "orientation": _framing_of(slide)[0],
            "source_crop": _framing_of(slide)[1],
            "fit": slide.edits.fit if slide.edits else None,
            # Wire shape of the slide's tilt (KRI-1138), None = upright; a row of a compiled turn.
            "tilt": slide.edits.tilt.model_dump(mode="json")
            if slide.edits is not None and slide.edits.tilt is not None
            else None,
        }
        if slide.edits is not None and slide.edits.drawing is not None:
            # Read-only hint (KRI-589): the creator drew on this slide. Present only
            # when true so a draft without drawings builds a byte-identical snapshot.
            row["has_drawing"] = True
        if slide.edits is not None and slide.edits.stickers:
            # Read-only hint (KRI-647), present only when true like has_drawing.
            row["has_stickers"] = True
        rows.append(row)
    return rows


def _framing_of(slide: SlideRef) -> tuple[str, dict[str, float] | None]:
    framing = slide.edits.framing if slide.edits else None
    if framing is None:
        return "portrait", None
    crop = framing.source_crop.model_dump() if framing.source_crop is not None else None
    return framing.orientation, crop


def _asset_upright_aspect(asset: Any) -> float | None:
    """Width / height of the photo as displayed, or None when unknown.

    Only dimensions stamped `dims_upright` count: photos analysed before KRI-44
    were measured on the raw (EXIF-unrotated) pixels, so trusting them would hand
    a rotated iPhone portrait the wrong crop shape.
    """
    analysis = getattr(asset, "analysis", None)
    if not isinstance(analysis, dict) or analysis.get("dims_upright") is not True:
        return None
    w, h = analysis.get("width"), analysis.get("height")
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in (w, h)):
        return round(float(w) / float(h), 4)
    aspect = getattr(asset, "aspect", None)
    if isinstance(aspect, (int, float)) and not isinstance(aspect, bool) and aspect > 0:
        return round(float(aspect), 4)
    return None


def _bar_row(slide_id: str, index: int, el: SlideTextElement, *, bar_id: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": bar_id,
        "text": el.text,
        "start_s": float(index),
        "end_s": float(index + 1),
        # The role the text-element schema accepts (the eval runner and the video
        # compiler validate bars strictly). Free texts get a `kria-` id so the video
        # "title" heuristic can never call slide 1's text the title.
        "role": "generative_intro",
        "font_family": el.font_family,
        "color": el.color,
        "size_px": el.size_px,
        "alignment": el.alignment,
        "position": _POSITION_IN.get(el.position, el.position),
        "stroke_width": el.stroke_width,
        "shadow_enabled": el.shadow_enabled,
        "effect": "static",
    }
    if el.x_frac is not None:
        row["x_frac"] = el.x_frac
    if el.y_frac is not None:
        row["y_frac"] = el.y_frac
    if el.max_width_frac is not None:
        row["max_width_frac"] = el.max_width_frac
    return row


def _bars_and_meta(
    draft: SlidePostDraft,
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, SlideTextElement]]]:
    """Text bars for every slide text + bar id -> (slide id, element)."""
    rows: list[dict[str, Any]] = []
    meta: dict[str, tuple[str, SlideTextElement]] = {}
    for index, slide in enumerate(draft.slides):
        label_taken = False
        for el in slide.edits.effective_texts() if slide.edits else []:
            if el.role == "label" and not label_taken:
                bar_id = _label_bar_id(slide.id)
                label_taken = True
            else:
                bar_id = f"kria-{slide.id}:{el.id}"
            rows.append(_bar_row(slide.id, index, el, bar_id=bar_id))
            meta[bar_id] = (slide.id, el)
    return rows, meta


def build_slide_post_snapshot(
    draft: SlidePostDraft,
    assets_by_id: Mapping[Any, Any],
    *,
    user_id: object = None,
    brief_binding: BriefBinding | None = None,
    sizes_enabled: bool = False,
    tilt_enabled: bool = False,
) -> dict[str, Any]:
    """The copilot's view of a slide post (``surface: "slide_post"``, editor ops v2).

    ``sizes_enabled`` (KRI-606) is decided by the route: the flags AND a client that
    round-trips the post size and per-photo Fit. Without it the size/fit ops are
    refused, so an older app is never told "it's square now" and then drops the field.
    ``tilt_enabled`` (KRI-1138) is the same kind of gate for ``set_slide_tilt``:
    MEDIA_TILT_ENABLED AND a protocol-28 build. Closed, the snapshot (and so the prompt)
    carries no tilt at all.
    """
    facts_on = settings.clip_facts_for(user_id)
    slots: list[dict[str, Any]] = []
    for index, row in enumerate(_slide_rows(draft)):
        slide = draft.slides[index]
        asset = assets_by_id.get(slide.asset_id)
        facts = _slide_facts(asset) if facts_on else []
        # Keyed by the stable slide id (`media_id`), so it follows the photo on reorder.
        slot = _safe_slot(row, index, facts=facts or None, seen=_slide_seen(asset) or None)
        slot["is_cover"] = index == draft.cover_index
        if row.get("has_drawing"):
            slot["has_drawing"] = True
        if row.get("has_stickers"):
            slot["has_stickers"] = True
        if settings.slide_post_framing_enabled:
            orientation, crop = _framing_of(slide)
            slot["orientation"] = orientation
            slot["cropped"] = crop is not None
            if sizes_enabled and slide.kind == "image":
                slot["fit"] = slide.edits.fit if slide.edits and slide.edits.fit else "fill"
            source_aspect = (
                _asset_upright_aspect(assets_by_id.get(slide.asset_id))
                if slide.kind == "image"
                else None
            )
            if source_aspect is not None:
                slot["source_aspect"] = source_aspect
        if tilt_enabled and row.get("tilt") is not None:
            slot["tilt"] = row["tilt"]
        slots.append(slot)
    rows, meta = _bars_and_meta(draft)
    text_bars: list[dict[str, Any]] = []
    for row in rows:
        bar = dict(row)
        slide_id, el = meta[row["id"]]
        if row["id"] == _label_bar_id(slide_id):
            bar["clip_id"] = slide_id
            bar["inferred"] = False
            if el.edited:
                bar["edited"] = True
        text_bars.append(bar)
    count = len(draft.slides)
    snapshot: dict[str, Any] = {
        "surface": SLIDE_POST_SURFACE,
        "allowed_op_families": list(_FAMILIES),
        "base_generation": f"slide-post-v{draft.version}",
        "has_narrated_captions": False,
        "max_duration_s": float(count),
        "remaining_duration_s": 0.0,
        "slots": slots,
        "text_bars": text_bars,
        "total_duration_s": float(count),
        "post": {
            "caption": draft.caption[:_MAX_CAPTION_SHOWN],
            "cover_index": draft.cover_index,
            "platform_profile": str(draft.platform_profile),
        },
    }
    if draft.aspect is not None:
        # Focus/zoom crops are resolved against the post's real shape.
        snapshot["post"]["aspect"] = draft.aspect
    if settings.slide_post_framing_enabled:
        snapshot["post"]["framing_enabled"] = True
        snapshot["post"]["landscape_allowed"] = draft.platform_profile == "tiktok_photo"
    if sizes_enabled and settings.slide_post_framing_enabled:
        from app.pipeline.slide_post.profiles import PLATFORM_PROFILES  # noqa: PLC0415

        spec = PLATFORM_PROFILES[draft.platform_profile]
        snapshot["post"]["sizes_enabled"] = True
        snapshot["post"]["aspect"] = draft.aspect or spec.default_aspect
        snapshot["post"]["allowed_aspects"] = list(spec.allowed_aspects)
    if tilt_enabled:
        snapshot["post"]["tilt_enabled"] = True
    if any(slot.get("facts") for slot in slots):
        snapshot["label_facts"] = True
    binding = brief_binding or draft.brief_binding
    if binding is not None:
        # Immutable request context for interpreting a follow-up. It is not a
        # claim that the request was fulfilled.
        snapshot["brief"] = binding.creator_request
    if _text_appearance_enabled():
        snapshot["text_appearance_version"] = 1
        snapshot["text_appearance"] = _text_appearance_inventory(text_bars, cues_present=False)
    snapshot["editor_ops_version"] = 2
    return snapshot


# --------------------------------------------------------------------- projection


def _apply_case(text: str, case: object) -> str:
    # KRI-781: the shared Turkish-aware text_case ("ışık'ta" -> "Işık'ta" for
    # Turkish; never str.title()'s "Emir'In").
    from app.agents._schemas.text_element import apply_text_case  # noqa: PLC0415

    return apply_text_case(text, case if isinstance(case, str) else None)


def _clamp(value: object, low: float, high: float, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(low, min(high, float(value)))


def _keep_number(value: float) -> int | float:
    """Whole numbers stay ints; a stored fractional native size is not rounded."""
    return int(value) if float(value).is_integer() else value


def _row_matches_element(row: dict[str, Any], el: SlideTextElement) -> bool:
    """True when the ops left this bar exactly as `_bar_row` projected it."""
    projected = _bar_row("", 0, el, bar_id="")
    for key, value in projected.items():
        if key in {"id", "start_s", "end_s", "role", "effect"}:
            continue
        if row.get(key) != value:
            return False
    return not any(row.get(k) is not None and k not in projected for k in _GEOMETRY_KEYS)


_GEOMETRY_KEYS = ("x_frac", "y_frac", "max_width_frac")


def _element_from_row(
    row: dict[str, Any],
    base: SlideTextElement | None,
    *,
    element_id: str,
    role: str,
    label_source: str | None,
    edited: bool,
) -> SlideTextElement:
    from app.agents._schemas.text_element import _ALLOWED_FONTS, _HEX_COLOR_RE  # noqa: PLC0415

    if (
        base is not None
        and base.id == element_id
        and base.role == role
        and base.label_source == label_source
        and base.edited == edited
        and row.get("text_case") in (None, "none")
        and _row_matches_element(row, base)
    ):
        # Untouched by the ops: keep the stored element byte-for-byte (no strip /
        # size clamp) so an untouched slide never counts as changed.
        return base
    data: dict[str, Any] = base.model_dump() if base is not None else {}
    text = _apply_case(str(row.get("text") or "").strip(), row.get("text_case"))
    if not text:
        raise KriaEditorOpError("A slide text cannot be empty")
    if len(text) > MAX_SLIDE_TEXT_LENGTH:
        raise KriaEditorOpError(f"Slide text can be at most {MAX_SLIDE_TEXT_LENGTH} characters")
    data.update(id=element_id, text=text, role=role, label_source=label_source, edited=edited)
    if row.get("font_family") in _ALLOWED_FONTS:
        data["font_family"] = row["font_family"]
    color = row.get("color")
    if isinstance(color, str) and _HEX_COLOR_RE.match(color):
        data["color"] = color
    if "size_px" in row:
        data["size_px"] = _keep_number(_clamp(row["size_px"], 8, 200, data.get("size_px", 86)))
    if row.get("alignment") in {"left", "center", "right"}:
        data["alignment"] = row["alignment"]
    position = _POSITION_OUT.get(str(row.get("position")))
    if position:
        data["position"] = position
    for key in ("x_frac", "y_frac"):
        if row.get(key) is not None:
            data[key] = _clamp(row[key], 0.0, 1.0, 0.5)
    if row.get("max_width_frac") is not None:
        data["max_width_frac"] = _clamp(row["max_width_frac"], 0.2, 1.0, 0.82)
    if "stroke_width" in row:
        data["stroke_width"] = _keep_number(_clamp(row["stroke_width"], 0, 20, 0))
    if isinstance(row.get("shadow_enabled"), bool):
        data["shadow_enabled"] = row["shadow_enabled"]
    try:
        return SlideTextElement.model_validate(data)
    except ValidationError as exc:  # pragma: no cover - fields are clamped above
        raise KriaEditorOpError("That text style isn't available on slides") from exc


def _slide_ids_for_bar(
    row: dict[str, Any],
    meta: dict[str, tuple[str, SlideTextElement]],
    windows: dict[str, tuple[float, float]],
) -> list[str]:
    """Slides (in INITIAL order) a bar belongs to."""
    target = row.get("_slide_id")
    if isinstance(target, str):
        # KRI-818: a one-slide text names its slide by id, never by position.
        return [target]
    row_id = str(row.get("id"))
    start, end = row.get("start_s"), row.get("end_s")
    known = meta.get(row_id)
    if known is not None:
        window = windows.get(known[0])
        if window is not None and (start, end) == window:
            return [known[0]]
    if row_id.startswith(_CLIP_LABEL_MEDIA_PREFIX):
        slide_id = row_id[len(_CLIP_LABEL_MEDIA_PREFIX) :]
        if slide_id in windows:
            return [slide_id]
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return []
    hit = [
        slide_id
        for slide_id, (w_start, w_end) in sorted(windows.items(), key=lambda kv: kv[1][0])
        if min(float(end), w_end) - max(float(start), w_start) >= 0.5 * (w_end - w_start)
    ]
    return hit


def _refuse_renumbering_guesses(
    draft: SlidePostDraft,
    ops: list[dict],
    windows: dict[str, tuple[float, float]],
    new_order: list[str],
) -> None:
    """Fail closed when a bundle's slide numbers could mean the post before OR after
    its own move (KRI-818, Troya T12).

    Numbers and windows in ops mean the post BEFORE the turn. When a ``reorder_clip``
    in the same bundle moves slides (a removal alone or a criterion sort does not count:
    the model cannot have meant a numbering it did not write), the before and after
    readings of a number can name different photos. A one-slide text or a cover change
    that names no slide id, and whose two readings disagree, is refused rather than
    guessed. A named slide id whose window matches neither reading is refused too.
    Refusal only: nothing here ever decides what the creator meant.
    """
    survivors = set(new_order)
    initial = [slide.id for slide in draft.slides if slide.id in survivors]
    moved = new_order != initial and any(op.get("op") == "reorder_clip" for op in ops)
    post_windows = {sid: (float(i), float(i + 1)) for i, sid in enumerate(new_order)}

    def same(window: tuple[float, float] | None, start: float, end: float) -> bool:
        return window is not None and abs(window[0] - start) + abs(window[1] - end) < 1e-6

    for op in ops:
        name = op.get("op")
        if name == "add_text":
            sid, start, end = op.get("slide_id"), op.get("start_s"), op.get("end_s")
            numeric = isinstance(start, (int, float)) and isinstance(end, (int, float))
            index = op.get("slide_positional_index")
            if sid and moved and isinstance(index, int):
                # A target given only by number: its before and after readings must
                # name the same photo, exactly like a number-only cover change.
                if index >= len(new_order) or new_order[index] != sid:
                    raise KriaEditorOpError(
                        f'Which slide should "{op.get("text")}" go on? The slides move in '
                        "this same edit, so I didn't guess"
                    )
            if sid and numeric:
                if not (
                    same(windows.get(sid), float(start), float(end))
                    or same(post_windows.get(sid), float(start), float(end))
                ):
                    raise KriaEditorOpError(
                        f'Which slide should "{op.get("text")}" go on? Its slide number '
                        "and photo don't match, so I didn't guess"
                    )
            elif not sid and moved:
                row = {"id": "", "start_s": start, "end_s": end}
                before = _slide_ids_for_bar(row, {}, windows)
                if len(before) == 1 and _slide_ids_for_bar(row, {}, post_windows) != before:
                    raise KriaEditorOpError(
                        f'Which slide should "{op.get("text")}" go on? The slides move in '
                        "this same edit, so I didn't guess"
                    )
        if name == "set_slide_cover" and moved and not op.get("slide_named"):
            index, target = op.get("slide_index"), op.get("slide_id")
            if isinstance(index, int) and (index >= len(new_order) or new_order[index] != target):
                raise KriaEditorOpError(
                    "Which photo should be the cover? The slides move in this same edit, "
                    "so I didn't guess"
                )


def compile_slide_post_ops(
    draft: SlidePostDraft,
    ops: list[dict],
) -> CompiledSlideDraft:
    """Apply parsed copilot ops to ``draft`` and return the next ``SlidePostDraft``.

    All-or-nothing: any op that cannot be represented raises ``KriaEditorOpError``.
    """
    for op in ops:
        name = str(op.get("op") or "")
        if name not in SLIDE_POST_OPS:
            raise KriaEditorOpError(f"{name or 'That operation'} isn't available for slide posts")

    rows, meta = _bars_and_meta(draft)
    slots = _slide_rows(draft)
    windows = {
        str(slot["media_id"]): (float(slot["output_start_s"]), float(slot["output_end_s"]))
        for slot in slots
    }
    edited_ids: set[str] = set()
    label_sources: dict[str, str] = {}
    label_ops: list[dict] = []
    before: dict[str, str] = {}
    before_rows: dict[str, dict[str, Any]] = {}
    # Bars some op actually changed (KRI-818 collateral guard): every other stored
    # text must come out byte-identical on its own slide.
    touched: set[str] = set()

    def hook(op: dict[str, Any], state: Any, phase: str) -> None:
        if phase == "before":
            before.clear()
            before.update({str(r.get("id")): str(r.get("text")) for r in state.text})
            before_rows.clear()
            before_rows.update({str(r.get("id")): copy.deepcopy(r) for r in state.text})
            return
        after_ids = {str(r.get("id")) for r in state.text}
        touched.update(set(before_rows) - after_ids)
        for row in state.text:
            row_id = str(row.get("id"))
            if before_rows.get(row_id) != row:
                touched.add(row_id)
                if row_id not in before_rows and op.get("op") == "add_text" and op.get("slide_id"):
                    row["_slide_id"] = str(op["slide_id"])
        is_label_op = op.get("op") == "label_each_clip"
        if is_label_op:
            label_ops.append(op)
        for row in state.text:
            row_id = str(row.get("id"))
            if not row_id.startswith(_CLIP_LABEL_MEDIA_PREFIX):
                continue
            if row_id in before and before[row_id] == str(row.get("text")):
                continue
            if is_label_op:
                label_sources[row_id] = (
                    "capture_time" if op.get("label_from") == "capture_time" else "place"
                )
            else:
                edited_ids.add(row_id)

    state = apply_text_lane_ops(rows, slots, ops, hook=hook)

    surviving = [row for row in state.slots if not row.get("removed")]
    if not surviving:
        raise KriaEditorOpError("A post needs at least one slide")
    _refuse_renumbering_guesses(draft, ops, windows, [str(r["media_id"]) for r in surviving])
    by_id = {slide.id: slide for slide in draft.slides}
    new_order = [str(row["media_id"]) for row in surviving]
    look_by_id = {str(row["media_id"]): row.get("look_preset", "none") for row in surviving}
    framing_by_id = {
        str(row["media_id"]): (row.get("orientation") or "portrait", row.get("source_crop"))
        for row in surviving
    }
    fit_by_id = {str(row["media_id"]): row.get("fit") for row in surviving}
    tilt_by_id = {str(row["media_id"]): row.get("tilt") for row in surviving}
    alive = set(new_order)

    # Group the final bars per slide, in bar order (originals first, then new).
    per_slide: dict[str, list[SlideTextElement]] = {sid: [] for sid in new_order}
    unsupported_style = False
    for op in ops:
        patch = op.get("patch")
        if isinstance(patch, dict) and set(patch) & _UNSUPPORTED_PATCH_FIELDS:
            unsupported_style = True
    for row in state.text:
        if row.get("removed"):
            continue
        targets = _slide_ids_for_bar(row, meta, windows)
        if not targets:
            raise KriaEditorOpError("That text doesn't land on any slide")
        row_id = str(row.get("id"))
        known = meta.get(row_id)
        if known is None and not any(sid in alive for sid in targets):
            # A NEW text whose slide is removed in the same edit: say so, never drop
            # it silently or move it to a neighbour (KRI-818).
            raise KriaEditorOpError(
                f'the slide for "{row.get("text")}" is removed in the same edit, '
                "so the text has nowhere to go"
            )
        targets = [sid for sid in targets if sid in alive]
        for sid in targets:
            base = known[1] if known is not None and known[0] == sid else None
            is_label = row_id == _label_bar_id(sid)
            if base is not None:
                element_id = base.id
            elif len(targets) == 1 and is_label:
                element_id = "label-" + uuid.uuid4().hex[:8]
            else:
                element_id = uuid.uuid4().hex[:12]
            if is_label:
                role = "label"
                if row_id in label_sources:
                    source, edited = label_sources[row_id], False
                else:
                    source = base.label_source if base else None
                    edited = bool(base.edited if base else False) or row_id in edited_ids
            elif base is not None and base.role == "label":
                # A second label on one slide (e.g. a duplicated location) has its own
                # kria- bar id; an untouched one keeps its role and source (KRI-818).
                role, source = "label", base.label_source
                edited = bool(base.edited)
            else:
                role, source = "text", None
                edited = bool(base.edited) if base else False
            per_slide[sid].append(
                _element_from_row(
                    row,
                    base,
                    element_id=element_id,
                    role=role,
                    label_source=source,
                    edited=edited,
                )
            )

    for bar_id, (sid, element) in meta.items():
        if bar_id in touched or sid not in alive:
            continue
        kept = element.model_dump()
        if not any(el.model_dump() == kept for el in per_slide[sid]):
            raise KriaEditorOpError("That edit would change a text it didn't name")

    new_slides: list[SlideRef] = []
    text_changed: list[int] = []
    look_changed = 0
    framing_changed = 0
    fit_changed = 0
    tilt_changed = 0
    for position, sid in enumerate(new_order):
        slide = by_id[sid]
        elements = per_slide[sid]
        if len(elements) > MAX_SLIDE_TEXTS:
            raise KriaEditorOpError(
                f"Slide {position + 1} would have more than {MAX_SLIDE_TEXTS} texts"
            )
        original = slide.edits.effective_texts() if slide.edits else []
        same_text = [e.model_dump() for e in elements] == [e.model_dump() for e in original]
        look = look_by_id[sid]
        original_look = slide.edits.look_preset if slide.edits else "none"
        orientation, crop = framing_by_id[sid]
        original_framing = _framing_of(slide)
        framing_same = (orientation, crop) == original_framing
        fit = fit_by_id[sid]
        original_fit = slide.edits.fit if slide.edits else None
        fit_same = fit == original_fit
        tilt = tilt_by_id[sid]
        original_tilt = (
            slide.edits.tilt.model_dump(mode="json")
            if slide.edits is not None and slide.edits.tilt is not None
            else None
        )
        tilt_same = tilt == original_tilt
        if same_text and look == original_look and framing_same and fit_same and tilt_same:
            new_slides.append(slide)
            continue
        if not same_text:
            text_changed.append(position + 1)
        if look != original_look:
            look_changed += 1
        if not framing_same:
            framing_changed += 1
        if not fit_same:
            fit_changed += 1
        if not tilt_same:
            tilt_changed += 1
        payload: dict[str, Any] = {
            "text": None,
            "look_preset": look,
            "texts": [e.model_dump(mode="python") for e in elements]
            if not same_text
            else (
                [e.model_dump(mode="python") for e in slide.edits.texts]
                if slide.edits and slide.edits.texts is not None
                else None
            ),
        }
        if payload["texts"] is None and slide.edits is not None and slide.edits.text is not None:
            payload["text"] = slide.edits.text.model_dump()
        if orientation != "portrait" or crop is not None:
            payload["framing"] = {"orientation": orientation, "source_crop": crop}
        if fit is not None:
            payload["fit"] = fit
        if tilt is not None:
            # Rebuilt from scratch above: carry the turn through a text or look change (KRI-1138).
            payload["tilt"] = tilt
        if slide.edits is not None and slide.edits.drawing is not None:
            # Rebuilt from scratch above: copy the creator's ink through, or a text/look
            # change on a drawn slide would silently erase it (KRI-589).
            payload["drawing"] = slide.edits.drawing.model_dump(mode="python")
        if slide.edits is not None and slide.edits.stickers:
            # Same for pasted image stickers (KRI-647).
            payload["stickers"] = [s.model_dump(mode="python") for s in slide.edits.stickers]
        try:
            new_slides.append(
                slide.model_copy(update={"edits": SlideEdits.model_validate(payload)})
            )
        except ValidationError as exc:
            raise KriaEditorOpError("That change isn't valid for a slide") from exc

    cover_id = str(state.post.get("cover_slide_id") or draft.slides[draft.cover_index].id)
    cover_index = new_order.index(cover_id) if cover_id in alive else 0
    caption = state.post.get("caption", draft.caption)
    aspect = state.post.get("aspect", draft.aspect)
    try:
        next_draft = bump_slide_post_version(
            draft, slides=new_slides, cover_index=cover_index, caption=caption, aspect=aspect
        )
    except ValidationError as exc:
        raise KriaEditorOpError("That change isn't valid for a post") from exc

    old_order = [slide.id for slide in draft.slides]
    moved = sum(1 for i, sid in enumerate(new_order) if i >= len(old_order) or old_order[i] != sid)
    removed = len(old_order) - len(new_order)
    changes: list[str] = []
    if removed:
        changes.append(f"Removed {removed} slide{'s' if removed != 1 else ''}")
    if moved and not removed:
        changes.append(f"Reordered {moved} slide{'s' if moved != 1 else ''}")
    labelled = {
        row_id[len(_CLIP_LABEL_MEDIA_PREFIX) :]
        for row_id in label_sources
        if row_id[len(_CLIP_LABEL_MEDIA_PREFIX) :] in alive
    }
    if labelled:
        kinds = {label_sources[_label_bar_id(sid)] for sid in labelled}
        noun = (
            "Location" if kinds == {"place"} else "Time" if kinds == {"capture_time"} else "Label"
        )
        changes.append(f"{noun} on {len(labelled)} slide{'s' if len(labelled) != 1 else ''}")
    other_text = [n for n in text_changed if new_order[n - 1] not in labelled]
    if other_text:
        changes.append(
            f"Text updated on {len(other_text)} slide{'s' if len(other_text) != 1 else ''}"
        )
    if look_changed:
        changes.append(f"Look changed on {look_changed} slide{'s' if look_changed != 1 else ''}")
    crops_reset = int(state.post.get("aspect_crops_reset", 0))
    if next_draft.aspect != draft.aspect or state.post.get("aspect_changed"):
        from app.pipeline.slide_post.profiles import PLATFORM_PROFILES  # noqa: PLC0415

        shown = next_draft.aspect or PLATFORM_PROFILES[next_draft.platform_profile].default_aspect
        was = draft.aspect or PLATFORM_PROFILES[draft.platform_profile].default_aspect
        if shown != was:
            line = f"Size is now {shown}"
            if crops_reset:
                noun = "slide" if crops_reset == 1 else "slides"
                line += f" \u00b7 crops reset on {crops_reset} {noun}"
            changes.append(line)
        # Those resets are already named in the size line.
        framing_changed = max(0, framing_changed - crops_reset)
    if framing_changed:
        changes.append(
            f"Framing changed on {framing_changed} slide{'s' if framing_changed != 1 else ''}"
        )
    if fit_changed:
        changes.append(f"Fit changed on {fit_changed} slide{'s' if fit_changed != 1 else ''}")
    if tilt_changed:
        changes.append(f"Tilt changed on {tilt_changed} slide{'s' if tilt_changed != 1 else ''}")
    if new_order[cover_index] != draft.slides[draft.cover_index].id:
        changes.append(f"Cover is now slide {cover_index + 1}")
    if caption != draft.caption:
        changes.append("Caption updated")

    notes: list[str] = []
    if unsupported_style:
        notes.append("Animation and spacing aren't available on slides, so those weren't applied.")
    place_ops = [op for op in label_ops if op.get("label_from") != "capture_time"]
    if place_ops:
        have = {sid for sid in new_order if any(el.role == "label" for el in per_slide[sid])}
        missing = len(new_order) - len(have)
        if missing:
            noun = "photo has" if missing == 1 else "photos have"
            notes.append(f"{missing} {noun} no location.")
    return CompiledSlideDraft(
        draft=next_draft,
        changes=changes,
        notes=notes,
        unsupported_requested_property=unsupported_style,
    )


# ------------------------------------------------------------------------- run


_CLIP_WORD = re.compile(r"\bclip(s?)\b", re.IGNORECASE)
# Quoted spans (the user's own text) are never reworded.
_QUOTED = re.compile(r'"[^"]*"|\u201c[^\u201d]*\u201d')
_WORDING_TOKEN = re.compile(_QUOTED.pattern + "|" + _CLIP_WORD.pattern, re.IGNORECASE)


def slide_wording(text: str) -> str:
    """Video wording -> slide wording ("clip 3" -> "slide 3")."""

    def swap(match: re.Match[str]) -> str:
        if _QUOTED.fullmatch(match.group(0)):
            return match.group(0)
        word = f"slide{match.group(1)}"
        return word.capitalize() if match.group(0)[0].isupper() else word

    return _WORDING_TOKEN.sub(swap, text)


def _same_content(a: SlidePostDraft, b: SlidePostDraft) -> bool:
    keys = {"version", "user_edited", "rendered_version"}
    return {k: v for k, v in a.model_dump(mode="json").items() if k not in keys} == {
        k: v for k, v in b.model_dump(mode="json").items() if k not in keys
    }


async def run_slide_post_chat_edit(
    *,
    draft: SlidePostDraft,
    assets_by_id: Mapping[Any, Any],
    message: str,
    turns: list[dict],
    user_id: object,
    server_version: int,
    run_context: RunContext | None = None,
    brief_binding: BriefBinding | None = None,
    sizes_enabled: bool = False,
    tilt_enabled: bool = False,
) -> SlidePostChatEditResponse:
    """One chat-edit turn. Never writes; every outcome is honest about what changed."""
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.routes._copilot import _honest_outcome  # noqa: PLC0415

    stored_binding = draft.brief_binding
    # A stored binding remains authoritative when the writer feature is rolled
    # back. A new binding is accepted only for the configured writer cohort.
    incoming_binding = (
        brief_binding if brief_binding is not None and settings.brief_binding_for(user_id) else None
    )
    active_binding = incoming_binding or stored_binding

    def response(outcome: SlideOutcome, reply: str, **extra: Any) -> SlidePostChatEditResponse:
        return SlidePostChatEditResponse(
            outcome=outcome, reply=reply, base_version=server_version, **extra
        )

    if active_binding is not None and len(active_binding.creator_request) > 12_000:
        return response(
            "unsupported",
            "This request context is too long to safely apply as a slide edit. "
            "Your draft is unchanged.",
        )

    snapshot = build_slide_post_snapshot(
        draft,
        assets_by_id,
        user_id=user_id,
        brief_binding=active_binding,
        sizes_enabled=sizes_enabled,
        tilt_enabled=tilt_enabled,
    )
    agent_input = EditCopilotInput(
        utterance=message,
        prior_turns=turns[:12],
        variant_snapshot=snapshot,
    )

    try:
        # This surface is a stateless HTTP-style caller. Keep its historical
        # 40-second cap even though durable Kria callers may use the agent's
        # larger timeout. Copy the context so cost/request attribution and any
        # caller-owned stricter cap remain intact.
        agent_context = replace(
            run_context or RunContext(),
            timeout_override_s=min(40.0, run_context.timeout_override_s)
            if run_context is not None and run_context.timeout_override_s is not None
            else 40.0,
        )
        output = await asyncio.to_thread(
            EditCopilotAgent(default_client()).run, agent_input, ctx=agent_context
        )
    except TerminalError as exc:
        log.warning("slide_post_chat_edit.agent_failed", error=str(exc)[:300])
        return response("failed", "I couldn't reach the editor assistant just now. Try again.")

    ops = [] if (output.needs_clarification or output.intent != "edit") else output.ops
    outcome, reply = _honest_outcome(output, ops, supports_proposed=True)
    suggestions = [slide_wording(s) for s in output.suggestions]
    if outcome != "proposed":
        mapped: SlideOutcome = (
            outcome if outcome in {"clarification", "unsupported", "no_effect"} else "failed"  # type: ignore[assignment]
        )
        return response(mapped, slide_wording(reply), suggestions=suggestions)

    try:
        compiled = compile_slide_post_ops(draft, ops)
    except KriaEditorOpError as exc:
        return response(
            "failed",
            slide_wording(f"I couldn't apply that: {exc}."),
            suggestions=suggestions,
        )
    if compiled.unsupported_requested_property:
        return response(
            "unsupported",
            "That requested text property isn't available on slides, "
            "so I left your draft unchanged.",
            suggestions=suggestions,
        )
    if _same_content(compiled.draft, draft):
        return response("no_effect", "Your slides already look like that.", suggestions=suggestions)
    if active_binding is not None:
        compiled.draft = compiled.draft.model_copy(update={"brief_binding": active_binding})
    notes = " ".join(slide_wording(n) for n in [output.reply_notes, *compiled.notes] if n)
    summary = ", ".join(compiled.changes) or "Updated your slides"
    reply_text = f"{summary}. {notes} Save when you're happy.".replace("  ", " ").strip()
    return response(
        "edited",
        reply_text,
        draft=compiled.draft,
        changes=compiled.changes,
        suggestions=suggestions,
    )


__all__ = [
    "CompiledSlideDraft",
    "SlidePostChatEditResponse",
    "build_slide_post_snapshot",
    "compile_slide_post_ops",
    "run_slide_post_chat_edit",
    "slide_wording",
]

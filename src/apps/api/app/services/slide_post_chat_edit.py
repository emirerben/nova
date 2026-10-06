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
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    """Path-free capture/understanding facts for one slide asset, [] when none exist.

    Lane B (KRI-300) adds ``clip_facts.slide_asset_facts(asset)``; swapping it in is a
    one-line change here (``return _prompt_facts(slide_asset_facts(asset))``).
    """
    from app.services.clip_facts import assignment_facts  # noqa: PLC0415

    try:
        capture = getattr(asset, "capture", None)
        analysis = getattr(asset, "analysis", None)
        return _prompt_facts(
            assignment_facts(
                {
                    "capture": capture if isinstance(capture, dict) else None,
                    "analysis": analysis if isinstance(analysis, dict) else None,
                }
            )
        )
    except Exception:  # noqa: BLE001 - facts are best-effort context, never a failure
        return []


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
        rows.append(
            {
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
            }
        )
    return rows


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
) -> dict[str, Any]:
    """The copilot's view of a slide post (``surface: "slide_post"``, editor ops v2)."""
    facts_on = settings.clip_facts_for(user_id)
    slots: list[dict[str, Any]] = []
    for index, row in enumerate(_slide_rows(draft)):
        slide = draft.slides[index]
        facts = _slide_facts(assets_by_id.get(slide.asset_id)) if facts_on else []
        slot = _safe_slot(row, index, facts=facts or None)
        slot["is_cover"] = index == draft.cover_index
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
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return text.title()
    return text


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

    def hook(op: dict[str, Any], state: Any, phase: str) -> None:
        if phase == "before":
            before.clear()
            before.update({str(r.get("id")): str(r.get("text")) for r in state.text})
            return
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
    by_id = {slide.id: slide for slide in draft.slides}
    new_order = [str(row["media_id"]) for row in surviving]
    look_by_id = {str(row["media_id"]): row.get("look_preset", "none") for row in surviving}
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
        targets = [sid for sid in targets if sid in alive]
        row_id = str(row.get("id"))
        known = meta.get(row_id)
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

    new_slides: list[SlideRef] = []
    text_changed: list[int] = []
    look_changed = 0
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
        if same_text and look == original_look:
            new_slides.append(slide)
            continue
        if not same_text:
            text_changed.append(position + 1)
        if look != original_look:
            look_changed += 1
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
        try:
            new_slides.append(
                slide.model_copy(update={"edits": SlideEdits.model_validate(payload)})
            )
        except ValidationError as exc:
            raise KriaEditorOpError("That change isn't valid for a slide") from exc

    cover_id = str(state.post.get("cover_slide_id") or draft.slides[draft.cover_index].id)
    cover_index = new_order.index(cover_id) if cover_id in alive else 0
    caption = state.post.get("caption", draft.caption)
    try:
        next_draft = bump_slide_post_version(
            draft, slides=new_slides, cover_index=cover_index, caption=caption
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
        draft, assets_by_id, user_id=user_id, brief_binding=active_binding
    )
    agent_input = EditCopilotInput(
        utterance=message,
        prior_turns=turns[:12],
        variant_snapshot=snapshot,
    )

    try:
        output = await asyncio.to_thread(
            EditCopilotAgent(default_client()).run, agent_input, ctx=run_context
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

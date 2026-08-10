"""nova.edit.director — proactive, ranked editorial suggestions.

Unlike the chat copilot, this agent never applies edits and never writes variant
state. It returns small, atomic bundles composed from the exact same validated
operation vocabulary the client already knows how to preview and save.
"""

from __future__ import annotations

import hashlib
import json
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.edit_copilot import (
    EditorOperationParseState,
    clean_editor_prompt_data,
    editor_effect_catalog,
    editor_font_catalog,
    editor_operation_contract,
    editor_snapshot_list,
    format_editor_snapshot,
    parse_editor_operation,
)
from app.config import settings
from app.pipeline.prompt_loader import load_prompt

EDIT_DIRECTOR_PROMPT_VERSION = "2026-08-09-v5"

SuggestionCategory = Literal["hook_pacing", "text", "audio", "effect", "transition"]
SuggestionApplyMode = Literal["instant", "omni_async", "server_async"]


class OmniSuggestion(BaseModel):
    action: Literal["generate_insert", "restyle_segment"]
    prompt: str = Field(min_length=1, max_length=500)
    insert_at_s: float = Field(ge=0.0)
    duration_s: float = Field(ge=3.0, le=10.0)
    source_clip_index: int | None = Field(default=None, ge=0)
    source_start_s: float | None = Field(default=None, ge=0.0)
    source_end_s: float | None = Field(default=None, ge=0.0)
    reference_clip_index: int | None = Field(default=None, ge=0)
    reference_frame_s: float | None = Field(default=None, ge=0.0)


class EditorSuggestion(BaseModel):
    id: str
    category: SuggestionCategory
    title: str = Field(min_length=1, max_length=80)
    rationale: str = Field(min_length=1, max_length=220)
    expected_benefit: str = Field(min_length=1, max_length=140)
    confidence: float = Field(ge=0.0, le=1.0)
    start_s: float = Field(ge=0.0)
    end_s: float = Field(ge=0.0)
    apply_mode: SuggestionApplyMode = "instant"
    ops: list[dict] = Field(default_factory=list, max_length=12)
    omni: OmniSuggestion | None = None


class EditDirectorInput(BaseModel):
    variant_snapshot: dict = Field(default_factory=dict)
    dismissed_suggestion_ids: list[str] = Field(default_factory=list, max_length=30)
    omni_enabled: bool = False


class EditDirectorOutput(BaseModel):
    suggestions: list[EditorSuggestion] = Field(default_factory=list, max_length=5)


def _clean_text(value: object, *, max_chars: int) -> str:
    return clean_editor_prompt_data(value, max_chars=max_chars)


def _suggestion_id(category: str, ops: list[dict], omni: dict | None) -> str:
    operation_fingerprints = [
        {
            "op": str(op.get("op") or ""),
            "targets": sorted(_operation_targets(op)),
        }
        for op in ops
    ]
    omni_fingerprint = (
        {
            "action": str(omni.get("action") or ""),
            "source_clip_index": omni.get("source_clip_index"),
        }
        if omni is not None
        else None
    )
    canonical = json.dumps(
        {
            "category": category,
            "ops": operation_fingerprints,
            "omni": omni_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"director-{hashlib.sha256(canonical.encode()).hexdigest()[:16]}"


def _operation_targets(op: dict) -> set[str]:
    """Return stable edit targets used to suppress contradictory suggestion cards."""
    name = str(op.get("op") or "")
    # Clip structure, timing, and transitions share one compatibility domain.
    # A reorder changes slot indices; a removal/split changes adjacency; and a
    # duration edit shifts downstream output windows. The browser correctly
    # rejects a later card whose original snapshot no longer describes that
    # timeline, so Director must never put two such cards in one accept-all
    # batch even when their nominal slot indices differ.
    if name in {
        "set_clip_duration",
        "set_clip_in",
        "reorder_clip",
        "remove_clip",
        "split_clip",
        "set_transition",
    }:
        return {"timeline:any"}
    if "bar_index" in op:
        return {f"text:{op['bar_index']}"}
    if "cue_index" in op:
        return {f"caption:{op['cue_index']}"}
    if "sfx_index" in op:
        return {f"sfx:{op['sfx_index']}"}
    if "overlay_index" in op:
        return {f"overlay:{op['overlay_index']}"}
    if "camera_effect_index" in op:
        return {f"camera:{op['camera_effect_index']}"}
    if "slot_index" in op:
        return {f"slot:{op['slot_index']}"}
    if name == "add_text":
        return {"text:new"}
    if name == "add_sfx":
        return {"sfx:new"}
    if name in {"add_overlay", "add_camera_effect"}:
        return {f"{name}:new"}
    if name == "accept_overlay_suggestion":
        return {f"overlay-suggestion:{op.get('suggestion_id')}"}
    if name == "apply_speech_cut_candidate":
        return {"speech-cut:any"}
    if name in {"swap_music", "set_mix"}:
        return {"audio:mix"}
    if name in {"set_intro_layout", "set_title"}:
        return {name}
    if name == "open_tool":
        return {f"tool:{op.get('tool')}"}
    return {f"op:{json.dumps(op, sort_keys=True, separators=(',', ':'))}"}


class EditDirectorAgent(Agent[EditDirectorInput, EditDirectorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.edit.director",
        prompt_id="edit_director",
        prompt_version=EDIT_DIRECTOR_PROMPT_VERSION,
        model=settings.edit_director_model,
        # Pro is the quality path, but a proactive card rail cannot spend two
        # 45s attempts before the Flash fallback even starts. Production traces
        # showed 100-120s end-to-end waits. One bounded attempt preserves the
        # deep review when it returns promptly and caps failover latency.
        max_attempts=1,
        backoff_s=(),
        timeout_s=30.0,
        thinking_level="high",
        enable_json_repair=True,
    )
    Input = EditDirectorInput
    Output = EditDirectorOutput
    response_json = True
    max_output_tokens = 6000

    def required_fields(self) -> list[str]:
        return ["suggestions"]

    def render_prompt(self, input: EditDirectorInput) -> str:  # noqa: A002
        dismissed = (
            "\n".join(
                f"- {_clean_text(item, max_chars=80)}"
                for item in input.dismissed_suggestion_ids[:30]
            )
            or "(none)"
        )
        return load_prompt(
            "edit_director",
            snapshot=format_editor_snapshot(input.variant_snapshot),
            dismissed=dismissed,
            omni_enabled="true" if input.omni_enabled else "false",
            font_catalog=editor_font_catalog(),
            effect_catalog=editor_effect_catalog(),
            operation_contract=editor_operation_contract(input.variant_snapshot),
        )

    def parse(self, raw_text: str, input: EditDirectorInput) -> EditDirectorOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"edit_director: invalid JSON — {exc}") from exc
        raw_suggestions = data.get("suggestions") if isinstance(data, dict) else None
        if not isinstance(raw_suggestions, list):
            raise SchemaError("edit_director: suggestions must be an array")

        dismissed = set(input.dismissed_suggestion_ids)
        total = max(0.0, float(input.variant_snapshot.get("total_duration_s") or 0.0))
        parsed: list[EditorSuggestion] = []
        selected_apply_mode: str | None = None
        seen: set[str] = set()
        seen_titles: set[str] = set()
        seen_targets: set[str] = set()
        categories = {"hook_pacing", "text", "audio", "effect", "transition"}

        for raw in raw_suggestions[:8]:
            if not isinstance(raw, dict):
                continue
            category = str(raw.get("category") or "")
            if category not in categories:
                continue
            title = _clean_text(raw.get("title"), max_chars=80)
            rationale = _clean_text(raw.get("rationale"), max_chars=220)
            benefit = _clean_text(raw.get("expected_benefit"), max_chars=140)
            if not title or not rationale or not benefit:
                continue
            title_key = title.casefold()
            if title_key in seen_titles:
                continue

            apply_mode = str(raw.get("apply_mode") or "instant")
            omni_raw = raw.get("omni") if isinstance(raw.get("omni"), dict) else None
            ops_raw = raw.get("ops") if isinstance(raw.get("ops"), list) else []
            ops: list[dict] = []
            state = EditorOperationParseState(1.0)
            invalid_op = False
            for raw_op in ops_raw[:12]:
                if isinstance(raw_op, dict) and raw_op.get("op") == "set_intro_layout":
                    invalid_op = True
                    break
                op = parse_editor_operation(raw_op, input.variant_snapshot, state)
                if op is None:
                    invalid_op = True
                    break
                ops.append(op)

            omni: OmniSuggestion | None = None
            if apply_mode == "omni_async":
                if not input.omni_enabled or omni_raw is None or ops:
                    continue
                try:
                    omni = OmniSuggestion.model_validate(omni_raw)
                except Exception:
                    continue
                known_clip_indices = {
                    int(slot["clip_index"])
                    for slot in editor_snapshot_list(input.variant_snapshot, ("slots",))
                    if isinstance(slot, dict) and isinstance(slot.get("clip_index"), int)
                }
                source_slots = [
                    slot
                    for slot in editor_snapshot_list(input.variant_snapshot, ("slots",))
                    if isinstance(slot, dict)
                    and not slot.get("removed")
                    and slot.get("clip_index") == omni.source_clip_index
                ]
                restyle_matches_slot = any(
                    abs(float(slot.get("in_s") or 0.0) - float(omni.source_start_s or 0.0)) <= 0.05
                    and abs(
                        float(slot.get("in_s") or 0.0)
                        + float(slot.get("duration_s") or 0.0)
                        - float(omni.source_end_s or 0.0)
                    )
                    <= 0.05
                    for slot in source_slots
                )
                if omni.action == "restyle_segment" and (
                    omni.source_start_s is None
                    or omni.source_end_s is None
                    or omni.source_clip_index is None
                    or omni.source_end_s <= omni.source_start_s
                    or omni.source_end_s - omni.source_start_s > 10.0
                    or omni.source_clip_index not in known_clip_indices
                    or not restyle_matches_slot
                ):
                    continue
                if (omni.reference_clip_index is None) != (omni.reference_frame_s is None) or (
                    omni.reference_clip_index is not None
                    and omni.reference_clip_index not in known_clip_indices
                ):
                    continue
            elif apply_mode == "instant":
                if invalid_op or not ops:
                    continue
            elif apply_mode == "server_async":
                if (
                    invalid_op
                    or omni_raw is not None
                    or len(ops) != 1
                    or ops[0].get("op") != "apply_speech_cut_candidate"
                ):
                    continue
            else:
                continue

            # A generated card is bound to the complete source revision. Any
            # instant accept changes that revision, and completing one Omni
            # card changes it for every other Omni card. Keep each returned
            # rail homogeneous, with at most one asynchronous card, so every
            # card shown can be accepted from the same review.
            if selected_apply_mode is not None and apply_mode != selected_apply_mode:
                continue
            if apply_mode in {"omni_async", "server_async"} and selected_apply_mode == apply_mode:
                continue

            try:
                confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.7))))
                start_s = max(0.0, float(raw.get("start_s", 0.0)))
                end_s = max(start_s, float(raw.get("end_s", start_s)))
            except (TypeError, ValueError):
                continue
            if total > 0:
                start_s = min(start_s, total)
                end_s = min(max(start_s, end_s), total)

            suggestion_id = _suggestion_id(category, ops, omni_raw)
            if suggestion_id in dismissed or suggestion_id in seen:
                continue
            targets: set[str] = {"omni:any"} if omni is not None else set()
            intra_card_conflict = False
            if omni is None:
                for op in ops:
                    op_targets = _operation_targets(op)
                    if targets & op_targets:
                        intra_card_conflict = True
                        break
                    targets.update(op_targets)
            if intra_card_conflict:
                continue
            if targets & seen_targets:
                continue
            seen.add(suggestion_id)
            seen_titles.add(title_key)
            seen_targets.update(targets)
            selected_apply_mode = apply_mode
            parsed.append(
                EditorSuggestion(
                    id=suggestion_id,
                    category=category,  # type: ignore[arg-type]
                    title=title,
                    rationale=rationale,
                    expected_benefit=benefit,
                    confidence=confidence,
                    start_s=start_s,
                    end_s=end_s,
                    apply_mode=apply_mode,  # type: ignore[arg-type]
                    ops=ops,
                    omni=omni,
                )
            )
            if len(parsed) == 5:
                break

        # Diversity and a 3-5 card rail are prompt-level quality goals. They
        # cannot be response-wide validity gates after per-card filtering:
        # discarding two useful, independently applicable cards turns a
        # partial model miss into an empty rail and a 502 for the creator.
        return EditDirectorOutput(suggestions=parsed)

    def schema_clarification(self) -> str:
        return (
            "\nReturn only JSON with a suggestions array containing 0-5 complete, "
            "non-overlapping suggestions. Every instant suggestion needs at least "
            "one valid operation copied field-for-field from AVAILABLE OPERATIONS. "
            'The discriminator is "op", never "name" or "action"; use the exact '
            "index and timing field names shown in those examples."
        )


class EditDirectorFallbackAgent(EditDirectorAgent):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.edit.director.fallback",
        prompt_id="edit_director",
        prompt_version=EDIT_DIRECTOR_PROMPT_VERSION,
        model=settings.edit_director_fallback_model,
        max_attempts=1,
        backoff_s=(),
        timeout_s=20.0,
        thinking_level="medium",
        enable_json_repair=True,
    )

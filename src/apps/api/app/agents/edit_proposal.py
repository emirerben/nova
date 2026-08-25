"""Draft a complete, reviewable story from all uploaded plan-item media."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict, deque
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, model_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import FastMontageCut

_SENSORY_CLAIM = re.compile(
    r"\b(?:delicious|tasty|flavorful|refreshing|favorite)\b",
    re.IGNORECASE,
)
_SENSORY_MODIFIER = re.compile(
    r"\b(?:delicious|tasty|flavorful|refreshing|favorite)\b(?=\s+\w)",
    re.IGNORECASE,
)
_PERSONAL_PRONOUN = re.compile(r"\b(?:i|we|my|our|us)\b", re.IGNORECASE)
_UNSUPPORTED_ACTION_LEAD = re.compile(
    r"^\s*(?:finally,?\s+)?(?:enjoying|discovering|relaxing|exploring|wandering|"
    r"visiting|tasting|trying)\b",
    re.IGNORECASE,
)
_FAST_CUT_TOTAL_TOLERANCE_S = 0.15
_FAST_DURATION_RECONCILE_TOLERANCE_S = 0.5
_FAST_DURATION_EPSILON_S = 0.001
_FAST_RECOVERABLE_CUT_MAX_S = 2.4


def minimum_required_sources(available: int) -> int:
    """Keep small edits varied without forcing one redundant source into the cut."""
    if available <= 3:
        return available
    if available < 7:
        return available - 1
    return 7


def _neutralize_sensory_modifier(text: str) -> str:
    cleaned = _SENSORY_MODIFIER.sub("", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = re.sub(r"\bA (?=[AEIOUaeiou])", "An ", cleaned)
    cleaned = re.sub(r"\ba (?=[AEIOUaeiou])", "an ", cleaned)
    cleaned = re.sub(r"\bAn (?=[^AEIOUaeiou\W])", "A ", cleaned)
    return re.sub(r"\ban (?=[^AEIOUaeiou\W])", "a ", cleaned)


def ai_draft_thought_has_unsupported_claim(text: str) -> bool:
    """Return whether model-authored copy asserts an unverified experience."""

    return bool(
        _PERSONAL_PRONOUN.search(text)
        or _UNSUPPORTED_ACTION_LEAD.search(text)
        or _SENSORY_CLAIM.search(text)
    )


class EditProposalMedia(BaseModel):
    media_id: str
    lane: Literal["clip", "asset"]
    kind: Literal["image", "video"]
    source_filename: str = ""
    duration_s: float | None = None
    user_context: str = ""
    subject: str = ""
    description: str = ""
    on_screen_text: str = ""
    best_moments: list[dict] = Field(default_factory=list)


class EditProposalAgentInput(BaseModel):
    idea: str = ""
    theme: str = ""
    direction: Literal["guided_story", "fast_montage", "text_explainer"]
    goal: str = ""
    pace: Literal["relaxed", "balanced", "fast"]
    # No artificial floor — the caller clamps this to what the uploaded
    # footage can actually support before invoking the agent.
    target_duration_s: int = Field(ge=3, le=60)
    media: list[EditProposalMedia] = Field(min_length=1, max_length=60)


class DraftStoryBeat(BaseModel):
    topic: str = Field(min_length=1, max_length=80)
    thought: str = Field(default="", max_length=280)
    media_ids: list[str] = Field(min_length=1, max_length=4)
    layout: Literal["fullscreen", "supporting_card"] = "fullscreen"
    duration_s: float = Field(ge=1.0, le=12.0)


class EditProposalAgentOutput(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    duration_s: int = Field(ge=3, le=60)
    story_beats: list[DraftStoryBeat] = Field(default_factory=list, max_length=5)
    # New fast-montage proposals use exact source windows. Legacy fast snapshots
    # omit this field and continue through the old story-beat compiler.
    fast_cuts: list[FastMontageCut] | None = Field(default=None, max_length=80)


class _RawFastMontageCut(BaseModel):
    """Provider cut shape before bounded server compilation."""

    cut_id: str = Field(min_length=1, max_length=100)
    media_id: str = Field(min_length=1, max_length=100)
    source_start_s: float = Field(ge=0)
    source_end_s: float = Field(gt=0)
    output_duration_s: float = Field(ge=0.4, le=_FAST_RECOVERABLE_CUT_MAX_S)
    role: Literal["hook", "build", "payoff"]
    transition: Literal["none"] = "none"
    beat_align: bool = False

    @model_validator(mode="after")
    def validate_finite_source_window(self) -> _RawFastMontageCut:
        values = (self.source_start_s, self.source_end_s, self.output_duration_s)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("fast montage cut timing must be finite")
        if self.source_end_s <= self.source_start_s:
            raise ValueError("fast montage cut source window must be positive")
        if abs(self.source_end_s - self.source_start_s - self.output_duration_s) > 0.001:
            raise ValueError("fast montage output duration must match its source window")
        return self


def _strict_fast_cut(raw_cut: _RawFastMontageCut, **updates) -> FastMontageCut:  # noqa: ANN003
    try:
        return FastMontageCut.model_validate({**raw_cut.model_dump(), **updates})
    except Exception as exc:  # noqa: BLE001
        raise SchemaError(f"edit_proposal: invalid fast cut — {exc}") from exc


def _compile_fast_cuts(
    raw_cuts: list,
) -> tuple[list[FastMontageCut], set[str], float]:
    """Compile a narrow provider timing violation into the persisted cut schema.

    Only exact, contiguous windows between 1.2s and 2.4s are recoverable. They
    are split without scaling or dropping source time, then interleaved by
    source so the resulting montage never repeats a source adjacently.
    """

    try:
        relaxed = [_RawFastMontageCut.model_validate(raw_cut) for raw_cut in raw_cuts]
    except Exception as exc:  # noqa: BLE001
        raise SchemaError(f"edit_proposal: invalid fast cut — {exc}") from exc
    if len({cut.cut_id for cut in relaxed}) != len(relaxed):
        raise SchemaError("edit_proposal: fast cut ids must be unique")
    raw_total_s = sum(cut.output_duration_s for cut in relaxed)
    if all(cut.output_duration_s <= 1.2 for cut in relaxed):
        return [_strict_fast_cut(cut) for cut in relaxed], set(), raw_total_s

    source_order: dict[str, int] = {}
    lanes: dict[str, deque[FastMontageCut]] = defaultdict(deque)
    repaired_ids: set[str] = set()
    expanded_count = 0
    for cut in relaxed:
        source_order.setdefault(cut.media_id, len(source_order))
        part_count = math.ceil(cut.output_duration_s / 1.2)
        part_duration_s = cut.output_duration_s / part_count
        if part_duration_s < 0.4 - _FAST_DURATION_EPSILON_S:
            raise SchemaError("edit_proposal: overlong fast cut cannot be split safely")
        for part_index in range(part_count):
            part_start_s = cut.source_start_s + part_duration_s * part_index
            part_end_s = (
                cut.source_end_s
                if part_index == part_count - 1
                else cut.source_start_s + part_duration_s * (part_index + 1)
            )
            part_start_s = round(part_start_s, 3)
            part_end_s = round(part_end_s, 3)
            normalized_duration_s = round(part_end_s - part_start_s, 3)
            cut_id = cut.cut_id if part_count == 1 else f"{cut.cut_id}-part-{part_index + 1}"
            compiled = _strict_fast_cut(
                cut,
                cut_id=cut_id,
                source_start_s=part_start_s,
                source_end_s=part_end_s,
                output_duration_s=normalized_duration_s,
                role="build",
                beat_align=False if part_count > 1 else cut.beat_align,
            )
            lanes[cut.media_id].append(compiled)
            if part_count > 1:
                repaired_ids.add(cut_id)
            expanded_count += 1
            if expanded_count > 80:
                raise SchemaError("edit_proposal: fast cut expansion exceeds 80 cuts")

    scheduled: list[FastMontageCut] = []
    previous_media_id: str | None = None
    while any(lanes.values()):
        candidates = [
            media_id for media_id, queue in lanes.items() if queue and media_id != previous_media_id
        ]
        if not candidates:
            raise SchemaError("edit_proposal: split fast cuts cannot avoid adjacent sources")
        media_id = min(
            candidates,
            key=lambda candidate: (-len(lanes[candidate]), source_order[candidate]),
        )
        scheduled.append(lanes[media_id].popleft())
        previous_media_id = media_id

    normalized: list[FastMontageCut] = []
    for index, cut in enumerate(scheduled):
        role = "hook" if index == 0 else "payoff" if index == len(scheduled) - 1 else "build"
        try:
            normalized.append(FastMontageCut.model_validate({**cut.model_dump(), "role": role}))
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"edit_proposal: invalid scheduled fast cut — {exc}") from exc
    return normalized, repaired_ids, raw_total_s


def _normalize_fast_montage_duration(
    payload: dict,
    input: EditProposalAgentInput,  # noqa: A002
) -> tuple[dict, set[str]]:
    """Reconcile harmless provider decimal drift to the server-owned target.

    Fast cuts are render-critical, so this validates their original shape and
    then fits their total to the server-owned target with bounded, deterministic
    tail-first adjustments. The provider's declared duration is only an intent
    check: LLM arithmetic may disagree with the valid cut windows it emitted.
    Story directions deliberately keep the legacy strict-integer contract.
    """

    declared_duration = payload.get("duration_s")
    if (
        isinstance(declared_duration, bool)
        or not isinstance(declared_duration, (int, float))
        or not math.isfinite(float(declared_duration))
    ):
        raise SchemaError("edit_proposal: fast montage duration must be finite and numeric")
    declared_duration_s = float(declared_duration)
    target_duration_s = float(input.target_duration_s)
    target_delta_s = target_duration_s - declared_duration_s
    if abs(target_delta_s) > _FAST_DURATION_RECONCILE_TOLERANCE_S:
        raise SchemaError("edit_proposal: fast montage duration is too far from the server target")

    raw_cuts = payload.get("fast_cuts")
    if not isinstance(raw_cuts, list) or not raw_cuts:
        # Let the normal output model retain its established missing/shape error.
        return payload, set()
    cuts, repaired_cut_ids, raw_total_s = _compile_fast_cuts(raw_cuts)

    # Reconcile against the actual cut total. Do not reject a fixable provider
    # arithmetic error merely because its declared total disagrees: each cut is
    # still constrained by the strict source window and 0.4-1.2s render bounds,
    # and the loop fails closed when those windows cannot reach the target.
    remaining_s = target_duration_s - raw_total_s
    media_by_id = {media.media_id: media for media in input.media}
    normalized_cuts = list(cuts)

    def assert_video_windows_do_not_overlap() -> None:
        windows_by_media: dict[str, list[tuple[float, float]]] = {}
        for candidate in normalized_cuts:
            media = media_by_id.get(candidate.media_id)
            if media is None or media.kind != "video":
                continue
            windows_by_media.setdefault(candidate.media_id, []).append(
                (candidate.source_start_s, candidate.source_end_s)
            )
        for windows in windows_by_media.values():
            windows.sort()
            for previous, current in zip(windows, windows[1:]):
                if current[0] < previous[1] - _FAST_DURATION_EPSILON_S:
                    raise SchemaError(
                        "edit_proposal: fast montage reuses overlapping source footage"
                    )

    assert_video_windows_do_not_overlap()
    for index in range(len(normalized_cuts) - 1, -1, -1):
        if abs(remaining_s) <= _FAST_DURATION_EPSILON_S:
            break
        cut = normalized_cuts[index]
        media = media_by_id.get(cut.media_id)
        if media is None:
            # The established source-identity check below reports this clearly.
            continue
        if remaining_s < 0:
            source_duration_s = float(media.duration_s or 0.0)
            minimum_duration_s = 0.8 if media.kind == "video" and source_duration_s >= 0.8 else 0.4
            capacity_s = cut.output_duration_s - minimum_duration_s
            adjustment_s = -min(-remaining_s, max(0.0, capacity_s))
        else:
            capacity_s = 1.2 - cut.output_duration_s
            if media.kind == "video":
                source_capacity_s = float(media.duration_s or 0.0) - cut.source_end_s
                next_source_start_s = min(
                    (
                        candidate.source_start_s
                        for candidate_index, candidate in enumerate(normalized_cuts)
                        if candidate_index != index
                        and candidate.media_id == cut.media_id
                        and candidate.source_start_s >= cut.source_end_s
                    ),
                    default=None,
                )
                if next_source_start_s is not None:
                    source_capacity_s = min(
                        source_capacity_s,
                        next_source_start_s - cut.source_end_s,
                    )
                capacity_s = min(capacity_s, max(0.0, source_capacity_s))
            adjustment_s = min(remaining_s, max(0.0, capacity_s))
        if abs(adjustment_s) <= _FAST_DURATION_EPSILON_S:
            continue
        new_duration_s = round(cut.output_duration_s + adjustment_s, 3)
        new_end_s = round(cut.source_start_s + new_duration_s, 3)
        try:
            normalized_cuts[index] = FastMontageCut.model_validate(
                {
                    **cut.model_dump(),
                    "source_end_s": new_end_s,
                    "output_duration_s": new_duration_s,
                    "beat_align": False,
                }
            )
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"edit_proposal: invalid reconciled fast cut — {exc}") from exc
        repaired_cut_ids.add(cut.cut_id)
        remaining_s = target_duration_s - sum(
            normalized.output_duration_s for normalized in normalized_cuts
        )

    if abs(remaining_s) > _FAST_DURATION_EPSILON_S:
        raise SchemaError("edit_proposal: fast montage duration cannot fit the server target")

    assert_video_windows_do_not_overlap()

    return (
        {
            **payload,
            "duration_s": input.target_duration_s,
            "fast_cuts": [cut.model_dump() for cut in normalized_cuts],
        },
        repaired_cut_ids,
    )


class EditProposalAgent(Agent[EditProposalAgentInput, EditProposalAgentOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.edit_proposal",
        prompt_id="edit_proposal",
        prompt_version="1.3.0",
        model="gemini-2.5-flash",
        thinking_budget=1024,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        enable_json_repair=True,
    )
    Input = EditProposalAgentInput
    Output = EditProposalAgentOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["title", "story_beats"]

    def render_prompt(self, input: EditProposalAgentInput) -> str:  # noqa: A002
        video_footage_s = sum(
            m.duration_s for m in input.media if m.kind == "video" and m.duration_s
        )
        footage_note = (
            f"Real available video footage totals about {video_footage_s:.1f}s across "
            f"{sum(1 for m in input.media if m.kind == 'video')} clip(s). Plan beats that "
            "fit inside what was actually filmed — never invent extra footage or imply a "
            "clip is longer than it is."
            if video_footage_s > 0
            else "No video footage was uploaded — every beat must use only the photos provided."
        )
        fast_timing_note = ""
        if input.direction == "fast_montage":
            minimum_fast_cuts = math.ceil(input.target_duration_s / 1.2)
            maximum_fast_cuts = math.floor(input.target_duration_s / 0.8)
            fast_timing_note = (
                f"For this {input.target_duration_s}s target, emit at least "
                f"{minimum_fast_cuts} cuts (normally no more than {maximum_fast_cuts}) so every "
                "cut stays at or below the absolute 1.2s maximum."
            )
        return load_prompt(
            "edit_proposal",
            idea=input.idea[:500],
            theme=input.theme[:500],
            direction=input.direction,
            goal=input.goal[:500]
            or "Make the uploaded material feel intentional and worth sharing.",
            pace=input.pace,
            target_duration_s=str(input.target_duration_s),
            fast_timing_note=fast_timing_note,
            footage_note=footage_note,
            media_json=json.dumps([row.model_dump() for row in input.media], ensure_ascii=False),
        )

    def parse(
        self,
        raw_text: str,
        input: EditProposalAgentInput,  # noqa: A002
    ) -> EditProposalAgentOutput:
        try:
            payload = json.loads(raw_text)
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"edit_proposal: invalid output — {exc}") from exc
        if not isinstance(payload, dict):
            raise SchemaError("edit_proposal: invalid output — expected an object")
        repaired_cut_ids: set[str] = set()
        if input.direction == "fast_montage":
            payload, repaired_cut_ids = _normalize_fast_montage_duration(payload, input)
        try:
            output = EditProposalAgentOutput.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"edit_proposal: invalid output — {exc}") from exc
        allowed = {m.media_id for m in input.media}
        media_by_id = {media.media_id: media for media in input.media}
        used: set[str] = set()
        for beat in output.story_beats:
            if not set(beat.media_ids) <= allowed:
                raise SchemaError("edit_proposal: beat references unknown media")
            if len(beat.media_ids) != len(set(beat.media_ids)):
                raise SchemaError("edit_proposal: beat repeats the same media")
            used.update(beat.media_ids)
        cuts = output.fast_cuts or []
        if input.direction == "fast_montage" and not cuts:
            raise SchemaError("edit_proposal: new fast montage proposals require fast_cuts")
        if input.direction == "fast_montage" and cuts:
            if cuts[0].role != "hook":
                raise SchemaError("edit_proposal: fast montage must open with a hook cut")
            if len(cuts) > 1 and cuts[-1].role != "payoff":
                raise SchemaError("edit_proposal: fast montage must end with a payoff cut")
            previous_media_id: str | None = None
            cut_sources: set[str] = set()
            total_cut_duration = 0.0
            for cut in cuts:
                media = media_by_id.get(cut.media_id)
                if media is None:
                    raise SchemaError("edit_proposal: fast cut references unknown media")
                if previous_media_id == cut.media_id:
                    raise SchemaError("edit_proposal: fast montage cannot repeat adjacent sources")
                previous_media_id = cut.media_id
                cut_sources.add(cut.media_id)
                total_cut_duration += cut.output_duration_s
                source_duration = float(media.duration_s or 0.0)
                if media.kind == "video" and cut.source_end_s > source_duration + 0.001:
                    raise SchemaError("edit_proposal: fast cut source window exceeds video")
                if cut.output_duration_s >= 0.8 or cut.cut_id in repaired_cut_ids:
                    continue
                if source_duration >= 0.8 or cut.output_duration_s < 0.4:
                    raise SchemaError(
                        "edit_proposal: fast cuts target 0.8-1.2s except truly short sources"
                    )
            minimum = minimum_required_sources(len(input.media))
            if len(cut_sources) < minimum:
                raise SchemaError(
                    f"edit_proposal: fast montage selected {len(cut_sources)} distinct sources; "
                    f"need at least {minimum}"
                )
            if abs(total_cut_duration - output.duration_s) > _FAST_CUT_TOTAL_TOLERANCE_S:
                raise SchemaError(
                    "edit_proposal: fast cut durations do not fit the declared duration"
                )
        else:
            minimum = minimum_required_sources(len(input.media))
            if len(used) < minimum:
                raise SchemaError(
                    f"edit_proposal: selected {len(used)} distinct sources; need at least {minimum}"
                )
        available_kinds = {media.kind for media in input.media}
        # Fast montage proposals intentionally leave ``story_beats`` empty;
        # their source-of-truth is the ordered cut list.
        variety_ids = cut_sources if input.direction == "fast_montage" and cuts else used
        used_kinds = {media.kind for media in input.media if media.media_id in variety_ids}
        if len(available_kinds) > 1 and used_kinds != available_kinds:
            raise SchemaError("edit_proposal: story must use both photos and videos")
        if input.direction in {"guided_story", "text_explainer"}:
            if not output.story_beats:
                raise SchemaError("edit_proposal: guided story needs story beats")
            minimum_beats = min(3, len(input.media))
            if len(output.story_beats) < minimum_beats:
                raise SchemaError(
                    f"edit_proposal: guided story needs at least {minimum_beats} beats"
                )
            if any(not beat.thought.strip() for beat in output.story_beats):
                raise SchemaError("edit_proposal: guided story thoughts cannot be empty")
        if input.direction != "fast_montage":
            minimum_topics = min(3, len(input.media))
            distinct_topics = {beat.topic.strip().casefold() for beat in output.story_beats}
            if len(distinct_topics) < minimum_topics:
                raise SchemaError(
                    f"edit_proposal: story needs at least {minimum_topics} distinct topics"
                )
        for beat in output.story_beats:
            has_creator_context = any(
                media_by_id[media_id].user_context.strip() for media_id in beat.media_ids
            )
            if not has_creator_context:
                beat.thought = _neutralize_sensory_modifier(beat.thought)
            if len(beat.thought.split()) > 18:
                raise SchemaError("edit_proposal: draft thought exceeds 18 words")
            if not has_creator_context and ai_draft_thought_has_unsupported_claim(beat.thought):
                raise SchemaError(
                    "edit_proposal: draft thought invents an unsupported personal experience"
                )
        if abs(output.duration_s - input.target_duration_s) > 5:
            raise SchemaError("edit_proposal: duration is too far from the creator's target")
        if input.direction != "fast_montage":
            beat_duration = sum(beat.duration_s for beat in output.story_beats)
            max_intro_gap = max(6.0, output.duration_s * 0.3)
            if (
                beat_duration > output.duration_s
                or output.duration_s - beat_duration > max_intro_gap
            ):
                raise SchemaError("edit_proposal: beat durations do not fit the declared duration")
        return output

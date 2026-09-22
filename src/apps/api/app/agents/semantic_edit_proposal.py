"""Semantic-only edit proposal agent.

This pass chooses the editorial grouping and source intent.  The scheduler owns
all timestamps, durations, source windows, and frame arithmetic.
"""

from __future__ import annotations

import json
import math
import re
from typing import ClassVar

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.edit_proposal import (
    EditProposalAgentInput,
    EditProposalAgentOutput,
    EditProposalMedia,
    _media_prompt_dict,
    _required_media_ids,
)
from app.pipeline.prompt_loader import load_prompt
from app.schemas.edit_proposal import creator_copy_match_key
from app.schemas.semantic_edit import (
    SemanticChapter,
    SemanticEditPlan,
    SemanticSource,
    SemanticTextBinding,
)

_QUOTED_TEXT_RE = re.compile(
    r"[\"\u201c\u201d]([^\"\u201c\u201d]{1,120})[\"\u201c\u201d]|(?<!\w)'([^']{1,120})'(?!\w)"
)
_FORBIDDEN_TIMING_KEYS = frozenset(
    {"duration_s", "start_s", "end_s", "source_start_s", "source_end_s", "output_duration_s"}
)
_GROUP_SPLIT_ERROR = "semantic_edit_proposal: clip intent group was split"
_GROUP_UNRELATED_ERROR = "semantic_edit_proposal: clip intent group has unrelated sources"
_GROUP_RETRY_HINT = (
    "Correction: every resolved GROUP or CAPTION is an exclusive contiguous block. Use exactly its "
    "assigned aliases in its block; keep every other alias outside that block, even when "
    "the setting is similar."
)


def _validate_reuse_once(plan: SemanticEditPlan, input: EditProposalAgentInput) -> None:  # noqa: A002
    """Reject duplicate video choices before the scheduler has to repair them."""
    if input.video_reuse_policy != "once":
        return
    video_ids = {media.media_id for media in input.media if media.kind == "video"}
    seen: set[str] = set()
    for chapter in plan.chapters:
        for source in chapter.sources:
            if source.media_id in video_ids and source.media_id in seen:
                raise SchemaError(
                    "semantic_edit_proposal: a video appears more than once under once reuse"
                )
            seen.add(source.media_id)


def _canonicalize_text_bindings(
    plan: SemanticEditPlan, input: EditProposalAgentInput, repairs: list[str]
) -> None:  # noqa: A002
    """Keep server-owned and unrequested copy out of the generic text lane."""
    server_owned = {text for text in (input.opening_title, input.closing_title) if text}
    server_owned_keys = {creator_copy_match_key(text) for text in server_owned}
    if input.direction != "fast_montage":
        server_owned.update(input.shot_labels or [])
        server_owned_keys.update(creator_copy_match_key(label) for label in input.shot_labels or [])
    label_keys = _resolved_label_keys(input)
    label_texts = _resolved_label_texts(input)
    captions = _creator_captions(input)
    caption_texts = _caption_texts(captions)
    retained: list[SemanticTextBinding] = []
    for index, binding in enumerate(plan.text_bindings):
        key = creator_copy_match_key(binding.text)
        if binding.text in label_texts or (key in label_keys and binding.text not in caption_texts):
            repairs.append(f"dropped_grounded_label_text_binding:{index}")
        elif binding.text in server_owned or (key in server_owned_keys and key not in captions):
            repairs.append(f"dropped_server_owned_text_binding:{index}")
        elif captions and key not in captions:
            repairs.append(f"dropped_unrequested_text_binding:{index}")
        else:
            exact_creator_copy = (
                _resolve_creator_caption(binding.text, captions) if captions else None
            )
            if captions and key in captions and exact_creator_copy is None:
                raise SchemaError("semantic_edit_proposal: creator caption variants are ambiguous")
            if exact_creator_copy and binding.text != exact_creator_copy:
                binding = binding.model_copy(update={"text": exact_creator_copy})
                repairs.append(f"canonicalized_creator_caption_binding:{index}")
            retained.append(binding)
    plan.text_bindings = retained


def _semantic_prompt_media(
    input: EditProposalAgentInput,  # noqa: A002
) -> tuple[list[EditProposalMedia], dict[str, str], dict[str, str]]:
    """Alias the full semantic manifest in stable input order.

    The legacy proposal prompt uses a 32-source shortlist.  A semantic plan
    can be rejected for omitting a resolved intent or required source, so its
    model must see every source that the preflight/scheduler may require.
    """
    alias_to_id = {f"m{index + 1:03d}": media.media_id for index, media in enumerate(input.media)}
    id_to_alias = {media_id: alias for alias, media_id in alias_to_id.items()}
    return (
        [
            media.model_copy(update={"media_id": id_to_alias[media.media_id]})
            for media in input.media
        ],
        alias_to_id,
        id_to_alias,
    )


def _semantic_required_media_ids(input: EditProposalAgentInput) -> set[str]:  # noqa: A002
    """Include every source a semantic constraint can make mandatory."""
    required = set(_required_media_ids(input))
    if input.montage_audio:
        required.update(input.montage_audio.source_media_ids)
    if input.montage_cadence:
        required.update(input.montage_cadence.source_media_ids)
    for intent in input.clip_intents or []:
        if intent.status == "resolved":
            required.update(assignment.media_id for assignment in intent.assignments)
    return required


def _creator_captions(input: EditProposalAgentInput) -> dict[str, list[str]]:  # noqa: A002
    """Creator-quoted captions are a complete allowlist (KRI-129)."""
    if input.shot_labels:
        return {}
    resolved = [
        intent.caption_text
        for intent in input.clip_intents or []
        if intent.status == "resolved" and intent.op == "caption" and intent.caption_text
    ]
    if resolved:
        resolved.extend(
            intent.creator_text
            for intent in input.clip_intents or []
            if intent.status == "resolved" and intent.op == "group" and intent.creator_text
        )
        phrases: dict[str, list[str]] = {}
        for text in resolved:
            key = creator_copy_match_key(text)
            if text not in phrases.setdefault(key, []):
                phrases[key].append(text)
        return phrases
    phrases: dict[str, list[str]] = {}
    for match in _QUOTED_TEXT_RE.finditer(input.creator_request):
        text = (match.group(1) or match.group(2) or "").strip()
        key = creator_copy_match_key(text)
        if key:
            if text not in phrases.setdefault(key, []):
                phrases[key].append(text)
    for intent in input.clip_intents or []:
        if intent.status != "resolved":
            continue
        text = (
            intent.caption_text
            if intent.op == "caption"
            else intent.creator_text
            if intent.op == "group"
            else None
        )
        if text:
            key = creator_copy_match_key(text)
            if text not in phrases.setdefault(key, []):
                phrases[key].append(text)
    for title in (input.opening_title, input.closing_title):
        if title:
            key = creator_copy_match_key(title)
            phrases[key] = [text for text in phrases.get(key, []) if text != title]
            if not phrases[key]:
                phrases.pop(key)
    return phrases


def _resolve_creator_caption(text: str, captions: dict[str, list[str]]) -> str | None:
    """Return exact creator copy, or None when normalized variants are ambiguous."""
    variants = captions.get(creator_copy_match_key(text), [])
    if text in variants:
        return text
    return variants[0] if len(variants) == 1 else None


def _caption_texts(captions: dict[str, list[str]]) -> set[str]:
    return {text for variants in captions.values() for text in variants}


def _resolved_label_keys(input: EditProposalAgentInput) -> set[str]:  # noqa: A002
    """Exact copy already owned by the worker's grounded-label render lane."""
    return {
        creator_copy_match_key(assignment.value)
        for intent in input.clip_intents or []
        if intent.status == "resolved" and intent.op == "label"
        for assignment in intent.assignments
        if assignment.value
    }


def _resolved_label_texts(input: EditProposalAgentInput) -> set[str]:  # noqa: A002
    return {
        assignment.value
        for intent in input.clip_intents or []
        if intent.status == "resolved" and intent.op == "label"
        for assignment in intent.assignments
        if assignment.value
    }


def _blank_grounded_label_thoughts(
    plan: SemanticEditPlan, input: EditProposalAgentInput, repairs: list[str]
) -> None:
    """Never let a model duplicate a resolved label through the generic text lane."""
    if input.shot_labels:
        return
    label_keys = _resolved_label_keys(input)
    label_texts = _resolved_label_texts(input)
    caption_texts = _caption_texts(_creator_captions(input))
    for index, chapter in enumerate(plan.chapters):
        if chapter.thought in label_texts or (
            creator_copy_match_key(chapter.thought) in label_keys
            and chapter.thought not in caption_texts
        ):
            chapter.thought = ""
            repairs.append(f"blanked_grounded_label_thought:{index}")


def _candidate_index(media: object, source_start_s: float) -> int | None:
    moments = getattr(media, "best_moments", [])
    candidates: list[tuple[float, int]] = []
    for index, moment in enumerate(moments):
        if not isinstance(moment, dict):
            continue
        value = moment.get("start_s", moment.get("start"))
        try:
            candidates.append((abs(float(value) - source_start_s), index))
        except (TypeError, ValueError):
            continue
    return min(candidates)[1] if candidates else None


def _legacy_source(
    media_id: str, source_start_s: float | None, input: EditProposalAgentInput
) -> SemanticSource:  # noqa: A002
    media = next((row for row in input.media if row.media_id == media_id), None)
    index = (
        _candidate_index(media, source_start_s) if media and source_start_s is not None else None
    )
    return SemanticSource(media_id=media_id, candidate_index=index)


def _alias_or_id(media_id: str, alias_to_id: dict[str, str], allowed: set[str]) -> str | None:
    resolved = alias_to_id.get(media_id)
    if resolved is not None:
        return resolved
    return media_id if media_id in allowed else None


def _semantic_constraint_block(
    input: EditProposalAgentInput, alias_to_id: dict[str, str]
) -> dict[str, object]:  # noqa: A002
    """Only server-confirmed constraints, rewritten into prompt aliases."""
    id_to_alias = {media_id: alias for alias, media_id in alias_to_id.items()}

    def aliases(ids: list[str]) -> list[str]:
        return [id_to_alias[media_id] for media_id in ids if media_id in id_to_alias]

    intents = []
    exclusive_groups = []
    for intent in input.clip_intents or []:
        data = intent.model_dump(mode="json")
        data["assignments"] = [
            {
                **assignment,
                "media_id": id_to_alias.get(assignment["media_id"], assignment["media_id"]),
            }
            for assignment in data["assignments"]
        ]
        intents.append(data)
        if intent.status == "resolved" and intent.op in {"group", "caption"}:
            exclusive_groups.append(
                {
                    "intent_id": intent.intent_id,
                    "aliases": aliases([assignment.media_id for assignment in intent.assignments]),
                }
            )
    cadence = input.montage_cadence
    return {
        "media_scope": input.media_scope,
        "required_media_ids": aliases(sorted(_semantic_required_media_ids(input))),
        "video_reuse_policy": input.video_reuse_policy,
        "required_chapter_count": len(input.shot_labels) if input.shot_labels else None,
        "montage_audio": (
            {
                "preserve_source_audio": input.montage_audio.preserve_source_audio,
                "preview_source_beds": input.montage_audio.preview_source_beds,
                "source_media_ids": aliases(input.montage_audio.source_media_ids),
            }
            if input.montage_audio
            else None
        ),
        "clip_intents": intents,
        "exclusive_group_aliases": exclusive_groups,
        "cadence": (
            {
                "mode": cadence.mode,
                "source_media_ids": aliases(cadence.source_media_ids),
                "reuse_policy": cadence.reuse_policy,
                "note": (
                    "Preserve source order only. The server chooses the repeat count and durations."
                ),
            }
            if cadence
            else None
        ),
        "mixed_media_timing": input.mixed_media_timing.model_dump(mode="json")
        if input.mixed_media_timing
        else None,
        "narration": {"present": input.narration_duration_s is not None},
    }


def semantic_plan_from_legacy(
    output: EditProposalAgentOutput,
    input: EditProposalAgentInput,  # noqa: A002
) -> SemanticEditPlan:
    """Project legacy output into semantic intent for fixtures and corrections only.

    This is deliberately not a production fallback: new work must ask the
    semantic agent, then let the server scheduler materialize a frame plan.
    """
    chapters: list[SemanticChapter] = []
    if output.fast_cuts:
        # Legacy cuts can number 80 while semantic plans intentionally cap
        # chapters at 20. Collapse repeated sources into their first semantic
        # appearance; the scheduler expands cadence/reuse into real cuts.
        cut_by_media: dict[str, object] = {}
        source_order: list[str] = []
        for cut in output.fast_cuts:
            if cut.media_id not in cut_by_media:
                cut_by_media[cut.media_id] = cut
                source_order.append(cut.media_id)
        for index in range(0, len(source_order), 4):
            source_ids = source_order[index : index + 4]
            source_cuts = [cut_by_media[media_id] for media_id in source_ids]
            role = "hook" if index == 0 else "payoff" if index + 4 >= len(source_order) else "build"
            chapters.append(
                SemanticChapter(
                    chapter_id=f"chapter-{index // 4 + 1}",
                    topic="Montage",
                    thought="",
                    role=role,
                    weight=max(sum(float(cut.output_duration_s) for cut in source_cuts), 0.001),
                    layout="fullscreen",
                    sources=[
                        _legacy_source(cut.media_id, cut.source_start_s, input)
                        for cut in source_cuts
                    ],
                )
            )
    else:
        total = sum(max(float(beat.duration_s), 0.0) for beat in output.story_beats) or 1.0
        for index, beat in enumerate(output.story_beats):
            chapters.append(
                SemanticChapter(
                    chapter_id=f"chapter-{index + 1}",
                    topic=beat.topic,
                    thought=beat.thought,
                    role="hook"
                    if index == 0
                    else "payoff"
                    if index == len(output.story_beats) - 1
                    else "build",
                    weight=max(float(beat.duration_s) / total, 0.001),
                    layout=beat.layout,
                    sources=[_legacy_source(media_id, None, input) for media_id in beat.media_ids],
                )
            )
    if not chapters:
        raise SchemaError("semantic_edit_proposal: legacy output has no editorial sources")
    bindings = [
        SemanticTextBinding(text=item.text, media_ids=[item.media_id])
        for item in output.montage_text_bindings
    ]
    plan = SemanticEditPlan(
        title=input.opening_title or output.title,
        chapters=chapters,
        montage_audio=input.montage_audio,
        text_bindings=bindings,
    )
    if input.shot_labels and input.direction != "fast_montage":
        if len(plan.chapters) != len(input.shot_labels):
            raise SchemaError("semantic_edit_proposal: legacy labels need one chapter each")
        for chapter, label in zip(plan.chapters, input.shot_labels, strict=True):
            chapter.thought = label
    elif captions := _creator_captions(input):
        label_keys = _resolved_label_keys(input)
        for chapter in plan.chapters:
            if creator_copy_match_key(chapter.thought) in label_keys:
                chapter.thought = ""
                continue
            exact = _resolve_creator_caption(chapter.thought, captions)
            chapter.thought = exact if exact is not None else ""
    SemanticEditProposalAgent._add_creator_caption_bindings(plan, input)
    if len(plan.text_bindings) > 12:
        raise SchemaError(
            "semantic_edit_proposal: legacy creator bindings exceed the 12-item limit"
        )
    return plan


class SemanticEditProposalAgent(Agent[EditProposalAgentInput, SemanticEditPlan]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.semantic_edit_proposal",
        prompt_id="semantic_edit_proposal",
        prompt_version="2.0.12",
        model="gemini-2.5-flash",
        thinking_budget=1024,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        enable_json_repair=True,
    )
    Input = EditProposalAgentInput
    Output = SemanticEditPlan
    response_json = True

    def required_fields(self) -> list[str]:
        return ["chapters"]

    def render_prompt(self, input: EditProposalAgentInput) -> str:  # noqa: A002
        prompt_media, aliases, id_to_alias = _semantic_prompt_media(input)
        group_ids_by_alias = {row.media_id: [] for row in prompt_media}
        for intent in input.clip_intents or []:
            if intent.status != "resolved" or intent.op not in {"group", "caption"}:
                continue
            for assignment in intent.assignments:
                alias = id_to_alias.get(assignment.media_id)
                if alias is not None:
                    group_ids_by_alias[alias].append(intent.intent_id)
        media_rows = []
        for row in prompt_media:
            projection = _media_prompt_dict(row)
            projection["server_exclusive_group_ids"] = group_ids_by_alias[row.media_id]
            media_rows.append(projection)
        return load_prompt(
            "semantic_edit_proposal",
            direction=input.direction,
            goal=input.goal or input.idea or input.theme or "(none)",
            creator_request=input.creator_request or "(none)",
            opening_title=input.opening_title or "",
            closing_title=input.closing_title or "",
            shot_labels_json=json.dumps(input.shot_labels or [], ensure_ascii=False),
            constraints_json=json.dumps(
                _semantic_constraint_block(input, aliases), ensure_ascii=False
            ),
            media_json=json.dumps(media_rows, ensure_ascii=False),
        )

    def parse(self, raw_text: str, input: EditProposalAgentInput) -> SemanticEditPlan:  # noqa: A002
        self._schema_retry_hint: str | None = None
        try:
            return self._parse(raw_text, input)
        except SchemaError as exc:
            if str(exc) in {
                _GROUP_SPLIT_ERROR,
                _GROUP_UNRELATED_ERROR,
                "semantic_edit_proposal: caption intent has unrelated sources",
            }:
                self._schema_retry_hint = _GROUP_RETRY_HINT
            elif str(exc) in {
                "semantic_edit_proposal: creator shot labels need one chapter each",
                "semantic_edit_proposal: a video appears more than once under once reuse",
                "semantic_edit_proposal: requested media coverage was dropped",
            }:
                self._schema_retry_hint = (
                    "Correction: include every alias in required_media_ids. Under once reuse, "
                    "each video alias can appear only once across the entire chapters array."
                )
                if input.shot_labels:
                    self._schema_retry_hint += (
                        f" Return exactly {len(input.shot_labels)} chapters, one per shot label "
                        "in order. Put opening footage inside the first labeled chapter; "
                        "do not add an introduction or ending chapter."
                    )
            raise

    def schema_clarification(self) -> str:
        suffix = getattr(self, "_schema_retry_hint", None)
        return super().schema_clarification() + (f"\n\n{suffix}" if suffix else "")

    def _parse(self, raw_text: str, input: EditProposalAgentInput) -> SemanticEditPlan:  # noqa: A002
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"semantic_edit_proposal: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict):
            raise SchemaError("semantic_edit_proposal: response must be an object")
        if any(key in payload for key in _FORBIDDEN_TIMING_KEYS):
            raise SchemaError("semantic_edit_proposal: timeline fields belong to the scheduler")
        if not isinstance(payload.get("chapters"), list):
            raise SchemaError("semantic_edit_proposal: response needs chapters")

        _prompt_media_rows, alias_to_id, id_to_alias = _semantic_prompt_media(input)
        allowed = set(id_to_alias)
        normalized = {**payload, "montage_audio": input.montage_audio, "repairs": []}
        parse_repairs: list[str] = []
        normalized_chapters: list[dict] = []
        for chapter_index, raw_chapter in enumerate(payload["chapters"]):
            if not isinstance(raw_chapter, dict):
                raise SchemaError(
                    f"semantic_edit_proposal: chapter {chapter_index} is not an object"
                )
            if any(key in raw_chapter for key in _FORBIDDEN_TIMING_KEYS):
                raise SchemaError("semantic_edit_proposal: chapter timing belongs to the scheduler")
            raw_sources = raw_chapter.get("sources")
            if not isinstance(raw_sources, list):
                raise SchemaError(f"semantic_edit_proposal: chapter {chapter_index} needs sources")
            if not raw_sources:
                # The server renders titles separately. Gemini sometimes adds
                # an otherwise-empty title/outro chapter despite that contract;
                # it has no editorial source or copy to preserve, so omit it.
                thought = str(raw_chapter.get("thought") or "").strip()
                server_titles = {
                    title for title in (input.opening_title, input.closing_title) if title
                }
                if thought and thought not in server_titles:
                    raise SchemaError(
                        f"semantic_edit_proposal: chapter {chapter_index} needs sources"
                    )
                repair = (
                    "dropped_empty_server_title_chapter" if thought else "dropped_empty_chapter"
                )
                parse_repairs.append(f"{repair}:{chapter_index}")
                continue
            sources: list[dict] = []
            for source_index, raw_source in enumerate(raw_sources):
                if not isinstance(raw_source, dict):
                    raise SchemaError(
                        f"semantic_edit_proposal: source {chapter_index}:{source_index} is invalid"
                    )
                media_id = alias_to_id.get(str(raw_source.get("media_id") or ""))
                if media_id is None and str(raw_source.get("media_id") or "") in allowed:
                    media_id = str(raw_source["media_id"])
                if media_id is None:
                    raise SchemaError("semantic_edit_proposal: source references unknown media")
                candidate_index = raw_source.get("candidate_index")
                moments = next(row.best_moments for row in input.media if row.media_id == media_id)
                if candidate_index is not None:
                    if isinstance(candidate_index, bool) or not isinstance(candidate_index, int):
                        raise SchemaError(
                            "semantic_edit_proposal: source references unknown candidate"
                        )
                    if not moments and candidate_index == 0:
                        # No prompt-visible preferred window exists. Treat a
                        # model's habitual candidate_index=0 as absent rather
                        # than rejecting an otherwise grounded source.
                        raw_source = {**raw_source, "candidate_index": None}
                        parse_repairs.append(
                            f"dropped_unavailable_candidate:{chapter_index}:{source_index}"
                        )
                    elif not moments or candidate_index < 0 or candidate_index >= len(moments):
                        raise SchemaError(
                            "semantic_edit_proposal: source references unknown candidate"
                        )
                sources.append({**raw_source, "media_id": media_id})
            normalized_chapters.append({**raw_chapter, "sources": sources})
        if input.shot_labels and input.direction != "fast_montage":
            if len(normalized_chapters) != len(input.shot_labels):
                raise SchemaError(
                    "semantic_edit_proposal: creator shot labels need one chapter each"
                )
            for chapter_index, (chapter, label) in enumerate(
                zip(normalized_chapters, input.shot_labels, strict=True)
            ):
                if chapter.get("thought") != label:
                    chapter["thought"] = label
                    parse_repairs.append(
                        f"replaced_server_owned_shot_label_thought:{chapter_index}"
                    )
        normalized["chapters"] = normalized_chapters
        raw_bindings = payload.get("text_bindings", [])
        if not isinstance(raw_bindings, list):
            raise SchemaError("semantic_edit_proposal: text_bindings must be a list")
        normalized_bindings: list[dict] = []
        for index, raw_binding in enumerate(raw_bindings):
            if not isinstance(raw_binding, dict):
                raise SchemaError(f"semantic_edit_proposal: text binding {index} is invalid")
            raw_media_ids = raw_binding.get("media_ids", [])
            if not isinstance(raw_media_ids, list):
                raise SchemaError(
                    f"semantic_edit_proposal: text binding {index} media_ids is invalid"
                )
            media_ids: list[str] = []
            for raw_media_id in raw_media_ids:
                media_id = _alias_or_id(str(raw_media_id), alias_to_id, allowed)
                if media_id is None:
                    raise SchemaError(
                        "semantic_edit_proposal: text binding references unknown media"
                    )
                if media_id not in media_ids:
                    media_ids.append(media_id)
            normalized_bindings.append({**raw_binding, "media_ids": media_ids})
        normalized["text_bindings"] = normalized_bindings
        try:
            plan = SemanticEditPlan.model_validate(normalized)
        except Exception as exc:  # noqa: BLE001
            raise SchemaError(f"semantic_edit_proposal: invalid output — {exc}") from exc

        if input.direction == "fast_montage" and plan.chapters[0].role != "hook":
            raise SchemaError("semantic_edit_proposal: first remaining chapter must be hook")

        # Canonicalization below may discard generic copy, but never invalid
        # targets: reject those before deciding whether its text is server-owned.
        self._validate_bindings(plan, input)

        used = {source.media_id for chapter in plan.chapters for source in chapter.sources}
        required = _semantic_required_media_ids(input)
        if required and not required <= used:
            raise SchemaError("semantic_edit_proposal: requested media coverage was dropped")
        repairs = parse_repairs
        if input.opening_title:
            plan.title = input.opening_title
        if not input.shot_labels or input.direction == "fast_montage":
            _blank_grounded_label_thoughts(plan, input, repairs)
        _canonicalize_text_bindings(plan, input, repairs)
        if not input.shot_labels and (captions := _creator_captions(input)):
            for index, chapter in enumerate(plan.chapters):
                key = creator_copy_match_key(chapter.thought)
                exact = _resolve_creator_caption(chapter.thought, captions)
                if chapter.thought and key in captions and exact is None:
                    raise SchemaError(
                        "semantic_edit_proposal: creator caption variants are ambiguous"
                    )
                if chapter.thought and exact is None:
                    chapter.thought = ""
                    repairs.append(f"blanked_unrequested_thought:{index}")
                elif exact is not None:
                    chapter.thought = exact
                    if input.direction == "fast_montage":
                        plan.text_bindings.append(
                            SemanticTextBinding(text=exact, chapter_ids=[chapter.chapter_id])
                        )
        _validate_reuse_once(plan, input)
        self._recover_single_misassigned_group(plan, input, alias_to_id, repairs)
        self._apply_resolved_caption_intents(plan, input, alias_to_id)
        self._add_creator_caption_bindings(plan, input)
        self._enforce_creator_text_bindings(plan, input)
        if len(plan.text_bindings) > 12:
            raise SchemaError("semantic_edit_proposal: creator bindings exceed the 12-item limit")
        plan.repairs.extend(repairs)
        self._validate_intents(plan, input, alias_to_id)
        _validate_reuse_once(plan, input)
        self._validate_bindings(plan, input)
        return plan

    @staticmethod
    def _recover_single_misassigned_group(
        plan: SemanticEditPlan,
        input: EditProposalAgentInput,
        aliases: dict[str, str],
        repairs: list[str],
    ) -> None:
        """Move one stray trusted-group member into its sole exclusive anchor."""
        if input.shot_labels:
            return
        for intent in input.clip_intents or []:
            text = (
                intent.caption_text
                if intent.op == "caption"
                else intent.creator_text
                if intent.op == "group"
                else None
            )
            if intent.status != "resolved" or not text:
                continue
            ids = [aliases.get(item.media_id, item.media_id) for item in intent.assignments]
            group = set(ids)
            overlaps = False
            for other in input.clip_intents or []:
                other_ids = {
                    aliases.get(item.media_id, item.media_id) for item in other.assignments
                }
                if (
                    other.status == "resolved"
                    and other is not intent
                    and other.op in {"group", "caption"}
                    and other_ids
                    and other_ids != group
                    and group & other_ids
                ):
                    overlaps = True
                    break
            if overlaps:
                continue
            anchors = [
                i
                for i, c in enumerate(plan.chapters)
                if any(s.media_id in group for s in c.sources)
                and all(s.media_id in group for s in c.sources)
            ]
            mixed = [
                i
                for i, c in enumerate(plan.chapters)
                if any(s.media_id in group for s in c.sources)
                and any(s.media_id not in group for s in c.sources)
            ]
            if not mixed:
                continue
            if (
                len(anchors) != 1
                or len(mixed) != 1
                or any(i == 0 and plan.chapters[i].role == "hook" for i in mixed)
            ):
                continue
            anchor, origin = anchors[0], mixed[0]
            if plan.chapters[anchor].layout != plan.chapters[origin].layout:
                continue
            moved = [s for s in plan.chapters[origin].sources if s.media_id in group]
            remain = [s for s in plan.chapters[origin].sources if s.media_id not in group]
            if not moved or not remain:
                continue
            if plan.chapters[origin].thought:
                continue
            moved_ids = {source.media_id for source in moved}
            if moved_ids & {source.media_id for source in plan.chapters[anchor].sources}:
                continue
            if any(
                binding.text != text
                and (
                    plan.chapters[origin].chapter_id in binding.chapter_ids
                    or plan.chapters[anchor].chapter_id in binding.chapter_ids
                    or moved_ids & set(binding.media_ids)
                )
                for binding in plan.text_bindings
            ):
                continue
            anchor_sources = plan.chapters[anchor].sources
            masses = {
                id(source): chapter.weight
                * (source.weight / sum(item.weight for item in chapter.sources))
                for chapter in (plan.chapters[anchor], plan.chapters[origin])
                for source in chapter.sources
            }
            if not all(math.isfinite(mass) and mass > 0 for mass in masses.values()):
                continue
            rank = {media_id: index for index, media_id in enumerate(ids)}
            merged = list(anchor_sources)
            for source in sorted(moved, key=lambda item: rank[item.media_id]):
                insert_at = next(
                    (
                        index
                        for index, existing in enumerate(merged)
                        if rank.get(existing.media_id, len(rank)) > rank[source.media_id]
                    ),
                    len(merged),
                )
                merged.insert(insert_at, source)
            chapter_totals = [
                sum(masses[id(source)] for source in sources) for sources in (remain, merged)
            ]
            if len(merged) > 80 or not all(math.isfinite(total) for total in chapter_totals):
                continue
            plan.chapters[origin].sources = remain
            plan.chapters[anchor].sources = merged
            for chapter in (plan.chapters[anchor], plan.chapters[origin]):
                for source in chapter.sources:
                    source.weight = masses[id(source)]
                chapter.weight = sum(source.weight for source in chapter.sources)
            repairs.append(f"recovered_misassigned_group:{intent.intent_id}")

    @staticmethod
    def _validate_bindings(plan: SemanticEditPlan, input: EditProposalAgentInput) -> None:
        chapter_ids = {chapter.chapter_id for chapter in plan.chapters}
        scheduled_media_ids = {
            source.media_id for chapter in plan.chapters for source in chapter.sources
        }
        for binding in plan.text_bindings:
            if (
                not set(binding.chapter_ids) <= chapter_ids
                or not set(binding.media_ids) <= scheduled_media_ids
            ):
                raise SchemaError("semantic_edit_proposal: text binding references unknown target")

    @staticmethod
    def _add_creator_caption_bindings(
        plan: SemanticEditPlan, input: EditProposalAgentInput
    ) -> None:
        """Fast montages render text bindings, never chapter thoughts."""
        if input.direction != "fast_montage":
            return
        captions = _creator_captions(input)
        if not captions:
            return
        scheduled = {source.media_id for chapter in plan.chapters for source in chapter.sources}
        existing = {(binding.text, tuple(binding.media_ids)) for binding in plan.text_bindings}
        for intent in input.clip_intents or []:
            if intent.status != "resolved" or intent.op == "label" or not intent.creator_text:
                continue
            text = _resolve_creator_caption(intent.creator_text, captions)
            if creator_copy_match_key(intent.creator_text) in captions and text is None:
                raise SchemaError("semantic_edit_proposal: creator caption variants are ambiguous")
            targets = [
                assignment.media_id
                for assignment in intent.assignments
                if assignment.media_id in scheduled
            ]
            key = (text or "", tuple(targets))
            same_text_exists = any(binding.text == text for binding in plan.text_bindings)
            if text and targets and key not in existing and not same_text_exists:
                plan.text_bindings.append(SemanticTextBinding(text=text, media_ids=targets))
                existing.add(key)

    @staticmethod
    def _apply_resolved_caption_intents(
        plan: SemanticEditPlan, input: EditProposalAgentInput, aliases: dict[str, str]
    ) -> None:
        """Bind trusted resolved captions to their exclusive assigned chapter(s)."""
        if input.shot_labels:
            return
        assigned: dict[str, str] = {}
        caption_texts = {
            intent.caption_text
            for intent in input.clip_intents or []
            if intent.status == "resolved" and intent.op == "caption" and intent.caption_text
        }
        bound_texts = caption_texts | {
            intent.creator_text
            for intent in input.clip_intents or []
            if intent.status == "resolved" and intent.op == "group" and intent.creator_text
        }
        for chapter in plan.chapters:
            if chapter.thought in bound_texts:
                chapter.thought = ""
        plan.text_bindings = [
            binding for binding in plan.text_bindings if binding.text not in bound_texts
        ]
        for intent in input.clip_intents or []:
            if intent.status != "resolved":
                continue
            text = (
                intent.caption_text
                if intent.op == "caption"
                else intent.creator_text
                if intent.op == "group"
                else None
            )
            if not text:
                continue
            ids = {aliases.get(item.media_id, item.media_id) for item in intent.assignments}
            indexes = [
                index
                for index, chapter in enumerate(plan.chapters)
                if any(source.media_id in ids for source in chapter.sources)
            ]
            if not indexes:
                raise SchemaError("semantic_edit_proposal: caption intent source was dropped")
            for index in indexes:
                if any(source.media_id not in ids for source in plan.chapters[index].sources):
                    raise SchemaError(
                        "semantic_edit_proposal: caption intent has unrelated sources"
                    )
                prior = assigned.get(plan.chapters[index].chapter_id)
                if prior is not None and prior != text:
                    raise SchemaError(
                        "semantic_edit_proposal: caption intents conflict on a chapter"
                    )
                assigned[plan.chapters[index].chapter_id] = text
        for chapter in plan.chapters:
            if text := assigned.get(chapter.chapter_id):
                chapter.thought = text
                if input.direction == "fast_montage":
                    plan.text_bindings.append(
                        SemanticTextBinding(text=text, chapter_ids=[chapter.chapter_id])
                    )

    @staticmethod
    def _enforce_creator_text_bindings(
        plan: SemanticEditPlan, input: EditProposalAgentInput
    ) -> None:
        """Quoted creator captions form a complete text-binding allowlist."""
        captions = _creator_captions(input)
        label_keys = _resolved_label_keys(input)
        label_texts = _resolved_label_texts(input)
        # Resolved labels are re-grounded by the worker's dedicated context
        # lane.  They must never become generic montage text, even if the
        # model copied a server-resolved value verbatim.
        for binding in plan.text_bindings:
            key = creator_copy_match_key(binding.text)
            if binding.text in label_texts or (
                key in label_keys and binding.text not in _caption_texts(captions)
            ):
                raise SchemaError(
                    "semantic_edit_proposal: resolved label belongs to the grounded label lane"
                )
        if not captions:
            return
        for binding in plan.text_bindings:
            key = creator_copy_match_key(binding.text)
            if key not in captions or binding.text not in captions[key]:
                raise SchemaError("semantic_edit_proposal: unrequested text binding was invented")
        present = [
            text
            for text in [
                *(chapter.thought for chapter in plan.chapters),
                *(binding.text for binding in plan.text_bindings),
            ]
            if text
        ]
        required_captions = _caption_texts(captions) - label_texts
        if not required_captions <= set(present):
            raise SchemaError("semantic_edit_proposal: creator caption was dropped")

    @staticmethod
    def _validate_intents(
        plan: SemanticEditPlan, input: EditProposalAgentInput, aliases: dict[str, str]
    ) -> None:
        if not input.clip_intents:
            return
        positions: dict[str, list[int]] = {}
        source_order: list[str] = []
        for index, chapter in enumerate(plan.chapters):
            for source in chapter.sources:
                positions.setdefault(source.media_id, []).append(index)
                source_order.append(source.media_id)
        for intent in input.clip_intents:
            if intent.status != "resolved":
                continue
            ids = [aliases.get(item.media_id, item.media_id) for item in intent.assignments]
            if not all(media_id in positions for media_id in ids):
                raise SchemaError("semantic_edit_proposal: clip intent source was dropped")
            if intent.op in {"group", "caption"}:
                indexes = sorted(
                    {index for media_id in ids for index in positions.get(media_id, [])}
                )
                if not indexes or indexes[-1] - indexes[0] + 1 != len(indexes):
                    raise SchemaError("semantic_edit_proposal: clip intent group was split")
                for chapter in plan.chapters[indexes[0] : indexes[-1] + 1]:
                    if any(source.media_id not in ids for source in chapter.sources):
                        raise SchemaError(
                            "semantic_edit_proposal: clip intent group has unrelated sources"
                        )
            if intent.op == "order" and intent.position in {"first", "last"}:
                own = [index for index, media_id in enumerate(source_order) if media_id in ids]
                other = [
                    index for index, media_id in enumerate(source_order) if media_id not in ids
                ]
                if (
                    own
                    and other
                    and (
                        (intent.position == "first" and max(own) > min(other))
                        or (intent.position == "last" and min(own) < max(other))
                    )
                ):
                    raise SchemaError("semantic_edit_proposal: clip intent order changed")

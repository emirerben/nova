"""Prepare one existing guided montage snapshot before offering render consent."""

import hashlib
import json
import math
from typing import Literal
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MontageAudioPlan,
    StoryBeat,
)
from app.schemas.media_collections import MAX_STORY_MOMENTS, StoryContentPlan
from app.schemas.research import StoryResearch
from app.services import media_collections
from app.services.media_collection_stories import candidate_evidence, story_out


def execution_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PreparedSourceContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    moment_id: UUID
    asset_id: UUID
    source_id: UUID
    generation: str
    start_s: float = Field(ge=0, le=0, allow_inf_nan=False)
    end_s: float = Field(ge=1, le=20, allow_inf_nan=False)
    evidence_scope: Literal["whole_source_context"]
    context_contract: Literal["whole-source-context-v1"]
    semantic_localization: Literal["none"]
    timing_basis: Literal["verified_source_bounds"]
    uncertain: Literal[True]
    fact_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    index_spec: str = Field(pattern=r"^[a-f0-9]{64}$")
    quality: dict


def _source_contexts(moments: list[dict]) -> list[PreparedSourceContext]:
    result = []
    for raw in moments:
        evidence = raw.get("evidence") or {}
        scope = raw.get("evidence_scope", evidence.get("evidence_scope", "timed_moment"))
        if scope != "whole_source_context":
            if evidence.get("evidence_scope") == "whole_source_context":
                raise ValueError("Story source context scope changed")
            continue
        result.append(
            PreparedSourceContext(
                moment_id=raw["id"],
                asset_id=raw["asset_id"],
                source_id=raw["source_id"],
                generation=raw["generation"],
                start_s=raw["start_s"],
                end_s=raw["end_s"],
                uncertain=raw["uncertain"],
                evidence_scope=scope,
                **{
                    key: evidence.get(key)
                    for key in (
                        "context_contract",
                        "semantic_localization",
                        "timing_basis",
                        "fact_version",
                        "index_spec",
                        "quality",
                    )
                },
            )
        )
    return result


class PreparedSourceWindowAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    moment_id: UUID
    selected_start_s: float = Field(ge=0)
    selected_end_s: float = Field(gt=0)
    original_duration_s: float = Field(gt=0)
    prepared_end_s: float = Field(gt=0)
    reason: Literal["original_source_bounds"] = "original_source_bounds"


class PreparedCollectionStory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    story_id: UUID
    story_revision: int
    collection_id: UUID
    collection_revision: int
    snapshot: EditProposalSnapshot
    strategy: dict
    compiled_digest: str
    content_plan: StoryContentPlan | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    research: StoryResearch | None = Field(default=None, exclude_if=lambda value: value is None)
    source_window_adjustments: list[PreparedSourceWindowAdjustment] = Field(
        default_factory=list, max_length=MAX_STORY_MOMENTS, exclude_if=lambda value: not value
    )
    source_contexts: list[PreparedSourceContext] = Field(
        default_factory=list, max_length=MAX_STORY_MOMENTS, exclude_if=lambda value: not value
    )


def _without_open_questions(plan):
    """Creator answers have no consumer downstream, so they stay in the story row only."""
    return plan.model_copy(update={"open_questions": []}) if plan else plan


def selected_story_requirement(prepared: PreparedCollectionStory):
    """The creator selected this complete reviewed story, not an empty delegation."""
    from app.kria.brief import BriefUpdate

    return BriefUpdate(
        kind="select",
        scope="global",
        description=prepared.snapshot.title,
        facts={
            "collection_story": {
                "story_id": str(prepared.story_id),
                "story_revision": prepared.story_revision,
                "collection_id": str(prepared.collection_id),
                "collection_revision": prepared.collection_revision,
                "prepared_digest": execution_digest(prepared.model_dump(mode="json")),
            }
        },
    )


def prepared_story_readiness(raw, *, strategy, media):
    """Read execution evidence; ownership/current revisions remain separately fenced."""
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.pipeline.guided_story import GuidedStoryError, compile_proposal_execution_plan

    prepared = PreparedCollectionStory.model_validate(raw)
    if CreativeStrategy.model_validate(strategy) != CreativeStrategy.model_validate(
        prepared.strategy
    ):
        raise ValueError("Selected story strategy changed")
    try:
        compiled_digest = execution_digest(compile_proposal_execution_plan(prepared.snapshot))
    except GuidedStoryError as exc:
        raise ValueError("Selected story execution is invalid") from exc
    if compiled_digest != prepared.compiled_digest:
        raise ValueError("Selected story execution changed")
    assignments = {
        str(row.get("media_id")): row
        for row in media.get("clip_assignments") or []
        if isinstance(row, dict)
    }
    for ref in prepared.snapshot.media:
        attached = assignments.get(ref.media_id) or {}
        if attached.get("gcs_path") != ref.gcs_path or str(
            attached.get("storage_generation")
        ) != str(ref.generation):
            raise ValueError("Selected story source binding changed")
    return selected_story_requirement(prepared), execution_digest(prepared.model_dump(mode="json"))


async def prepare_story_snapshot(db, owner, story, item):
    from app.pipeline.guided_story import compile_proposal_execution_plan
    from app.pipeline.phone_recipe_shared import EXPORT_SAFETY_MARGIN_S
    from app.services.phone_sources import bind_phone_sources
    from app.services.proposal_planning import refresh_snapshot_schedule

    collection = await media_collections.owned_collection(
        db, owner, story.collection_id, revision=story.collection_revision
    )
    evidence = await candidate_evidence(
        db,
        owner,
        story.collection_id,
        story.collection_revision,
        moment_ids=[UUID(value["id"]) for value in story.moments_json],
        retained_story=story,
    )
    current = {str(value.id): value for value in evidence.moments}
    if any(value["id"] not in current for value in story.moments_json):
        raise HTTPException(409, "Story source changed; refresh the collection")
    contexts = _source_contexts(
        [current[value["id"]].model_dump(mode="json") for value in story.moments_json]
    )
    if contexts != _source_contexts(story.moments_json):
        raise HTTPException(409, "Story source context evidence changed; review it again")
    eligible = await media_collections.eligible_sources(
        db, owner, media_collections.members(collection)
    )
    assignments = {
        (value.get("library_asset_id"), value.get("library_source_id")): value
        for value in item.clip_assignments or []
        if isinstance(value, dict)
    }
    refs, cuts, beats = {}, [], []
    phone_bindings, source_window_adjustments = {}, []
    for index, raw in enumerate(story.moments_json):
        moment = current[raw["id"]]
        asset, source = eligible[moment.asset_id]
        assignment = assignments.get((str(asset.id), str(source.id)))
        if (
            asset.kind != "video"
            or source.role != "analysis_proxy"
            or assignment is None
            or assignment.get("gcs_path") != source.object_path
        ):
            raise HTTPException(409, "Attach this story's exact video sources to a new chat first")
        media_id = str(assignment.get("media_id") or "")
        if not media_id:
            raise HTTPException(409, "Story source has no render binding")
        try:
            binding = bind_phone_sources([assignment], [source.object_path])[0]
        except ValueError as exc:
            raise HTTPException(
                422, "Link this story's originals before preparing its draft"
            ) from exc
        phone_bindings[media_id] = binding
        start_frame, end_frame = round(moment.start_s * 30), round(moment.end_s * 30)
        prepared_end_s = moment.end_s
        if end_frame / 30 > binding.original.duration_s:
            safe_end = math.floor(
                (binding.original.duration_s - EXPORT_SAFETY_MARGIN_S) * 30 + 1e-9
            )
            maximum_correction = math.ceil((EXPORT_SAFETY_MARGIN_S + 1 / 30) * 30)
            if safe_end <= start_frame or end_frame - safe_end > maximum_correction:
                raise HTTPException(
                    422,
                    "The original is shorter than this story's selected footage; review its timing",
                )
            prepared_end_s = safe_end / 30
            source_window_adjustments.append(
                PreparedSourceWindowAdjustment(
                    moment_id=moment.id,
                    selected_start_s=moment.start_s,
                    selected_end_s=moment.end_s,
                    original_duration_s=binding.original.duration_s,
                    prepared_end_s=prepared_end_s,
                )
            )
        duration = float((asset.metadata_json or {}).get("duration_s") or 0)
        refs[media_id] = MediaRef(
            lane="clip",
            media_id=media_id,
            gcs_path=source.object_path,
            generation=source.object_generation,
            kind="video",
            duration_s=duration,
            content_hash=source.uploaded_sha256,
        )
        role = (
            "hook" if index == 0 else "payoff" if index == len(story.moments_json) - 1 else "build"
        )
        length = prepared_end_s - moment.start_s
        cuts.append(
            FastMontageCut(
                cut_id=str(moment.id),
                media_id=media_id,
                source_start_s=moment.start_s,
                source_end_s=prepared_end_s,
                output_duration_s=length,
                role=role,
            )
        )
        beats.append(
            StoryBeat(
                beat_id=str(moment.id),
                topic=role,
                thought="",
                media_ids=[media_id],
                duration_s=length,
            )
        )
    # The shared scheduler rounds source boundaries to the 30 fps output grid.
    # Pin its resulting length before preparing consent; arbitrary measured
    # endpoints (for example 9.98s) need not land on a frame boundary.
    duration_frames = sum(
        round(value.source_end_s * 30) - round(value.source_start_s * 30) for value in cuts
    )
    if not 30 <= duration_frames <= 60 * 30:
        raise HTTPException(422, "This story cannot fit the supported export duration")
    content_plan = _without_open_questions(
        StoryContentPlan.model_validate(story.story_json["content_plan"])
        if story.story_json.get("content_plan")
        else None
    )
    if content_plan and len(content_plan.beat_reasons) != len(cuts):
        raise HTTPException(422, "Review this idea's sequence before building the project")
    # Proposed wording is shown verbatim before selection and again in the
    # shared expiring approval. Source-analysis descriptions never supply copy.
    copy_fields = (
        {
            "opening_title": content_plan.opening_text,
            "opening_title_duration_s": min(
                3.0, duration_frames / 30 * (0.65 if content_plan.closing_text else 1)
            ),
            "closing_title": content_plan.closing_text,
            "font_family": "Inter-Bold",
        }
        if content_plan
        else {}
    )
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        goal=story.story_json["rationale"],
        title=content_plan.opening_text if content_plan else story.story_json["title"],
        text_policy="legacy" if content_plan else "none",
        duration_s=duration_frames / 30,
        media=list(refs.values()),
        story_beats=beats,
        fast_cuts=cuts,
        video_reuse_policy="allow_repeat",
        montage_audio=MontageAudioPlan(
            preserve_source_audio=story.story_json.get("audio_policy", "preserve_source_audio")
            == "preserve_source_audio",
            source_media_ids=list(refs),
        ),
        output_orientation="portrait",
        **copy_fields,
    )
    snapshot = refresh_snapshot_schedule(snapshot)
    compiled = compile_proposal_execution_plan(snapshot)
    expected_copy = (
        [
            content_plan.opening_text,
            *([content_plan.closing_text] if content_plan.closing_text else []),
        ]
        if content_plan
        else []
    )
    if [value["text"] for value in compiled.get("text_elements", [])] != expected_copy:
        raise HTTPException(422, "This project cannot preserve the reviewed on-screen wording")
    strategy = {
        "direction": "fast_montage",
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "render_program": "guided",
        "montage_audio": snapshot.montage_audio.model_dump(),
        "video_reuse_policy": "allow_repeat",
        "selected_media_ids": list(refs),
        "caption_style": "none",
        "target_duration_s": snapshot.duration_s,
        "rationale": story.story_json["rationale"],
        **copy_fields,
    }
    # Offer consent only after the actual native compiler and publication gates
    # can execute this same source-bound snapshot. No media or model I/O occurs.
    from app.pipeline.guided_story import GuidedStoryExecutionPlan
    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
    from app.services.creator_render_contract import build_render_contract, verify_phone_recipe
    from app.services.phone_rollout import validate_phone_pilot_recipe

    try:
        recipe = compile_phone_guided_plan(
            GuidedStoryExecutionPlan.model_validate(compiled), tuple(phone_bindings.values())
        )
        validate_phone_pilot_recipe(recipe)
        contract = build_render_contract(strategy, generation_id="camera-roll-preflight")
        verify_phone_recipe(
            contract,
            recipe,
            source_audio={key: value.original.has_audio for key, value in phone_bindings.items()},
        )
    except (UnsupportedPhonePlan, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return PreparedCollectionStory(
        story_id=story.id,
        story_revision=story.revision,
        collection_id=story.collection_id,
        collection_revision=story.collection_revision,
        snapshot=snapshot,
        strategy=strategy,
        compiled_digest=execution_digest(compiled),
        content_plan=content_plan,
        research=story_out(story).research,
        source_contexts=contexts,
        source_window_adjustments=source_window_adjustments,
    )


async def plan_selected_story(db, *, reference, thread_id, item_id, creator_id):
    """Build inert intents from a validated server selection; no model may rewrite it."""
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.kria.brief_binding import snapshot_media
    from app.kria.contracts import KriaToolIntent, KriaTurnPlan
    from app.kria.planner import PlannedKriaTurn
    from app.kria.strategy_policy import RefusedStrategy, check_strategy_for_runtime_v2
    from app.models import ContentPlan, MediaCollectionStory, Persona, PlanItem
    from app.services.creator_sessions import resolve_item_creator_context

    story = await db.get(MediaCollectionStory, UUID(reference["story_id"]), populate_existing=True)
    if (
        story is None
        or story.user_id != creator_id
        or story.revision != reference["story_revision"]
    ):
        raise HTTPException(409, "Chosen story changed; review it again")
    if any(
        value["type"] == "rejection" and value["story_revision"] == story.revision
        for value in story.events_json
    ):
        raise HTTPException(409, "This story was rejected; choose another")
    item = await db.get(PlanItem, item_id)
    plan = await db.get(ContentPlan, item.content_plan_id) if item else None
    if plan is None or plan.user_id != creator_id or item.current_job_id is not None:
        raise HTTPException(409, "Choose a new chat for this story")
    prepared = await prepare_story_snapshot(db, creator_id, story, item)
    persona = await db.get(Persona, plan.persona_id)
    manifest, _ = await resolve_item_creator_context(
        db, item, persona=persona, runs_guided_voiceover=False
    )
    strategy = CreativeStrategy.model_validate(prepared.strategy)
    checked = check_strategy_for_runtime_v2(manifest, strategy)
    if isinstance(checked, RefusedStrategy):
        raise HTTPException(422, checked.question)
    # A capability repair cannot silently change the selected recommendation.
    for key, expected in prepared.strategy.items():
        if getattr(checked.strategy, key) != getattr(strategy, key):
            raise HTTPException(
                422, "This story cannot preserve its timing and sound on this version of the app"
            )
    prepared.strategy = strategy.model_dump(mode="json", exclude_none=True)
    duration_label = f"{round(prepared.snapshot.duration_s, 1):g}"
    summary = f"{story.story_json['title']}. {duration_label} seconds; " + (
        "chosen source windows and original sound. No added text or music."
        if prepared.snapshot.montage_audio.preserve_source_audio
        else "chosen source windows, muted. No added text or music."
    )
    if content_plan := story.story_json.get("content_plan"):
        wording = [
            content_plan["opening_text"],
            *([content_plan["closing_text"]] if content_plan.get("closing_text") else []),
        ]
        summary = (
            f"{story.story_json['title']}. {duration_label} seconds. "
            f"{content_plan['angle']} "
            + (
                "Original sound. "
                if prepared.snapshot.montage_audio.preserve_source_audio
                else "Muted. "
            )
            + "Proposed on-screen wording (included only if you approve this draft): "
            + " / ".join(json.dumps(value, ensure_ascii=False) for value in wording)
            + ". No added music or narration."
        )
    if prepared.source_window_adjustments:
        summary += " Clip ends are shortened slightly to fit your iPhone originals."
    if prepared.source_contexts:
        summary += (
            " Whole clip; no timed highlight detected."
            if len(prepared.source_contexts) == 1
            else " Whole clips; no timed highlights detected."
        )
        if any(
            value.quality.get("overlapping_unusable_spans") for value in prepared.source_contexts
        ):
            summary += " Cached quality notes flag a source span for review."
    from app.config import settings
    from app.kria.brief import load_latest_brief

    brief_fields = {}
    if settings.brief_binding_for(creator_id):
        previous_brief = await load_latest_brief(db, thread_id)
        brief_fields = {
            "brief_updates": (selected_story_requirement(prepared),),
            "brief_route": "replan",
            "brief_expected_version": previous_brief.version if previous_brief else 0,
            "brief_clip_ids": tuple(ref.media_id for ref in prepared.snapshot.media),
            "brief_manifest": manifest,
        }
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="act",
            turn_value="review",
            intents=[
                KriaToolIntent(
                    intent_id="chosen-story",
                    tool_name="draft.apply_strategy",
                    tool_version=1,
                    arguments={"strategy": prepared.strategy, "summary": summary},
                ),
                KriaToolIntent(
                    intent_id="review-story",
                    tool_name="render.request",
                    tool_version=1,
                    arguments={},
                    depends_on=["chosen-story"],
                ),
            ],
        ),
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
        media_snapshot=snapshot_media(item),
        prepared_collection_story=prepared.model_dump(mode="json"),
        readiness_manifest=manifest,
        **brief_fields,
    )


def install_approved_story(db, *, item, owner, raw, strategy, dry_run=False):
    """Called only after the shared approval has been explicitly granted and claimed."""
    from datetime import UTC, datetime

    from app.models import MediaAsset, MediaAssetSource, MediaCollection, MediaCollectionStory
    from app.pipeline.guided_story import compile_proposal_execution_plan
    from app.schemas.edit_proposal import (
        ApprovedProposalSnapshot,
        EditProposal,
        canonical_media_digest,
        parse_edit_proposal,
    )

    prepared = PreparedCollectionStory.model_validate(raw)
    story = db.get(
        MediaCollectionStory,
        prepared.story_id,
        populate_existing=True,
        with_for_update={"read": True},
    )
    collection = db.get(
        MediaCollection,
        prepared.collection_id,
        populate_existing=True,
        with_for_update={"read": True},
    )
    if (
        story is None
        or collection is None
        or story.user_id != owner
        or collection.user_id != owner
        or story.revision != prepared.story_revision
        or collection.revision != prepared.collection_revision
    ):
        raise ValueError("Approved story or collection changed")
    if any(
        value["type"] == "rejection" and value["story_revision"] == story.revision
        for value in story.events_json
    ):
        raise ValueError("Approved story was rejected")
    if any(strategy.get(key) != value for key, value in prepared.strategy.items()):
        raise ValueError("Approved story strategy changed")
    if prepared.research != story_out(story).research:
        raise ValueError("Approved story research changed")
    if prepared.content_plan != _without_open_questions(story_out(story).content_plan):
        raise ValueError("Approved story content plan changed")
    if prepared.source_contexts != _source_contexts(story.moments_json):
        raise ValueError("Approved story context evidence changed")
    if (
        execution_digest(compile_proposal_execution_plan(prepared.snapshot))
        != prepared.compiled_digest
    ):
        raise ValueError("Prepared story compiler version changed")
    for value in story.moments_json:
        source = db.get(
            MediaAssetSource,
            UUID(value["source_id"]),
            populate_existing=True,
            with_for_update={"read": True},
        )
        asset = db.get(MediaAsset, UUID(value["asset_id"]), populate_existing=True)
        if (
            source is None
            or asset is None
            or asset.user_id != owner
            or asset.archived_at is not None
            or source.asset_id != asset.id
            or source.user_id != owner
            or source.status != "available"
            or source.object_generation != value["generation"]
        ):
            raise ValueError("Approved story source changed")
    attempt_id = f"collection-{story.id}-{story.revision}"
    if dry_run:
        return attempt_id
    previous = parse_edit_proposal(item.edit_proposal)
    if previous and previous.generation_attempt_id == attempt_id and previous.status == "approved":
        return attempt_id
    version = previous.proposal_version + 1 if previous else 1
    digest = canonical_media_digest(prepared.snapshot.media, prepared.snapshot.narration)
    approved = ApprovedProposalSnapshot(
        proposal_version=version,
        media_digest=digest,
        approved_at=datetime.now(UTC),
        snapshot=prepared.snapshot,
        approval_mode="user",
    )
    item.edit_proposal = EditProposal(
        proposal_version=version,
        generation_attempt_id=attempt_id,
        media_digest=digest,
        status="approved",
        draft=prepared.snapshot,
        last_approved=approved,
        approval_mode="user",
        planning_diagnostics={"prepared_collection_story": prepared.model_dump(mode="json")},
    ).model_dump(mode="json")
    return attempt_id


async def validate_prepared_story_for_approval(db, *, owner, item, raw):
    from sqlalchemy import select

    from app.models import MediaCollection, MediaCollectionStory

    prepared = PreparedCollectionStory.model_validate(raw)
    collection = await db.scalar(
        select(MediaCollection)
        .where(MediaCollection.id == prepared.collection_id, MediaCollection.user_id == owner)
        .with_for_update(read=True)
    )
    story = await db.scalar(
        select(MediaCollectionStory)
        .where(MediaCollectionStory.id == prepared.story_id, MediaCollectionStory.user_id == owner)
        .with_for_update(read=True)
    )
    if (
        collection is None
        or story is None
        or collection.revision != prepared.collection_revision
        or story.revision != prepared.story_revision
    ):
        raise HTTPException(409, "Chosen collection or story changed")
    if any(
        value["type"] == "rejection" and value["story_revision"] == story.revision
        for value in story.events_json
    ):
        raise HTTPException(409, "This story was rejected")
    current = await prepare_story_snapshot(db, owner, story, item)
    if (
        current.compiled_digest != prepared.compiled_digest
        or current.research != prepared.research
        or current.content_plan != prepared.content_plan
        or current.source_contexts != prepared.source_contexts
    ):
        raise HTTPException(409, "Chosen story source, wording or compiler changed")

"""Prepare one existing guided montage snapshot before offering render consent."""

import hashlib
import json
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MontageAudioPlan,
    StoryBeat,
)
from app.services import media_collections
from app.services.media_collection_stories import candidate_evidence


def execution_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PreparedCollectionStory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    story_id: UUID
    story_revision: int
    collection_id: UUID
    collection_revision: int
    snapshot: EditProposalSnapshot
    strategy: dict
    compiled_digest: str


async def prepare_story_snapshot(db, owner, story, item):
    from app.pipeline.guided_story import compile_proposal_execution_plan
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
    )
    current = {str(value.id): value for value in evidence.moments}
    if any(value["id"] not in current for value in story.moments_json):
        raise HTTPException(409, "Story source changed; refresh the collection")
    eligible = await media_collections.eligible_sources(
        db, owner, media_collections.members(collection)
    )
    assignments = {
        (value.get("library_asset_id"), value.get("library_source_id")): value
        for value in item.clip_assignments or []
        if isinstance(value, dict)
    }
    refs, cuts, beats = {}, [], []
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
        length = moment.end_s - moment.start_s
        cuts.append(
            FastMontageCut(
                cut_id=str(moment.id),
                media_id=media_id,
                source_start_s=moment.start_s,
                source_end_s=moment.end_s,
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
    snapshot = EditProposalSnapshot(
        direction="fast_montage",
        goal=story.story_json["rationale"],
        title=story.story_json["title"],
        text_policy="none",
        duration_s=sum(value.output_duration_s for value in cuts),
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
    )
    snapshot = refresh_snapshot_schedule(snapshot)
    compiled = compile_proposal_execution_plan(snapshot)
    if compiled.get("text_elements"):
        raise HTTPException(422, "This story unexpectedly adds unconfirmed on-screen text")
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
    }
    return PreparedCollectionStory(
        story_id=story.id,
        story_revision=story.revision,
        collection_id=story.collection_id,
        collection_revision=story.collection_revision,
        snapshot=snapshot,
        strategy=strategy,
        compiled_digest=execution_digest(compiled),
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
    summary = f"{story.story_json['title']}. {prepared.snapshot.duration_s:g} seconds; " + (
        "chosen source windows and original sound. No added text or music."
        if prepared.snapshot.montage_audio.preserve_source_audio
        else "chosen source windows, muted. No added text or music."
    )
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
    if current.compiled_digest != prepared.compiled_digest:
        raise HTTPException(409, "Chosen story source or compiler changed")

"""Grounded, reviewable candidates. All execution remains on the shared draft path."""

import asyncio
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException

from app.agents._model_client import default_client
from app.agents._runtime import TerminalError
from app.agents.camera_roll_stories import (
    CameraRollStoriesAgent,
    CameraRollStoriesInput,
    CameraRollStoriesOutput,
)
from app.agents.media_moment_relevance import MomentCandidate
from app.models import MediaCollectionStory
from app.pipeline.clip_cache import (
    MediaAssetFactKey,
    complete_media_asset_fact,
)
from app.schemas.media_collections import CollectionStoriesOut, CollectionStoryOut
from app.services import creator_editorial_taste, media_collections, media_moments
from app.services.media_collection_analysis import claim_collection_analysis, collection_run_context
from app.services.researched_text import ResearchUnavailable
from app.services.story_idea_research import (
    research_spec_identity,
    research_story_ideas,
    story_content_digest,
)


def event(kind, revision, **fields):
    return {"type": kind, "story_revision": revision, "at": datetime.now(UTC).isoformat(), **fields}


def story_out(row):
    plan = row.story_json.get("content_plan")
    details = dict(row.story_json)
    if research := details.get("research"):
        details["research"] = {
            **research,
            "review_current": research["reviewed_content_sha256"]
            == story_content_digest(
                row.story_json["title"], plan, [v["id"] for v in row.moments_json]
            ),
        }
    return CollectionStoryOut(
        id=row.id,
        collection_id=row.collection_id,
        collection_revision=row.collection_revision,
        revision=row.revision,
        moments=row.moments_json,
        duration_s=sum(value["end_s"] - value["start_s"] for value in row.moments_json),
        rejected=any(
            value["type"] == "rejection" and value["story_revision"] == row.revision
            for value in row.events_json
        ),
        on_screen_copy=(
            [plan["opening_text"], *([plan["closing_text"]] if plan.get("closing_text") else [])]
            if plan
            else []
        ),
        **details,
    )


async def candidate_evidence(db, owner, identifier, revision, query="", *, moment_ids=None):
    evidence = await media_moments.search_moments(
        db,
        owner,
        identifier,
        revision=revision,
        query=query,
        limit=100,
        all_candidates=True,
        moment_ids=moment_ids,
    )
    collection = await media_collections.owned_collection(db, owner, identifier, revision=revision)
    eligible = await media_collections.eligible_sources(
        db, owner, media_collections.members(collection)
    )
    supported = {
        source.id
        for asset, source in eligible.values()
        if asset.kind == "video" and source.role == "analysis_proxy"
    }
    supported_assets = {
        asset.id
        for asset, source in eligible.values()
        if asset.kind == "video" and source.role == "analysis_proxy"
    }
    # Only offer the hardware-verified adapter. Other catalogue sources remain
    # searchable, but cannot be advertised as executable phone stories.
    return evidence.model_copy(
        update={
            "moments": [value for value in evidence.moments if value.source_id in supported],
            "unindexed_asset_ids": [
                value for value in evidence.unindexed_asset_ids if value in supported_assets
            ],
        }
    )


def require_requested_coverage(evidence, body):
    if evidence.unindexed_asset_ids and not body.allow_partial_analysis:
        count = len(evidence.unindexed_asset_ids)
        subject = "video still needs" if count == 1 else "videos still need"
        raise HTTPException(
            409,
            f"{count} {subject} usable analysis. "
            "Wait for analysis or retry those uploads, then find ideas again.",
        )


async def generate_stories(db, owner, identifier, body):
    evidence = await candidate_evidence(db, owner, identifier, body.expected_revision, body.query)
    require_requested_coverage(evidence, body)
    candidates = [value for value in evidence.moments if 1 <= value.end_s - value.start_s <= 20]
    if not candidates:
        return CollectionStoriesOut(revision=body.expected_revision, stories=[], coverage=evidence)
    # Retrieve editorial examples against the actual outing, even when the
    # creator leaves the broad default query unchanged.
    taste_query = (body.query + " " + " ".join(value.text[:160] for value in candidates))[:2000]
    taste = await creator_editorial_taste.generation_context(db, owner, taste_query)
    input = CameraRollStoriesInput(
        query=body.query,
        editorial_taste_context=taste,
        candidates=[
            MomentCandidate(
                id=str(value.id),
                source=str(value.source_id),
                kind=value.kind,
                text=value.text[:800],
                quality_notes=json.dumps(value.evidence.get("quality") or {}, ensure_ascii=False)[
                    :600
                ],
                start_s=value.start_s,
                end_s=value.end_s,
            )
            for value in candidates
        ],
    )
    spec = CameraRollStoriesAgent.spec
    research_topic = getattr(body, "research_topic", None)
    digest = hashlib.sha256(
        json.dumps(
            [
                [spec.name, spec.model, spec.prompt_version],
                input.model_dump(),
                [value.evidence.get("index_spec") for value in candidates],
                body.expected_revision,
                body.allow_partial_analysis,
                [research_topic, research_spec_identity()] if research_topic else None,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    first = candidates[0]
    claim = await asyncio.to_thread(
        claim_collection_analysis,
        MediaAssetFactKey(
            str(owner), digest, spec.name, spec.model, spec.prompt_version, "stories-v1"
        ),
        cache_only=body.cache_only,
        canonical_asset_id=str(first.asset_id),
        canonical_source_id=str(first.source_id),
        lease_seconds=150 if research_topic else 90,
    )
    if claim.status == "ready":
        ideas = CameraRollStoriesOutput.model_validate(claim.result_json)
    elif claim.status == "claimed":
        try:
            ideas = await asyncio.to_thread(
                research_story_ideas
                if research_topic
                else CameraRollStoriesAgent(default_client()).run,
                input,
                *([research_topic] if research_topic else []),
                ctx=collection_run_context(
                    creator_id=str(owner),
                    request_id=f"collection-stories:{digest}:{claim.attempt_token}",
                    deadline_monotonic=time.monotonic() + (115 if research_topic else 25),
                    estimated_max_cost_usd=0.05,
                ),
            )
            if not await asyncio.to_thread(complete_media_asset_fact, claim, ideas.model_dump()):
                raise HTTPException(409, "Story evidence changed; retry")
        except (TerminalError, ResearchUnavailable) as exc:
            await asyncio.to_thread(complete_media_asset_fact, claim, None, failed=True)
            raise HTTPException(503, "Ideas could not be completed. Please try again.") from exc
        except Exception:
            await asyncio.to_thread(complete_media_asset_fact, claim, None, failed=True)
            raise
    else:
        raise HTTPException(
            409 if claim.status == "busy" else 503, "Stories are pending; retry shortly"
        )
    await media_collections.lock_library_owner(db, owner)
    # Serialize the final check with pause, Forget, profile changes and feedback.
    # Never hold this lock across a provider call.
    await creator_editorial_taste.lock_owner(db, owner)
    if taste != await creator_editorial_taste.generation_context(db, owner, taste_query):
        raise HTTPException(409, "Personalization changed; request ideas again")
    fresh = await candidate_evidence(db, owner, identifier, body.expected_revision, body.query)
    require_requested_coverage(fresh, body)
    by_id = {str(value.id): value for value in fresh.moments}
    accepted = []
    seen = set()
    for index, idea in enumerate(ideas.stories):
        ids = idea.moment_ids
        if len(set(ids)) != len(ids) or any(value not in by_id for value in ids):
            continue
        moments = [by_id[value] for value in ids]
        duration = sum(value.end_s - value.start_s for value in moments)
        identity = frozenset(ids)
        if not 1 <= duration <= 60 or identity in seen:
            continue
        seen.add(identity)
        story_id = uuid.uuid5(identifier, f"{digest}:{index}")
        row = await db.get(MediaCollectionStory, story_id)
        if row is None:
            row = MediaCollectionStory(
                id=story_id,
                collection_id=identifier,
                user_id=owner,
                collection_revision=body.expected_revision,
                revision=1,
                story_json=idea.model_dump(exclude={"moment_ids"}),
                moments_json=[value.model_dump(mode="json") for value in moments],
                events_json=[
                    event(
                        "offered", 1, editorial_taste=creator_editorial_taste.context_receipt(taste)
                    )
                ],
            )
            db.add(row)
        accepted.append(story_out(row))
    await db.commit()
    return CollectionStoriesOut(revision=body.expected_revision, stories=accepted, coverage=fresh)


async def owned_story(db, owner, identifier, story_id, body):
    await media_collections.lock_library_owner(db, owner)
    await media_collections.owned_collection(db, owner, identifier, revision=body.expected_revision)
    row = await db.get(MediaCollectionStory, story_id, populate_existing=True)
    if row is None or row.user_id != owner or row.collection_id != identifier:
        raise HTTPException(404, "Story not found")
    if row.collection_revision != body.expected_revision:
        raise HTTPException(409, "Story belongs to an older collection")
    duplicate = next(
        (
            value
            for value in row.events_json
            if value.get("client_event_id") == body.client_event_id
        ),
        None,
    )
    if (
        duplicate is not None
        and duplicate["story_revision"] != body.expected_story_revision
        and duplicate["type"] != "correction"
    ):
        raise HTTPException(409, "Review identity reused for another story revision")
    if duplicate is None and row.revision != body.expected_story_revision:
        raise HTTPException(409, "Story changed; refresh before correcting it")
    return row, duplicate


async def correct_story(db, owner, identifier, story_id, body):
    row, duplicate = await owned_story(db, owner, identifier, story_id, body)
    digest = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    if duplicate:
        if duplicate.get("request_digest") != digest:
            raise HTTPException(409, "Correction identity reused")
        return story_out(row)
    if len(row.events_json) >= 200:
        raise HTTPException(422, "Start a new story after 200 review actions")
    moments = list(row.moments_json)
    if body.audio_policy is not None:
        row.story_json = {**row.story_json, "audio_policy": body.audio_policy}
    elif body.content_plan is not None:
        if len(body.content_plan.beat_reasons) != len(moments):
            raise HTTPException(422, "Keep one reason for each story moment")
        row.story_json = {
            **row.story_json,
            "content_plan": body.content_plan.model_dump(mode="json"),
        }
    elif body.moment_order is not None:
        known = {value["id"]: value for value in moments}
        if any(str(value) not in known for value in body.moment_order):
            raise HTTPException(422, "Reordering can use only this story's current moments")
        if plan := row.story_json.get("content_plan"):
            reasons = dict(
                zip((value["id"] for value in moments), plan["beat_reasons"], strict=True)
            )
            row.story_json = {
                **row.story_json,
                "content_plan": {
                    **plan,
                    "beat_reasons": [reasons[str(value)] for value in body.moment_order],
                },
            }
        moments = [known[str(value)] for value in body.moment_order]
    else:
        fresh = await candidate_evidence(
            db, owner, identifier, body.expected_revision, moment_ids=[body.moment_id]
        )
        replacement = next((value for value in fresh.moments if value.id == body.moment_id), None)
        if replacement is None or not 1 <= replacement.end_s - replacement.start_s <= 20:
            raise HTTPException(
                422, "Choose a current complete interval between one and twenty seconds"
            )
        if body.replace_index >= len(moments):
            raise HTTPException(422, "That story moment does not exist")
        if any(
            value["id"] == str(body.moment_id)
            for index, value in enumerate(moments)
            if index != body.replace_index
        ):
            raise HTTPException(422, "A story cannot repeat the same interval")
        moments[body.replace_index] = replacement.model_dump(mode="json")
        if plan := row.story_json.get("content_plan"):
            reasons = list(plan["beat_reasons"])
            reasons[body.replace_index] = (
                "Replacement chosen by you; review how it supports this idea."
            )
            row.story_json = {**row.story_json, "content_plan": {**plan, "beat_reasons": reasons}}
    if sum(value["end_s"] - value["start_s"] for value in moments) > 60:
        raise HTTPException(422, "This story would exceed sixty seconds")
    row.moments_json = moments
    row.revision += 1
    row.events_json = [
        *row.events_json,
        event(
            "correction",
            row.revision,
            client_event_id=body.client_event_id,
            request_digest=digest,
            replace_index=body.replace_index,
        ),
    ]
    await db.commit()
    return story_out(row)


async def expose_story(db, owner, identifier, story_id, body):
    row, duplicate = await owned_story(db, owner, identifier, story_id, body)
    if duplicate and duplicate["type"] != "exposure":
        raise HTTPException(409, "Review identity reused")
    if not duplicate:
        if len(row.events_json) >= 200:
            raise HTTPException(422, "Start a new story after 200 review actions")
        row.events_json = [
            *row.events_json,
            event("exposure", row.revision, client_event_id=body.client_event_id),
        ]
        await db.commit()
    return story_out(row)


async def reject_story(db, owner, identifier, story_id, body):
    row, duplicate = await owned_story(db, owner, identifier, story_id, body)
    if duplicate and duplicate["type"] != "rejection":
        raise HTTPException(409, "Review identity reused")
    if not duplicate:
        if len(row.events_json) >= 200:
            raise HTTPException(422, "Start a new story after 200 review actions")
        row.events_json = [
            *row.events_json,
            event("rejection", row.revision, client_event_id=body.client_event_id),
        ]
        await db.commit()
    return story_out(row)


async def select_story(db, owner, identifier, story_id, body):
    from app.config import settings
    from app.kria.api_schemas import SubmitTurnBody, TurnAccepted
    from app.kria.runtime import submit_turn
    from app.models import CreationThread, CreatorAgentTurn, PlanItem
    from app.services.media_collection_preparation import prepare_story_snapshot

    if not settings.kria_runtime_v2_enabled:
        raise HTTPException(
            422, "The verified draft flow is unavailable; keep this story for later"
        )
    row, duplicate = await owned_story(db, owner, identifier, story_id, body)
    if duplicate:
        if duplicate["type"] != "selection" or duplicate.get("thread_id") != str(body.thread_id):
            raise HTTPException(409, "Selection identity reused")
        turn = await db.get(CreatorAgentTurn, uuid.UUID(duplicate["turn_id"]))
        thread = await db.get(CreationThread, body.thread_id)
        return TurnAccepted(
            turn_id=str(turn.id), thread_revision=thread.revision, status=turn.status
        ), turn.status == "pending"
    if story_out(row).rejected:
        raise HTTPException(409, "Choose another story; this version was rejected")
    thread = await db.get(CreationThread, body.thread_id)
    if thread is None or thread.creator_id != owner:
        raise HTTPException(404, "Chat not found")
    if thread.runtime_version != 2 or thread.active_job_id is not None:
        raise HTTPException(422, "Choose a new chat with the verified draft flow")
    item = await db.get(PlanItem, thread.active_plan_item_id)
    if item is None or item.current_job_id is not None:
        raise HTTPException(409, "Choose a new chat for this story")
    await prepare_story_snapshot(db, owner, row, item)
    # A chosen idea becomes a named ordinary project, retaining its creative
    # reason after the collection screen is dismissed. This grants no render consent.
    thread.title = row.story_json["title"]
    item.idea = row.story_json["title"]
    plan = row.story_json.get("content_plan")
    item.rationale = plan["why_it_works"] if plan else row.story_json["rationale"]
    if plan:
        item.filming_suggestion = plan["angle"]
    if len(row.events_json) >= 200:
        raise HTTPException(422, "Start a new story after 200 review actions")
    # submit_turn commits the selection event and durable work in one transaction;
    # the ordinary worker/reconciler owns publication and the expiring approval.
    accepted, publish = await submit_turn(
        db,
        thread_id=body.thread_id,
        creator_id=owner,
        body=SubmitTurnBody(
            message=(
                f"Prepare chosen story {row.id} revision {row.revision}: {row.story_json['title']}"
            ),
            expected_thread_revision=body.expected_thread_revision,
            client_event_id=body.client_event_id,
        ),
        _camera_roll_story={"story_id": str(row.id), "story_revision": row.revision},
    )
    # The first commit created the turn; this append is telemetry only. Losing its
    # acknowledgement cannot change the source event's immutable story binding.
    row = await db.get(MediaCollectionStory, story_id, populate_existing=True)
    row.events_json = [
        *row.events_json,
        event(
            "selection",
            row.revision,
            client_event_id=body.client_event_id,
            thread_id=str(body.thread_id),
            turn_id=accepted.turn_id,
        ),
    ]
    await db.commit()
    return accepted, publish

"""Recommendation selection must cross the existing expiring approval boundary."""

import asyncio
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    MediaAsset,
    MediaAssetAnalysis,
    MediaAssetSource,
    MediaCollectionStory,
    MediaSourceMoment,
    Persona,
    PlanItem,
)
from app.tasks.kria_runtime import _claim_approval_dispatch, run_kria_turn
from tests.integration.test_media_collections import collection_world, selection  # noqa: F401, F811

_ACCEPTED_CAPTION = "A crossing  and an arrival.\n\nKeep this spacing."
_RECOVERABLE_CASES = {"held", "empty-caption", "whitespace-caption", "lost-telemetry"}


@pytest.mark.parametrize(
    "with_content_plan,fractional_end,change_before_approval,caption,readiness_enabled,recovery_case",
    [
        pytest.param(
            content,
            fractional,
            changed,
            _ACCEPTED_CAPTION if content else None,
            True,
            None,
            id=(
                f"{'content' if content else 'legacy'}-"
                f"{'fractional' if fractional else 'whole'}-"
                f"{'stale-collection' if changed else 'approve'}"
            ),
        )
        for content in (False, True)
        for fractional in (False, True)
        for changed in (False, True)
    ]
    + [pytest.param(True, False, False, "", True, None, id="explicit-empty-caption")]
    + [
        pytest.param(True, False, changed, _ACCEPTED_CAPTION, True, None, id=f"changed-{changed}")
        for changed in (
            "story",
            "source",
            "approval",
            "prepared-window",
            "prepared-caption",
            "prepared-missing",
        )
    ]
    + [
        pytest.param(False, True, False, None, False, None, id="legacy-readiness-off"),
        pytest.param(True, False, False, "", False, None, id="empty-caption-readiness-off"),
        pytest.param(
            "whole-source-context",
            False,
            False,
            _ACCEPTED_CAPTION,
            True,
            None,
            id="whole-source-context",
        ),
        pytest.param(
            "whole-source-context",
            False,
            False,
            "",
            True,
            None,
            id="whole-source-context-empty-caption",
        ),
    ]
    + [
        pytest.param(
            "whole-source-context",
            False,
            changed,
            _ACCEPTED_CAPTION,
            True,
            None,
            id=f"whole-source-{changed}",
        )
        for changed in ("prepared-context", "prepared-context-missing", "story-context")
    ]
    + [
        pytest.param(
            "retained:" + condition,
            False,
            False,
            _ACCEPTED_CAPTION,
            True,
            "held" if condition == "held" else None,
            id="retained-spec-" + condition,
        )
        for condition in (
            "valid",
            "held",
            "generation",
            "hash",
            "path",
            "window",
            "frozen-window",
            "text",
            "quality",
            "frozen-quality",
            "missing-fact",
            "changed-fact",
            "wrong-fact-identity",
            "unknown-spec",
            "future-spec",
            "foreign-moment",
            "stale-collection",
            "nonready-fact",
        )
    ]
    + [
        pytest.param(
            "retained:valid",
            False,
            changed,
            _ACCEPTED_CAPTION,
            True,
            None,
            id="retained-spec-after-approval-" + changed,
        )
        for changed in ("source", "story", "prepared-window", "prepared-caption", "approval")
    ]
    # Seed the historical hold, then use the real HTTP/worker/approval chain.
    # This is recovery of an existing selection, not success in a new project.
    + [
        pytest.param(True, False, False, value, True, case, id=f"recover-{case}")
        for case, value in (
            ("held", _ACCEPTED_CAPTION),
            ("empty-caption", ""),
            ("whitespace-caption", "   \n "),
            ("changed-thread", _ACCEPTED_CAPTION),
            ("changed-media", _ACCEPTED_CAPTION),
            ("changed-story", _ACCEPTED_CAPTION),
            ("changed-collection", _ACCEPTED_CAPTION),
            ("changed-generation", _ACCEPTED_CAPTION),
            ("pending-job", _ACCEPTED_CAPTION),
            ("running-job", _ACCEPTED_CAPTION),
            ("completed-job", _ACCEPTED_CAPTION),
            ("prepared", _ACCEPTED_CAPTION),
            ("approved", _ACCEPTED_CAPTION),
            ("other-refusal", _ACCEPTED_CAPTION),
            ("cancelled", _ACCEPTED_CAPTION),
            ("lost-telemetry", _ACCEPTED_CAPTION),
            ("reused-recovery-key", _ACCEPTED_CAPTION),
            ("flags-off", _ACCEPTED_CAPTION),
        )
    ],
)
async def test_selection_prepares_common_draft_then_explicit_approval_freezes_execution(
    collection_world,  # noqa: F811
    monkeypatch,
    change_before_approval,
    fractional_end,
    with_content_plan,
    caption,
    readiness_enabled,
    recovery_case,
):
    client, assets, sources, owner, _app = collection_world
    source_context = with_content_plan == "whole-source-context"
    retained_condition = (
        with_content_plan.split(":", 1)[1]
        if isinstance(with_content_plan, str) and with_content_plan.startswith("retained:")
        else None
    )
    if retained_condition:
        monkeypatch.setattr(settings, "whisper_backend", "openai-api")
        from dataclasses import replace

        from app.agents.clip_metadata import ClipMetadataAgent

        current_metadata_spec = ClipMetadataAgent.spec
        monkeypatch.setattr(
            ClipMetadataAgent, "spec", replace(current_metadata_spec, prompt_version="2026-10-13.1")
        )
    if recovery_case:
        from app.routes import creation_threads

        _app.include_router(creation_threads.router, prefix="/creation-threads")
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", readiness_enabled)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", readiness_enabled)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", readiness_enabled)
    monkeypatch.setattr("app.tasks.kria_runtime.run_kria_turn.delay", lambda *args: None)
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "kria_runtime_v2_phone_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        ["basicComposition", "local1080Export", "audioMix"],
    )
    proof_input = os.environ.get("KRIA_CAMERA_ROLL_PROOF_INPUT")
    assignments = []
    metadata_by_path = {}
    from types import SimpleNamespace

    from app import storage
    from app.services import media_library_analysis as facts
    from tests.services.test_phone_sources import receipt

    async with AsyncSessionLocal() as db:
        for index in range(2):
            previous_source = await db.get(MediaAssetSource, uuid.UUID(sources[index]))
            asset = await db.get(MediaAsset, uuid.UUID(assets[index]))
            duration = 8.166667 if source_context and index == 0 else 180
            asset.metadata_json = {**asset.metadata_json, "duration_s": duration}
            original_hash, original_bytes = "a" * 64, 1000
            uploaded_hash, uploaded_bytes = (
                previous_source.uploaded_sha256,
                previous_source.byte_count,
            )
            if source_context:
                # Reusable facts are keyed by verified bytes, not the source row.
                # Different replayed content needs a distinct synthetic identity.
                uploaded_hash = hashlib.sha256(f"context-source-{asset.id}".encode()).hexdigest()
            if proof_input:
                file = Path(proof_input) / f"source-{index}.mp4"
                with file.open("rb") as stream:
                    original_hash = hashlib.file_digest(stream, "sha256").hexdigest()
                original_bytes = file.stat().st_size
                uploaded_hash, uploaded_bytes = original_hash, original_bytes
            source = MediaAssetSource(
                user_id=owner.id,
                asset_id=asset.id,
                role="analysis_proxy",
                object_path=f"users/{owner.id}/media-library/analysis-proxy-{asset.id}.mp4",
                object_generation="42",
                uploaded_sha256=uploaded_hash,
                byte_count=uploaded_bytes,
                content_type="video/mp4",
                status="available",
            )
            db.add(source)
            await db.flush()
            analyses = (
                await db.scalars(
                    select(MediaAssetAnalysis).where(
                        MediaAssetAnalysis.source_id == previous_source.id
                    )
                )
            ).all()
            ref = facts._Source(
                str(asset.id),
                str(source.id),
                str(owner.id),
                "video",
                source.role,
                source.object_path,
                source.object_generation,
                source.uploaded_sha256,
                source.status,
            )
            for analysis in analyses:
                import copy

                result = copy.deepcopy(analysis.result_json)
                if source_context and index == 0:
                    result = (
                        {"words": []}
                        if analysis.analyzer == "nova.media.transcript"
                        else {
                            "duration_s": duration,
                            "description": "Synthetic red test source.",
                            "understanding": {"notable_moments": []},
                            "best_moments": [],
                            "unusable_spans": [
                                {
                                    "start_s": 1,
                                    "end_s": 2,
                                    "reason": "Synthetic cached quality note",
                                }
                            ],
                        }
                    )
                elif source_context and analysis.analyzer != "nova.media.transcript":
                    for moment in result.get("understanding", {}).get("notable_moments", []):
                        if "ferry" in moment.get("description", "").lower():
                            moment["description"] = "Synthetic blue test source."
                key = facts._key(
                    ref, "transcript" if analysis.analyzer == "nova.media.transcript" else None
                )
                db.add(
                    MediaAssetAnalysis(
                        user_id=owner.id,
                        asset_id=asset.id,
                        source_id=source.id,
                        analyzer=key.analyzer,
                        model=key.model,
                        prompt_version=key.prompt_version,
                        schema_version=key.schema_version,
                        source_identity=key.source_identity,
                        status="ready",
                        result_json=result,
                    )
                )
            sources[index] = str(source.id)
            record = receipt(f"analysis-proxy-clip-{index}")
            record.update(
                gcs_path=source.object_path,
                storage_generation="42",
                duration_s=duration,
                library_asset_id=assets[index],
                library_source_id=sources[index],
            )
            proxy = record["upload_contract"]["proxy"]
            proxy["duration_s"] = duration
            proxy["original"].update(
                duration_s=duration,
                sha256=original_hash,
                byte_count=original_bytes,
                width=640,
                height=360,
            )
            assignments.append(record)
            metadata_by_path[source.object_path] = SimpleNamespace(
                generation="42", size=source.byte_count
            )
        await db.commit()
    monkeypatch.setattr(storage, "object_metadata", lambda path: metadata_by_path[path])
    path = f"/me/media-collections/{uuid.uuid4()}"
    collection_id = uuid.UUID(path.rsplit("/", 1)[1])
    selected_members = selection(assets, sources)
    additional_context = None
    if recovery_case == "held" and not retained_condition:
        # An existing partial idea may predate another member's completed facts.
        # Enrich that member during index refresh without changing membership or
        # any of the two accepted source windows.
        async with AsyncSessionLocal() as db:
            extra_asset = MediaAsset(
                user_id=owner.id,
                kind="video",
                status="ready",
                metadata_json={"duration_s": 8.166667, "filename": "extra-synthetic.mp4"},
            )
            db.add(extra_asset)
            await db.flush()
            extra_source = MediaAssetSource(
                user_id=owner.id,
                asset_id=extra_asset.id,
                role="analysis_proxy",
                object_path=f"users/{owner.id}/extra-{extra_asset.id}.mp4",
                object_generation="42",
                uploaded_sha256=hashlib.sha256(str(extra_asset.id).encode()).hexdigest(),
                byte_count=1234,
                content_type="video/mp4",
                status="available",
            )
            db.add(extra_source)
            await db.flush()
            additional_context = (extra_asset.id, extra_source.id)
            selected_members["members"].append(
                {"asset_id": str(extra_asset.id), "source_id": str(extra_source.id)}
            )
            metadata_by_path[extra_source.object_path] = SimpleNamespace(generation="42", size=1234)
            await db.commit()
    await client.put(path, json=selected_members)
    await client.post(f"{path}/index", json={"expected_revision": 1})
    moments = (
        await client.get(
            f"{path}/moments",
            params={"query": "synthetic" if source_context else "ferry", "expected_revision": 1},
        )
    ).json()["moments"]
    if source_context:
        assert len(moments) == 2, moments
        moments.sort(key=lambda value: value.get("evidence_scope") == "whole_source_context")
        assert moments[-1]["source_id"] == sources[0]
        assert (moments[-1]["start_s"], moments[-1]["end_s"]) == (0, 8.166667)
        assert moments[-1]["evidence_scope"] == "whole_source_context"
    if fractional_end:
        async with AsyncSessionLocal() as db:
            moment = await db.get(MediaSourceMoment, uuid.UUID(moments[0]["id"]))
            moment.end_s -= 0.02
            moments[0]["end_s"] = moment.end_s
            await db.commit()
    async with AsyncSessionLocal() as db:
        persona = Persona(
            user_id=owner.id,
            persona_status="ready",
            persona={"tone": "direct", "content_pillars": []},
        )
        db.add(persona)
        await db.flush()
        plan = ContentPlan(user_id=owner.id, persona_id=persona.id)
        db.add(plan)
        await db.flush()
        item = PlanItem(
            content_plan_id=plan.id,
            position=1,
            idea="Outing",
            edit_format="montage",
            audio_mode="original",
            clip_gcs_paths=[record["gcs_path"] for record in assignments],
            clip_assignments=assignments,
        )
        db.add(item)
        await db.flush()
        session = CreatorAgentSession(creator_id=owner.id, plan_item_id=item.id)
        db.add(session)
        await db.flush()
        thread = CreationThread(
            creator_id=owner.id,
            runtime_version=2,
            content_plan_id=plan.id,
            active_plan_item_id=item.id,
            active_creator_agent_session_id=session.id,
            revision=0,
            state={"media_count": 2, "media": [{"media_id": "clip-0"}, {"media_id": "clip-1"}]},
        )
        db.add(thread)
        await db.flush()
        story = MediaCollectionStory(
            id=uuid.uuid4(),
            collection_id=collection_id,
            user_id=owner.id,
            collection_revision=1,
            revision=1,
            story_json={
                "title": "Boat journey",
                "hook": "Crossing",
                "payoff": "Arrival",
                "rationale": "A journey",
                **(
                    {
                        "content_plan": {
                            "version": 1,
                            "format": "text_story",
                            "audience": "Weekend explorers",
                            "angle": "A quiet crossing becomes an arrival",
                            "why_it_works": "The last view answers the opening question.",
                            "opening_text": "Where does this crossing lead?",
                            "closing_text": "Worth the trip?",
                            "caption": caption,
                            "beat_reasons": ["Establish the journey" for _ in moments],
                        }
                    }
                    if with_content_plan
                    else {}
                ),
            },
            moments_json=moments,
            events_json=[],
        )
        if with_content_plan:
            from app.schemas.research import StoryResearch
            from app.services.story_idea_research import story_content_digest

            story.story_json["research"] = StoryResearch(
                topic="Ferry terminal history",
                evidence=dict(
                    facts=[
                        dict(
                            id="fact-0",
                            text="Recorded terminal history.",
                            sources=[
                                dict(
                                    title="Recorded archive", url="https://si.edu/recorded-fixture"
                                )
                            ],
                        )
                    ],
                    retrieved_at="2026-10-21T00:00:00Z",
                    search_entry_html="<div>Recorded attribution</div>",
                ),
                reviewed_content_sha256=story_content_digest(
                    story.story_json["title"],
                    story.story_json["content_plan"],
                    [value["id"] for value in moments],
                ),
            ).model_dump(mode="json")
        db.add(story)
        await db.commit()
        story_id, thread_id, item_id = story.id, thread.id, item.id
    if retained_condition:
        import copy

        from app.services import media_moments

        monkeypatch.setattr(ClipMetadataAgent, "spec", current_metadata_spec)
        current_search = await client.get(
            f"{path}/moments", params={"query": "ferry", "expected_revision": 1}
        )
        assert current_search.status_code == 200 and current_search.json()["moments"] == []
        assert all(
            m["evidence"]["index_spec"] != media_moments._index_spec("video") for m in moments
        )
        async with AsyncSessionLocal() as db:
            moment = await db.get(MediaSourceMoment, uuid.UUID(moments[0]["id"]))
            source = await db.get(MediaAssetSource, moment.source_id)
            stored = await db.get(MediaCollectionStory, story_id)
            if retained_condition in {"generation", "hash", "path"}:
                if retained_condition == "generation":
                    metadata_by_path[source.object_path] = SimpleNamespace(
                        generation="changed", size=source.byte_count
                    )
                elif retained_condition == "hash":
                    moment.evidence_json = {**moment.evidence_json, "uploaded_sha256": "b" * 64}
                    frozen = copy.deepcopy(stored.moments_json)
                    frozen[0]["evidence"]["uploaded_sha256"] = "b" * 64
                    stored.moments_json = frozen
                else:
                    # Verified source rows are immutable in PostgreSQL. Change
                    # the mutable attachment binding, not that invariant.
                    bound = await db.get(PlanItem, item_id)
                    attached = copy.deepcopy(bound.clip_assignments)
                    attached[0]["gcs_path"] += ".changed"
                    bound.clip_assignments = attached
            elif retained_condition in {"window", "frozen-window"}:
                moment.end_s -= 0.1
                if retained_condition == "frozen-window":
                    frozen = copy.deepcopy(stored.moments_json)
                    frozen[0]["end_s"] = moment.end_s
                    stored.moments_json = frozen
            elif retained_condition == "text":
                moment.text = "Unreviewed replacement observation"
            elif retained_condition in {"quality", "frozen-quality"}:
                e = copy.deepcopy(moment.evidence_json)
                e["quality"]["source_visibility"] = "Changed quality"
                moment.evidence_json = e
                if retained_condition == "frozen-quality":
                    frozen = copy.deepcopy(stored.moments_json)
                    frozen[0]["evidence"]["quality"] = e["quality"]
                    stored.moments_json = frozen
            elif retained_condition in {
                "missing-fact",
                "changed-fact",
                "wrong-fact-identity",
                "nonready-fact",
            }:
                fact = await db.scalar(
                    select(MediaAssetAnalysis).where(
                        MediaAssetAnalysis.source_id == source.id,
                        MediaAssetAnalysis.analyzer == "nova.video.clip_metadata",
                        MediaAssetAnalysis.prompt_version == "2026-10-13.1",
                    )
                )
                if retained_condition == "missing-fact":
                    await db.delete(fact)
                elif retained_condition == "changed-fact":
                    fact.result_json = {**fact.result_json, "description": "Changed retained facts"}
                elif retained_condition == "nonready-fact":
                    fact.status = "failed"
                else:
                    fact.source_identity += "/wrong-source"
            elif retained_condition == "future-spec":
                monkeypatch.setattr(
                    ClipMetadataAgent,
                    "spec",
                    replace(current_metadata_spec, prompt_version="2099-01-01.1"),
                )
            elif retained_condition == "unknown-spec":
                moment.evidence_json = {**moment.evidence_json, "index_spec": "f" * 64}
                frozen = copy.deepcopy(stored.moments_json)
                frozen[0]["evidence"]["index_spec"] = "f" * 64
                stored.moments_json = frozen
            elif retained_condition == "foreign-moment":
                frozen = copy.deepcopy(stored.moments_json)
                frozen[0]["source_id"] = sources[2]
                stored.moments_json = frozen
            elif retained_condition == "stale-collection":
                from app.models import MediaCollection

                (await db.get(MediaCollection, collection_id)).revision += 1
            await db.commit()

    # Preserve the native attachment event identities so choosing again exercises
    # the actual repeat-attachment HTTP boundary before selection recovery.
    initial_revision = 0
    if recovery_case:
        from app.kria.runtime import _append_event

        async with AsyncSessionLocal() as db:
            thread = await db.get(CreationThread, thread_id)
            for index, assignment in enumerate(assignments):
                media_id = assignment["media_id"]
                await _append_event(
                    db,
                    thread,
                    role="user",
                    event_type="media_attached",
                    content=None,
                    client_event_id=f"ios-library-{media_id}",
                    payload={
                        "media": [{"media_id": media_id}],
                        "library_selections": [
                            {
                                "media_id": media_id,
                                "asset_id": assets[index],
                                "source_id": sources[index],
                            }
                        ],
                    },
                )
            initial_revision = thread.revision
            await db.commit()
    selection_body = {
        "expected_revision": 1,
        "expected_story_revision": 1,
        "client_event_id": "selection-1",
        "thread_id": str(thread_id),
        "expected_thread_revision": initial_revision,
    }
    selected = await client.post(
        f"{path}/stories/{story_id}/select",
        json=selection_body,
    )
    if retained_condition not in {None, "valid", "held"}:
        assert selected.status_code == 409, selected.text
        async with AsyncSessionLocal() as db:
            assert (
                await db.scalar(
                    select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread_id)
                )
                is None
            )
            assert (await db.get(PlanItem, item_id)).current_job_id is None
        return
    assert selected.status_code == 200, selected.text
    accepted = selected.json()
    if recovery_case:
        from app.kria.approval_readiness import digest
        from app.kria.brief_binding import media_identity, snapshot_media
        from app.kria.runtime import _append_event

        original_turn_id = accepted["turn_id"]
        if recovery_case in {"prepared", "approved"}:
            assert (await asyncio.to_thread(run_kria_turn.run, original_turn_id))[
                "status"
            ] == "awaiting_approval"
            if recovery_case == "approved":
                async with AsyncSessionLocal() as db:
                    approval = await db.scalar(
                        select(CreatorAgentApproval).where(
                            CreatorAgentApproval.thread_id == thread_id
                        )
                    )
                    thread = await db.get(CreationThread, thread_id)
                    await decide_approval(
                        db,
                        thread_id=thread_id,
                        creator_id=owner.id,
                        approval_id=approval.id,
                        decision="approve",
                        body=ApprovalDecisionBody(
                            expected_thread_revision=thread.revision,
                            expected_draft_revision=approval.draft_revision,
                            expected_approval_fingerprint=approval_fingerprint(approval),
                        ),
                    )
        else:
            # Historical, saved incident shape. No responsible planner/checker is
            # mocked: the successor below runs today's real preparation chain.
            async with AsyncSessionLocal() as db:
                turn = await db.get(CreatorAgentTurn, uuid.UUID(original_turn_id))
                thread = await db.get(CreationThread, thread_id)
                item = await db.get(PlanItem, item_id)
                null_digest = hashlib.sha256(b"null").hexdigest()
                held = await _append_event(
                    db,
                    thread,
                    role="assistant",
                    event_type="assistant_response",
                    content="I held this first draft back.",
                    payload={
                        "turn_id": str(turn.id),
                        "brief_coverage": {
                            "stage": "draft",
                            "reason": "approval_requirements_not_ready",
                            "approval_readiness": {
                                "ready": False,
                                "brief_version": 0,
                                "requirements": [],
                                "brief_digest": null_digest,
                                "prepared_evidence_digest": null_digest,
                                "media_digest": digest(media_identity(snapshot_media(item))),
                                "policy_problem": (
                                    "The renderer cannot make this effect."
                                    if recovery_case == "other-refusal"
                                    else "I need to establish the requested changes "
                                    "before offering approval."
                                ),
                            },
                        },
                    },
                )
                turn.observed_event_id = held.id
                turn.status = "cancelled" if recovery_case == "cancelled" else "completed"
                turn.completed_at = datetime.now(UTC)
                if recovery_case == "lost-telemetry":
                    (await db.get(MediaCollectionStory, story_id)).events_json = []
                await db.commit()
        # Native saved-card access reads the retained row, not the model cache.
        # Prompt/cache/index changes must not be needed to reach this hold.
        saved = await client.get(
            f"{path}/stories",
            params={"expected_revision": 1, "story_ids": str(story_id)},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["stories"][0]["id"] == str(story_id)
        assert saved.json()["stories"][0]["moments"] == moments
        assert saved.json()["stories"][0]["content_plan"]["caption"] == caption
        # Same attachment identities used by SavedMediaReuse: no new revision,
        # no source mutation. The route returns the current project each time.
        async with AsyncSessionLocal() as db:
            retry_revision = (await db.get(CreationThread, thread_id)).revision
        for index, assignment in enumerate(assignments):
            media_id = assignment["media_id"]
            attached = await client.post(
                f"/creation-threads/{thread_id}/media",
                json={
                    "expected_revision": retry_revision,
                    "client_event_id": f"ios-library-{media_id}",
                    "media": [
                        {
                            "media_id": media_id,
                            "library_asset_id": assets[index],
                            "library_source_id": sources[index],
                            "kind": "video",
                            "filename": "qa.mp4",
                            "content_type": "video/mp4",
                        }
                    ],
                },
            )
            assert attached.status_code == 200, attached.text
            assert attached.json()["revision"] == retry_revision
        replay = await client.post(f"{path}/stories/{story_id}/select", json=selection_body)
        assert replay.status_code == 200, replay.text
        assert replay.json()["turn_id"] == original_turn_id
        if recovery_case == "cancelled":
            assert replay.json()["status"] == "cancelled"
        elif recovery_case != "approved":
            assert replay.json()["status"] == (
                "awaiting_approval" if recovery_case == "prepared" else "completed"
            )
        if recovery_case in _RECOVERABLE_CASES:
            assert replay.json().get("recoverable_held_turn_id") == original_turn_id
        elif recovery_case in {"other-refusal", "cancelled", "prepared", "approved"}:
            assert replay.json().get("recoverable_held_turn_id") is None
        if recovery_case == "flags-off":
            monkeypatch.setattr(settings, "kria_approval_readiness_enabled", False)
        if recovery_case == "reused-recovery-key":
            async with AsyncSessionLocal() as db:
                thread = await db.get(CreationThread, thread_id)
                unrelated = await _append_event(
                    db,
                    thread,
                    role="user",
                    event_type="user_message",
                    content="Another request",
                    payload={},
                    client_event_id=f"camera-roll-recover-{original_turn_id}",
                )
                db.add(
                    CreatorAgentTurn(
                        thread_id=thread_id,
                        source_event_id=unrelated.id,
                        client_event_id=f"camera-roll-recover-{original_turn_id}",
                        request_digest="a" * 64,
                        status="pending",
                    )
                )
                await db.commit()
        if recovery_case.startswith("changed-") or recovery_case.endswith("-job"):
            async with AsyncSessionLocal() as db:
                thread = await db.get(CreationThread, thread_id)
                if recovery_case == "changed-thread":
                    await _append_event(
                        db,
                        thread,
                        role="user",
                        event_type="user_message",
                        content="Keep my later edit",
                    )
                elif recovery_case == "changed-media":
                    item = await db.get(PlanItem, item_id)
                    item.clip_gcs_paths = ["different-source.mp4"]
                elif recovery_case == "changed-story":
                    (await db.get(MediaCollectionStory, story_id)).revision += 1
                elif recovery_case == "changed-collection":
                    from app.models import MediaCollection

                    (await db.get(MediaCollection, collection_id)).revision += 1
                elif recovery_case == "changed-generation":
                    metadata_by_path[assignments[0]["gcs_path"]] = SimpleNamespace(
                        generation="changed", size=1000
                    )
                else:
                    # Even a historical Job no longer pointed to by current_job_id
                    # makes this a used project, not a pre-draft hold.
                    db.add(
                        Job(
                            user_id=owner.id,
                            content_plan_item_id=item_id,
                            raw_storage_path=f"users/{owner.id}/qa-only",
                            selected_platforms=[],
                            status={
                                "pending-job": "queued",
                                "running-job": "processing",
                                "completed-job": "variants_ready",
                            }[recovery_case],
                        )
                    )
                await db.commit()
        recovery_body = {
            **selection_body,
            "expected_thread_revision": retry_revision,
            "recover_held_turn_id": original_turn_id,
        }
        attempts = await asyncio.gather(
            *[
                client.post(f"{path}/stories/{story_id}/select", json=recovery_body)
                for _ in range(2)
            ]
        )
        if recovery_case not in _RECOVERABLE_CASES:
            assert all(response.status_code == 409 for response in attempts), [
                r.text for r in attempts
            ]
            async with AsyncSessionLocal() as db:
                assert len(
                    (
                        await db.scalars(
                            select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread_id)
                        )
                    ).all()
                ) == (2 if recovery_case == "reused-recovery-key" else 1)
                assert (await db.get(PlanItem, item_id)).current_job_id is None
            return
        assert all(response.status_code == 200 for response in attempts), [r.text for r in attempts]
        accepted = attempts[0].json()
        assert accepted["turn_id"] == attempts[1].json()["turn_id"] != original_turn_id
        async with AsyncSessionLocal() as db:
            original = await db.get(CreatorAgentTurn, uuid.UUID(original_turn_id))
            assert original.status == "completed" and original.observed_event_id is not None
            assert (
                len(
                    (
                        await db.scalars(
                            select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread_id)
                        )
                    ).all()
                )
                == 2
            )
    async with AsyncSessionLocal() as db:
        assert (
            await db.scalars(
                select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
            )
        ).all() == []
    result = await asyncio.to_thread(run_kria_turn.run, accepted["turn_id"])
    assert result["status"] == "awaiting_approval", result
    with sync_session() as db:
        approval = db.scalar(
            select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
        )
        assert str(approval.turn_id) == accepted["turn_id"]
        if recovery_case:
            assert str(approval.turn_id) != original_turn_id, (
                "Recovery requires a fresh approval identity"
            )
        draft = db.get(CreatorEditDraft, approval.draft_id)
        assert draft.snapshot_json["prepared_collection_story"]["story_id"] == str(story_id)
        frozen = draft.snapshot_json["prepared_collection_story"]
        if source_context:
            assert "whole clip" in approval.consequence_summary.lower()
            assert "no timed highlight" in approval.consequence_summary.lower()
            contexts = frozen["source_contexts"]
            assert len(contexts) == 1
            assert contexts[0]["source_id"] == sources[0]
            assert contexts[0]["moment_id"] == moments[-1]["id"]
            assert contexts[0]["evidence_scope"] == "whole_source_context"
            assert contexts[0]["uncertain"] is True
            assert contexts[0]["quality"] == moments[-1]["evidence"]["quality"]
            assert (
                contexts[0]["quality"]["overlapping_unusable_spans"][0]["reason"]
                == "Synthetic cached quality note"
            )
            assert "cached quality notes" in approval.consequence_summary.lower()
        else:
            assert "source_contexts" not in frozen, (
                "Legacy prepared metadata/digest stays unchanged"
            )
        if readiness_enabled:
            readiness = draft.snapshot_json["brief_coverage"]["approval_readiness"]
            assert readiness["ready"] is True and readiness["brief_version"] > 0
            assert readiness["requirements"] and all(
                row["disposition"] == "executable" for row in readiness["requirements"]
            )
            assert readiness["prepared_evidence_digest"] != hashlib.sha256(b"null").hexdigest()
            assert draft.snapshot_json["brief_binding"]["state"] == "pinned"
            coverage = draft.snapshot_json["brief_coverage"]
            assert coverage["unresolved_ids"] == [], "A ready accepted story has no unmet choices"
            assert set(coverage["enforced_ids"]) == {
                row["requirement_id"] for row in readiness["requirements"]
            }
        else:
            assert draft.snapshot_json.get("brief_binding") is None
        prepared_research = frozen.get("research")
        if with_content_plan:
            assert frozen["research"]["evidence"]["facts"][0]["sources"][0]["url"] == (
                "https://si.edu/recorded-fixture"
            )
        from app.pipeline.guided_story import compile_proposal_execution_plan
        from app.schemas.edit_proposal import EditProposalSnapshot

        rendered_text = [
            row["text"]
            for row in compile_proposal_execution_plan(
                EditProposalSnapshot.model_validate(frozen["snapshot"])
            )["text_elements"]
        ]
        if with_content_plan:
            assert rendered_text == ["Where does this crossing lead?", "Worth the trip?"]
            assert "A journey" not in rendered_text
        else:
            assert rendered_text == []
        assert db.get(PlanItem, item_id).edit_proposal is None, (
            "Preparing a recommendation grants no render consent"
        )
        assert _claim_approval_dispatch(approval.id) is None
        fingerprint = approval_fingerprint(approval)
        approval_id, draft_revision = approval.id, approval.draft_revision
        thread_revision = db.get(CreationThread, thread_id).revision
    if recovery_case == "held" and not retained_condition:
        # Refreshing the index after approval preparation cannot invalidate a
        # previously selected timed story or replace its pinned windows/caption.
        async with AsyncSessionLocal() as db:
            extra_asset_id, extra_source_id = additional_context
            extra_source = await db.get(MediaAssetSource, extra_source_id)
            ref = facts._Source(
                str(extra_asset_id),
                str(extra_source_id),
                str(owner.id),
                "video",
                extra_source.role,
                extra_source.object_path,
                extra_source.object_generation,
                extra_source.uploaded_sha256,
                extra_source.status,
            )
            for analyzer, result in (
                (
                    None,
                    {
                        "duration_s": 8.166667,
                        "description": "Synthetic extra source context",
                        "understanding": {"notable_moments": []},
                    },
                ),
                ("transcript", {"words": []}),
            ):
                key = facts._key(ref, analyzer)
                db.add(
                    MediaAssetAnalysis(
                        user_id=owner.id,
                        asset_id=extra_asset_id,
                        source_id=extra_source_id,
                        analyzer=key.analyzer,
                        model=key.model,
                        prompt_version=key.prompt_version,
                        schema_version=key.schema_version,
                        source_identity=key.source_identity,
                        status="ready",
                        result_json=result,
                    )
                )
            await db.commit()
        refreshed = await client.post(f"{path}/index", json={"expected_revision": 1})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["context_asset_ids"] == [str(extra_asset_id)]
        with sync_session() as db:
            assert (
                len(
                    db.scalars(
                        select(MediaSourceMoment).where(
                            MediaSourceMoment.source_id == extra_source_id
                        )
                    ).all()
                )
                == 1
            )
            assert all(
                db.get(MediaSourceMoment, uuid.UUID(value["id"])) is not None for value in moments
            )
            assert (
                db.get(CreatorEditDraft, approval.draft_id).snapshot_json[
                    "prepared_collection_story"
                ]
                == frozen
            )
    if change_before_approval:
        if change_before_approval is True:
            changed = await client.put(
                path,
                json={
                    **selection(assets, sources),
                    "expected_revision": 1,
                    "name": "Corrected outing",
                },
            )
            assert changed.status_code == 200
        elif change_before_approval != "approval":
            with sync_session() as db:
                if change_before_approval == "story":
                    row = db.get(MediaCollectionStory, story_id)
                    row.revision += 1
                elif change_before_approval == "source":
                    row = db.get(MediaAssetSource, uuid.UUID(sources[0]))
                    # Source rows are immutable; simulate changed storage identity at
                    # the storage boundary rather than disabling that DB protection.
                    metadata_by_path[row.object_path] = SimpleNamespace(
                        generation="replaced-generation", size=row.byte_count
                    )
                elif change_before_approval == "story-context":
                    import copy

                    row = db.get(MediaCollectionStory, story_id)
                    value = copy.deepcopy(row.moments_json)
                    value[-1]["evidence"]["quality"]["source_visibility"] = "Unapproved replacement"
                    row.moments_json = value
                else:
                    import copy

                    row = db.get(CreatorEditDraft, approval.draft_id)
                    value = copy.deepcopy(row.snapshot_json)
                    prepared = value["prepared_collection_story"]
                    if change_before_approval == "prepared-missing":
                        value.pop("prepared_collection_story")
                    elif change_before_approval == "prepared-window":
                        prepared["snapshot"]["fast_cuts"][0]["source_start_s"] += 0.5
                    elif change_before_approval == "prepared-context":
                        prepared["source_contexts"][0]["quality"] = {}
                    elif change_before_approval == "prepared-context-missing":
                        prepared.pop("source_contexts")
                    else:
                        prepared["content_plan"]["caption"] = "Unapproved replacement caption"
                    row.snapshot_json = value
                db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as blocked:
                await decide_approval(
                    db,
                    thread_id=thread_id,
                    creator_id=owner.id,
                    approval_id=approval_id,
                    decision="approve",
                    body=ApprovalDecisionBody(
                        expected_thread_revision=thread_revision,
                        expected_draft_revision=draft_revision,
                        expected_approval_fingerprint="0" * 64
                        if change_before_approval == "approval"
                        else fingerprint,
                    ),
                )
            assert blocked.value.code in {
                "approval_stale"
                if change_before_approval == "approval"
                else "camera_roll_story_stale",
                "approval_requirements_not_ready"
                if change_before_approval
                in {
                    "prepared-window",
                    "prepared-missing",
                    "prepared-context",
                    "prepared-context-missing",
                }
                else "camera_roll_story_stale",
            }
        with sync_session() as db:
            assert db.get(CreatorAgentApproval, approval_id).status == (
                "pending" if change_before_approval == "approval" else "cancelled"
            )
            assert db.get(PlanItem, item_id).current_job_id is None
        return
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread_id,
            creator_id=owner.id,
            approval_id=approval_id,
            decision="approve",
            body=ApprovalDecisionBody(
                expected_thread_revision=thread_revision,
                expected_draft_revision=draft_revision,
                expected_approval_fingerprint=fingerprint,
            ),
        )
    claim = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
    assert claim is not None
    assert claim.creator_guided_attempt_id is not None
    from app.tasks.content_plan_build import dispatch_item_render_for

    monkeypatch.setattr(
        "app.services.job_dispatch.enqueue_orchestrator_sync", lambda *args, **kwargs: None
    )
    dispatched = await asyncio.to_thread(
        dispatch_item_render_for,
        str(item_id),
        0,
        creator_strategy=claim.strategy,
        creator_guided_attempt_id=claim.creator_guided_attempt_id,
    )
    assert dispatched.outcome == "dispatched", dispatched
    if recovery_case:
        replay = await client.post(f"{path}/stories/{story_id}/select", json=recovery_body)
        assert replay.status_code == 200 and replay.json()["turn_id"] == accepted["turn_id"]
        repeated_dispatch = await asyncio.to_thread(
            dispatch_item_render_for,
            str(item_id),
            0,
            creator_strategy=claim.strategy,
            creator_guided_attempt_id=claim.creator_guided_attempt_id,
        )
        assert repeated_dispatch.outcome == "already_active"
        with sync_session() as db:
            jobs = db.scalars(select(Job).where(Job.content_plan_item_id == item_id)).all()
            assert len(jobs) == 1 and str(jobs[0].id) == dispatched.job_id
    from app.tasks.generative_build import _guided_execution_plan

    with sync_session() as db:
        guided = db.get(Job, uuid.UUID(dispatched.job_id)).assembly_plan["guided_edit"]
    loaded, track = await asyncio.to_thread(
        _guided_execution_plan, dispatched.job_id, guided, emit_decided=False
    )
    assert track is None
    with sync_session() as db:
        changed_job = db.get(Job, uuid.UUID(dispatched.job_id))
        changed_job.all_candidates = {
            **changed_job.all_candidates,
            "creator_strategy": {
                **changed_job.all_candidates["creator_strategy"],
                "licensed_sfx": {"effect_id": "unapproved-later-choice"},
            },
        }
        db.commit()
    reloaded, track = await asyncio.to_thread(
        _guided_execution_plan, dispatched.job_id, guided, emit_decided=False
    )
    assert reloaded == loaded and track is None, (
        "Later planning inputs cannot alter a frozen recommendation"
    )
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        job = db.get(Job, uuid.UUID(dispatched.job_id))
        frozen = job.assembly_plan["guided_story_execution_plan"]
        assert loaded == frozen, "The worker must consume the frozen choices verbatim"
        assert [
            (value["source_start_s"], value["source_end_s"]) for value in frozen["story_timeline"]
        ] == [
            (round(value["start_s"] * 30) / 30, round(value["end_s"] * 30) / 30)
            for value in moments
        ]
        assert [row["text"] for row in frozen["text_elements"]] == (
            ["Where does this crossing lead?", "Worth the trip?"] if with_content_plan else []
        )
        assert job.assembly_plan["prepared_collection_story"]["story_id"] == str(story_id)
        if source_context:
            assert job.assembly_plan["prepared_collection_story"]["source_contexts"] == contexts
        if with_content_plan:
            assert job.assembly_plan["prepared_collection_story"]["research"] == prepared_research
        assert item.edit_proposal["status"] == "approved"
        snapshot = item.edit_proposal["last_approved"]["snapshot"]
        assert snapshot.get("text_policy", "legacy") == ("legacy" if with_content_plan else "none")
        assert [
            (value["source_start_s"], value["source_end_s"]) for value in snapshot["fast_cuts"]
        ] == [
            (round(value["start_s"] * 30) / 30, round(value["end_s"] * 30) / 30)
            for value in moments
        ]
        assert (
            item.edit_proposal["planning_diagnostics"]["prepared_collection_story"][
                "compiled_digest"
            ]
            == draft.snapshot_json["prepared_collection_story"]["compiled_digest"]
        )

        if proof_input:
            from app.services.phone_sources import bind_phone_sources

            output = Path(proof_input)
            (output / "prepared.json").write_text(
                json.dumps(draft.snapshot_json["prepared_collection_story"], indent=2)
            )
            bindings = bind_phone_sources(item.clip_assignments, item.clip_gcs_paths)
            if source_context:
                from app.pipeline.guided_story import GuidedStoryExecutionPlan
                from app.pipeline.phone_guided_plan import compile_phone_guided_plan
                from app.services.media_collection_preparation import PreparedCollectionStory

                bound = PreparedCollectionStory.model_validate(
                    draft.snapshot_json["prepared_collection_story"]
                )
                # Compiler-only comparison; the stripped copy is never approved
                # or persisted (the negative control above refuses doing that).
                before_metadata = bound.model_copy(update={"source_contexts": []})
                recipes = [
                    compile_phone_guided_plan(
                        GuidedStoryExecutionPlan.model_validate(
                            compile_proposal_execution_plan(value.snapshot)
                        ),
                        bindings,
                    ).model_dump_json()
                    for value in (before_metadata, bound)
                ]
                assert recipes[0] == recipes[1], (
                    "Scope/quality provenance cannot change render choices"
                )
                (output / "metadata-render-equivalence.json").write_text(
                    json.dumps(
                        {
                            "byte_equivalent": True,
                            "native_recipe_sha256": hashlib.sha256(recipes[1].encode()).hexdigest(),
                            "source_context_count": len(bound.source_contexts),
                            "compiled_digest": bound.compiled_digest,
                        },
                        indent=2,
                    )
                )
            (output / "bindings.json").write_text(
                json.dumps([value.model_dump(mode="json") for value in bindings], indent=2)
            )
            (output / "originals.json").write_text(
                json.dumps(
                    {
                        record["media_id"]: str((output / f"source-{index}.mp4").resolve())
                        for index, record in enumerate(assignments)
                    },
                    indent=2,
                )
            )

    if with_content_plan:
        from app.kria import plan_blocks, plan_payloads

        caption_starts = []
        monkeypatch.setattr(plan_blocks, "enabled", lambda: True)
        monkeypatch.setattr(
            plan_blocks, "_start_post_caption", lambda *args: caption_starts.append(args)
        )
        plan_blocks.emit_plan_blocks(
            dispatched.job_id, [plan_blocks.block("title", "decided", "Selected story title")]
        )
        assert caption_starts == [], "An approved caption must not start speculative model copy"
        with sync_session() as db:
            job = db.get(Job, uuid.UUID(dispatched.job_id))
            raw = plan_blocks._variant_post_caption(job)
        assert raw is not None and raw["text"] == caption
        assert plan_payloads.finalize_payload("post_caption", raw)["text"] == caption
        plan_blocks.emit_post_caption(dispatched.job_id)
        with sync_session() as db:
            sections = plan_blocks.reduce_job_blocks(
                plan_blocks.load_plan_events(db, thread_id), str(dispatched.job_id)
            )
        assert sections["post_caption"]["payload"]["text"] == caption


async def test_ready_source_without_timed_evidence_stays_unindexed_without_retry(
    collection_world,  # noqa: F811
):
    """Completed facts without intervals are not evidence of a queued analysis."""
    client, assets, sources, _owner, _app = collection_world
    async with AsyncSessionLocal() as db:
        rows = (
            await db.scalars(
                select(MediaAssetAnalysis).where(
                    MediaAssetAnalysis.source_id == uuid.UUID(sources[0])
                )
            )
        ).all()
        assert len(rows) == 2 and all(row.status == "ready" for row in rows)
        for row in rows:
            row.result_json = (
                {"words": []}
                if row.analyzer == "nova.media.transcript"
                else {
                    "understanding": {"notable_moments": []},
                    "important_moments": [],
                    "best_moments": [],
                }
            )
        await db.commit()
    path = f"/me/media-collections/{uuid.uuid4()}"
    assert (await client.put(path, json=selection(assets, sources))).status_code == 200
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.status_code == 200, indexed.text
    assert assets[0] in indexed.json()["unusable_timing_asset_ids"]
    assert indexed.json()["missing_analysis_asset_ids"] == []
    found = await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 1})
    assert found.status_code == 200, found.text
    assert assets[0] in found.json()["unindexed_asset_ids"]
    async with AsyncSessionLocal() as db:
        assert (
            await db.scalars(
                select(MediaSourceMoment).where(
                    MediaSourceMoment.source_id == uuid.UUID(sources[0])
                )
            )
        ).all() == []
        rows = (
            await db.scalars(
                select(MediaAssetAnalysis).where(
                    MediaAssetAnalysis.source_id == uuid.UUID(sources[0])
                )
            )
        ).all()
        assert len(rows) == 2 and all(row.status == "ready" for row in rows)
    # collection_world rejects every download/provider analysis attempt. No
    # highlights are fabricated and the original READY facts stay unchanged.


@pytest.mark.parametrize(
    "condition",
    [
        "complete",
        "pending-video",
        "pending-transcript",
        "pending-without-duration",
        "no-analysis",
        "wrong-spec",
        "wrong-source",
        "wrong-generation",
        "zero",
        "negative",
        "nan",
        "infinite",
        "out-of-bounds",
        "missing-probe",
        "too-long",
        "too-short",
        "probe-only",
    ],
)
async def test_completed_untimed_source_gets_truthful_bounded_context_only(
    collection_world,  # noqa: F811
    monkeypatch,
    condition,
):
    from types import SimpleNamespace

    from app import storage

    client, assets, sources, owner, _app = collection_world
    duration = 8.166667
    if condition in {"too-long", "too-short"}:
        duration = 21 if condition == "too-long" else 0.5
    async with AsyncSessionLocal() as db:
        asset = await db.get(MediaAsset, uuid.UUID(assets[0]))
        metadata = {**asset.metadata_json, "duration_s": duration}
        if condition == "pending-without-duration":
            metadata["duration_s"] = None
        if condition in {"zero", "negative", "nan", "infinite"}:
            metadata["duration_s"] = {"zero": 0, "negative": -1, "nan": "nan", "infinite": "inf"}[
                condition
            ]
        asset.metadata_json = metadata
        rows = (
            await db.scalars(
                select(MediaAssetAnalysis).where(
                    MediaAssetAnalysis.source_id == uuid.UUID(sources[0])
                )
            )
        ).all()
        for row in rows:
            transcript = row.analyzer == "nova.media.transcript"
            row.result_json = (
                {"words": []}
                if transcript
                else {
                    "duration_s": duration if condition != "out-of-bounds" else 10,
                    "description": "Synthetic red test footage; no localized scene was detected.",
                    "understanding": {
                        "summary": "Synthetic red test footage",
                        "notable_moments": [],
                    },
                    "important_moments": [],
                    "best_moments": [],
                }
            )
            if condition == "missing-probe" and not transcript:
                row.result_json = {k: v for k, v in row.result_json.items() if k != "duration_s"}
            if condition == "probe-only" and not transcript:
                row.result_json = {**row.result_json, "source": "probe_only"}
            if (
                condition in {"pending-video", "pending-without-duration"}
                and not transcript
                or condition == "pending-transcript"
                and transcript
            ):
                row.status = "analyzing"
            if condition == "no-analysis":
                await db.delete(row)
            if condition == "wrong-spec":
                row.schema_version += "/other-spec"
            if condition == "wrong-source":
                row.source_identity += "/other-source"
        await db.commit()
    if condition == "wrong-generation":
        monkeypatch.setattr(
            storage,
            "object_metadata",
            lambda path: SimpleNamespace(generation="replaced", size=1234),
        )
    path = f"/me/media-collections/{uuid.uuid4()}"
    body = selection(assets, sources, count=1)
    assert (await client.put(path, json=body)).status_code == 200
    indexed = await client.post(f"{path}/index", json={"expected_revision": 1})
    assert indexed.status_code == 200, indexed.text
    found = await client.get(
        f"{path}/moments", params={"query": "synthetic", "expected_revision": 1}
    )
    assert found.status_code == 200, found.text
    if condition == "complete":
        assert indexed.json()["missing_analysis_asset_ids"] == []
        assert indexed.json()["context_asset_ids"] == [assets[0]]
        assert indexed.json()["unusable_timing_asset_ids"] == []
        moments = found.json()["moments"]
        assert len(moments) == 1
        moment = moments[0]
        assert (moment["start_s"], moment["end_s"]) == (0, duration)
        assert moment["evidence_scope"] == "whole_source_context" and moment["uncertain"] is True
        assert moment["evidence"]["semantic_localization"] == "none"
        assert moment["evidence"]["timing_basis"] == "verified_source_bounds"
        assert moment["source_id"] == sources[0] and moment["generation"] == "42"
        assert "Synthetic red" in moment["text"]
        assert found.json()["unindexed_asset_ids"] == []
        async with AsyncSessionLocal() as db:
            asset = await db.get(MediaAsset, uuid.UUID(assets[0]))
            asset.metadata_json = {**asset.metadata_json, "duration_s": duration - 1}
            await db.commit()
        stale = await client.get(
            f"{path}/moments", params={"query": "synthetic", "expected_revision": 1}
        )
        assert stale.status_code == 200 and stale.json()["moments"] == []
        assert stale.json()["unindexed_asset_ids"] == [assets[0]], (
            "Cached context cannot outlive its verified bounds"
        )
        refreshed = await client.post(f"{path}/index", json={"expected_revision": 1})
        assert refreshed.status_code == 200
        assert refreshed.json()["unusable_timing_asset_ids"] == [assets[0]]
        assert refreshed.json()["missing_analysis_asset_ids"] == []
    else:
        assert found.json()["moments"] == [], (
            "Unknown or conflicting evidence cannot become a usable moment"
        )
        if condition in {
            "pending-video",
            "pending-transcript",
            "pending-without-duration",
            "no-analysis",
            "wrong-spec",
            "wrong-source",
        }:
            assert indexed.json()["missing_analysis_asset_ids"] == [assets[0]]
        elif condition != "wrong-generation":
            assert indexed.json()["unusable_timing_asset_ids"] == [assets[0]]
            assert indexed.json()["missing_analysis_asset_ids"] == []
    # Every storage download/provider analysis attempt is rejected by the fixture.
    # Original data never enters this synthetic database.

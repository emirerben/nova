"""Recommendation selection must cross the existing expiring approval boundary."""

import asyncio
import hashlib
import json
import os
import uuid
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


@pytest.mark.parametrize(
    "with_content_plan,fractional_end,change_before_approval,caption,readiness_enabled",
    [
        pytest.param(
            content,
            fractional,
            changed,
            _ACCEPTED_CAPTION if content else None,
            True,
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
    + [pytest.param(True, False, False, "", True, id="explicit-empty-caption")]
    + [
        pytest.param(True, False, changed, _ACCEPTED_CAPTION, True, id=f"changed-{changed}")
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
        pytest.param(False, True, False, None, False, id="legacy-readiness-off"),
        pytest.param(True, False, False, "", False, id="empty-caption-readiness-off"),
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
):
    client, assets, sources, owner, _app = collection_world
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
            original_hash, original_bytes = "a" * 64, 1000
            uploaded_hash, uploaded_bytes = (
                previous_source.uploaded_sha256,
                previous_source.byte_count,
            )
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
                        result_json=analysis.result_json,
                    )
                )
            sources[index] = str(source.id)
            record = receipt(f"analysis-proxy-clip-{index}")
            record.update(
                gcs_path=source.object_path,
                storage_generation="42",
                duration_s=180,
                library_asset_id=assets[index],
                library_source_id=sources[index],
            )
            proxy = record["upload_contract"]["proxy"]
            proxy["duration_s"] = 180
            proxy["original"].update(
                duration_s=180,
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
    await client.put(path, json=selection(assets, sources))
    await client.post(f"{path}/index", json={"expected_revision": 1})
    moments = (
        await client.get(f"{path}/moments", params={"query": "ferry", "expected_revision": 1})
    ).json()["moments"]
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
    selected = await client.post(
        f"{path}/stories/{story_id}/select",
        json={
            "expected_revision": 1,
            "expected_story_revision": 1,
            "client_event_id": "selection-1",
            "thread_id": str(thread_id),
            "expected_thread_revision": 0,
        },
    )
    assert selected.status_code == 200, selected.text
    accepted = selected.json()
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
        draft = db.get(CreatorEditDraft, approval.draft_id)
        assert draft.snapshot_json["prepared_collection_story"]["story_id"] == str(story_id)
        frozen = draft.snapshot_json["prepared_collection_story"]
        if readiness_enabled:
            readiness = draft.snapshot_json["brief_coverage"]["approval_readiness"]
            assert readiness["ready"] is True and readiness["brief_version"] > 0
            assert readiness["requirements"] and all(
                row["disposition"] == "executable" for row in readiness["requirements"]
            )
            assert readiness["prepared_evidence_digest"] != hashlib.sha256(b"null").hexdigest()
            assert draft.snapshot_json["brief_binding"]["state"] == "pinned"
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
                else:
                    import copy

                    row = db.get(CreatorEditDraft, approval.draft_id)
                    value = copy.deepcopy(row.snapshot_json)
                    prepared = value["prepared_collection_story"]
                    if change_before_approval == "prepared-missing":
                        value.pop("prepared_collection_story")
                    elif change_before_approval == "prepared-window":
                        prepared["snapshot"]["fast_cuts"][0]["source_start_s"] += 0.5
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
                if change_before_approval in {"prepared-window", "prepared-missing"}
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

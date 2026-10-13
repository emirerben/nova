"""KRI-651: rendered follow-ups retain honest provider-failure recovery.

Failure matrix, declared before the fixture:
- An unknown provider outcome must never become a completed clarification.
- Extraction must not contact Main Creator or Copilot after an unknown outcome.
- The shared turn deadline must bound the extractor's provider call.
- Creator-visible recovery must preserve EN/TR, hide private provider details,
  and retain the submitted request, footage, rendered generation, and typed brief.

Only provider transport and broker publishing are substituted; the healthy arm
also crosses the real Brief Extractor parser before publishing its question.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.agents._runtime import ModelInvocation, ProviderOutcomeUnknownError
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria.api_schemas import SubmitTurnBody
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.runtime import read_delta, submit_turn
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("language", ["en", "tr"])
async def test_rendered_followup_provider_unknown_fails_once_without_losing_the_brief(
    monkeypatch, unknown, language
):
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(run_kria_turn, "soft_time_limit", 45)
    user_id, thread_id, session_id = _seed_runtime_project()
    hook = "Finish line feelings 😤" if language == "en" else "Bitiş çizgisinde 😤"
    request = "Try again" if language == "en" else "Tekrar dene"
    question = (
        "Do you want to revise this edit or make a new version?"
        if language == "en"
        else "Bu düzenlemeyi değiştirmek mi, yeni bir sürüm yapmak mı istiyorsun?"
    )
    brief = CreativeBrief(
        version=1,
        requirements=[BriefRequirement(id="r1", kind="text", scope="title", literal=hook)],
    )
    requirements = [req.model_dump(mode="json") for req in brief.requirements]
    footage = ["users/test/marathon.mp4"]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "edit_format": "montage", "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.content_mode = "existing_footage"
        item.clip_gcs_paths = footage
        item.clip_assignments = [
            {"media_id": "clip-1", "gcs_path": footage[0], "kind": "video", "duration_s": 4.1}
        ]
        job = Job(
            user_id=user_id,
            status="variants_ready",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            all_candidates={"clip_paths": footage},
            assembly_plan={
                "variants": [
                    {
                        "variant_id": "original_text",
                        "resolved_archetype": "montage",
                        "render_status": "ready",
                        "render_generation_id": "generation-1",
                        "text_elements": [
                            {"id": "hook", "text": hook, "start_s": 0.0, "end_s": 2.0}
                        ],
                    }
                ]
            },
        )
        db.add(job)
        db.flush()
        item.current_job_id = job.id
        session = db.get(CreatorAgentSession, session_id)
        session.target_job_id = job.id
        session.target_variant_id = "original_text"
        session.target_generation_id = "generation-1"
        db.add(CreativeBriefVersion(thread_id=thread_id, version=1, requirements=requirements))
        db.commit()
        job_id = job.id
        assembly = job.assembly_plan

    async with AsyncSessionLocal() as db:
        accepted, publish = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=request, client_event_id=uuid.uuid4().hex, expected_thread_revision=2
            ),
        )
    assert publish
    calls = []
    published = []

    class BriefTransport:
        def invoke(self, **kwargs):
            calls.append(kwargs)
            assert "request_scope" in kwargs["prompt"], "expected the narrow Brief Extractor"
            if unknown:
                raise ProviderOutcomeUnknownError("PRIVATE extractor result unknown")
            return ModelInvocation(
                raw_text=json.dumps(
                    {"brief_updates": [], "request_scope": "clarify", "clarification": question}
                )
            )

    monkeypatch.setattr("app.kria.planner.default_client", lambda: BriefTransport())
    monkeypatch.setattr(
        run_kria_turn, "apply_async", lambda **kwargs: published.append(kwargs["args"])
    )
    if unknown:
        with pytest.raises(ProviderOutcomeUnknownError):
            await asyncio.to_thread(run_kria_turn.run, str(accepted.turn_id))
    else:
        result = await asyncio.to_thread(run_kria_turn.run, str(accepted.turn_id))
        assert result["status"] == "completed"
    assert len(calls) == 1, "extraction triggered another provider request"
    assert 0 < calls[0]["timeout_s"] <= 25
    assert hook in calls[0]["prompt"]
    assert request in calls[0]["prompt"]
    assert published == []

    async with AsyncSessionLocal() as db:
        delta = await read_delta(
            db, thread_id=thread_id, creator_id=user_id, after_sequence=1, limit=20
        )
    replies = [event for event in delta.events if event.role == "assistant"]
    assert len(replies) == 1
    assert "PRIVATE" not in json.dumps(replies[0].model_dump(mode="json"))
    with sync_session() as db:
        turn = db.get(CreatorAgentTurn, accepted.turn_id)
        assert turn.lease_epoch == 1
        if unknown:
            assert turn.status == "failed"
            assert turn.error["code"] == "runtime_provider_outcome_unknown"
            assert replies[0].event_type == "assistant_error"
            assert replies[0].payload["code"] == turn.error["code"]
            assert replies[0].payload["retryable"] is True
            assert ("AI service" if language == "en" else "Yapay zekâ") in replies[0].content
        else:
            assert turn.status == "completed"
            assert replies[0].content == question
        assert db.get(CreationThreadEvent, turn.source_event_id).content == request
        item = db.get(PlanItem, thread.active_plan_item_id)
        assert item.clip_gcs_paths == footage
        assert item.current_job_id == job_id
        assert db.get(Job, job_id).assembly_plan == assembly
        versions = list(
            db.scalars(
                select(CreativeBriefVersion).where(CreativeBriefVersion.thread_id == thread_id)
            )
        )
        assert len(versions) == 1
        assert versions[0].requirements == requirements

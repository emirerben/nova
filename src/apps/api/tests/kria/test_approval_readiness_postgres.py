"""KRI-721 acceptance: provider replay -> real runtime/DB -> HTTP approval.

Failure matrix is recorded in docs/reviews/kri-721-approval-readiness.md before
implementation. Only provider transport and broker publication are substituted.
No planner, checker, policy, compiler or approval service is mocked.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from app.agents._runtime import ModelInvocation
from app.auth import get_current_user
from app.config import settings
from app.database import sync_session
from app.kria.runtime import approval_fingerprint
from app.main import app
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorEditDraft,
    PlanItem,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit


@pytest.fixture(autouse=True)
def readiness_flags(monkeypatch):
    for name in (
        "kria_runtime_v2_enabled",
        "main_creator_agent_enabled",
        "kria_creative_brief_enabled",
        "kria_brief_binding_enabled",
        "guided_edit_capability_enabled",
        "edit_format_day_vlog_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    if hasattr(settings, "kria_approval_readiness_enabled"):
        monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)
    monkeypatch.setattr(settings, "clip_intents_enabled", False)
    monkeypatch.setattr(settings, "ios_device_only_mode", False)
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


async def prepare(
    monkeypatch,
    *,
    language="en",
    extra=None,
    seed=None,
    message=None,
    updates_override=None,
    phone=False,
):
    user_id, thread_id, _ = seed or _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage" if phone else "day_vlog"
        source_path = (
            f"users/{user_id}/raw/analysis-proxy-readiness.mp4"
            if phone
            else "users/test/gameday.mp4"
        )
        item.clip_gcs_paths = [source_path]
        item.clip_assignments = [
            {
                "media_id": "clip-1",
                "gcs_path": source_path,
                "kind": "video",
                "duration_s": 8,
                "analysis": {"summary": "A stadium flag", "has_speech": False},
            }
        ]
        if phone:
            row = item.clip_assignments[0]
            item.clip_assignments = [
                {
                    **row,
                    "manifest_identity": "clip-1",
                    "storage_generation": "1",
                    "has_audio": True,
                    "upload_contract": {
                        "purpose": "analysis_proxy",
                        "proxy": {
                            "duration_s": 8.0,
                            "width": 640,
                            "height": 360,
                            "frame_rate": 30,
                            "original": {
                                "sha256": "1" * 64,
                                "byte_count": 100,
                                "duration_s": 8.0,
                                "width": 640,
                                "height": 360,
                                "has_audio": True,
                            },
                        },
                    },
                }
            ]
            monkeypatch.setattr(settings, "phone_rendering_enabled", True)
            monkeypatch.setattr(settings, "phone_render_user_ids", [user_id])
            monkeypatch.setattr(
                settings,
                "phone_render_verified_features",
                [
                    "authoredText",
                    "basicComposition",
                    "local1080Export",
                    "positionedText",
                    "audioMix",
                ],
            )
        revision = thread.revision
        db.commit()
    updates = [
        {"kind": "text", "scope": "title", "literal": "Gameday."},
        {
            "kind": "timing",
            "scope": "global",
            "description": "6 seconds",
            "facts": {"duration_s": 6},
        },
    ]
    if extra:
        updates.append(extra)
    if updates_override is not None:
        updates = updates_override
    output = {
        "action": {
            "kind": "propose_strategy",
            "summary": "A six-second Gameday edit.",
            "strategy": {
                "direction": "fast_montage" if phone else "guided_story",
                "edit_format": "montage" if phone else "day_vlog",
                "audio_strategy": "original_audio",
                "pacing": "balanced",
                "render_program": "guided",
                "opening_title": "Gameday.",
                "target_duration_s": 6,
                "selected_media_ids": ["clip-1"],
                "rationale": "Use the stadium flag.",
            },
        },
        "brief_updates": updates,
    }
    calls = []

    class Transport:
        def invoke(self, **kwargs):
            calls.append(kwargs["model"])
            return ModelInvocation(raw_text=json.dumps(output))

    monkeypatch.setattr("app.kria.planner.default_client", lambda: Transport())
    message = message or (
        'Use the title "Gameday." and make the edit 6 seconds.'
        if language == "en"
        else 'Başlık "Gameday." olsun ve düzenleme 6 saniye olsun.'
    )
    if extra:
        message += " " + (extra.get("description") or extra.get("literal") or "")
    accepted = await _submit(user_id, thread_id, message, revision)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] in {"completed", "awaiting_approval"}, result
    assert len(calls) == 1, "Readiness must not add model calls"
    with sync_session() as db:
        approvals = list(
            db.scalars(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == thread_id,
                    CreatorAgentApproval.status == "pending",
                )
            )
        )
        events = list(
            db.scalars(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                )
                .order_by(CreationThreadEvent.sequence)
            )
        )
        return user_id, thread_id, approvals, events


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("phone", [False, True])
@pytest.mark.parametrize(
    "extra",
    [
        {"kind": "audio", "scope": "global", "description": "Remove only the wind noise"},
        {
            "kind": "style",
            "scope": "global",
            "description": "Lift the shadows in the footage",
            "facts": {"footage_light": "brighter"},
        },
        {
            "kind": "select",
            "scope": "clip:clip-1",
            "description": "Use the exact moment when the flag unfolds",
        },
    ],
)
async def test_unproven_or_unsupported_request_has_no_approval(monkeypatch, language, extra, phone):
    if language == "tr":
        extra = {
            **extra,
            "description": {
                "audio": "Yalnızca rüzgâr sesini kaldır",
                "style": "Çekimlerdeki gölgeleri aydınlat",
                "select": "Bayrağın açıldığı tam anı kullan",
            }[extra["kind"]],
        }
    _, thread_id, approvals, events = await prepare(
        monkeypatch, language=language, extra=extra, phone=phone
    )
    assert not approvals, "An unproven requested change was offered for approval"
    response = events[-1]
    coverage = (response.payload or {}).get("brief_coverage") or {}
    assert coverage.get("reason") == "approval_requirements_not_ready", response.content
    rows = coverage["approval_readiness"]["requirements"]
    assert {row["requirement_id"] for row in rows} == {"r1", "r2", "r3"}
    assert all(
        row["disposition"] == "executable" for row in rows if row["requirement_id"] in {"r1", "r2"}
    )
    assert next(row for row in rows if row["requirement_id"] == "r3")["disposition"] in {
        "unsupported",
        "needs_choice",
        "evidence_missing",
    }
    assert extra["description"] in response.content
    with sync_session() as db:
        assert (
            db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id).current_job_id
            is None
        )
        assert not list(
            db.scalars(
                select(CreatorAgentExecution).where(
                    CreatorAgentExecution.target_thread_id == thread_id,
                    CreatorAgentExecution.risk == "approval_required",
                )
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("phone", [False, True])
async def test_supported_compound_request_reaches_real_http_approval(monkeypatch, language, phone):
    user_id, thread_id, approvals, events = await prepare(
        monkeypatch, language=language, phone=phone
    )
    assert len(approvals) == 1, events[-1].content
    approval = approvals[0]
    with sync_session() as db:
        draft = db.get(CreatorEditDraft, approval.draft_id)
        readiness = draft.snapshot_json["brief_coverage"]["approval_readiness"]
        assert readiness["ready"]
        assert {r["requirement_id"] for r in readiness["requirements"]} == {"r1", "r2"}
        body = {
            "expected_thread_revision": db.get(CreationThread, thread_id).revision,
            "expected_draft_revision": approval.draft_revision,
            "expected_approval_fingerprint": approval_fingerprint(approval),
        }
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, email="qa@test.local"
    )
    published = []
    monkeypatch.setattr("app.routes.kria_runtime._publish_approval", published.append)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            f"/creation-threads/{thread_id}/approvals/{approval.id}/approve", json=body
        )
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "approved"
    assert published == [str(approval.id)]


@pytest.mark.asyncio
async def test_missing_request_extraction_cannot_certify_an_empty_request(monkeypatch):
    _, _, approvals, events = await prepare(monkeypatch, updates_override=[])
    assert not approvals, "Missing extracted requirements cannot count as complete evidence"
    coverage = (events[-1].payload or {}).get("brief_coverage") or {}
    assert coverage.get("reason") == "approval_requirements_not_ready"
    assert not coverage["approval_readiness"]["ready"]
    assert not (events[-1].payload or {}).get("choice_question"), (
        "Missing request evidence cannot offer a waiver of unnamed requirements"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["source", "capability", "missing_assessment", "missing_requirement"]
)
async def test_direct_http_approval_rechecks_readiness_and_cannot_bypass_it(monkeypatch, mutation):
    user_id, thread_id, approvals, _ = await prepare(monkeypatch)
    assert len(approvals) == 1
    approval = approvals[0]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        draft = db.get(CreatorEditDraft, approval.draft_id)
        body = {
            "expected_thread_revision": thread.revision,
            "expected_draft_revision": approval.draft_revision,
            "expected_approval_fingerprint": approval_fingerprint(approval),
        }
        if mutation == "source":
            item = db.get(PlanItem, thread.active_plan_item_id)
            item.clip_assignments = [{**item.clip_assignments[0], "gcs_generation": "replaced"}]
        elif mutation == "missing_assessment":
            snapshot = dict(draft.snapshot_json)
            coverage = dict(snapshot.get("brief_coverage") or {})
            coverage.pop("approval_readiness", None)
            draft.snapshot_json = {**snapshot, "brief_coverage": coverage}
        elif mutation == "missing_requirement":
            snapshot = dict(draft.snapshot_json)
            coverage = dict(snapshot.get("brief_coverage") or {})
            report = dict(coverage.get("approval_readiness") or {})
            report["requirements"] = (report.get("requirements") or [])[:1]
            draft.snapshot_json = {
                **snapshot,
                "brief_coverage": {**coverage, "approval_readiness": report},
            }
        db.commit()
    if mutation == "capability":
        monkeypatch.setattr(settings, "edit_format_day_vlog_enabled", False)
    # A stamped assessment remains mandatory after enrollment is turned off.
    if mutation in {"source", "capability", "missing_requirement"} and hasattr(
        settings, "kria_approval_readiness_enabled"
    ):
        monkeypatch.setattr(settings, "kria_approval_readiness_enabled", False)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, email="qa@test.local"
    )
    published = []
    monkeypatch.setattr("app.routes.kria_runtime._publish_approval", published.append)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await client.post(
            f"/creation-threads/{thread_id}/approvals/{approval.id}/approve", json=body
        )
        replay = await client.post(
            f"/creation-threads/{thread_id}/approvals/{approval.id}/approve", json=body
        )
    assert result.status_code == 409, result.text
    assert result.json()["problem"]["code"] == "approval_requirements_not_ready"
    assert replay.status_code == 409
    assert published == []
    with sync_session() as db:
        assert db.get(CreatorAgentApproval, approval.id).status == "cancelled"
        assert (
            db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id).current_job_id
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["Make this simpler version", "Okay", "No, keep everything"])
@pytest.mark.parametrize("phone", [False, True])
async def test_only_named_omissions_can_remove_an_unrenderable_request(monkeypatch, answer, phone):
    extra = {"kind": "audio", "scope": "global", "description": "Remove only the wind noise"}
    user_id, thread_id, approvals, _ = await prepare(monkeypatch, extra=extra, phone=phone)
    assert not approvals
    _, _, approvals, _ = await prepare(
        monkeypatch,
        seed=(user_id, thread_id, None),
        message=answer,
        updates_override=[],
        phone=phone,
    )
    if answer == "Make this simpler version":
        assert len(approvals) == 1
        with sync_session() as db:
            snapshot = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json
            requirements = snapshot["brief_binding"]["brief"]["requirements"]
            assert next(r for r in requirements if r["id"] == "r3")["status"] == "superseded"
            assert snapshot["brief_coverage"]["approval_readiness"]["ready"]
    else:
        assert not approvals


@pytest.mark.asyncio
async def test_original_camera_audio_has_a_contract_commitment(monkeypatch):
    extra = {"kind": "audio", "scope": "global", "description": "Keep the clips' original sound"}
    _, _, approvals, events = await prepare(monkeypatch, extra=extra)
    assert len(approvals) == 1, events[-1].content
    with sync_session() as db:
        coverage = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json["brief_coverage"]
        report = coverage["approval_readiness"]
        row = next(r for r in report["requirements"] if r["requirement_id"] == "r3")
        assert row["disposition"] == "executable"
        assert row["evidence_owner"] == "render_contract:audio"


@pytest.mark.asyncio
async def test_enrollment_off_retains_legacy_unstamped_behavior(monkeypatch):
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", False)
    _, _, approvals, _ = await prepare(
        monkeypatch,
        extra={
            "kind": "audio",
            "scope": "global",
            "description": "Remove only the wind noise",
        },
    )
    assert len(approvals) == 1
    with sync_session() as db:
        coverage = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json["brief_coverage"]
        assert "approval_readiness" not in coverage


@pytest.mark.asyncio
@pytest.mark.parametrize("add_unsupported_glow", [False, True])
async def test_scoped_editor_approval_accounts_for_the_entire_request(
    monkeypatch, add_unsupported_glow
):
    from app.database import AsyncSessionLocal
    from app.kria.api_schemas import SubmitTurnBody
    from app.kria.runtime import submit_turn
    from tests.kria.test_plan_review import _seed_job

    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = _seed_job(user_id, session_id)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.clip_gcs_paths = ["users/test/matcha.mp4"]
        item.clip_assignments = [
            {
                "media_id": "clip-1",
                "gcs_path": item.clip_gcs_paths[0],
                "kind": "video",
                "duration_s": 8,
            }
        ]
        db.commit()
    updates = [
        {
            "kind": "style",
            "scope": "global",
            "description": "Make the title red",
            "facts": {
                "style_intent": {"target": "title", "set": [{"field": "color", "value": "#FF0000"}]}
            },
        }
    ]
    if add_unsupported_glow:
        updates.append(
            {"kind": "style", "scope": "global", "description": "Add a glow to the title"}
        )
    calls = []

    class Transport:
        def invoke(self, **kwargs):
            prompt = kwargs["prompt"]
            extraction = "request_scope" in prompt
            calls.append("extractor" if extraction else "copilot")
            output = (
                {"brief_updates": updates, "request_scope": "edit"}
                if extraction
                else {
                    "intent": "edit",
                    "confidence": 1,
                    "reply": "I prepared the title change.",
                    "claimed_ops": ["patch_text"],
                    "ops": [
                        {
                            "op": "patch_text",
                            "selector": {"ids": ["hook"]},
                            "target_ids": ["hook"],
                            "expected_count": 1,
                            "patch": {"color": "#FF0000"},
                        }
                    ],
                }
            )
            return ModelInvocation(raw_text=json.dumps(output))

    monkeypatch.setattr("app.kria.planner.default_client", lambda: Transport())
    monkeypatch.setattr("app.routes._copilot.default_client", lambda: Transport())
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Make the title red"
                + (" and add a glow to it" if add_unsupported_glow else ""),
                client_event_id="scoped-readiness",
                expected_thread_revision=2,
                scope=["title"],
            ),
        )
    await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert sorted(calls) == ["copilot", "extractor"]
    with sync_session() as db:
        approvals = list(
            db.scalars(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.thread_id == thread_id,
                    CreatorAgentApproval.status == "pending",
                )
            )
        )
        if add_unsupported_glow:
            assert not approvals
            assert (
                db.get(
                    PlanItem, db.get(CreationThread, thread_id).active_plan_item_id
                ).current_job_id
                == job_id
            )
        else:
            assert len(approvals) == 1
            report = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json[
                "brief_coverage"
            ]["approval_readiness"]
            assert report["ready"]
            assert [r["requirement_id"] for r in report["requirements"]] == ["r1"]

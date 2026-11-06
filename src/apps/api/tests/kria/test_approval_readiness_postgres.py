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
    no_title=False,
    clips=1,
    output_extra=None,
    summary=None,
    action=None,
    prompts=None,
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
        if clips > 1:
            # KRI-814: the Troya run had 5 and 28 ready clips.
            item.clip_gcs_paths = [f"users/test/troya-{n}.mp4" for n in range(1, clips + 1)]
            item.clip_assignments = [
                {
                    "media_id": f"clip-{n}",
                    "gcs_path": f"users/test/troya-{n}.mp4",
                    "kind": "video",
                    "duration_s": 8,
                    "analysis": {"summary": f"Troya ruins, shot {n}", "has_speech": False},
                }
                for n in range(1, clips + 1)
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
            "summary": summary or "A six-second Gameday edit.",
            "strategy": {
                "direction": "fast_montage" if phone else "guided_story",
                "edit_format": "montage" if phone else "day_vlog",
                "audio_strategy": "original_audio",
                "pacing": "balanced",
                "render_program": "guided",
                "opening_title": "Gameday.",
                "target_duration_s": 6 if clips == 1 else 30,
                "selected_media_ids": [f"clip-{n}" for n in range(1, clips + 1)],
                "rationale": "Use the stadium flag.",
            },
        },
        "brief_updates": updates,
        **(output_extra or {}),
    }
    if no_title:
        output["action"]["strategy"].pop("opening_title")
    if action is not None:
        output["action"] = action
    calls = []

    class Transport:
        def invoke(self, **kwargs):
            calls.append(kwargs["model"])
            if prompts is not None:
                prompts.append(kwargs["prompt"])
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


UNVERIFIED = {
    "en": "I can't check this before the video is made; I'll point it out after.",
    "tr": "Bunu video hazırlanmadan önce kontrol edemiyorum; sonra sana göstereceğim.",
}
LOOK = {"en": "Have a look at these in the video:", "tr": "Bunlara videoda bir göz at:"}


def _head_readiness(thread_id) -> dict:  # noqa: ANN001
    with sync_session() as db:
        draft = db.scalars(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
            )
        ).one()
        return draft.snapshot_json["brief_coverage"]["approval_readiness"]


async def _approve_over_http(monkeypatch, user_id, thread_id, approval) -> httpx.Response:  # noqa: ANN001
    with sync_session() as db:
        body = {
            "expected_thread_revision": db.get(CreationThread, thread_id).revision,
            "expected_draft_revision": approval.draft_revision,
            "expected_approval_fingerprint": approval_fingerprint(approval),
        }
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, email="qa@test.local"
    )
    monkeypatch.setattr("app.routes.kria_runtime._publish_approval", lambda _id: None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            f"/creation-threads/{thread_id}/approvals/{approval.id}/approve", json=body
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("phone", [False, True])
@pytest.mark.parametrize(
    ("extra", "outcome"),
    [
        # An English reader owns "remove ... noise" and cannot show it on this draft, so it
        # holds; nothing reads the Turkish wording, so there it is checked after render. The
        # phone unified montage judges the ask at render (route disclosure), so it is approved
        # there and named for a look; every other route keeps the hold.
        (
            {"kind": "audio", "scope": "global", "description": "Remove only the wind noise"},
            {"en": "held", "en_phone": "unverified", "tr": "unverified"},
        ),
        # KRI-983: no draft check owns these, so they are approved and named for a look.
        (
            {"kind": "audio", "scope": "global", "description": "original camera audio"},
            {"en": "unverified", "tr": "unverified"},
        ),
        (
            {
                "kind": "audio",
                "scope": "global",
                "description": "Keep the clips' original sound",
                "facts": {"audio_intent": "noise_removal"},
            },
            {"en": "unverified", "tr": "unverified"},
        ),
        (
            {
                "kind": "style",
                "scope": "global",
                "description": "Lift the shadows in the footage",
                "facts": {"footage_light": "brighter"},
            },
            {"en": "simpler", "tr": "simpler"},
        ),
        (
            {
                "kind": "select",
                "scope": "clip:clip-1",
                "description": "Use the exact moment when the flag unfolds",
            },
            {"en": "unverified", "tr": "unverified"},
        ),
    ],
)
async def test_each_unproven_request_is_held_simplified_or_left_to_check_after_render(
    monkeypatch, language, extra, outcome, phone
):
    if phone and "en_phone" in outcome:
        outcome = {**outcome, "en": outcome["en_phone"]}
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
    if outcome[language] == "simpler":
        assert len(approvals) == 1
        reply = next(event for event in events if event.event_type == "draft_applied")
        assert extra["description"] in reply.content
        assert ("Left out:" if language == "en" else "Çıkarıldı:") in reply.content
        assert "Should I" not in reply.content and "yapayım mı" not in reply.content
        with sync_session() as db:
            draft = db.scalars(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
                )
            ).one()
            snapshot = draft.snapshot_json
            assert snapshot["strategy"]["opening_title"] == "Gameday."
            assert snapshot["brief_coverage"]["automatic_omission_ids"] == ["r3"]
            report = snapshot["brief_coverage"]["approval_readiness"]
            assert report["ready"] is True
            assert {r["requirement_id"] for r in report["requirements"]} == {"r1", "r2"}
            assert approvals[0].consequence_summary.endswith(reply.content)
        return
    if outcome[language] == "unverified":
        assert len(approvals) == 1
        reply = next(event for event in events if event.event_type == "draft_applied")
        assert f"{LOOK[language]}\n" in reply.content
        assert f"- {extra['description']}" in reply.content.split(LOOK[language], 1)[1]
        report = _head_readiness(thread_id)
        assert report["ready"] is True
        rows = {row["requirement_id"]: row for row in report["requirements"]}
        assert {key: row["disposition"] for key, row in rows.items()} == {
            "r1": "executable",
            "r2": "executable",
            "r3": "unverified",
        }
        assert rows["r3"]["reason"] == UNVERIFIED[language]
        return
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


DREAMY = {"kind": "style", "scope": "global", "description": "Make it feel dreamy"}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "tr"])
@pytest.mark.parametrize("phone", [False, True])
async def test_an_ask_nothing_checks_before_render_is_approved_and_named_for_a_look(
    monkeypatch, language, phone
):
    """KRI-983: "make it feel dreamy" has no draft check; it is approved, not dead-ended."""
    user_id, thread_id, approvals, events = await prepare(
        monkeypatch, language=language, extra=DREAMY, phone=phone
    )
    assert len(approvals) == 1, events[-1].content
    reply = next(event for event in events if event.event_type == "draft_applied")
    assert f"- {DREAMY['description']}" in reply.content.split(LOOK[language], 1)[1]
    report = _head_readiness(thread_id)
    r3 = next(row for row in report["requirements"] if row["requirement_id"] == "r3")
    assert (report["ready"], r3["disposition"], r3["reason"]) == (
        True,
        "unverified",
        UNVERIFIED[language],
    )
    result = await _approve_over_http(monkeypatch, user_id, thread_id, approvals[0])
    assert (result.status_code, result.json()["status"]) == (200, "approved"), result.text


@pytest.mark.asyncio
@pytest.mark.parametrize("phone", [False, True])
async def test_a_known_miss_still_holds_beside_an_unchecked_ask(monkeypatch, phone):
    """Negative control: the freeze checker says no before render, so the draft holds."""
    freeze = {"kind": "style", "scope": "global", "description": "Freeze the last frame"}
    _, _, approvals, events = await prepare(
        monkeypatch, updates_override=[DREAMY, freeze], phone=phone
    )
    assert not approvals
    rows = {
        row["requirement_id"]: row["disposition"]
        for row in events[-1].payload["brief_coverage"]["approval_readiness"]["requirements"]
    }
    assert rows == {"r1": "unverified", "r2": "unsupported"}


@pytest.mark.asyncio
async def test_an_unchecked_ask_never_outweighs_a_policy_problem(monkeypatch):
    """Negative control: the edit's renderer is gone at approval; nothing unverified passes."""
    user_id, thread_id, approvals, _ = await prepare(monkeypatch, extra=DREAMY)
    assert len(approvals) == 1
    monkeypatch.setattr(settings, "edit_format_day_vlog_enabled", False)
    result = await _approve_over_http(monkeypatch, user_id, thread_id, approvals[0])
    assert result.status_code == 409, result.text
    assert result.json()["problem"]["code"] == "approval_requirements_not_ready"


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
@pytest.mark.parametrize("message", ["Suggest an edit.", "suggest an edit"])
async def test_stock_open_request_reaches_approval_without_a_brief(monkeypatch, message):
    """KRI-774: the app's media-only message / chip #1 states no requirement by construction.

    Prod thread fcbdd70d: no brief row existed, so the draft was held back with "I need to
    establish the requested changes". The creator delegated every choice; nothing is missing.
    """
    user_id, thread_id, approvals, events = await prepare(
        monkeypatch, updates_override=[], message=message, no_title=True
    )
    coverage = (events[-1].payload or {}).get("brief_coverage") or {}
    assert len(approvals) == 1, coverage
    assert coverage.get("reason") != "approval_requirements_not_ready"
    approval = approvals[0]
    with sync_session() as db:
        draft = db.get(CreatorEditDraft, approval.draft_id)
        readiness = draft.snapshot_json["brief_coverage"]["approval_readiness"]
        assert readiness["ready"]
        assert readiness["requirements"] == []
        assert draft.snapshot_json["brief_binding"]["state"] == "none"
        body = {
            "expected_thread_revision": db.get(CreationThread, thread_id).revision,
            "expected_draft_revision": approval.draft_revision,
            "expected_approval_fingerprint": approval_fingerprint(approval),
        }
    # The real approval endpoint rechecks readiness from the saved binding alone.
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
async def test_only_the_stock_sentence_is_exempt_from_the_empty_request_block(monkeypatch):
    """Negative control for KRI-774: any other message with no extracted requirements stays
    blocked (it states a request, so an empty extraction is missing evidence)."""
    _, _, approvals, events = await prepare(
        monkeypatch,
        updates_override=[],
        message='Use the title "Gameday." and make the edit 6 seconds.',
    )
    assert not approvals
    coverage = (events[-1].payload or {}).get("brief_coverage") or {}
    assert coverage.get("reason") == "approval_requirements_not_ready"
    assert not coverage["approval_readiness"]["ready"]


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
    # The phone unified montage approves the wind-noise ask (route disclosure); this pins the
    # consent rule on the hold, which that route's old behaviour is with the switch off.
    monkeypatch.setattr(settings, "kria_route_disclosure_enabled", not phone)
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
@pytest.mark.parametrize("phone", [False, True])
async def test_dropping_wind_cleanup_preserves_typed_source_audio(monkeypatch, phone):
    monkeypatch.setattr(settings, "kria_route_disclosure_enabled", not phone)  # see above
    updates = [
        {"kind": "text", "scope": "title", "literal": "Gameday."},
        {
            "kind": "timing",
            "scope": "global",
            "description": "6 seconds",
            "facts": {"duration_s": 6},
        },
        {
            "kind": "audio",
            "scope": "global",
            "description": "original camera audio",
            "facts": {"audio_intent": "source_unchanged"},
        },
        {"kind": "audio", "scope": "global", "description": "Remove only the wind noise"},
    ]
    user_id, thread_id, approvals, events = await prepare(
        monkeypatch, updates_override=updates, phone=phone
    )
    assert not approvals
    report = events[-1].payload["brief_coverage"]["approval_readiness"]
    assert {
        r["requirement_id"] for r in report["requirements"] if r["disposition"] == "executable"
    } == {"r1", "r2", "r3"}
    _, _, approvals, _ = await prepare(
        monkeypatch,
        seed=(user_id, thread_id, None),
        message="Make this simpler version",
        updates_override=[],
        phone=phone,
    )
    assert len(approvals) == 1
    with sync_session() as db:
        snapshot = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json
        requirements = snapshot["brief_binding"]["brief"]["requirements"]
        assert next(r for r in requirements if r["id"] == "r4")["status"] == "superseded"
        source_audio = next(r for r in requirements if r["id"] == "r3")
        assert source_audio["status"] != "superseded"
        assert source_audio["facts"]["audio_intent"] == "source_unchanged"
        assert snapshot["brief_coverage"]["approval_readiness"]["ready"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phone", [False, True])
@pytest.mark.parametrize(
    "description,facts",
    [
        ("Keep the clips' original sound", {}),
        ("original camera audio", {"audio_intent": "source_unchanged"}),
        ("kameranın özgün sesi", {"audio_intent": "source_unchanged"}),
    ],
)
async def test_original_camera_audio_has_a_contract_commitment(
    monkeypatch, phone, description, facts
):
    extra = {"kind": "audio", "scope": "global", "description": description, "facts": facts}
    user_id, thread_id, approvals, events = await prepare(monkeypatch, extra=extra, phone=phone)
    assert len(approvals) == 1, events[-1].content
    with sync_session() as db:
        coverage = db.get(CreatorEditDraft, approvals[0].draft_id).snapshot_json["brief_coverage"]
        report = coverage["approval_readiness"]
        row = next(r for r in report["requirements"] if r["requirement_id"] == "r3")
        assert row["disposition"] == "executable"
        assert row["evidence_owner"] == "render_contract:audio"
        approval = approvals[0]
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

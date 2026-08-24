"""Route tests for Phase 5 plan-item upload + generation endpoints (mock-DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.schemas.edit_proposal import (
    ApprovedProposalSnapshot,
    EditProposal,
    EditProposalSnapshot,
    MediaRef,
    ProposalBrief,
    StoryBeat,
    canonical_media_digest,
)
from app.tasks.content_plan_build import DispatchResult


def _patch_dispatch_ok():
    """Stub the plans/014 sync dispatch helper with a successful outcome."""
    return patch(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        return_value=DispatchResult("dispatched", job_id=str(uuid.uuid4())),
    )


def _user() -> MagicMock:
    u = MagicMock()
    u.id = uuid.uuid4()
    return u


def _async_db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.get = AsyncMock(return_value=None)
    db.expire_all = MagicMock()
    return db


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def teardown_function() -> None:
    app.dependency_overrides.clear()


def _owned_item(user_id: uuid.UUID, *, clips=None, filming_guide=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = clips or []
    item.day_index = 1
    item.theme = "t"
    item.idea = "i"
    item.filming_suggestion = None
    item.rationale = None
    item.filming_guide = filming_guide if filming_guide is not None else []
    item.current_job_id = None
    item.current_job = None
    item.item_status = "idea"
    item.user_edited = False
    # M4: explicit None so plan_item_response does not mistake MagicMock for a dict.
    item.conformance = None
    # 0055: new nullable/added fields.
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None
    item.source_idea_seed_text = None
    item.edit_format = None
    item.smart_captions_enabled = False
    item.montage_preset = "classic"
    item.voiceover_gcs_path = None
    item.audio_mode = "kria"
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None
    plan = MagicMock()
    plan.id = item.content_plan_id
    plan.user_id = user_id
    plan.persona_id = uuid.uuid4()
    plan.ownership_epoch = 0
    plan.ownership_quarantined_at = None
    return item, plan


def _db_for(item, plan, *, assignment=None) -> AsyncMock:
    """DB mock matching _load_owned_item: the item is loaded via
    execute().scalar_one_or_none() (eager-loads current_job), the plan
    ownership check via get().

    M4: also handles _get_instruction_level's Persona get() call —
    returns a mock persona with style=None so instruction_level defaults to 'full'.
    """
    db = _async_db()
    persona_mock = MagicMock()
    persona_mock.id = plan.persona_id
    persona_mock.user_id = plan.user_id
    persona_mock.persona = {}
    persona_mock.idea_seeds = []
    persona_mock.style = None  # → instruction_level defaults to "full"
    plan._owned_persona_fixture = persona_mock

    async def _execute(stmt, *_args, **_kwargs):  # noqa: ANN001
        value = persona_mock if "FROM personas" in str(stmt) else item
        return MagicMock(scalar_one_or_none=MagicMock(return_value=value))

    db.execute = AsyncMock(side_effect=_execute)

    # get() is called with different classes: ContentPlan (returns plan),
    # Persona (returns persona_mock). Use side_effect to differentiate.
    from app.models import (  # noqa: PLC0415
        CreatorStyleAssignment,
    )
    from app.models import (
        Persona as PersonaRow,
    )

    async def _get_side_effect(cls, pk, **_kwargs):
        if cls is PersonaRow:
            return persona_mock
        if cls is CreatorStyleAssignment:
            return assignment
        return plan

    db.get = AsyncMock(side_effect=_get_side_effect)
    return db


def test_clip_upload_urls_rejects_batch_over_cap(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    response = client.post(
        f"/plan-items/{item.id}/upload-urls",
        json={
            "files": [
                {"filename": f"clip-{i}.mp4", "content_type": "video/mp4", "file_size_bytes": 1}
                for i in range(51)
            ]
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail == {
        "code": "clip_upload_limit_exceeded",
        "message": "This item is capped at 50 clips. You currently have 0; you can add 50 more.",
        "limit": 50,
        "current": 0,
        "requested": 51,
        "remaining": 50,
    }


def test_clip_upload_urls_counts_existing_clips_before_signing(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"existing-{i}" for i in range(49)])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    response = client.post(
        f"/plan-items/{item.id}/upload-urls",
        json={
            "files": [
                {"filename": "a.mp4", "content_type": "video/mp4", "file_size_bytes": 1},
                {"filename": "b.mp4", "content_type": "video/mp4", "file_size_bytes": 1},
            ]
        },
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "clip_upload_limit_exceeded"
    assert detail["limit"] == 50
    assert detail["current"] == 49
    assert detail["requested"] == 2
    assert detail["remaining"] == 1
    assert detail["message"] == (
        "This item is capped at 50 clips. You currently have 49; you can add 1 more."
    )


def test_attach_clips_returns_capacity_detail_when_final_state_exceeds_cap(
    client: TestClient,
) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.clip_assignments = []
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    paths = [f"users/{user.id}/plan/{item.id}/clip-{i}.mp4" for i in range(51)]

    response = client.post(
        f"/plan-items/{item.id}/clips",
        json={"clip_gcs_paths": paths},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "clip_upload_limit_exceeded"
    assert detail["limit"] == 50
    assert detail["current"] == 51
    assert detail["remaining"] == 0
    assert detail["message"] == (
        "This item is capped at 50 clips. You currently have 51; you can add 0 more."
    )


def test_generate_requires_clips(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, clips=[])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 409


def test_generate_allows_current_approved_asset_only_story(monkeypatch, client: TestClient) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[])
    media = MediaRef(
        lane="asset",
        media_id=str(uuid.uuid4()),
        gcs_path=f"users/{user.id}/plan/{item.id}/pool/town.mp4",
        generation="51",
        kind="video",
    )
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="Tell the trip in five places",
        pace="relaxed",
        duration_s=10,
        title="Summer 26",
        media=[media],
        story_beats=[
            StoryBeat(
                beat_id="lisbon",
                topic="Lisbon",
                thought="Lisbon",
                media_ids=[media.media_id],
                duration_s=2,
            )
        ],
    )
    digest = canonical_media_digest(snapshot.media)
    item.edit_proposal = EditProposal(
        proposal_version=3,
        generation_attempt_id="attempt-1",
        media_digest=digest,
        status="approved",
        draft=snapshot,
        last_approved=ApprovedProposalSnapshot(
            proposal_version=3,
            media_digest=digest,
            approved_at=datetime.now(UTC),
            snapshot=snapshot,
        ),
    ).model_dump(mode="json")
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with _patch_dispatch_ok() as dispatch:
        response = client.post(f"/plan-items/{item.id}/generate")

    assert response.status_code == 200
    dispatch.assert_called_once_with(str(item.id), 0)


def test_collection_shape_omits_full_proposal_and_preview_signing(monkeypatch) -> None:
    from app.routes import plan_items as routes  # noqa: PLC0415

    user = _user()
    item, _plan = _owned_item(user.id)
    item.clip_assignments = []
    item.edit_proposal = EditProposal(
        proposal_version=1,
        generation_attempt_id="attempt-1",
        status="analyzing",
    ).model_dump(mode="json")
    monkeypatch.setattr(
        routes,
        "_edit_proposal_response",
        lambda _item: (_ for _ in ()).throw(AssertionError("must not sign list previews")),
    )

    response = routes.plan_item_response(item, include_edit_proposal=False)

    assert response.edit_proposal is None


@pytest.mark.parametrize(
    ("proposal_status", "expected_code"),
    [
        (None, "proposal_required"),
        ("analyzing", "proposal_analyzing"),
        ("drafting", "proposal_analyzing"),
        ("draft", "proposal_draft"),
        ("failed", "proposal_failed"),
        ("stale", "proposal_stale"),
    ],
)
def test_generate_returns_explicit_proposal_gate_codes(
    monkeypatch,
    client: TestClient,
    proposal_status: str | None,
    expected_code: str,
) -> None:
    """Kill-switch pin: GUIDED_AUTO_DESIGN_ENABLED=False keeps today's strict

    enforcement byte-identical (explicit proposal_generate_error 409s), even
    though the item has media the auto-design flow would otherwise pick up.
    """

    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", True)
    monkeypatch.setattr(app_settings, "guided_auto_design_enabled", False)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_proposal = None
    if proposal_status is not None:
        item.edit_proposal = EditProposal(
            proposal_version=1,
            generation_attempt_id="attempt-1",
            status=proposal_status,
            brief=ProposalBrief(),
        ).model_dump(mode="json")
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    response = client.post(f"/plan-items/{item.id}/generate")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == expected_code


def test_generate_auto_designs_instead_of_409_when_flag_on_and_media_present(
    monkeypatch, client: TestClient
) -> None:
    """GUIDED_AUTO_DESIGN_ENABLED: no proposal + media present -> 200

    "designing" (reserve with approval_mode="auto" + enqueue
    draft_edit_proposal, which reads the auto-finalize intent off that
    persisted envelope — no task kwarg, P2-6), never the 409 a plain
    proposal_generate_error would raise.
    """

    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", True)
    monkeypatch.setattr(app_settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(app_settings, "guided_auto_design_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_proposal = None
    item.clip_assignments = [
        {"gcs_path": f"users/{user.id}/plan/0/a.mp4", "shot_id": None, "user_note": ""}
    ]
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kw: apply_calls.append(kw),
    )

    response = client.post(f"/plan-items/{item.id}/generate")

    assert response.status_code == 200
    assert len(apply_calls) == 1
    assert "auto_finalize" not in apply_calls[0].get("kwargs", {})  # P2-6: no task kwarg
    body = response.json()
    assert body["guided_edit_auto_design"] is True
    assert body["edit_proposal"]["status"] == "analyzing"
    assert body["edit_proposal"]["approval_mode"] == "auto"


def test_generate_auto_design_ignores_direction_confirmation_rollout_flag(
    monkeypatch, client: TestClient
) -> None:
    """The primary Generate path never pauses for an inferred direction."""

    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", False)
    monkeypatch.setattr(app_settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(app_settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(app_settings, "guided_edit_direction_confirmation_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_proposal = None
    item.clip_assignments = [
        {"gcs_path": f"users/{user.id}/plan/0/a.mp4", "shot_id": None, "user_note": ""}
    ]
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kw: apply_calls.append(kw),
    )

    response = client.post(f"/plan-items/{item.id}/generate")

    assert response.status_code == 200
    assert len(apply_calls) == 1
    body = response.json()
    assert body["edit_proposal"]["status"] == "analyzing"
    assert body["edit_proposal"]["approval_mode"] == "auto"
    assert body["edit_proposal"].get("guidance") is None


def test_generate_confirmation_without_capability_fails_closed_without_unconfirmable_attempt(
    monkeypatch, client: TestClient
) -> None:
    """Flag skew must not create a proposal the UI cannot show or confirm."""

    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", False)
    monkeypatch.setattr(app_settings, "guided_edit_capability_enabled", False)
    monkeypatch.setattr(app_settings, "guided_edit_direction_confirmation_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_proposal = None
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with (
        patch(
            "app.tasks.content_plan_build.dispatch_item_render_for",
            return_value=DispatchResult("proposal_required"),
        ) as dispatch,
        patch("app.tasks.edit_proposal_build.draft_edit_proposal.apply_async") as draft_task,
    ):
        response = client.post(f"/plan-items/{item.id}/generate")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "proposal_required"
    dispatch.assert_called_once_with(str(item.id), 0)
    draft_task.assert_not_called()
    assert item.edit_proposal is None


def test_generate_audio_led_bypasses_guided_gate_and_hides_capabilities(
    monkeypatch, client: TestClient
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", True)
    monkeypatch.setattr(app_settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(app_settings, "guided_auto_design_enabled", True)
    monkeypatch.setattr(app_settings, "guided_edit_conversation_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_format = "narrated_ready"
    item.audio_mode = "voiceover"
    item.voiceover_gcs_path = "voiceover-uploads/u/voice.m4a"
    item.edit_proposal = EditProposal(
        proposal_version=1,
        generation_attempt_id="draft-exists",
        status="draft",
        brief=ProposalBrief(),
    ).model_dump(mode="json")
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with (
        _patch_dispatch_ok() as dispatch,
        patch("app.tasks.edit_proposal_build.draft_edit_proposal.apply_async") as draft_task,
    ):
        response = client.post(f"/plan-items/{item.id}/generate")

    assert response.status_code == 200
    dispatch.assert_called_once_with(str(item.id), 0)
    draft_task.assert_not_called()
    body = response.json()
    assert body["guided_edit_available"] is False
    assert body["guided_edit_conversation_available"] is False
    assert body["guided_edit_auto_design"] is False
    assert body["edit_proposal"]["status"] == "draft"


def test_generate_audio_led_does_not_use_asset_only_guided_media(
    monkeypatch, client: TestClient
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", True)
    monkeypatch.setattr(app_settings, "guided_edit_capability_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[])
    item.edit_format = "narrated_ready"
    item.audio_mode = "voiceover"
    item.voiceover_gcs_path = "voiceover-uploads/u/voice.m4a"
    media = MediaRef(
        lane="asset",
        media_id=str(uuid.uuid4()),
        gcs_path=f"users/{user.id}/plan/{item.id}/pool/seed.mp4",
        generation="1",
        kind="video",
    )
    snapshot = EditProposalSnapshot(
        title="Dormant",
        duration_s=5,
        media=[media],
        story_beats=[
            StoryBeat(
                beat_id="b1",
                topic="scene",
                thought="scene",
                media_ids=[media.media_id],
                duration_s=2,
            )
        ],
    )
    digest = canonical_media_digest(snapshot.media)
    item.edit_proposal = EditProposal(
        proposal_version=1,
        generation_attempt_id="approved",
        media_digest=digest,
        status="approved",
        last_approved=ApprovedProposalSnapshot(
            proposal_version=1,
            media_digest=digest,
            approved_at=datetime.now(UTC),
            snapshot=snapshot,
        ),
    ).model_dump(mode="json")
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    response = client.post(f"/plan-items/{item.id}/generate")

    assert response.status_code == 409
    assert response.json()["detail"] == "Upload at least one clip before generating"


def test_generate_auto_design_in_flight_is_idempotent_no_duplicate_attempt(
    monkeypatch, client: TestClient
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", True)
    monkeypatch.setattr(app_settings, "guided_edit_capability_enabled", True)
    monkeypatch.setattr(app_settings, "guided_auto_design_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.clip_assignments = [
        {"gcs_path": f"users/{user.id}/plan/0/a.mp4", "shot_id": None, "user_note": ""}
    ]
    item.edit_proposal = EditProposal(
        proposal_version=1,
        generation_attempt_id="attempt-already-running",
        status="analyzing",
        approval_mode="auto",
        brief=ProposalBrief(),
    ).model_dump(mode="json")
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kw: apply_calls.append(kw),
    )

    response = client.post(f"/plan-items/{item.id}/generate")

    assert response.status_code == 200
    assert apply_calls == []  # no duplicate attempt/task
    body = response.json()
    assert body["edit_proposal"]["status"] == "analyzing"
    assert body["edit_proposal"]["generation_attempt_id"] == "attempt-already-running"


def test_generate_enqueues_when_clips_present(client: TestClient) -> None:
    # plans/014: the sync path dispatches in-request via dispatch_item_render_for
    # (real-DB coverage lives in test_plan_item_sync_dispatch.py; here we pin
    # only that the route routes through the helper).
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{0}/plan/0/a.mp4"])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with _patch_dispatch_ok() as dispatch:
        resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 200
    dispatch.assert_called_once_with(str(item.id), 0)


def test_generate_unknown_dispatch_outcome_is_500(client: TestClient) -> None:
    """CA3/M1 pin: a future DispatchOutcome the route doesn't map must surface
    as an explicit 500 — never fall through to the 200 success path (the
    silent-no-op class plans/014 exists to kill)."""
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        # Literal is not runtime-enforced on the frozen dataclass — exactly the
        # hole a future outcome value would slip through.
        return_value=DispatchResult("future_outcome"),  # type: ignore[arg-type]
    ):
        resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 500


def test_generate_missing_row_outcome_is_404(client: TestClient) -> None:
    """Route mapping pin: missing_row (item deleted between the ownership load
    and the helper's locked re-load — TOCTOU) surfaces as 404, not success."""
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        return_value=DispatchResult("missing_row"),
    ):
        resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 404


def test_generate_rejects_photo_clip_for_classic_montage(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/still.jpg"])
    item.montage_preset = "classic"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 422
    assert "Photo collage" in resp.json()["detail"]
    assert "remove it from Main footage" in resp.json()["detail"]


def test_generate_auto_design_never_bypasses_narrated_voiceover_requirement(
    monkeypatch, client: TestClient
) -> None:
    """P1-2 (2026-08-18 adversarial review): _maybe_auto_design_generate must

    never get a chance to intercept a narrated item with no voiceover and
    self-narration off — that would re-open the "started a narrated render
    with no audio" dogfood bug via auto-design's own clip-only montage
    fallback, which dispatches through the exact same legacy path.
    """

    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "narrated_self_narration_enabled", False, raising=False)
    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", True)
    monkeypatch.setattr(app_settings, "guided_auto_design_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_format = "narrated_ready"
    item.voiceover_gcs_path = None
    item.clip_assignments = [
        {"gcs_path": f"users/{user.id}/plan/0/a.mp4", "shot_id": None, "user_note": ""}
    ]
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kw: apply_calls.append(kw),
    )

    resp = client.post(f"/plan-items/{item.id}/generate")

    assert resp.status_code == 409
    assert "voiceover" in resp.json()["detail"].lower()
    assert apply_calls == []  # auto-design never even started


def test_generate_auto_design_never_bypasses_photo_collage_requirement(
    monkeypatch, client: TestClient
) -> None:
    """P1-2: raw photos without a collage preset must still 422 even with

    auto-design on and an eligible (but unapproved) guided-edit state.
    """

    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "guided_edit_enforcement_enabled", True)
    monkeypatch.setattr(app_settings, "guided_auto_design_enabled", True)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/still.jpg"])
    item.montage_preset = "classic"
    item.clip_assignments = [
        {"gcs_path": f"users/{user.id}/plan/0/still.jpg", "shot_id": None, "user_note": ""}
    ]
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    apply_calls: list[dict] = []
    monkeypatch.setattr(
        "app.tasks.edit_proposal_build.draft_edit_proposal.apply_async",
        lambda **kw: apply_calls.append(kw),
    )

    resp = client.post(f"/plan-items/{item.id}/generate")

    assert resp.status_code == 422
    assert "Photo collage" in resp.json()["detail"]
    assert "remove it from Main footage" in resp.json()["detail"]
    assert apply_calls == []


def test_generate_blocks_narrated_without_voiceover(monkeypatch, client: TestClient) -> None:
    """Self-narration OFF (default): a narrated item with clips but NO voiceover is
    rejected (the spine is the narration). Kill-switch pin for the pre-self-narration
    behavior — reproduces the 'started a narrated render with no audio' bug."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "narrated_self_narration_enabled", False, raising=False)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_format = "narrated_ready"
    item.voiceover_gcs_path = None
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 409
    assert "voiceover" in resp.json()["detail"].lower()


def test_generate_allows_narrated_without_voiceover_when_self_narration_on(
    monkeypatch, client: TestClient
) -> None:
    """Self-narration ON: the voiceover requirement lifts — clips alone dispatch the
    render and _resolve_archetype routes by the footage's own speech."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "narrated_self_narration_enabled", True, raising=False)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_format = "narrated_ready"
    item.voiceover_gcs_path = None
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with _patch_dispatch_ok() as dispatch:
        resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 200
    dispatch.assert_called_once_with(str(item.id), 0)


def test_generate_voiceover_mode_requires_recording_even_with_self_narration_on(
    monkeypatch, client: TestClient
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "narrated_self_narration_enabled", True, raising=False)
    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_format = "narrated_ready"
    item.audio_mode = "voiceover"
    item.voiceover_gcs_path = None
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with _patch_dispatch_ok() as dispatch:
        resp = client.post(f"/plan-items/{item.id}/generate")

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Record or upload your final voiceover before generating"
    dispatch.assert_not_called()


def test_generate_self_narration_on_still_requires_clips(monkeypatch, client: TestClient) -> None:
    """Self-narration ON does not relax the clips requirement — zero clips still 409."""
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "narrated_self_narration_enabled", True, raising=False)
    user = _user()
    item, plan = _owned_item(user.id, clips=[])
    item.edit_format = "narrated_ready"
    item.voiceover_gcs_path = None
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 409


def test_generate_allows_narrated_with_voiceover(client: TestClient) -> None:

    user = _user()
    item, plan = _owned_item(user.id, clips=[f"users/{user.id}/plan/0/a.mp4"])
    item.edit_format = "narrated_ready"
    item.voiceover_gcs_path = "voiceover-uploads/abc/voice.m4a"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with _patch_dispatch_ok() as dispatch:
        resp = client.post(f"/plan-items/{item.id}/generate")
    assert resp.status_code == 200
    dispatch.assert_called_once_with(str(item.id), 0)


def test_set_voiceover_stores_path_and_selects_voiceover_audio_mode(
    monkeypatch, client: TestClient
) -> None:
    from app.config import settings as app_settings
    from app.storage import ObjectMetadata

    monkeypatch.setattr(
        app_settings, "generative_direct_voiceover_strict_enabled", True, raising=False
    )
    user = _user()
    item, plan = _owned_item(user.id)
    item.voiceover_gcs_path = None
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    gcs_path = f"voiceover-uploads/direct/{user.id}/some-vo.webm"
    with patch(
        "app.routes.plan_items.storage.object_metadata",
        return_value=ObjectMetadata(
            path=gcs_path,
            generation="1",
            etag=None,
            size=10_000,
            content_type="audio/webm",
        ),
    ):
        resp = client.patch(
            f"/plan-items/{item.id}/voiceover", json={"voiceover_gcs_path": gcs_path}
        )
    assert resp.status_code == 200
    assert item.voiceover_gcs_path == gcs_path
    assert item.audio_mode == "voiceover"


@pytest.mark.parametrize("path_kind", ["legacy", "synthetic_direct"])
def test_set_voiceover_flag_off_accepts_metadata_validated_old_client_path(
    path_kind: str, monkeypatch, client: TestClient
) -> None:
    from app.auth import SYNTHETIC_USER_ID
    from app.config import settings as app_settings
    from app.storage import ObjectMetadata

    monkeypatch.setattr(
        app_settings, "generative_direct_voiceover_strict_enabled", False, raising=False
    )
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    gcs_path = (
        "voiceover-uploads/legacy/old-recorder.webm"
        if path_kind == "legacy"
        else f"voiceover-uploads/direct/{SYNTHETIC_USER_ID}/old-recorder/voice.webm"
    )
    with patch(
        "app.routes.plan_items.storage.object_metadata",
        return_value=ObjectMetadata(
            path=gcs_path,
            generation="1",
            etag=None,
            size=10_000,
            content_type="audio/webm",
        ),
    ) as metadata:
        resp = client.patch(
            f"/plan-items/{item.id}/voiceover", json={"voiceover_gcs_path": gcs_path}
        )

    assert resp.status_code == 200
    metadata.assert_called_once_with(gcs_path)
    assert item.voiceover_gcs_path == gcs_path
    assert item.audio_mode == "voiceover"


def test_set_voiceover_rejects_bad_prefix(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.patch(
        f"/plan-items/{item.id}/voiceover", json={"voiceover_gcs_path": "music/bad.mp3"}
    )
    assert resp.status_code == 422


def test_set_voiceover_strict_mode_rejects_another_users_direct_recording(
    monkeypatch, client: TestClient
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(
        app_settings, "generative_direct_voiceover_strict_enabled", True, raising=False
    )
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/plan-items/{item.id}/voiceover",
        json={"voiceover_gcs_path": f"voiceover-uploads/direct/{uuid.uuid4()}/take.webm"},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Voiceover recording does not belong to this account"


def test_set_voiceover_flag_off_still_rejects_another_users_direct_recording(
    monkeypatch, client: TestClient
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(
        app_settings, "generative_direct_voiceover_strict_enabled", False, raising=False
    )
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.routes.plan_items.storage.object_metadata") as metadata:
        resp = client.patch(
            f"/plan-items/{item.id}/voiceover",
            json={"voiceover_gcs_path": f"voiceover-uploads/direct/{uuid.uuid4()}/take.webm"},
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Voiceover recording does not belong to this account"
    metadata.assert_not_called()


@pytest.mark.parametrize("path_kind", ["legacy", "synthetic_direct"])
def test_set_voiceover_strict_mode_rejects_old_client_paths_before_metadata(
    path_kind: str, monkeypatch, client: TestClient
) -> None:
    from app.auth import SYNTHETIC_USER_ID
    from app.config import settings as app_settings

    monkeypatch.setattr(
        app_settings, "generative_direct_voiceover_strict_enabled", True, raising=False
    )
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    gcs_path = (
        "voiceover-uploads/legacy/old-recorder.webm"
        if path_kind == "legacy"
        else f"voiceover-uploads/direct/{SYNTHETIC_USER_ID}/old-recorder/voice.webm"
    )
    with patch("app.routes.plan_items.storage.object_metadata") as metadata:
        resp = client.patch(
            f"/plan-items/{item.id}/voiceover", json={"voiceover_gcs_path": gcs_path}
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Voiceover recording does not belong to this account"
    metadata.assert_not_called()


def test_set_voiceover_flag_off_rejects_non_audio_object(monkeypatch, client: TestClient) -> None:
    from app.config import settings as app_settings
    from app.storage import ObjectMetadata

    monkeypatch.setattr(
        app_settings, "generative_direct_voiceover_strict_enabled", False, raising=False
    )
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    gcs_path = "voiceover-uploads/legacy/not-audio.mp4"
    with patch(
        "app.routes.plan_items.storage.object_metadata",
        return_value=ObjectMetadata(
            path=gcs_path,
            generation="1",
            etag=None,
            size=10_000,
            content_type="video/mp4",
        ),
    ):
        resp = client.patch(
            f"/plan-items/{item.id}/voiceover", json={"voiceover_gcs_path": gcs_path}
        )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Voiceover recording must be audio"
    assert item.voiceover_gcs_path is None
    assert item.audio_mode == "kria"


def test_set_voiceover_clears_with_null(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.voiceover_gcs_path = "voiceover-uploads/old.webm"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.patch(f"/plan-items/{item.id}/voiceover", json={"voiceover_gcs_path": None})
    assert resp.status_code == 200
    assert item.voiceover_gcs_path is None


def test_direction_audio_upload_url_uses_owned_separate_prefix(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    expected_path = f"users/{user.id}/plan/{item.id}/direction-audio/note.webm"
    with patch(
        "app.storage.presigned_put_url_for_plan_item",
        return_value=("https://signed.example/direction", expected_path),
    ) as signed:
        resp = client.post(
            f"/plan-items/{item.id}/direction-audio/upload-url",
            json={
                "filename": "note.webm",
                "content_type": "audio/webm",
                "file_size_bytes": 2048,
            },
        )
    assert resp.status_code == 200
    assert resp.json() == {
        "upload_url": "https://signed.example/direction",
        "gcs_path": expected_path,
    }
    assert signed.call_args.kwargs["filename"].startswith("direction-audio/")


def test_direction_audio_transcript_updates_notes_not_voiceover(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.notes = "Keep the candid moments."
    item.voiceover_gcs_path = "voiceover-uploads/keep-this.webm"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    gcs_path = f"users/{user.id}/plan/{item.id}/direction-audio/note.webm"
    metadata = SimpleNamespace(size=2048, content_type="audio/webm")
    transcript = SimpleNamespace(full_text="Make the opening feel faster.")
    with (
        patch("app.storage.object_metadata", return_value=metadata),
        patch("app.storage.download_to_file"),
        patch("app.services.audio_download.probe_duration", return_value=12.0),
        patch("app.pipeline.transcribe.transcribe_whisper", return_value=transcript),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/direction-audio/transcribe",
            json={"gcs_path": gcs_path},
        )
    assert resp.status_code == 200
    assert resp.json() == {"notes": "Keep the candid moments.\nMake the opening feel faster."}
    assert item.notes == "Keep the candid moments.\nMake the opening feel faster."
    assert item.voiceover_gcs_path == "voiceover-uploads/keep-this.webm"


def test_direction_audio_transcript_rejects_long_recording(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    gcs_path = f"users/{user.id}/plan/{item.id}/direction-audio/note.webm"
    metadata = SimpleNamespace(size=2048, content_type="audio/webm")
    with (
        patch("app.storage.object_metadata", return_value=metadata),
        patch("app.storage.download_to_file"),
        patch("app.services.audio_download.probe_duration", return_value=90.1),
        patch("app.pipeline.transcribe.transcribe_whisper") as transcribe,
    ):
        resp = client.post(
            f"/plan-items/{item.id}/direction-audio/transcribe",
            json={"gcs_path": gcs_path},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Direction notes must be 90 seconds or shorter"
    transcribe.assert_not_called()


def test_direction_audio_transcript_rejects_foreign_item_path(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/direction-audio/transcribe",
        json={"gcs_path": f"users/{user.id}/plan/{uuid.uuid4()}/direction-audio/note.webm"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Direction recording does not belong to this item"


def test_attach_clips_rejects_foreign_prefix(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/clips",
        json={"clip_gcs_paths": ["users/someone-else/plan/x/clip.mp4"]},
    )
    assert resp.status_code == 422


def _db_for_pool_attach(item, plan, *, asset_kind, asset_status="ready"):
    """_db_for + a second execute() result for the pool-promotion kind check.
    asset_kind None → no PlanItemAsset row found (deleted/foreign object)."""
    db = _db_for(item, plan)
    load_result = MagicMock()
    load_result.scalar_one_or_none = MagicMock(return_value=item)
    asset_result = MagicMock()
    rows = []
    if asset_kind is not None:
        rows = [
            MagicMock(
                gcs_path=f"users/{plan.user_id}/plan/{item.id}/pool/asset.bin",
                kind=asset_kind,
                status=asset_status,
            )
        ]
    asset_result.scalars = MagicMock(return_value=rows)

    # execute() serves BOTH the item load/reload (any number of calls) and the
    # one pool-asset kind lookup — discriminate on the compiled query target.
    async def _execute(query, *args, **kwargs):
        if "plan_item_assets" in str(query):
            return asset_result
        if "FROM personas" in str(query):
            return MagicMock(scalar_one_or_none=MagicMock(return_value=plan._owned_persona_fixture))
        return load_result

    db.execute = AsyncMock(side_effect=_execute)
    return db


def test_attach_clips_accepts_pool_asset_prefix(client: TestClient) -> None:
    """The Visuals-pool "Use in edit" promotion re-attaches a pool object
    (users/{uid}/plan/{item}/pool/…) as a clip — that path is INSIDE the item's
    allowed prefix and must keep attaching WHEN the pool row is a video. Pins
    the contract the frontend promotion in AssetPool.tsx depends on."""
    user = _user()
    item, plan = _owned_item(user.id)
    pool_path = f"users/{user.id}/plan/{item.id}/pool/asset.bin"
    db = _db_for_pool_attach(item, plan, asset_kind="video")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={"clip_gcs_paths": [pool_path]},
        )
    assert resp.status_code == 200
    assert pool_path in resp.json()["clip_gcs_paths"]


def test_attach_clips_rejects_non_video_pool_asset(client: TestClient) -> None:
    """Server-side twin of the AssetPool video-only affordance: an IMAGE pool
    asset attached as a clip via direct API call must 422 loudly, not fail the
    render confusingly later."""
    user = _user()
    item, plan = _owned_item(user.id)
    pool_path = f"users/{user.id}/plan/{item.id}/pool/asset.bin"
    db = _db_for_pool_attach(item, plan, asset_kind="image")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/clips",
        json={"clip_gcs_paths": [pool_path]},
    )
    assert resp.status_code == 422
    assert "Photo collage" in resp.json()["detail"]
    assert "remove it from Main footage" in resp.json()["detail"]


def test_attach_clips_rejects_pool_path_without_asset_row(client: TestClient) -> None:
    """A pool-prefixed path with NO PlanItemAsset row (deleted or never registered)
    is unverifiable — reject the NEW attach; previously attached paths are exempt
    (the full-set re-send must keep working after a pool-row delete)."""
    user = _user()
    item, plan = _owned_item(user.id)
    pool_path = f"users/{user.id}/plan/{item.id}/pool/asset.bin"
    db = _db_for_pool_attach(item, plan, asset_kind=None)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/clips",
        json={"clip_gcs_paths": [pool_path]},
    )
    assert resp.status_code == 422


def test_attach_clips_rejects_pool_asset_being_deleted(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    pool_path = f"users/{user.id}/plan/{item.id}/pool/asset.bin"
    db = _db_for_pool_attach(
        item,
        plan,
        asset_kind="video",
        asset_status="cleanup_pending",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.post(
        f"/plan-items/{item.id}/clips",
        json={"clip_gcs_paths": [pool_path]},
    )

    assert resp.status_code == 422


def test_attach_clips_realready_pool_path_skips_kind_check(client: TestClient) -> None:
    """An ALREADY-attached pool path re-sent in the full assignment set does not
    re-run the kind lookup — a promoted clip survives its pool row's deletion."""
    user = _user()
    item, plan = _owned_item(user.id)
    pool_path = f"users/{user.id}/plan/{item.id}/pool/asset.bin"
    item.clip_assignments = [{"gcs_path": pool_path, "shot_id": None, "user_note": ""}]
    item.clip_gcs_paths = [pool_path]
    # Plain _db_for: if the kind-check query ran, .scalars() on its MagicMock
    # result would raise — passing proves the check was skipped.
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={"clip_gcs_paths": [pool_path]},
        )
    assert resp.status_code == 200


def test_upload_urls_returns_signed_puts(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(
        "app.storage.presigned_put_url_for_plan_item",
        return_value=("https://signed.example/put", f"users/{user.id}/plan/{item.id}/x.mp4"),
    ):
        resp = client.post(
            f"/plan-items/{item.id}/upload-urls",
            json={
                "files": [
                    {"filename": "x.mp4", "content_type": "video/mp4", "file_size_bytes": 1000}
                ]
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["urls"]) == 1
    assert body["urls"][0]["gcs_path"].startswith(f"users/{user.id}/plan/")


def test_upload_urls_rejects_images_for_classic_montage(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.montage_preset = "classic"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/upload-urls",
        json={
            "files": [{"filename": "x.jpg", "content_type": "image/jpeg", "file_size_bytes": 1000}]
        },
    )
    assert resp.status_code == 400
    assert "x.jpg" in resp.json()["detail"]
    assert "remove it from Main footage" in resp.json()["detail"]


@pytest.mark.parametrize("preset", ["classic", "masonry", "polaroid_wall"])
def test_upload_urls_rejects_images_for_manual_draft_before_signing(
    preset: str, client: TestClient
) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.montage_preset = preset
    item.current_job = MagicMock(mode="manual_draft", status="draft")
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(
        "app.storage.presigned_put_url_for_plan_item",
        return_value=("https://signed.example/put", f"users/{user.id}/plan/{item.id}/x.jpg"),
    ) as signed:
        resp = client.post(
            f"/plan-items/{item.id}/upload-urls",
            json={
                "files": [
                    {"filename": "x.jpg", "content_type": "image/jpeg", "file_size_bytes": 1000}
                ]
            },
        )
    assert resp.status_code == 400
    assert "x.jpg" in resp.json()["detail"]
    assert "remove it from Main footage" in resp.json()["detail"]
    signed.assert_not_called()


@pytest.mark.parametrize("preset", ["masonry", "polaroid_wall"])
def test_upload_urls_accepts_images_for_collage_montage(
    client: TestClient,
    preset: str,
) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.edit_format = "montage"
    item.montage_preset = preset
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    with patch(
        "app.storage.presigned_put_url_for_plan_item",
        return_value=("https://signed.example/put", f"users/{user.id}/plan/{item.id}/x.jpg"),
    ) as signed:
        resp = client.post(
            f"/plan-items/{item.id}/upload-urls",
            json={
                "files": [
                    {"filename": "x.jpg", "content_type": "image/jpeg", "file_size_bytes": 1000}
                ]
            },
        )
    assert resp.status_code == 200
    signed.assert_called_once()
    assert signed.call_args.kwargs["content_type"] == "image/jpeg"


def test_upload_urls_rejects_images_when_stale_collage_preset_is_not_montage(
    client: TestClient,
) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.edit_format = "narrated_ready"
    item.montage_preset = "polaroid_wall"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.post(
        f"/plan-items/{item.id}/upload-urls",
        json={
            "files": [{"filename": "x.jpg", "content_type": "image/jpeg", "file_size_bytes": 1000}]
        },
    )
    assert resp.status_code == 400
    assert "x.jpg" in resp.json()["detail"]
    assert "remove it from Main footage" in resp.json()["detail"]


# ── filming_guide serialization ───────────────────────────────────────────────


def test_get_plan_item_returns_filming_guide(client: TestClient) -> None:
    """GET /plan-items/{id} returns filming_guide list from the item row."""
    user = _user()
    guide = [{"what": "creator to camera", "how": "eye level", "duration_s": 8}]
    item, plan = _owned_item(user.id, filming_guide=guide)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    # filming_guide now includes shot_id (null for pre-0052 rows without a stamped id).
    assert body["filming_guide"] == [
        {
            "shot_id": None,
            "what": "creator to camera",
            "how": "eye level",  # noqa: E501
            "duration_s": 8,
            "clip_count": 1,
        }
    ]


# ── M4: attach_clips fire-and-forget + instruction_level ─────────────────────


def test_attach_clips_returns_200_immediately(client: TestClient) -> None:
    """POST /plan-items/{id}/clips returns 200 without blocking on conformance analysis.

    The conformance task is fire-and-forget: analyze_item_conformance.delay()
    is called but the response must not wait for it to complete.
    """
    user = _user()
    item, plan = _owned_item(
        user.id,
        clips=[],
        filming_guide=[{"what": "creator at desk", "how": "eye level", "duration_s": 5}],
    )
    db = _db_for(item, plan)
    clip_path = f"users/{user.id}/plan/{item.id}/clip.mp4"
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    with patch("app.tasks.conformance_build.analyze_item_conformance") as mock_task:
        mock_task.delay = MagicMock()
        resp = client.post(
            f"/plan-items/{item.id}/clips",
            json={"clip_gcs_paths": [clip_path]},
        )

    assert resp.status_code == 200
    # Delay was called once with the item id (fire-and-forget).
    mock_task.delay.assert_called_once_with(str(item.id), 0)


def test_get_plan_item_returns_instruction_level(client: TestClient) -> None:
    """GET /plan-items/{id} includes instruction_level (default 'full' when style absent)."""
    user = _user()
    item, plan = _owned_item(user.id)
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["instruction_level"] == "full"


def test_get_plan_item_returns_conformance_null_when_absent(client: TestClient) -> None:
    """GET /plan-items/{id} returns conformance=null when no verdict has run yet."""
    user = _user()
    item, plan = _owned_item(user.id)
    item.conformance = None
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["conformance"] is None


def test_get_plan_item_filming_guide_empty_for_legacy_items(client: TestClient) -> None:
    """Legacy items (filming_guide=[]) return filming_guide:[] in the response."""
    user = _user()
    item, plan = _owned_item(user.id, filming_guide=[])
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    resp = client.get(f"/plan-items/{item.id}")
    assert resp.status_code == 200
    assert resp.json()["filming_guide"] == []


def test_get_plan_item_smart_captions_fails_closed_by_default(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.edit_format = "subtitled"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.get(f"/plan-items/{item.id}")

    assert resp.status_code == 200
    assert resp.json()["smart_captions_enabled"] is False
    assert resp.json()["smart_captions_available"] is False
    assert resp.json()["smart_captions_unavailable_reason"] == "feature_disabled"


def test_patch_smart_captions_rejects_unavailable_enable(client: TestClient, monkeypatch) -> None:
    from app.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "smart_captions_enabled", False)
    user = _user()
    item, plan = _owned_item(user.id)
    item.edit_format = "subtitled"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"smart_captions_enabled": True},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "smart_captions_unavailable:feature_disabled"
    assert item.smart_captions_enabled is False


def test_patch_smart_captions_uses_server_assignment(client: TestClient, monkeypatch) -> None:
    from app.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "smart_captions_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(settings, "smart_captions_default_preset_id", "cigdem", raising=False)
    monkeypatch.setattr(settings, "smart_captions_default_preset_version", "v2", raising=False)
    user = _user()
    item, plan = _owned_item(user.id)
    item.edit_format = "subtitled"
    assignment = SimpleNamespace(enabled=True, preset_id="cigdem", preset_version="v1")
    db = _db_for(item, plan, assignment=assignment)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"smart_captions_enabled": True},
    )

    assert resp.status_code == 200
    assert item.smart_captions_enabled is True
    assert resp.json()["smart_captions_available"] is True
    assert resp.json()["smart_captions_unavailable_reason"] is None


def test_patch_smart_captions_defaults_available_without_assignment(
    client: TestClient, monkeypatch
) -> None:
    from app.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "smart_captions_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(settings, "smart_captions_default_preset_id", "cigdem", raising=False)
    monkeypatch.setattr(settings, "smart_captions_default_preset_version", "v2", raising=False)
    user = _user()
    item, plan = _owned_item(user.id)
    item.edit_format = "subtitled"
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"smart_captions_enabled": True},
    )

    assert resp.status_code == 200
    assert item.smart_captions_enabled is True
    assert resp.json()["smart_captions_available"] is True
    assert resp.json()["smart_captions_unavailable_reason"] is None


def test_patch_smart_sound_design_keeps_smart_captions_enabled(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.edit_format = "subtitled"
    item.smart_captions_enabled = True
    item.smart_sound_design_enabled = True
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"smart_sound_design_enabled": False},
    )

    assert resp.status_code == 200
    assert item.smart_captions_enabled is True
    assert item.smart_sound_design_enabled is False
    assert resp.json()["smart_sound_design_enabled"] is False


def test_changing_away_from_subtitled_disables_smart_captions(client: TestClient) -> None:
    user = _user()
    item, plan = _owned_item(user.id)
    item.edit_format = "subtitled"
    item.smart_captions_enabled = True
    db = _db_for(item, plan)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db

    resp = client.patch(f"/plan-items/{item.id}", json={"edit_format": "montage"})

    assert resp.status_code == 200
    assert item.smart_captions_enabled is False


def test_plan_item_response_tolerates_malformed_guide() -> None:
    """Malformed JSONB shots (non-dict, missing keys) are skipped or defaulted."""
    from app.models import PlanItem  # noqa: PLC0415
    from app.routes.plan_items import plan_item_response  # noqa: PLC0415

    item = MagicMock(spec=PlanItem)
    item.id = uuid.uuid4()
    item.day_index = 1
    item.theme = "test theme"
    item.idea = "test idea"
    item.filming_guide = [
        "not a dict",  # skipped
        {"what": "creator plating dish"},  # kept: missing 'how' and 'duration_s' → defaults
        {"how": "wide", "duration_s": 4},  # kept: missing 'what' → "" default
    ]
    item.filming_suggestion = None
    item.rationale = None
    item.clip_gcs_paths = []
    item.current_job_id = None
    item.current_job = None
    item.item_status = "idea"
    item.user_edited = False
    item.conformance = None  # M4: must be None/dict, not a MagicMock attribute
    item.clip_assignments = []
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None
    item.source_idea_seed_text = None
    item.edit_format = None
    item.voiceover_gcs_path = None
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None

    resp = plan_item_response(item)
    # non-dict skipped; 2 dicts kept with defaults
    assert len(resp.filming_guide) == 2
    assert resp.filming_guide[0].what == "creator plating dish"
    assert resp.filming_guide[0].how == ""
    assert resp.filming_guide[0].duration_s == 1  # FilmingShotResponse default is 1 (not 0)
    assert resp.filming_guide[1].what == ""
    assert resp.filming_guide[1].how == "wide"


# ── landscape_fit (0057) ─────────────────────────────────────────────────────


def test_patch_landscape_fit_persists_fit(client: TestClient) -> None:
    """PATCH landscape_fit='fit' must be written to the item row."""
    user = _user()
    item, plan = _owned_item(user.id)
    item.landscape_fit = "fill"  # current value before PATCH

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _db_for(item, plan)

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"landscape_fit": "fit"},
        headers={"Authorization": "Bearer test"},
    )
    assert resp.status_code == 200
    assert item.landscape_fit == "fit"


def test_patch_landscape_fit_persists_fill(client: TestClient) -> None:
    """PATCH landscape_fit='fill' (crop) must also be accepted."""
    user = _user()
    item, plan = _owned_item(user.id)
    item.landscape_fit = "fit"

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _db_for(item, plan)

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"landscape_fit": "fill"},
        headers={"Authorization": "Bearer test"},
    )
    assert resp.status_code == 200
    assert item.landscape_fit == "fill"


def test_patch_landscape_fit_rejects_junk(client: TestClient) -> None:
    """Invalid landscape_fit values must be rejected with 422 (Pydantic Literal validates)."""
    user = _user()
    item, plan = _owned_item(user.id)
    item.landscape_fit = "fit"  # must stay unchanged

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _db_for(item, plan)

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"landscape_fit": "stretch"},
        headers={"Authorization": "Bearer test"},
    )
    # 422 expected — Pydantic Literal["fit","fill"] rejects out-of-contract values
    assert resp.status_code == 422
    assert item.landscape_fit == "fit"  # unchanged


@pytest.mark.parametrize("preset", ["masonry", "polaroid_wall"])
def test_patch_montage_preset_persists_collage_presets(
    client: TestClient,
    preset: str,
) -> None:
    """PATCH montage_preset for collage presets must be written to the item row."""
    user = _user()
    item, plan = _owned_item(user.id)
    item.montage_preset = "classic"

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _db_for(item, plan)

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"montage_preset": preset},
        headers={"Authorization": "Bearer test"},
    )
    assert resp.status_code == 200
    assert item.montage_preset == preset
    assert resp.json()["montage_preset"] == preset


def test_patch_montage_preset_rejects_junk(client: TestClient) -> None:
    """Invalid montage_preset values must be rejected with 422."""
    user = _user()
    item, plan = _owned_item(user.id)
    item.montage_preset = "classic"

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: _db_for(item, plan)

    resp = client.patch(
        f"/plan-items/{item.id}",
        json={"montage_preset": "polaroid"},
        headers={"Authorization": "Bearer test"},
    )
    assert resp.status_code == 422
    assert item.montage_preset == "classic"


def test_plan_item_response_surface_landscape_fit() -> None:
    """plan_item_response() must expose landscape_fit; fallback to 'fit' for MagicMock."""
    from app.models import PlanItem  # noqa: PLC0415
    from app.routes.plan_items import plan_item_response  # noqa: PLC0415

    item = MagicMock(spec=PlanItem)
    item.id = uuid.uuid4()
    item.day_index = 1
    item.theme = "t"
    item.idea = "i"
    item.filming_guide = []
    item.filming_suggestion = None
    item.rationale = None
    item.clip_gcs_paths = []
    item.current_job_id = None
    item.current_job = None
    item.item_status = "idea"
    item.user_edited = False
    item.conformance = None
    item.clip_assignments = []
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None
    item.source_idea_seed_text = None
    item.edit_format = None
    item.montage_preset = "masonry"
    item.voiceover_gcs_path = None
    item.landscape_fit = "fill"  # explicitly set
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None

    resp = plan_item_response(item)
    assert resp.landscape_fit == "fill"
    assert resp.montage_preset == "masonry"


def test_plan_item_response_landscape_fit_defaults_to_fit() -> None:
    """When landscape_fit is None (pre-migration row or missing attr), fallback is 'fit'."""
    from app.models import PlanItem  # noqa: PLC0415
    from app.routes.plan_items import plan_item_response  # noqa: PLC0415

    item = MagicMock(spec=PlanItem)
    item.id = uuid.uuid4()
    item.day_index = 1
    item.theme = "t"
    item.idea = "i"
    item.filming_guide = []
    item.filming_suggestion = None
    item.rationale = None
    item.clip_gcs_paths = []
    item.current_job_id = None
    item.current_job = None
    item.item_status = "idea"
    item.user_edited = False
    item.conformance = None
    item.clip_assignments = []
    item.position = 1
    item.scheduled_date = None
    item.notes = None
    item.scenes = []
    item.source_idea_seed_id = None
    item.source_idea_seed_text = None
    item.edit_format = None
    item.montage_preset = None
    item.voiceover_gcs_path = None
    item.landscape_fit = None  # pre-migration row: None → should default to "fit"
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None

    resp = plan_item_response(item)
    assert resp.landscape_fit == "fit"
    assert resp.montage_preset == "classic"

"""Unit test for persona threading in generate_plan_item_videos (mock DB).

Locks that the per-item plan task loads the creator's persona + the item's
theme/idea and forwards them to the shared build_generative_job — the data path
that makes content-plan hooks persona-coherent (intro_writer threading).
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.agents._schemas.content_plan import PlanItemSpec
from app.models import ContentPlan, Job, PlanItem, SpeechCleanupAnalysis
from app.models import Persona as PersonaRow
from app.tasks.content_plan_build import (
    _dispatch_item_render,
    _guided_render_queue,
    _item_direction_snapshot,
    dispatch_item_render_for,
    generate_content_plan,
    generate_plan_item_videos,
    regenerate_content_plan,
)


def test_chat_item_direction_snapshot_wins_over_account_plan_snapshot() -> None:
    thread_snapshot = {"memory_revision": 7, "source": "project_override"}
    plan_snapshot = {"memory_revision": 4, "source": "content_plan"}
    result = MagicMock()
    result.scalar_one_or_none.return_value = thread_snapshot
    session = MagicMock()
    session.execute.return_value = result

    assert (
        _item_direction_snapshot(
            session,
            SimpleNamespace(id=uuid.uuid4()),
            SimpleNamespace(creator_direction_snapshot=plan_snapshot),
        )
        is thread_snapshot
    )


def test_non_chat_item_direction_snapshot_uses_immutable_plan_snapshot() -> None:
    plan_snapshot = {"memory_revision": 4, "source": "content_plan"}
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session = MagicMock()
    session.execute.return_value = result

    assert (
        _item_direction_snapshot(
            session,
            SimpleNamespace(id=uuid.uuid4()),
            SimpleNamespace(creator_direction_snapshot=plan_snapshot),
        )
        is plan_snapshot
    )


def _recovery_payload(source_fingerprint: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_fingerprint": source_fingerprint,
        "detector_version": "mixed-gap-v1",
        "source_window_start_s": 0.0,
        "source_window_end_s": 12.0,
        "language": "en",
        "timed_words": [],
        "cut_plan": {
            "keep_segments": [{"start_s": 0.0, "end_s": 12.0}],
            "removed": [],
            "time_saved_s": 0.0,
            "version": 2,
            "bailout_reason": None,
            "clamped": False,
        },
        "findings": [],
        "safety_signals": {
            "transcript_low_confidence": False,
            "silence_detection_status": "ok",
            "selected_plan": "baseline",
            "candidate_status": "not_run",
            "bailout_reason": None,
            "clamped": False,
        },
        "diagnostics": {},
        "public_receipt": {
            "candidate_count": 0,
            "category_counts": {
                "filler_sounds": 0,
                "long_pauses": 0,
                "retakes": 0,
            },
            "estimated_removed_ms": 0,
        },
    }


def test_mixed_media_guided_render_uses_deploy_fenced_queue() -> None:
    approved = {
        "snapshot": {
            "mixed_media_timing": {
                "image_hold": "very_fast",
                "video_hold": "longer",
                "boundary_style": "cut",
            }
        }
    }

    assert _guided_render_queue(approved) == "creator-guided-jobs"
    assert _guided_render_queue(None) == "plan-jobs"


def test_chat_creator_render_uses_version_fenced_queue() -> None:
    assert _guided_render_queue(None, {"edit_format": "montage"}) == "creator-render-v2"


def test_cadence_guided_render_uses_deploy_fenced_queue() -> None:
    approved = {
        "snapshot": {
            "montage_cadence": {
                "mode": "round_robin",
                "source_media_ids": ["clip-1", "clip-2"],
                "cut_duration_s": 1,
                "reuse_policy": "no_repeat",
            }
        }
    }

    assert _guided_render_queue(approved) == "creator-guided-jobs"


def test_legacy_generate_task_keeps_wire_shape_and_fences_creator_sessions() -> None:
    """Old two-argument Celery messages remain consumable during rollout."""
    item_id = uuid.uuid4()
    with patch(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        return_value=SimpleNamespace(outcome="missing_row"),
    ) as dispatch:
        generate_plan_item_videos.run(str(item_id), 7)

    dispatch.assert_called_once_with(
        str(item_id),
        7,
        reject_active_creator_session=True,
    )


@pytest.fixture(autouse=True)
def _legacy_task_owner_loader(monkeypatch: pytest.MonkeyPatch):
    """Keep old mock-session tests focused on their original behavior.

    Ownership-specific regressions below override this loader explicitly. The
    production loader's compound SQL is covered by the service tests.
    """
    from app.services.content_plan_persona import PlanPersonaOwnershipError
    from app.tasks import content_plan_build as task_module

    def _load(session, plan, *, for_update=False):  # noqa: ANN001, ARG001
        if not isinstance(getattr(plan, "ownership_epoch", None), int):
            plan.ownership_epoch = 0
        persona = session.get(PersonaRow, plan.persona_id)
        if persona is None:
            raise PlanPersonaOwnershipError(plan)
        return persona

    monkeypatch.setattr(task_module, "load_owned_plan_persona_sync", _load)
    monkeypatch.setattr(task_module, "_lock_plan_items", lambda _session, items: list(items))


def _session_with(item, plan, persona_row) -> MagicMock:
    # Live dispatch now resolves every job to required_v1/off_v1. Most legacy
    # MagicMock fixtures predate the preference and otherwise fabricate a
    # truthy attribute on access, accidentally requesting strict cleanup.
    if "speech_cleanup_enabled" not in vars(item):
        item.speech_cleanup_enabled = False
    plan.id = item.content_plan_id
    session = MagicMock()

    def _get(model, _pk, **_kw):
        return {PlanItem: item, ContentPlan: plan, PersonaRow: persona_row}.get(model)

    session.get = MagicMock(side_effect=_get)
    session.execute.return_value.scalar_one_or_none.return_value = None
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def test_persona_forwarded_to_build_generative_job() -> None:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "first 5am workout"
    item.idea = "film the dark early start"

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {
        "tone": "no-excuses gym motivation",
        "content_pillars": ["morning routines", "discipline"],
    }

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, persona_row)
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    mock_build.assert_called_once()
    kwargs = mock_build.call_args.kwargs
    assert kwargs["mode"] == "content_plan"
    assert kwargs["persona_tone"] == "no-excuses gym motivation"
    assert kwargs["persona_pillars"] == ["morning routines", "discipline"]
    assert kwargs["item_theme"] == "first 5am workout"
    assert kwargs["item_idea"] == "film the dark early start"
    assert kwargs["variant_policy"] == "content_plan_primary"


def test_main_creator_native_selection_resolves_only_exact_owned_clip_ids() -> None:
    from app.tasks.content_plan_build import _creator_selected_clip_paths

    item = MagicMock()
    item.clip_assignments = [
        {"media_id": "media-a", "gcs_path": "users/u/plan/i/a.mp4"},
        {"media_id": "media-b", "gcs_path": "users/u/plan/i/b.mp4"},
    ]
    selected = _creator_selected_clip_paths(
        item,
        ["users/u/plan/i/a.mp4", "users/u/plan/i/b.mp4"],
        {
            "edit_format": "montage",
            "render_program": "native",
            "selected_media_ids": ["media-b"],
        },
    )

    assert selected == ["users/u/plan/i/b.mp4"]


def test_main_creator_native_pool_only_selection_fails_closed_to_no_clips() -> None:
    from app.tasks.content_plan_build import _creator_selected_clip_paths

    item = MagicMock()
    item.clip_assignments = [
        {"media_id": "media-a", "gcs_path": "users/u/plan/i/a.mp4"},
    ]

    assert (
        _creator_selected_clip_paths(
            item,
            ["users/u/plan/i/a.mp4"],
            {
                "edit_format": "montage",
                "render_program": "native",
                "selected_media_ids": ["asset-pool-only"],
            },
        )
        == []
    )


def test_multi_clip_talking_head_forwarded_to_build_generative_job() -> None:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = [
        "users/u/plan/i/spoken.mp4",
        "users/u/plan/i/broll-1.mp4",
    ]
    item.clip_assignments = [
        {"gcs_path": item.clip_gcs_paths[0], "shot_id": None, "user_note": ""},
        {"gcs_path": item.clip_gcs_paths[1], "shot_id": None, "user_note": ""},
    ]
    item.filming_guide = []
    item.theme = "recreate reel"
    item.idea = "multi-clip lyric-style edit"
    item.edit_format = "talking_head"
    item.voiceover_gcs_path = None
    item.landscape_fit = "fit"
    item.montage_preset = "classic"
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.preference_summary = ""

    persona_row = MagicMock()
    persona_row.persona = {"tone": "direct", "content_pillars": []}

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, persona_row)
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    kwargs = mock_build.call_args.kwargs
    assert kwargs["clip_paths"] == item.clip_gcs_paths
    assert kwargs["edit_format"] == "talking_head"


def test_smart_captions_context_is_resolved_and_pinned_at_dispatch() -> None:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/talking.mp4"]
    item.clip_assignments = []
    item.filming_guide = []
    item.theme = "brand mascots"
    item.idea = "explain four examples"
    item.edit_format = "subtitled"
    # Default-on contract: the stored per-item flag must NOT gate the request —
    # the resolver's server ladder (kill switch / format / assignment) decides.
    item.smart_captions_enabled = False
    item.smart_sound_design_enabled = True
    item.voiceover_gcs_path = None
    item.landscape_fit = "fit"
    item.montage_preset = "classic"
    item.voiceover_bed_level = None
    item.voiceover_caption_style = None

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.preference_summary = ""

    persona_row = MagicMock()
    persona_row.persona = {"tone": "direct", "content_pillars": []}
    job = MagicMock()
    job.id = uuid.uuid4()
    smart_context = {"preset_id": "cigdem", "preset_version": "v1"}

    ctx = _session_with(item, plan, persona_row)
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=smart_context,
        ) as mock_resolve,
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    mock_resolve.assert_called_once_with(
        user_id=plan.user_id,
        edit_format="subtitled",
        requested=True,
        sound_design_enabled=True,
        db=ctx.__enter__.return_value,
    )
    assert mock_build.call_args.kwargs["smart_captions"] == smart_context


@pytest.mark.parametrize(
    ("requested", "expected_contract"),
    [(False, "off_v1"), (True, "required_v1")],
)
def test_dispatch_snapshots_only_explicit_speech_cleanup_contracts(
    monkeypatch: pytest.MonkeyPatch,
    requested: bool,
    expected_contract: str,
) -> None:
    from app.config import settings
    from app.tasks.content_plan_build import _dispatch_item_render

    item_id = uuid.uuid4()
    clip_path = "users/u/plan/i/talking.mp4"
    item = SimpleNamespace(
        id=item_id,
        clip_gcs_paths=[clip_path],
        clip_assignments=[{"media_id": "m1", "gcs_path": clip_path}],
        filming_guide=[],
        theme="camera explanation",
        idea="explain one idea",
        edit_format="subtitled",
        audio_mode="kria",
        voiceover_gcs_path=None,
        landscape_fit="fit",
        montage_preset="classic",
        voiceover_bed_level=None,
        voiceover_caption_style=None,
        smart_sound_design_enabled=True,
        speech_cleanup_enabled=requested,
        current_job_id=None,
        edit_proposal=None,
    )
    plan = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        preference_summary="",
        ownership_epoch=0,
    )
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={})
    session = MagicMock()

    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    with (
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=None,
        ),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=0,
            creator_request="x" * 2000,
        )

    assert result.outcome == "dispatched"
    assert job.assembly_plan["speech_cleanup_requested"] is requested
    assert job.assembly_plan["speech_cleanup_contract"] == expected_contract
    assert job.assembly_plan["silence_cut_disabled"] is (not requested)
    assert job.assembly_plan["speech_cleanup_contract"] != "legacy_auto"
    assert isinstance(job.assembly_plan["creator_generation_id"], str)
    assert job.assembly_plan["creator_generation_id"]
    assert mock_build.call_args.kwargs["creator_request"] == "x" * 1000


def _cleanup_dispatch_item() -> SimpleNamespace:
    item_id = uuid.uuid4()
    clip_path = "users/u/plan/i/talking.mp4"
    return SimpleNamespace(
        id=item_id,
        clip_gcs_paths=[clip_path],
        clip_assignments=[
            {
                "media_id": "registered-spine",
                "gcs_path": clip_path,
                "storage_generation": "generation-17",
                "duration_s": 12.0,
                "has_audio": True,
            }
        ],
        filming_guide=[],
        theme="camera explanation",
        idea="explain one idea",
        edit_format="subtitled",
        audio_mode="kria",
        voiceover_gcs_path=None,
        landscape_fit="fit",
        montage_preset="classic",
        voiceover_bed_level=None,
        voiceover_caption_style=None,
        smart_sound_design_enabled=True,
        speech_cleanup_enabled=False,
        speech_cleanup_notice=None,
        current_job_id=None,
        edit_proposal=None,
    )


def _cleanup_analysis(
    item: SimpleNamespace,
    *,
    status: str = "ready",
    candidate_count: int = 2,
    fingerprint: str = "active-source-fingerprint",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item.id,
        source_kind="embedded_spine",
        source_media_identity="registered-spine",
        source_storage_path=item.clip_gcs_paths[0],
        source_generation="generation-17",
        window_start_s=0.0,
        window_end_s=12.0,
        source_policy_fingerprint=fingerprint,
        engine_version="speech-cleanup-v2",
        detector_version="mixed-gap-v2",
        status=status,
        superseded_at=None,
        candidate_count=candidate_count,
        decision=None,
        decision_at=None,
        analysis_payload={
            "timed_words": [{"text": "private", "start_s": 0.2, "end_s": 0.6}],
            "cut_plan": {
                "version": 2,
                "removed": [] if candidate_count == 0 else [{"start_s": 1.0, "end_s": 1.4}],
            },
        },
    )


def _run_cleanup_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: str,
    candidate_count: int,
    choice: str | None,
) -> tuple[SimpleNamespace, SimpleNamespace, MagicMock, MagicMock]:
    from app.config import settings

    item = _cleanup_dispatch_item()
    row = _cleanup_analysis(item, status=status, candidate_count=candidate_count)
    plan = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        preference_summary="",
        ownership_epoch=4,
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={},
        all_candidates={"clip_paths": list(item.clip_gcs_paths)},
    )
    session = MagicMock()
    session.get.return_value = row

    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(settings, "edit_format_talking_head_enabled", True)
    monkeypatch.setattr(settings, "narrated_self_narration_enabled", True)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(
            source=SimpleNamespace(source_policy_fingerprint="active-source-fingerprint")
        ),
    )
    enqueue = MagicMock()
    with (
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=None,
        ),
        patch("app.services.generative_jobs.build_generative_job", return_value=job),
        patch("app.services.job_dispatch.enqueue_orchestrator_sync", enqueue),
    ):
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=4,
            speech_cleanup_analysis_id=str(row.id),
            speech_cleanup_choice=choice,
        )

    assert result.outcome == "dispatched"
    return item, row, session, job


@pytest.mark.parametrize(
    ("status", "candidate_count", "choice", "contract", "decision", "outcome"),
    [
        ("ready", 2, "clean", "required_v1", "clean", None),
        ("ready", 2, "keep_original", "off_v1", "keep_original", "declined"),
        ("no_findings", 0, None, "off_v1", None, "checked_no_change"),
        (
            "failed",
            0,
            "create_without_cleanup",
            "off_v1",
            "create_without_cleanup",
            "bypassed_unchecked",
        ),
    ],
)
def test_preflight_decision_mints_truthful_immutable_job_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    candidate_count: int,
    choice: str | None,
    contract: str,
    decision: str | None,
    outcome: str | None,
) -> None:
    item, row, session, job = _run_cleanup_dispatch(
        monkeypatch,
        status=status,
        candidate_count=candidate_count,
        choice=choice,
    )

    assert row.decision == decision
    assert (row.decision_at is not None) is (decision is not None)
    assert item.speech_cleanup_enabled is (choice == "clean")
    assert job.assembly_plan["speech_cleanup_contract"] == contract
    if choice == "create_without_cleanup":
        private = job.assembly_plan["_speech_cleanup_internal"]
        assert private == {"outcome_analysis_id": str(row.id)}
        assert "preflight_snapshot" not in private
    else:
        assert job.assembly_plan["speech_cleanup_preflight_contract"] == "snapshot_v1"
        private = job.assembly_plan["_speech_cleanup_internal"]
        preflight = private["preflight_snapshot"]
        assert preflight["analysis_id"] == str(row.id)
        assert preflight["source"] == {
            "kind": "embedded_spine",
            "media_identity": "registered-spine",
            "storage_path": "users/u/plan/i/talking.mp4",
            "generation": "generation-17",
            "window_start_s": 0.0,
            "window_end_s": 12.0,
            "source_policy_fingerprint": "active-source-fingerprint",
        }
        assert preflight["analysis"] == row.analysis_payload
        binding = private["source_binding"]
        assert binding["source_slot"] == 0
        assert binding["source_instance_id"] == (job.all_candidates["clip_source_instance_ids"][0])
    if outcome is None:
        assert "speech_cleanup_outcome" not in job.assembly_plan
    else:
        receipt = job.assembly_plan["speech_cleanup_outcome"]
        assert receipt == {
            "status": outcome,
            "removal_count": 0,
            "removed_ms": 0,
            "job_id": str(job.id),
            "render_generation_id": job.assembly_plan["creator_generation_id"],
        }
    session.add.assert_called_once_with(job)
    session.commit.assert_called_once_with()


def _run_cleanup_application_recovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    action: str,
    expected_generation: str = "failed-generation",
    failure_reason: str = "apply_failed",
    active_source_generation: str = "generation-17",
) -> tuple[
    object,
    SimpleNamespace,
    SimpleNamespace,
    SimpleNamespace,
    SimpleNamespace,
    MagicMock,
    MagicMock,
]:
    from app.config import settings
    from app.services.speech_cleanup_preflight import analysis_snapshot

    item = _cleanup_dispatch_item()
    item.content_plan_id = uuid.uuid4()
    item.speech_cleanup_enabled = True
    fingerprint = "a" * 64
    row = _cleanup_analysis(item, fingerprint=fingerprint)
    row.engine_version = "preflight-v1-2026-09-05"
    row.detector_version = "mixed-gap-v1"
    row.analysis_payload = _recovery_payload(fingerprint)
    row.decision = "clean"
    row.decision_at = datetime.now(UTC)
    raw_snapshot = analysis_snapshot(row)
    old_binding = str(uuid.uuid4())
    owner_id = uuid.uuid4()
    old_job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        content_plan_item_id=item.id,
        content_plan_ownership_epoch=4,
        status="variants_failed",
        failure_reason="speech_cleanup_failed",
        assembly_plan={
            "creator_generation_id": "failed-generation",
            "speech_cleanup_contract": "required_v1",
            "speech_cleanup_preflight_contract": "snapshot_v1",
            "speech_cleanup_failure_reason": failure_reason,
            "variants": [
                {
                    "variant_id": "talking_head",
                    "error_class": "speech_cleanup_failed",
                    "speech_cleanup_failure_reason": failure_reason,
                }
            ],
            "_speech_cleanup_internal": {
                "preflight_snapshot": copy.deepcopy(raw_snapshot),
                "source_binding": {
                    "source_slot": 0,
                    "source_instance_id": old_binding,
                },
            },
        },
    )
    item.current_job_id = old_job.id
    plan = SimpleNamespace(
        id=item.content_plan_id,
        user_id=owner_id,
        preference_summary="",
        ownership_epoch=4,
    )
    persona = SimpleNamespace(
        persona={"tone": "direct", "content_pillars": []},
        tiktok_profile=None,
        style=None,
    )
    new_job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={},
        all_candidates={"clip_paths": list(item.clip_gcs_paths)},
    )
    session = MagicMock()

    def get(model: object, identifier: object, **_kwargs: object) -> object | None:
        if model is PlanItem:
            return item
        if model is Job and identifier == old_job.id:
            return old_job
        if model is SpeechCleanupAnalysis and identifier == row.id:
            return row
        return None

    session.get.side_effect = get
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    monkeypatch.setattr(
        "app.tasks.content_plan_build.sync_session",
        lambda: context,
    )
    monkeypatch.setattr(
        "app.tasks.content_plan_build._lock_owned_plan_persona",
        lambda *_args, **_kwargs: (plan, persona),
    )
    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(settings, "edit_format_talking_head_enabled", True)
    monkeypatch.setattr(settings, "narrated_self_narration_enabled", True)
    source = SimpleNamespace(
        source_kind="embedded_spine",
        media_id="registered-spine",
        storage_path=item.clip_gcs_paths[0],
        generation=active_source_generation,
        source_policy_fingerprint=fingerprint,
    )
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(source=source),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup.capability_for_item",
        lambda *_args, **_kwargs: SimpleNamespace(available=True),
    )
    build = MagicMock(return_value=new_job)
    enqueue = MagicMock()
    with (
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=None,
        ),
        patch("app.services.edit_proposals.proposal_generate_error", return_value=None),
        patch("app.services.generative_jobs.build_generative_job", build),
        patch("app.services.job_dispatch.enqueue_orchestrator_sync", enqueue),
        patch(
            "app.services.speech_cleanup_preflight.schedule_item_preflight_sync",
            side_effect=AssertionError("recovery must not schedule analysis"),
        ),
    ):
        result = dispatch_item_render_for(
            str(item.id),
            4,
            speech_cleanup_action=action,
            expected_job_id=str(old_job.id),
            expected_render_generation_id=expected_generation,
            expected_speech_cleanup_analysis_id=str(row.id),
        )
    return result, item, row, old_job, new_job, build, enqueue


def test_retry_required_reuses_exact_failed_job_snapshot_without_analysis_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, item, row, old_job, new_job, build, enqueue = _run_cleanup_application_recovery(
        monkeypatch, action="retry_required"
    )

    assert result.outcome == "dispatched"
    assert item.current_job_id == new_job.id
    assert row.decision == "clean"
    assert new_job.assembly_plan["speech_cleanup_contract"] == "required_v1"
    old_private = old_job.assembly_plan["_speech_cleanup_internal"]
    new_private = new_job.assembly_plan["_speech_cleanup_internal"]
    assert new_private["preflight_snapshot"] == old_private["preflight_snapshot"]
    assert new_private["preflight_snapshot"] is not old_private["preflight_snapshot"]
    assert new_private["source_binding"] != old_private["source_binding"]
    assert (
        new_private["source_binding"]["source_instance_id"]
        == (new_job.all_candidates["clip_source_instance_ids"][0])
    )
    assert new_job.assembly_plan["creator_generation_id"] != "failed-generation"
    build.assert_called_once()
    enqueue.assert_called_once()


def test_disable_and_create_after_ready_apply_failure_stamps_unchecked_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, item, row, _old_job, new_job, build, enqueue = _run_cleanup_application_recovery(
        monkeypatch, action="disable_and_create"
    )

    assert result.outcome == "dispatched"
    assert item.speech_cleanup_enabled is False
    assert row.status == "ready"
    assert row.decision == "create_without_cleanup"
    assert new_job.assembly_plan["speech_cleanup_contract"] == "off_v1"
    assert new_job.assembly_plan["_speech_cleanup_internal"] == {"outcome_analysis_id": str(row.id)}
    assert new_job.assembly_plan["speech_cleanup_outcome"] == {
        "status": "bypassed_unchecked",
        "removal_count": 0,
        "removed_ms": 0,
        "job_id": str(new_job.id),
        "render_generation_id": new_job.assembly_plan["creator_generation_id"],
    }
    build.assert_called_once()
    enqueue.assert_called_once()


def test_disable_and_create_is_available_after_snapshot_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, item, row, _old_job, new_job, build, enqueue = _run_cleanup_application_recovery(
        monkeypatch,
        action="disable_and_create",
        failure_reason="snapshot_mismatch",
    )

    assert result.outcome == "dispatched"
    assert item.speech_cleanup_enabled is False
    assert row.decision == "create_without_cleanup"
    assert new_job.assembly_plan["speech_cleanup_outcome"]["status"] == "bypassed_unchecked"
    build.assert_called_once()
    enqueue.assert_called_once()


def test_retry_does_not_loop_a_known_snapshot_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, item, row, old_job, _new_job, build, enqueue = _run_cleanup_application_recovery(
        monkeypatch,
        action="retry_required",
        failure_reason="snapshot_mismatch",
    )

    assert result.outcome == "speech_cleanup_recovery_conflict"
    assert item.current_job_id == old_job.id
    assert row.decision == "clean"
    build.assert_not_called()
    enqueue.assert_not_called()


def test_cleanup_application_recovery_is_job_generation_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, item, row, old_job, _new_job, build, enqueue = _run_cleanup_application_recovery(
        monkeypatch,
        action="retry_required",
        expected_generation="stale-generation",
    )

    assert result.outcome == "speech_cleanup_recovery_conflict"
    assert item.current_job_id == old_job.id
    assert row.decision == "clean"
    build.assert_not_called()
    enqueue.assert_not_called()


def test_cleanup_application_recovery_rejects_active_source_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, item, row, old_job, _new_job, build, enqueue = _run_cleanup_application_recovery(
        monkeypatch,
        action="retry_required",
        active_source_generation="replacement-generation",
    )

    assert result.outcome == "speech_cleanup_recovery_conflict"
    assert item.current_job_id == old_job.id
    assert row.decision == "clean"
    build.assert_not_called()
    enqueue.assert_not_called()


def _run_cleanup_preflight_publish_recovery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcome_status: str | None,
    active_source_generation: str = "generation-17",
    expected_generation: str = "publish-failed-generation",
) -> tuple[
    object,
    SimpleNamespace,
    SimpleNamespace,
    SimpleNamespace,
    SimpleNamespace,
    MagicMock,
    MagicMock,
]:
    """Retry a Job that committed its cleanup contract before publish failed."""
    from app.config import settings
    from app.services.speech_cleanup_preflight import analysis_snapshot

    item = _cleanup_dispatch_item()
    item.content_plan_id = uuid.uuid4()
    fingerprint = "b" * 64
    status = "failed" if outcome_status == "bypassed_unchecked" else "ready"
    candidate_count = 0 if outcome_status == "checked_no_change" else 2
    if outcome_status == "checked_no_change":
        status = "no_findings"
    row = _cleanup_analysis(
        item,
        status=status,
        candidate_count=candidate_count,
        fingerprint=fingerprint,
    )
    row.engine_version = "preflight-v1-2026-09-05"
    row.detector_version = "mixed-gap-v1"
    row.analysis_payload = _recovery_payload(fingerprint)
    row.decision = {
        None: "clean",
        "declined": "keep_original",
        "checked_no_change": None,
        "bypassed_unchecked": "create_without_cleanup",
    }[outcome_status]
    row.decision_at = datetime.now(UTC) if row.decision is not None else None
    item.speech_cleanup_enabled = outcome_status is None
    owner_id = uuid.uuid4()
    old_job_id = uuid.uuid4()
    old_generation = "publish-failed-generation"
    old_plan: dict[str, object] = {
        "creator_generation_id": old_generation,
        "speech_cleanup_contract": "required_v1" if outcome_status is None else "off_v1",
    }
    if outcome_status == "bypassed_unchecked":
        old_plan["_speech_cleanup_internal"] = {"outcome_analysis_id": str(row.id)}
    else:
        old_plan["speech_cleanup_preflight_contract"] = "snapshot_v1"
        old_plan["_speech_cleanup_internal"] = {
            "preflight_snapshot": analysis_snapshot(row),
        }
    if outcome_status is not None:
        old_plan["speech_cleanup_outcome"] = {
            "status": outcome_status,
            "removal_count": 0,
            "removed_ms": 0,
            "job_id": str(old_job_id),
            "render_generation_id": old_generation,
        }
    old_job = SimpleNamespace(
        id=old_job_id,
        user_id=owner_id,
        content_plan_item_id=item.id,
        content_plan_ownership_epoch=4,
        status="processing_failed",
        failure_reason="dispatch_publish_failed",
        assembly_plan=old_plan,
    )
    item.current_job_id = old_job.id
    plan = SimpleNamespace(
        id=item.content_plan_id,
        user_id=owner_id,
        preference_summary="",
        ownership_epoch=4,
    )
    persona = SimpleNamespace(
        persona={"tone": "direct", "content_pillars": []},
        tiktok_profile=None,
        style=None,
    )
    new_job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={},
        all_candidates={"clip_paths": list(item.clip_gcs_paths)},
    )
    session = MagicMock()

    def get(model: object, identifier: object, **_kwargs: object) -> object | None:
        if model is PlanItem:
            return item
        if model is Job and identifier == old_job.id:
            return old_job
        if model is SpeechCleanupAnalysis and identifier == row.id:
            return row
        return None

    session.get.side_effect = get
    context = MagicMock()
    context.__enter__.return_value = session
    context.__exit__.return_value = False
    monkeypatch.setattr("app.tasks.content_plan_build.sync_session", lambda: context)
    monkeypatch.setattr(
        "app.tasks.content_plan_build._lock_owned_plan_persona",
        lambda *_args, **_kwargs: (plan, persona),
    )
    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(settings, "edit_format_talking_head_enabled", True)
    monkeypatch.setattr(settings, "narrated_self_narration_enabled", True)
    source = SimpleNamespace(
        source_kind="embedded_spine",
        media_id="registered-spine",
        storage_path=item.clip_gcs_paths[0],
        generation=active_source_generation,
        source_policy_fingerprint=fingerprint,
    )
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(source=source),
    )
    build = MagicMock(return_value=new_job)
    enqueue = MagicMock()
    with (
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=None,
        ),
        patch("app.services.edit_proposals.proposal_generate_error", return_value=None),
        patch("app.services.generative_jobs.build_generative_job", build),
        patch("app.services.job_dispatch.enqueue_orchestrator_sync", enqueue),
        patch(
            "app.services.speech_cleanup_preflight.schedule_item_preflight_sync",
            side_effect=AssertionError("publish retry must not schedule analysis"),
        ),
    ):
        result = dispatch_item_render_for(
            str(item.id),
            4,
            speech_cleanup_action="retry_preflight_dispatch",
            expected_job_id=str(old_job.id),
            expected_render_generation_id=expected_generation,
            expected_speech_cleanup_analysis_id=str(row.id),
        )
    return result, item, row, old_job, new_job, build, enqueue


@pytest.mark.parametrize(
    "outcome_status",
    [None, "declined", "checked_no_change", "bypassed_unchecked"],
)
def test_preflight_publish_retry_preserves_every_exact_cleanup_decision(
    monkeypatch: pytest.MonkeyPatch,
    outcome_status: str | None,
) -> None:
    result, item, row, old_job, new_job, build, enqueue = _run_cleanup_preflight_publish_recovery(
        monkeypatch,
        outcome_status=outcome_status,
    )

    assert result.outcome == "dispatched"
    assert item.current_job_id == new_job.id
    assert new_job.assembly_plan["speech_cleanup_contract"] == (
        "required_v1" if outcome_status is None else "off_v1"
    )
    if outcome_status is None:
        assert (
            new_job.assembly_plan["_speech_cleanup_internal"]["preflight_snapshot"]
            == (old_job.assembly_plan["_speech_cleanup_internal"]["preflight_snapshot"])
        )
        assert "speech_cleanup_outcome" not in new_job.assembly_plan
    else:
        receipt = new_job.assembly_plan["speech_cleanup_outcome"]
        assert receipt == {
            "status": outcome_status,
            "removal_count": 0,
            "removed_ms": 0,
            "job_id": str(new_job.id),
            "render_generation_id": new_job.assembly_plan["creator_generation_id"],
        }
        if outcome_status == "bypassed_unchecked":
            assert new_job.assembly_plan["_speech_cleanup_internal"] == {
                "outcome_analysis_id": str(row.id)
            }
        else:
            assert (
                new_job.assembly_plan["_speech_cleanup_internal"]["preflight_snapshot"]
                == old_job.assembly_plan["_speech_cleanup_internal"]["preflight_snapshot"]
            )
    assert new_job.assembly_plan["creator_generation_id"] != "publish-failed-generation"
    build.assert_called_once()
    enqueue.assert_called_once()


def test_preflight_publish_retry_fails_closed_on_source_or_generation_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, item, _row, old_job, _new_job, build, enqueue = _run_cleanup_preflight_publish_recovery(
        monkeypatch,
        outcome_status="bypassed_unchecked",
        active_source_generation="replacement-generation",
    )

    assert result.outcome == "speech_cleanup_recovery_conflict"
    assert item.current_job_id == old_job.id
    build.assert_not_called()
    enqueue.assert_not_called()


def test_cleanup_decision_and_job_commit_before_broker_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    item = _cleanup_dispatch_item()
    row = _cleanup_analysis(item)
    plan = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), preference_summary="", ownership_epoch=0
    )
    job = SimpleNamespace(
        id=uuid.uuid4(),
        assembly_plan={},
        all_candidates={"clip_paths": list(item.clip_gcs_paths)},
    )
    session = MagicMock()
    session.get.return_value = row
    order: list[str] = []

    def commit() -> None:
        assert row.decision == "clean"
        session.add.assert_called_once_with(job)
        assert job.assembly_plan["speech_cleanup_contract"] == "required_v1"
        assert "preflight_snapshot" in job.assembly_plan["_speech_cleanup_internal"]
        order.append("commit")

    def enqueue(*_args: object, **_kwargs: object) -> None:
        assert order == ["commit"]
        order.append("publish")

    session.commit.side_effect = commit
    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(
            source=SimpleNamespace(source_policy_fingerprint="active-source-fingerprint")
        ),
    )
    with (
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=None,
        ),
        patch("app.services.generative_jobs.build_generative_job", return_value=job),
        patch("app.services.job_dispatch.enqueue_orchestrator_sync", side_effect=enqueue),
    ):
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=0,
            speech_cleanup_analysis_id=str(row.id),
            speech_cleanup_choice="clean",
        )

    assert result.outcome == "dispatched"
    assert order == ["commit", "publish"]


@pytest.mark.parametrize("failure", ["missing", "wrong_item", "superseded", "fingerprint"])
def test_invalid_preflight_snapshot_mints_no_job(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from app.config import settings

    item = _cleanup_dispatch_item()
    row = _cleanup_analysis(item)
    if failure == "wrong_item":
        row.plan_item_id = uuid.uuid4()
    elif failure == "superseded":
        row.superseded_at = datetime.now(UTC)
    elif failure == "fingerprint":
        row.source_policy_fingerprint = "obsolete-source"
    plan = SimpleNamespace(
        id=uuid.uuid4(), user_id=uuid.uuid4(), preference_summary="", ownership_epoch=0
    )
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={})
    session = MagicMock()
    session.get.return_value = None if failure == "missing" else row
    monkeypatch.setattr(settings, "speech_cleanup_mode", "opt_in")
    monkeypatch.setattr(settings, "silence_cut_enabled", True)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_args, **_kwargs: SimpleNamespace(
            source=SimpleNamespace(source_policy_fingerprint="active-source-fingerprint")
        ),
    )
    with (
        patch(
            "app.services.smart_captions.resolve_smart_captions_context_sync",
            return_value=None,
        ),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync") as enqueue,
    ):
        result = _dispatch_item_render(
            session,
            item,
            plan,
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=0,
            speech_cleanup_analysis_id=str(row.id),
            speech_cleanup_choice="clean",
        )

    assert result.outcome == "speech_cleanup_analysis_conflict"
    assert row.decision is None
    build.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()
    enqueue.assert_not_called()


def test_audio_only_preflight_cannot_persist_choice_or_mint_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _cleanup_dispatch_item()
    row = _cleanup_analysis(item)
    item.clip_gcs_paths = []
    item.clip_assignments = []
    item.edit_format = "narrated_planned"
    item.audio_mode = "voiceover"
    item.voiceover_gcs_path = "users/u/plan/i/voice.m4a"
    item.voiceover_generation = "voice-generation-3"
    item.voiceover_duration_s = 12.0
    session = MagicMock()
    session.get.return_value = row

    with (
        patch("app.services.generative_jobs.build_generative_job") as build,
        patch(
            "app.services.plan_item_media.resolve_item_narration",
            return_value=SimpleNamespace(
                source=SimpleNamespace(source_policy_fingerprint=row.source_policy_fingerprint)
            ),
        ),
    ):
        result = _dispatch_item_render(
            session,
            item,
            SimpleNamespace(
                id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                preference_summary="",
                ownership_epoch=0,
            ),
            {"tone": "direct", "content_pillars": []},
            ownership_epoch=0,
            speech_cleanup_analysis_id=str(row.id),
            speech_cleanup_choice="clean",
        )

    assert result.outcome == "video_required"
    assert row.decision is None
    assert item.speech_cleanup_enabled is False
    build.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_missing_persona_rejects_before_job_or_queue() -> None:
    # Missing persona is an invalid plan/persona pair. Never mint a Job with an
    # empty/default persona snapshot.
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "first 5am workout"
    item.idea = ""

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, None)  # persona row missing
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync") as enqueue,
    ):
        generate_plan_item_videos.run(str(item.id))

    mock_build.assert_not_called()
    enqueue.assert_not_called()
    ctx.__enter__.return_value.add.assert_not_called()


# ---- post-generation near-duplicate dedup (_dedup_and_replace) --------------

from app.agents._schemas.content_plan import ContentPlanInput, ContentPlanOutput  # noqa: E402
from app.agents._schemas.persona import Persona  # noqa: E402
from app.tasks.content_plan_build import _dedup_and_replace  # noqa: E402


class _FakeAgent:
    """Stands in for ContentPlanGeneratorAgent: records calls, returns a canned
    regen output (or raises) so the dedup orchestration is testable with no LLM."""

    def __init__(self, regen=None, raises=False) -> None:  # noqa: ANN001
        self.calls = 0
        self._regen = regen
        self._raises = raises

    def run(self, agent_input, ctx):  # noqa: ANN001, ARG002
        self.calls += 1
        if self._raises:
            raise RuntimeError("regen boom")
        return self._regen


def _spec(day: int, idea: str, **kw) -> PlanItemSpec:  # noqa: ANN003
    return PlanItemSpec(day_index=day, theme=kw.pop("theme", "pillar"), idea=idea, **kw)


def _plan_input() -> ContentPlanInput:
    return ContentPlanInput(
        persona=Persona(
            summary="s",
            content_pillars=["a"],
            tone="warm",
            audience="x",
            posting_cadence="4/wk",
            sample_topics=["y"],
        ),
        horizon_days=3,
    )


def test_dedup_skips_regen_when_no_duplicates() -> None:
    output = ContentPlanOutput(
        items=[
            _spec(1, "tour of my favorite coffee shops"),
            _spec(2, "best hiking trails near me"),
            _spec(3, "how I meal prep for the week"),
        ]
    )
    agent = _FakeAgent(raises=True)  # would blow up if regen were attempted
    result = _dedup_and_replace(agent, _plan_input(), output, "pid")
    assert agent.calls == 0  # no extra LLM call when the plan is already varied
    assert result is output


def test_dedup_replaces_duplicate_with_distinct_regen_idea() -> None:
    output = ContentPlanOutput(
        items=[
            _spec(1, "5am gym workout motivation routine"),
            _spec(2, "my favorite weekend brunch spots downtown"),
            _spec(3, "early morning gym workout motivation routine"),  # dup of day 1
        ]
    )
    regen = ContentPlanOutput(
        items=[
            _spec(
                7,
                "a guide to local hiking trails",
                theme="outdoors",
                rationale="save-worthy",
                edit_format="single_hero",
            ),
        ]
    )
    agent = _FakeAgent(regen=regen)
    result = _dedup_and_replace(agent, _plan_input(), output, "pid")

    assert agent.calls == 1
    assert [it.day_index for it in result.items] == [1, 2, 3]  # full length, day kept
    day3 = next(it for it in result.items if it.day_index == 3)
    assert day3.idea == "a guide to local hiking trails"
    assert day3.edit_format == "single_hero"  # content fields carried from candidate
    assert day3.rationale == "save-worthy"


def test_dedup_keeps_original_when_regen_fails() -> None:
    output = ContentPlanOutput(
        items=[
            _spec(1, "5am gym workout motivation routine"),
            _spec(2, "early morning gym workout motivation routine"),  # dup
        ]
    )
    agent = _FakeAgent(raises=True)
    result = _dedup_and_replace(agent, _plan_input(), output, "pid")
    assert result is output  # best-effort: a failed regen never degrades the plan


def test_dedup_keeps_original_slot_when_regen_has_no_distinct_idea() -> None:
    output = ContentPlanOutput(
        items=[
            _spec(1, "5am gym workout motivation routine"),
            _spec(2, "early morning gym workout motivation routine"),  # dup of day 1
        ]
    )
    # Regen only offers another near-dup → nothing distinct to swap in.
    regen = ContentPlanOutput(items=[_spec(9, "5am gym workout motivation session")])
    agent = _FakeAgent(regen=regen)
    result = _dedup_and_replace(agent, _plan_input(), output, "pid")
    assert [it.idea for it in result.items] == [
        "5am gym workout motivation routine",
        "early morning gym workout motivation routine",
    ]


# ── regenerate_content_plan: the "their say" invariant ────────────────────────


# ── regenerate_content_plan: the "their say" invariant ────────────────────────


def _plan_item(day: int, *, user_edited: bool, current_job_id: uuid.UUID | None) -> MagicMock:
    it = MagicMock()
    it.day_index = day
    it.user_edited = user_edited  # explicit — a bare MagicMock attr is truthy
    it.current_job_id = current_job_id
    it.theme = f"old theme {day}"
    it.idea = f"old idea {day}"
    return it


def _valid_persona() -> dict:
    return {
        "summary": "you film calm morning routines",
        "content_pillars": ["mornings", "discipline"],
        "tone": "warm and steady",
        "audience": "people who want a calmer start",
        "posting_cadence": "3-4 posts/week",
        "sample_topics": ["sunrise walk"],
    }


def test_regenerate_preserves_user_edited_and_in_flight_items() -> None:
    """The load-bearing invariant: regenerate replaces ONLY a day that is neither
    hand-edited nor already rendering. Day 1 (user_edited) and day 3 (current_job)
    are kept verbatim; only day 2 is deleted and re-inserted from fresh AI output."""
    user_id = uuid.uuid4()
    edited = _plan_item(1, user_edited=True, current_job_id=None)
    regenerable = _plan_item(2, user_edited=False, current_job_id=None)
    in_flight = _plan_item(3, user_edited=False, current_job_id=uuid.uuid4())

    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = [edited, regenerable, in_flight]

    persona_row = MagicMock()
    persona_row.persona = _valid_persona()

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    # Fresh AI output proposes all three days; only the regenerable one may land.
    output = MagicMock()
    output.items = [
        PlanItemSpec(day_index=1, theme="NEW 1", idea="new idea 1"),
        PlanItemSpec(day_index=2, theme="NEW 2", idea="new idea 2"),
        PlanItemSpec(day_index=3, theme="NEW 3", idea="new idea 3"),
    ]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch(
            "app.services.feedback_summary.rollup_user_feedback",
            return_value="liked: 3, disliked: 1",
        ),
    ):
        regenerate_content_plan.run(str(plan.id))

    # The feedback summary was persisted on the plan.
    assert plan.preference_summary == "liked: 3, disliked: 1"
    # ONLY the regenerable day-2 item was deleted (protected days untouched).
    deleted = [c.args[0] for c in session.delete.call_args_list]
    assert deleted == [regenerable]
    # ONLY a single new item was added, for day 2, from the fresh AI output.
    added = [c.args[0] for c in session.add.call_args_list]
    assert len(added) == 1
    assert added[0].day_index == 2
    assert added[0].theme == "NEW 2"


# ── filming_guide persistence in both copy blocks ─────────────────────────────


def _spec_with_guide() -> PlanItemSpec:
    """A PlanItemSpec with a non-empty filming_guide for persistence assertions."""
    from app.agents._schemas.content_plan import ShotSpec  # noqa: PLC0415

    return PlanItemSpec(
        day_index=1,
        theme="morning routine",
        idea="film the 5am gym start",
        filming_guide=[
            ShotSpec(what="creator lacing shoes", how="close-up", duration_s=5),
        ],
    )


def test_generate_persists_filming_guide() -> None:
    """generate_content_plan must pass filming_guide into the PlanItem row.

    Locks the persistence copy block so a missed filming_guide=[...] line is
    caught before reaching prod (where it would silently store [] on every item).
    """
    plan_id = uuid.uuid4()
    plan = MagicMock()
    plan.id = plan_id
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = []
    plan.plan_status = "generating"

    persona_row = MagicMock()
    persona_row.persona = _valid_persona()

    user = MagicMock()
    user.onboarding_status = "plan_ready"

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
            __import__("app.models", fromlist=["User"]).User: user,
        }.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    spec = _spec_with_guide()
    output = MagicMock()
    output.items = [spec]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch("app.tasks.content_plan_build._dedup_and_replace", return_value=output),
    ):
        from app.tasks.content_plan_build import generate_content_plan  # noqa: PLC0415

        generate_content_plan.run(str(plan_id))

    added = [c.args[0] for c in session.add.call_args_list]
    assert len(added) == 1
    persisted_guide = added[0].filming_guide
    assert isinstance(persisted_guide, list)
    assert len(persisted_guide) == 1
    assert persisted_guide[0]["what"] == "creator lacing shoes"
    assert persisted_guide[0]["duration_s"] == 5


def test_regenerate_persists_filming_guide() -> None:
    """regenerate_content_plan must pass filming_guide into the PlanItem row.

    This is the second copy block — both must be updated or regenerated plans
    silently lose their filming guides.
    """
    plan_id = uuid.uuid4()
    regenerable = _plan_item(2, user_edited=False, current_job_id=None)

    plan = MagicMock()
    plan.id = plan_id
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = [regenerable]

    persona_row = MagicMock()
    persona_row.persona = _valid_persona()

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    spec = _spec_with_guide()
    output = MagicMock()
    output.items = [spec]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch(
            "app.services.feedback_summary.rollup_user_feedback",
            return_value="",
        ),
    ):
        regenerate_content_plan.run(str(plan_id))

    added = [c.args[0] for c in session.add.call_args_list]
    assert len(added) == 1
    persisted_guide = added[0].filming_guide
    assert isinstance(persisted_guide, list)
    assert len(persisted_guide) == 1
    assert persisted_guide[0]["what"] == "creator lacing shoes"


# ── posts_per_week flows from persona JSONB into ContentPlanInput ─────────────


def test_persona_posts_per_week_reaches_plan_input() -> None:
    """Persona JSONB with posts_per_week=3 must produce a Persona with posts_per_week=3
    inside ContentPlanInput, which the plan generator then uses to derive the cap.

    This is the task-boundary test: `Persona(**persona_row.persona)` in
    generate_content_plan / regenerate_content_plan must forward the new key
    without any extra code, because Persona validates via **kwargs.
    """
    from app.agents._schemas.content_plan import ContentPlanInput  # noqa: PLC0415
    from app.agents._schemas.persona import Persona  # noqa: PLC0415

    persona_dict = {
        "summary": "you film calm morning routines",
        "content_pillars": ["mornings", "discipline"],
        "tone": "warm and steady",
        "audience": "people who want a calmer start",
        "posting_cadence": "3-4 posts/week",
        "posts_per_week": 3,
        "sample_topics": ["sunrise walk"],
    }
    # Simulate what generate_content_plan / regenerate_content_plan does.
    persona = Persona(**persona_dict)
    assert persona.posts_per_week == 3

    plan_input = ContentPlanInput(persona=persona, horizon_days=30)
    assert plan_input.persona.posts_per_week == 3


# ── M1 idea_seeds plumbing: persona.idea_seeds[].text → ContentPlanInput ─────


def test_locked_plan_persona_uses_global_lock_order() -> None:
    """Plan builds lock ContentPlan before the owned Persona writer lock."""
    from app.tasks.content_plan_build import _lock_owned_plan_persona  # noqa: PLC0415

    plan_id = uuid.uuid4()
    plan = MagicMock()
    plan.id = plan_id
    plan.ownership_epoch = 4
    persona_row = MagicMock()
    session = MagicMock()
    session.get.return_value = plan

    with patch(
        "app.tasks.content_plan_build.load_owned_plan_persona_sync",
        return_value=persona_row,
    ) as load_persona:
        assert _lock_owned_plan_persona(session, plan_id) == (plan, persona_row)

    # populate_existing is part of the contract, not an incidental kwarg. Without
    # it, a plan this session already cached unlocked keeps its pre-lock
    # ownership_epoch, and the epoch check silently passes for a stale worker.
    # Pin it so the pairing cannot be dropped.
    session.get.assert_called_once_with(
        ContentPlan, plan_id, with_for_update=True, populate_existing=True
    )
    load_persona.assert_called_once_with(session, plan, for_update=True)


def _gen_session(plan, persona_row) -> MagicMock:
    """sync_session context mock for generate_content_plan."""
    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def test_idea_seeds_reach_content_plan_input() -> None:
    """persona_row.idea_seeds[].text must flow into ContentPlanInput.user_idea_seeds.

    Guards the silent-default regression: if the extraction is dropped or the
    field name drifts, user ideas are silently ignored and every plan falls back
    to the market IDEA_BANK. Blank seeds must be filtered out.
    """
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.plan_status = "generating"

    persona_row = MagicMock()
    persona_row.persona = {
        "summary": "tech founder sharing behind-the-scenes",
        "content_pillars": ["building", "founder life"],
        "tone": "honest and direct",
        "audience": "indie hackers",
        "posting_cadence": "3 posts/week",
    }
    persona_row.idea_seeds = [
        {"id": "a1", "text": "behind-the-scenes of my launch week", "status": "pending"},
        {"id": "a2", "text": "", "status": "pending"},  # blank — must be filtered
        {"id": "a3", "text": "day-in-the-life: moving apartments", "status": "pending"},
    ]
    persona_row.tiktok_profile = None
    persona_row.style = None

    captured_inputs: list[ContentPlanInput] = []
    output = MagicMock()
    output.items = []
    agent = MagicMock()

    def _run(agent_input, ctx):  # noqa: ANN001, ARG001
        captured_inputs.append(agent_input)
        return output

    agent.run = MagicMock(side_effect=_run)

    ctx = _gen_session(plan, persona_row)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch("app.tasks.content_plan_build._dedup_and_replace", return_value=output),
    ):
        generate_content_plan.run(str(plan.id))

    assert len(captured_inputs) == 1
    seeds = captured_inputs[0].user_idea_seeds
    assert "behind-the-scenes of my launch week" in seeds
    assert "day-in-the-life: moving apartments" in seeds
    # blank seed must be filtered
    assert "" not in seeds
    assert len(seeds) == 2


# ── reroll_plan_item task tests ──────────────────────────────────────────────


from app.tasks.content_plan_build import reroll_plan_item  # noqa: E402


def _reroll_session(item, plan, persona_row=None) -> MagicMock:
    """sync_session context mock that routes get() by model class."""
    session = MagicMock()

    def _get(model, _pk, **_kw):  # noqa: ANN001
        return {PlanItem: item, ContentPlan: plan, PersonaRow: persona_row}.get(model)

    session.get = MagicMock(side_effect=_get)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _idea_item(day: int = 3, idea: str = "film the 5am start") -> MagicMock:
    it = MagicMock()
    it.id = uuid.uuid4()
    it.content_plan_id = uuid.uuid4()
    it.day_index = day
    it.theme = "morning routine"
    it.idea = idea
    it.filming_suggestion = None
    it.rationale = None
    it.filming_guide = []
    it.item_status = "rerolling"
    it.user_edited = False
    return it


def _plan_with_items(items) -> MagicMock:
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = items
    for item in items:
        item.content_plan_id = plan.id
    return plan


def _persona_row() -> MagicMock:
    row = MagicMock()
    row.persona = _valid_persona()
    return row


def test_reroll_patches_item_fields_preserving_day_index() -> None:
    """Fresh spec is applied to the item in-place; day_index is untouched."""
    item = _idea_item(day=7, idea="old idea")
    sibling = MagicMock()
    sibling.idea = "sibling idea"
    plan = _plan_with_items([item, sibling])

    fresh_spec = PlanItemSpec(day_index=14, theme="new theme", idea="brand new idea")
    output = MagicMock()
    output.items = [fresh_spec]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    ctx = _reroll_session(item, plan, _persona_row())

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch(
            "app.tasks.content_plan_build.choose_replacements",
            return_value=[fresh_spec],
        ),
    ):
        reroll_plan_item.run(str(item.id))

    # Fields patched
    assert item.theme == "new theme"
    assert item.idea == "brand new idea"
    assert item.item_status == "idea"
    assert item.user_edited is False
    # day_index must be preserved (we never write it)
    assert item.day_index == 7


def test_reroll_resets_to_idea_on_failure() -> None:
    """Agent throws; item_status must be reset to 'idea' (best-effort)."""
    item = _idea_item()
    plan = _plan_with_items([item])

    ctx = _reroll_session(item, plan, _persona_row())

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch(
            "app.tasks.content_plan_build.ContentPlanGeneratorAgent",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(Exception),  # retry re-raises
    ):
        reroll_plan_item.run(str(item.id))

    assert item.item_status == "idea"


def test_reroll_exclude_list_is_all_plan_ideas() -> None:
    """ContentPlanInput.exclude_ideas must include all plan item ideas."""
    item = _idea_item(idea="film the dark start")
    other = MagicMock()
    other.idea = "cook a quick meal"
    plan = _plan_with_items([item, other])

    captured_inputs: list[ContentPlanInput] = []

    output = MagicMock()
    output.items = []
    agent = MagicMock()

    def _run(agent_input, ctx):  # noqa: ANN001, ARG001
        captured_inputs.append(agent_input)
        return output

    agent.run = MagicMock(side_effect=_run)

    ctx = _reroll_session(item, plan, _persona_row())

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch("app.tasks.content_plan_build.choose_replacements", return_value=[]),
    ):
        reroll_plan_item.run(str(item.id))

    assert len(captured_inputs) == 1
    excluded = captured_inputs[0].exclude_ideas
    assert "film the dark start" in excluded
    assert "cook a quick meal" in excluded


# ── Narrative clip order (filming-guide alignment) ────────────────────────────


def _narrative_item(guide: list[dict], assignments: list[dict]) -> MagicMock:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.filming_guide = guide
    item.clip_assignments = assignments
    return item


def test_narrative_order_derives_guide_order_not_attach_order() -> None:
    """clip_assignments arrive in client attach order; the guide's shot
    sequence must win."""
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [
        {"shot_id": "s1", "what": "opening", "duration_s": 4},
        {"shot_id": "s2", "what": "middle", "duration_s": 5},
        {"shot_id": "s3", "what": "ending", "duration_s": 3},
    ]
    # Attached scrambled: s3's clip first, pool clip, s1's, s2's.
    assignments = [
        {"gcs_path": "u/c3.mp4", "shot_id": "s3"},
        {"gcs_path": "u/pool.mp4", "shot_id": None},
        {"gcs_path": "u/c1.mp4", "shot_id": "s1"},
        {"gcs_path": "u/c2.mp4", "shot_id": "s2"},
    ]
    # set_item_clips puts slot clips first (attach order), pool after:
    clip_paths = ["u/c3.mp4", "u/c1.mp4", "u/c2.mp4", "u/pool.mp4"]
    item = _narrative_item(guide, assignments)

    ordered, count = _narrative_clip_order(item, clip_paths)

    assert ordered == ["u/c1.mp4", "u/c2.mp4", "u/c3.mp4", "u/pool.mp4"]
    assert count == 3


def test_narrative_order_stale_shot_id_becomes_pool() -> None:
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [{"shot_id": "s1", "what": "opening", "duration_s": 4}]
    assignments = [
        {"gcs_path": "u/stale.mp4", "shot_id": "s-removed-by-reroll"},
        {"gcs_path": "u/c1.mp4", "shot_id": "s1"},
    ]
    clip_paths = ["u/stale.mp4", "u/c1.mp4"]
    item = _narrative_item(guide, assignments)

    ordered, count = _narrative_clip_order(item, clip_paths)

    assert ordered == ["u/c1.mp4", "u/stale.mp4"]
    assert count == 1


def test_narrative_order_no_guide_is_noop() -> None:
    from app.tasks.content_plan_build import _narrative_clip_order

    item = _narrative_item([], [{"gcs_path": "u/a.mp4", "shot_id": None}])
    ordered, count = _narrative_clip_order(item, ["u/a.mp4"])

    assert ordered == ["u/a.mp4"]
    assert count == 0


def test_narrative_order_no_shot_assignments_is_noop() -> None:
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [{"shot_id": "s1", "what": "opening", "duration_s": 4}]
    item = _narrative_item(guide, [{"gcs_path": "u/a.mp4", "shot_id": None}])
    ordered, count = _narrative_clip_order(item, ["u/a.mp4"])

    assert ordered == ["u/a.mp4"]
    assert count == 0


def test_narrative_order_assignment_path_not_in_clip_paths_ignored() -> None:
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [{"shot_id": "s1", "what": "opening", "duration_s": 4}]
    item = _narrative_item(guide, [{"gcs_path": "u/ghost.mp4", "shot_id": "s1"}])
    ordered, count = _narrative_clip_order(item, ["u/real.mp4"])

    assert ordered == ["u/real.mp4"]
    assert count == 0


def test_narrative_order_multi_clip_per_shot() -> None:
    """Multiple clips assigned to the same shot all appear in guide order, then pool."""
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [
        {"shot_id": "s1", "what": "opening", "duration_s": 4},
        {"shot_id": "s2", "what": "closing", "duration_s": 5},
    ]
    # s1 has two clips attached in reverse order; s2 has one; one pool clip.
    assignments = [
        {"gcs_path": "u/c1b.mp4", "shot_id": "s1"},
        {"gcs_path": "u/c2.mp4", "shot_id": "s2"},
        {"gcs_path": "u/c1a.mp4", "shot_id": "s1"},
        {"gcs_path": "u/pool.mp4", "shot_id": None},
    ]
    clip_paths = ["u/c1b.mp4", "u/c2.mp4", "u/c1a.mp4", "u/pool.mp4"]
    item = _narrative_item(guide, assignments)

    ordered, count = _narrative_clip_order(item, clip_paths)

    # Guide order: s1 clips (both) before s2 clip; pool is tail.
    assert ordered[:2] == ["u/c1b.mp4", "u/c1a.mp4"]  # both s1 clips in attach order
    assert ordered[2] == "u/c2.mp4"  # s2 clip
    assert ordered[3] == "u/pool.mp4"  # pool last
    assert count == 3  # 3 clips placed (the 2 for s1 + 1 for s2)


# ── Footage pool ────────────────────────────────────────────────────────────────


def test_pool_match_limit_within_matcher_schema():
    """_POOL_MATCH_LIMIT must validate against ClipPlanMatcherInput's schema bound.

    Regression: the pool shipped with limit 8 against a le=7 field — every pool
    match failed at pydantic validation before the matcher ever ran (dogfood,
    2026-06-11)."""
    from app.agents.clip_plan_matcher import ClipPlanMatcherInput, ClipSummary, PlanItemSummary
    from app.tasks.content_plan_build import _POOL_MATCH_LIMIT

    inp = ClipPlanMatcherInput(
        clips=[
            ClipSummary(
                clip_gcs_path="users/u/plan-pool/p/a.mp4",
                hook_text="",
                hook_score=5.0,
                detected_subject="street scene",
                transcript_excerpt="",
            )
        ],
        items=[PlanItemSummary(item_id="i1", theme="t", idea="i", filming_suggestion="")],
        max_assignments=_POOL_MATCH_LIMIT,
    )
    assert inp.max_assignments == _POOL_MATCH_LIMIT


# ── Generate ideas: one fresh bare idea per click ─────────────────────────────


def _generate_valid_persona(summary: str = "creator") -> dict:
    return {
        "summary": summary,
        "content_pillars": ["fitness"],
        "tone": "direct",
        "audience": "beginners",
        "posting_cadence": "daily",
        "sample_topics": ["mobility"],
    }


def _generate_existing_item(idea: str, *, day_index: int | None, position: int) -> MagicMock:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.idea = idea
    item.day_index = day_index
    item.position = position
    return item


def _plan_for_generate(plan_id: str, items: list[MagicMock]) -> MagicMock:
    plan = MagicMock()
    plan.id = uuid.UUID(plan_id)
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = {}
    plan.plan_status = "generating"
    plan.generation_started_at = datetime.now(UTC)
    plan.items = items
    return plan


def _ctx(session: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _fallback_result(row: MagicMock | None) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.first.return_value = row
    return result


def _generated_output(*specs: PlanItemSpec) -> ContentPlanOutput:
    return ContentPlanOutput(items=list(specs))


def _generated_spec(theme: str = "Desk Mobility", idea: str = "three stretches") -> PlanItemSpec:
    from app.agents._schemas.content_plan import ShotSpec  # noqa: PLC0415

    return PlanItemSpec(
        day_index=1,
        theme=theme,
        idea=idea,
        filming_suggestion="Film each stretch beside your desk",
        rationale="solves a common pain point",
        edit_format="single_hero",
        filming_guide=[ShotSpec(what="show tight shoulders", how="medium shot", duration_s=3)],
    )


def test_generate_ideas_creates_exactly_one_unscheduled_idea() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    existing = _generate_existing_item("existing draft", day_index=None, position=4)
    plan = _plan_for_generate(plan_id, [existing])
    plan.events = {"text": "conference next week"}

    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()

    added = []
    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    session.add = MagicMock(side_effect=added.append)
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
    ):
        mock_trace.return_value = _ctx(MagicMock())
        mock_agent_cls.return_value.run.return_value = _generated_output(
            _generated_spec(),
            _generated_spec("Second idea", "should be ignored"),
        )

        generate_ideas_into_plan.run(plan_id)

    mock_agent_cls.return_value.run.assert_called_once()
    agent_input = mock_agent_cls.return_value.run.call_args.args[0]
    assert agent_input.horizon_days == 1
    assert agent_input.events == "conference next week"
    assert len(added) == 1
    assert added[0].day_index is None
    assert added[0].item_status == "idea"
    assert added[0].position == 5
    assert added[0].theme == "Desk Mobility"
    assert added[0].filming_suggestion == "Film each stretch beside your desk"
    assert added[0].rationale == "solves a common pain point"
    assert added[0].edit_format == "single_hero"
    assert added[0].filming_guide[0]["what"] == "show tight shoulders"
    assert added[0].filming_guide[0]["shot_id"]
    assert existing.day_index is None
    assert plan.plan_status == "ready"


def test_generate_ideas_passes_existing_ideas_as_exclusions() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(
        plan_id,
        [
            _generate_existing_item("existing scheduled post", day_index=3, position=1),
            _generate_existing_item("", day_index=None, position=2),
            _generate_existing_item("existing bare draft", day_index=None, position=3),
        ],
    )

    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    session.add = MagicMock()
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
    ):
        mock_trace.return_value = _ctx(MagicMock())
        mock_agent_cls.return_value.run.return_value = _generated_output(_generated_spec())

        generate_ideas_into_plan.run(plan_id)

    agent_input = mock_agent_cls.return_value.run.call_args.args[0]
    assert agent_input.exclude_ideas == ["existing scheduled post", "existing bare draft"]
    assert agent_input.user_idea_seeds == []


def test_generate_ideas_ignores_legacy_scheduled_items_except_exclusions_and_position() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    legacy = _generate_existing_item("legacy scheduled idea", day_index=8, position=9)
    bare = _generate_existing_item("bare draft", day_index=None, position=2)
    plan = _plan_for_generate(plan_id, [legacy, bare])

    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()

    added = []
    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    session.add = MagicMock(side_effect=added.append)
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
    ):
        mock_trace.return_value = _ctx(MagicMock())
        mock_agent_cls.return_value.run.return_value = _generated_output(_generated_spec())

        generate_ideas_into_plan.run(plan_id)

    agent_input = mock_agent_cls.return_value.run.call_args.args[0]
    assert agent_input.exclude_ideas == ["legacy scheduled idea", "bare draft"]
    assert len(added) == 1
    assert added[0].position == 10
    assert added[0].day_index is None
    assert legacy.day_index == 8
    assert bare.day_index is None


def test_generate_ideas_dangling_persona_has_no_owner_fallback() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {ContentPlan: plan}.get(model)
    )
    session.add = MagicMock()
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
    ):
        mock_agent_cls.return_value.run.return_value = _generated_output(_generated_spec())

        generate_ideas_into_plan.run(plan_id)

    mock_agent_cls.assert_not_called()
    session.execute.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()
    assert plan.plan_status == "generating"


def test_generate_ideas_sparse_persona_fails_before_agent() -> None:
    """An unready payload cannot be replaced by invented generic context."""
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])

    sparse_persona = MagicMock()
    sparse_persona.persona = {"footage_type_bias": ["mixed"]}

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: sparse_persona,
        }.get(model)
    )
    session.add = MagicMock()
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
    ):
        mock_trace.return_value = _ctx(MagicMock())
        mock_agent_cls.return_value.run.return_value = _generated_output(_generated_spec())

        generate_ideas_into_plan.run(plan_id)

    session.execute.assert_not_called()
    mock_agent_cls.assert_not_called()
    assert plan.plan_status == "failed"
    session.add.assert_not_called()
    session.commit.assert_called_once()


def test_generate_ideas_no_persona_rejects_without_writing_item() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {ContentPlan: plan}.get(model)
    )
    session.execute.return_value = _fallback_result(None)
    session.add = MagicMock()
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch.object(generate_ideas_into_plan.request, "retries", 1),
    ):
        generate_ideas_into_plan.run(plan_id)

    mock_agent_cls.assert_not_called()
    session.add.assert_not_called()
    assert plan.plan_status == "generating"
    session.commit.assert_not_called()


def test_generate_ideas_agent_failure_retries_without_terminalizing() -> None:
    from celery.exceptions import Retry  # noqa: PLC0415

    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])

    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    session.add = MagicMock()
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
        patch.object(generate_ideas_into_plan.request, "retries", 0),
        patch.object(generate_ideas_into_plan, "retry", side_effect=Retry()) as mock_retry,
        pytest.raises(Retry),
    ):
        mock_trace.return_value = _ctx(MagicMock())
        mock_agent_cls.return_value.run.side_effect = RuntimeError("agent boom")

        generate_ideas_into_plan.run(plan_id)

    session.add.assert_not_called()
    assert plan.plan_status == "generating"
    session.commit.assert_not_called()
    mock_retry.assert_called_once()
    assert isinstance(mock_retry.call_args.kwargs["exc"], RuntimeError)


def test_generate_ideas_retry_publish_failure_marks_failed() -> None:
    from celery.exceptions import Reject  # noqa: PLC0415

    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])
    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()
    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
        patch.object(generate_ideas_into_plan.request, "retries", 0),
        patch.object(
            generate_ideas_into_plan,
            "retry",
            side_effect=Reject(RuntimeError("retry publish failed"), requeue=False),
        ),
        pytest.raises(Reject),
    ):
        mock_trace.return_value = _ctx(MagicMock())
        mock_agent_cls.return_value.run.side_effect = RuntimeError("agent boom")
        generate_ideas_into_plan.run(plan_id)

    assert plan.plan_status == "failed"
    session.commit.assert_called_once()


def test_generate_ideas_retry_exhaustion_marks_failed() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])

    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    session.add = MagicMock()
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
        patch.object(generate_ideas_into_plan.request, "retries", 1),
        pytest.raises(RuntimeError, match="agent boom"),
    ):
        mock_trace.return_value = _ctx(MagicMock())
        mock_agent_cls.return_value.run.side_effect = RuntimeError("agent boom")

        generate_ideas_into_plan.run(plan_id)

    session.add.assert_not_called()
    assert plan.plan_status == "failed"
    session.commit.assert_called_once()


def test_generate_ideas_setup_failure_without_snapshot_does_not_write() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])
    session = MagicMock()
    session.get = MagicMock(side_effect=[RuntimeError("setup boom"), plan])
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch.object(generate_ideas_into_plan.request, "retries", 1),
        pytest.raises(RuntimeError, match="setup boom"),
    ):
        generate_ideas_into_plan.run(plan_id)

    assert plan.plan_status == "generating"
    session.commit.assert_not_called()


def test_generate_ideas_stale_delivery_noops_for_terminal_plan() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])
    plan.plan_status = "ready"
    session = MagicMock()
    session.get = MagicMock(return_value=plan)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
    ):
        generate_ideas_into_plan.run(plan_id)

    mock_agent_cls.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_generate_ideas_stale_attempt_token_noops_after_retry() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])
    previous_token = (plan.generation_started_at - timedelta(seconds=1)).isoformat()
    session = MagicMock()
    session.get = MagicMock(return_value=plan)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
    ):
        generate_ideas_into_plan.run(plan_id, previous_token)

    mock_agent_cls.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_generate_ideas_attempt_token_matches_equivalent_timezone_offset() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])
    equivalent_token = plan.generation_started_at.astimezone(
        timezone(timedelta(hours=1))
    ).isoformat()
    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()
    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
    ):
        mock_trace.return_value = _ctx(MagicMock())
        mock_agent_cls.return_value.run.return_value = _generated_output(_generated_spec())
        generate_ideas_into_plan.run(plan_id, equivalent_token)

    mock_agent_cls.assert_called_once()
    assert plan.plan_status == "ready"
    session.commit.assert_called_once()


def test_generate_ideas_stale_success_does_not_write_after_sibling_terminalizes() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])
    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()

    setup_session = MagicMock()
    setup_session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    final_session = MagicMock()
    final_session.get = MagicMock(return_value=plan)
    sessions = iter([_ctx(setup_session), _ctx(final_session)])

    with (
        patch("app.tasks.content_plan_build.sync_session", side_effect=lambda: next(sessions)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
    ):
        mock_trace.return_value = _ctx(MagicMock())

        def succeed_after_sibling(*_args, **_kwargs):
            plan.plan_status = "ready"
            return _generated_output(_generated_spec())

        mock_agent_cls.return_value.run.side_effect = succeed_after_sibling
        generate_ideas_into_plan.run(plan_id)

    final_session.get.assert_any_call(
        ContentPlan,
        uuid.UUID(plan_id),
        with_for_update=True,
        populate_existing=True,
    )
    final_session.add.assert_not_called()
    final_session.commit.assert_not_called()


def test_generate_ideas_stale_failure_does_not_overwrite_ready_plan() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    plan = _plan_for_generate(plan_id, [])

    persona_row = MagicMock()
    persona_row.persona = _generate_valid_persona()

    run_session = MagicMock()
    run_session.get = MagicMock(
        side_effect=lambda model, _pk, **_kwargs: {
            ContentPlan: plan,
            PersonaRow: persona_row,
        }.get(model)
    )
    failed_transition_session = MagicMock()
    failed_transition_session.get = MagicMock(return_value=plan)

    sessions = iter([_ctx(run_session), _ctx(failed_transition_session)])

    with (
        patch("app.tasks.content_plan_build.sync_session", side_effect=lambda: next(sessions)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
        patch.object(generate_ideas_into_plan.request, "retries", 1),
        pytest.raises(RuntimeError, match="agent boom"),
    ):
        mock_trace.return_value = _ctx(MagicMock())

        def fail_after_sibling_success(*_args, **_kwargs):
            plan.plan_status = "ready"
            raise RuntimeError("agent boom")

        mock_agent_cls.return_value.run.side_effect = fail_after_sibling_success
        generate_ideas_into_plan.run(plan_id)

    assert plan.plan_status == "ready"
    failed_transition_session.get.assert_any_call(
        ContentPlan,
        uuid.UUID(plan_id),
        with_for_update=True,
        populate_existing=True,
    )
    failed_transition_session.commit.assert_not_called()


def test_generate_ideas_plan_gone_returns_cleanly() -> None:
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    session = MagicMock()
    session.get = MagicMock(return_value=None)
    session.execute = MagicMock()
    session.add = MagicMock()
    session.commit = MagicMock()

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=_ctx(session)),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent") as mock_agent_cls,
    ):
        generate_ideas_into_plan.run(str(uuid.uuid4()))

    mock_agent_cls.assert_not_called()
    session.execute.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


# ── landscape_fit threading (0057) ───────────────────────────────────────────


def test_landscape_fit_forwarded_to_build_generative_job() -> None:
    """generate_plan_item_videos must pass item.landscape_fit to build_generative_job."""
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "morning routine"
    item.idea = "film the sunrise"
    item.landscape_fit = "fit"  # user's landscape preference

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {"tone": "motivational", "content_pillars": []}

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, persona_row)
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    kwargs = mock_build.call_args.kwargs
    assert kwargs["landscape_fit"] == "fit"


def test_landscape_fit_fill_forwarded() -> None:
    """landscape_fit='fill' (crop) is also threaded faithfully."""
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "evening walk"
    item.idea = "wide landscape shot"
    item.landscape_fit = "fill"

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {"tone": "calm", "content_pillars": []}

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, persona_row)
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    kwargs = mock_build.call_args.kwargs
    assert kwargs["landscape_fit"] == "fill"


def test_masonry_preset_forwarded_to_build_generative_job() -> None:
    """generate_plan_item_videos must pass item.montage_preset to build_generative_job."""
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "training prep"
    item.idea = "make a collage wall"
    item.landscape_fit = "fit"
    item.montage_preset = "masonry"

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {"tone": "focused", "content_pillars": []}

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, persona_row)
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    kwargs = mock_build.call_args.kwargs
    assert kwargs["montage_preset"] == "masonry"


@pytest.mark.parametrize(
    ("audio_mode", "expected_voiceover", "expected_policy"),
    [
        ("voiceover", "voiceover-uploads/u/take.webm", "content_plan_primary"),
        ("original", None, "content_plan_original"),
        ("kria", None, "content_plan_primary"),
    ],
)
def test_audio_mode_controls_active_soundtrack(
    audio_mode: str,
    expected_voiceover: str | None,
    expected_policy: str,
) -> None:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "morning"
    item.idea = "make a short"
    item.audio_mode = audio_mode
    item.voiceover_gcs_path = "voiceover-uploads/u/take.webm"

    plan = MagicMock(user_id=uuid.uuid4(), persona_id=uuid.uuid4())
    persona_row = MagicMock()
    persona_row.persona = {"tone": "direct", "content_pillars": []}
    job = MagicMock(id=uuid.uuid4())

    with (
        patch(
            "app.tasks.content_plan_build.sync_session",
            return_value=_session_with(item, plan, persona_row),
        ),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    kwargs = mock_build.call_args.kwargs
    assert kwargs["voiceover_gcs_path"] == expected_voiceover
    assert kwargs["variant_policy"] == expected_policy

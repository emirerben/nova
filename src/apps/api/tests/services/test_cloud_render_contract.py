from __future__ import annotations

import pytest

import app.tasks.generative_build as generative_build
from app.services.cloud_render_contract import (
    CloudRenderContractError,
    preflight_cloud_contract,
    verify_cloud_variant,
)
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    build_render_contract,
)
from tests.tasks.conftest import FakeJob, patch_job_session


def _assembly(strategy: dict) -> dict:
    contract = build_render_contract(strategy, generation_id="generation-1")
    assert contract is not None
    return {CONTRACT_FIELD: contract.model_dump(mode="json")}


def test_no_contract_preserves_legacy_output() -> None:
    assert verify_cloud_variant({}, {"ok": True, "render_status": "ready"}) is None


def test_requirement_marker_refuses_missing_root_contract() -> None:
    with pytest.raises(CloudRenderContractError, match="requirements are missing"):
        preflight_cloud_contract({}, candidates={REQUIREMENT_VERSION_FIELD: 1})
    with pytest.raises(CloudRenderContractError, match="requirements are missing"):
        verify_cloud_variant(
            {},
            {"ok": True, "render_status": "ready", "video_path": "jobs/x/output.mp4"},
            candidates={REQUIREMENT_VERSION_FIELD: 1},
        )


def test_contract_with_no_objective_requirement_needs_no_receipt() -> None:
    assembly = _assembly({"pacing": "fast"})

    assert (
        verify_cloud_variant(
            assembly,
            {"ok": True, "render_status": "ready", "video_path": "jobs/x/output.mp4"},
        )
        is not None
    )


def test_verified_actual_duration_accepts_supported_contract() -> None:
    assembly = _assembly({"target_duration_s": 24, "target_duration_requested": True})

    contract = verify_cloud_variant(
        assembly,
        {
            "ok": True,
            "render_status": "ready",
            "video_path": "jobs/x/output.mp4",
            "render_receipt": {"verified": True, "actual_duration_s": 24},
        },
    )

    assert contract is not None


def test_measured_classic_duration_accepts_duration_only_contract() -> None:
    assembly = _assembly({"target_duration_s": 24, "target_duration_requested": True})

    assert (
        verify_cloud_variant(
            assembly,
            {
                "ok": True,
                "render_status": "ready",
                "video_path": "jobs/x/output.mp4",
                "duration_s": 24,
            },
        )
        is not None
    )


def test_original_audio_receipt_claim_never_proves_camera_audio() -> None:
    assembly = _assembly({"audio_strategy": "original_audio"})

    with pytest.raises(CloudRenderContractError, match="camera-audio"):
        preflight_cloud_contract(assembly)
    with pytest.raises(CloudRenderContractError, match="camera-audio"):
        verify_cloud_variant(
            assembly,
            {
                "ok": True,
                "render_status": "ready",
                "video_path": "jobs/x/output.mp4",
                "render_receipt": {
                    "verified": True,
                    "actual_duration_s": 24,
                    "source_audio_preserved": True,
                },
            },
        )


def test_missing_renderer_evidence_declines_text_requirement() -> None:
    assembly = _assembly({"opening_title": "Verified words"})

    with pytest.raises(CloudRenderContractError, match="on-screen text"):
        verify_cloud_variant(
            assembly,
            {
                "ok": True,
                "render_status": "ready",
                "video_path": "jobs/x/output.mp4",
                "render_receipt": {"verified": True, "actual_duration_s": 12},
            },
        )


def test_pending_or_failed_variant_is_not_a_publication_claim() -> None:
    assembly = _assembly({"opening_title": "Verified words"})
    # The publication helper calls this only for playable ready results; a
    # direct verifier remains strict so callers cannot accidentally treat a
    # pending receipt as evidence.
    with pytest.raises(CloudRenderContractError, match="on-screen text"):
        verify_cloud_variant(
            assembly,
            {"render_receipt": {"verified": True, "actual_duration_s": 12}},
        )


def test_upsert_rejects_unproven_playable_output_before_storing_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(assembly_plan=_assembly({"opening_title": "Verified words"}))
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build,
        "_attach_variant_posters",
        lambda result, **_kwargs: (dict(result), []),
    )

    assert not generative_build._upsert_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        {
            "variant_id": "original_text",
            "ok": True,
            "render_status": "ready",
            "video_path": "generative-jobs/job/output.mp4",
            "render_receipt": {"verified": True, "actual_duration_s": 12},
        },
    )

    stored = job.assembly_plan["variants"][0]
    assert stored["render_status"] == "failed"
    assert stored["error_class"] == "creator_render_contract_unverified"
    assert "video_path" not in stored


def test_update_rejects_minimal_ready_replacement_and_keeps_last_good_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [
                {
                    "variant_id": "original_text",
                    "ok": True,
                    "render_status": "ready",
                    "video_path": "generative-jobs/job/last-good.mp4",
                    "duration_s": 24,
                }
            ],
        }
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build,
        "_attach_variant_posters",
        lambda result, **_kwargs: (dict(result), []),
    )

    assert not generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {"video_path": "generative-jobs/job/new.mp4", "render_status": "ready"},
    )

    stored = job.assembly_plan["variants"][0]
    assert stored["render_status"] == "failed"
    assert stored["video_path"] == "generative-jobs/job/last-good.mp4"
    assert stored["error_class"] == "creator_render_contract_unverified"


def test_staged_replacement_requires_fresh_evidence_before_ready_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [
                {
                    "variant_id": "original_text",
                    "ok": True,
                    "render_status": "ready",
                    "video_path": "generative-jobs/job/last-good.mp4",
                    "duration_s": 24,
                }
            ],
        }
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build, "_attach_variant_posters", lambda result, **_: (dict(result), [])
    )

    accepted = {}
    assert not generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {"video_path": "generative-jobs/job/new.mp4", "render_status": "rendering"},
        accepted_state=accepted,
    )

    stored = job.assembly_plan["variants"][0]
    assert stored["render_status"] == "failed"
    assert stored["video_path"] == "generative-jobs/job/last-good.mp4"
    assert accepted.get("accepted") is not True


def test_staged_valid_duration_can_later_transition_to_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [{"variant_id": "original_text", "render_status": "rendering"}],
        }
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build, "_attach_variant_posters", lambda result, **_: (dict(result), [])
    )

    assert generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {
            "video_path": "generative-jobs/job/new.mp4",
            "duration_s": 24,
            "render_status": "rendering",
        },
    )
    assert generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {"render_status": "ready"},
    )
    assert job.assembly_plan["variants"][0]["render_status"] == "ready"


def test_failed_contract_replacement_cannot_be_revived_by_ready_only_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "failed",
                    "video_path": "generative-jobs/job/last-good.mp4",
                    "duration_s": 24,
                    "error_class": "creator_render_contract_unverified",
                }
            ],
        }
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build, "_attach_variant_posters", lambda result, **_: (dict(result), [])
    )

    assert not generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111", "original_text", {"render_status": "ready"}
    )
    assert job.assembly_plan["variants"][0]["render_status"] == "failed"


def test_ready_only_duration_mutation_is_reverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "ready",
                    "video_path": "generative-jobs/job/verified.mp4",
                    "duration_s": 24,
                }
            ],
        }
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build, "_attach_variant_posters", lambda result, **_: (dict(result), [])
    )

    assert not generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {"render_status": "ready", "duration_s": 1},
    )
    assert job.assembly_plan["variants"][0]["render_status"] == "failed"
    assert job.assembly_plan["variants"][0]["video_path"] == "generative-jobs/job/verified.mp4"


@pytest.mark.parametrize("staged", [False, True])
def test_fresh_verified_retry_recovers_from_contract_failure(
    monkeypatch: pytest.MonkeyPatch,
    staged: bool,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [
                {
                    "variant_id": "original_text",
                    "ok": False,
                    "render_status": "failed",
                    "video_path": "generative-jobs/job/last-good.mp4",
                    "error": "This edit couldn't keep the confirmed length.",
                    "error_class": "creator_render_contract_unverified",
                }
            ],
        }
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build, "_attach_variant_posters", lambda result, **_: (dict(result), [])
    )

    assert generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {
            "video_path": "generative-jobs/job/retry.mp4",
            "duration_s": 24,
            "render_status": "rendering" if staged else "ready",
        },
    )
    if staged:
        assert generative_build._update_variant_entry(
            "11111111-1111-1111-1111-111111111111",
            "original_text",
            {"render_status": "ready"},
        )
    stored = job.assembly_plan["variants"][0]
    assert stored["render_status"] == "ready"
    assert stored["ok"] is True
    assert "error_class" not in stored


def test_media_overlay_refuses_objective_contract_before_in_place_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "ready",
                    "video_path": "generative-jobs/job/last-good.mp4",
                }
            ],
        }
    )
    patch_job_session(monkeypatch, job)

    with pytest.raises(CloudRenderContractError, match="replace a confirmed output in place"):
        generative_build._run_media_overlay_pass(
            job_id="11111111-1111-1111-1111-111111111111",
            variant_id="original_text",
            overlays_raw=[],
        )

    assert job.assembly_plan["variants"][0]["video_path"] == "generative-jobs/job/last-good.mp4"


def test_sfx_refuses_objective_contract_before_in_place_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "ready",
                    "video_path": "generative-jobs/job/last-good.mp4",
                }
            ],
        }
    )
    patch_job_session(monkeypatch, job)

    with pytest.raises(CloudRenderContractError, match="replace a confirmed output in place"):
        generative_build._run_sfx_pass(
            job_id="11111111-1111-1111-1111-111111111111",
            variant_id="original_text",
            sfx_raw=[],
        )

    assert job.assembly_plan["variants"][0]["video_path"] == "generative-jobs/job/last-good.mp4"


def test_speech_cut_private_candidate_cannot_publish_wrong_confirmed_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.tasks.test_generative_build import (  # noqa: PLC0415
        _patch_speech_rerender_terminal_side_effects,
        _required_speech_rerender_fixture,
    )

    job_id = "11111111-1111-1111-1111-111111111111"
    generation = "a" * 32
    operation_id = "b" * 32
    attempt_id = "task:0:abc"
    job, _result = _required_speech_rerender_fixture(
        job_id, generation=generation, operation_id=operation_id, attempt_id=attempt_id
    )
    job.assembly_plan.update(
        _assembly({"target_duration_s": 24, "target_duration_requested": True})
    )
    _patch_speech_rerender_terminal_side_effects(monkeypatch, job)
    monkeypatch.setattr(
        generative_build, "_attach_variant_posters", lambda result, **_: (dict(result), [])
    )

    assert generative_build._update_required_speech_staged_variant(
        job_id,
        variant_id="subtitled",
        generation=generation,
        patch={"duration_s": 1, "render_status": "ready"},
        expected_operation_id=operation_id,
        expected_attempt_id=attempt_id,
    )
    with pytest.raises(CloudRenderContractError, match="confirmed length"):
        generative_build._publish_speech_cut_rerender(
            job_id, expected_operation_id=operation_id, expected_attempt_id=attempt_id
        )

    assert job.assembly_plan["variants"][0]["video_path"].endswith("last-good.mp4")


def test_update_accepts_minimal_ready_replacement_with_fresh_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [{"variant_id": "original_text", "render_status": "rendering"}],
        }
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build,
        "_attach_variant_posters",
        lambda result, **_kwargs: (dict(result), []),
    )

    assert generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {
            "video_path": "generative-jobs/job/new.mp4",
            "render_status": "ready",
            "duration_s": 24,
        },
    )

    stored = job.assembly_plan["variants"][0]
    assert stored["render_status"] == "ready"
    assert stored["video_path"] == "generative-jobs/job/new.mp4"


def test_legacy_update_keeps_inherited_receipt_and_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_status": "ready",
                    "video_path": "generative-jobs/job/old.mp4",
                    "duration_s": 19,
                    "render_receipt": {"legacy": True},
                }
            ]
        }
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build,
        "_attach_variant_posters",
        lambda result, **_kwargs: (dict(result), []),
    )

    assert generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {"video_path": "generative-jobs/job/new.mp4", "render_status": "ready"},
    )
    stored = job.assembly_plan["variants"][0]
    assert stored["video_path"] == "generative-jobs/job/new.mp4"
    assert stored["duration_s"] == 19
    assert stored["render_receipt"] == {"legacy": True}


def test_cloud_worker_preflight_rejects_text_contract_before_ingest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "11111111-1111-1111-1111-111111111111"
    job = FakeJob(
        job_id=job_id,
        assembly_plan=_assembly({"opening_title": "Exact approved title"}),
        all_candidates={"clip_paths": ["slot-uploads/clip.mp4"]},
        status="queued",
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build,
        "_ingest_clips",
        lambda *_args, **_kwargs: pytest.fail("unsupported contract reached ingest"),
        raising=False,
    )

    generative_build._run_generative_job(job_id)

    assert job.status == "processing_failed"
    assert job.failure_reason == "creator_render_contract_unsupported"
    assert "on-screen text" in job.error_detail


@pytest.mark.parametrize("duration,expected", [(1, "variants_failed"), (24, "variants_ready")])
def test_direct_finalization_rechecks_actual_duration_before_publication(
    monkeypatch, duration, expected
):
    job = FakeJob(
        assembly_plan=_assembly({"target_duration_s": 24, "target_duration_requested": True})
    )
    patch_job_session(monkeypatch, job)
    result = {
        "variant_id": "v",
        "rank": 1,
        "text_mode": "none",
        "ok": True,
        "render_status": "ready",
        "video_path": "jobs/x/new.mp4",
        "duration_s": duration,
    }
    generative_build._finalize_job("11111111-1111-1111-1111-111111111111", [result])
    assert job.status == expected
    stored = job.assembly_plan["variants"][0]
    if expected == "variants_failed":
        assert not stored.get("video_path")
        assert stored["ok"] is False
        assert "length" in stored["error"]
    else:
        assert stored["video_path"] == result["video_path"]

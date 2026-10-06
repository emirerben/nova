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

    assert generative_build._upsert_variant_entry(
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

    assert generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "original_text",
        {"video_path": "generative-jobs/job/new.mp4", "render_status": "ready"},
    )

    stored = job.assembly_plan["variants"][0]
    assert stored["render_status"] == "failed"
    assert stored["video_path"] == "generative-jobs/job/last-good.mp4"
    assert stored["error_class"] == "creator_render_contract_unverified"


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

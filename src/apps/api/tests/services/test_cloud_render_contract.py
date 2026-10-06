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


# --- KRI-470 PR-A: typed cloud declines ------------------------------------------
#
# Failure modes: the reason/field path is lost between the cloud contract and the
# persisted failure (preflight job, published variant, finalized variant); the
# declared adapter table disagrees with what preflight/publication really raise;
# a retry of a rejected variant keeps the stale decline of its predecessor.

_UNEVIDENCED_STRATEGIES = {
    "exact_texts": ({"opening_title": "Exact words"}, "opening_title"),
    "audio_source_ids": (
        {
            "audio_strategy": "original_audio",
            "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["talk"]},
        },
        "montage_audio.source_media_ids[]",
    ),
    "original_audio": ({"audio_strategy": "original_audio"}, "montage_audio.preserve_source_audio"),
    "order_required": ({"ordering_choice": "chronological"}, None),
}


@pytest.mark.parametrize("requirement", sorted(_UNEVIDENCED_STRATEGIES))
def test_preflight_declines_unevidenced_requirements_as_capability_unavailable(requirement):
    strategy, path = _UNEVIDENCED_STRATEGIES[requirement]
    contract = build_render_contract(strategy, generation_id="generation-1")
    assert contract is not None
    if requirement == "order_required":
        # Chronology needs capture times; with none the contract is unresolved.
        assert contract.unresolved
        expected = ("needs_choice", None)
    else:
        expected = ("capability_unavailable", path)
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract({CONTRACT_FIELD: contract.model_dump(mode="json")})
    assert (exc.value.decline_reason, exc.value.field_path) == expected
    assert exc.value.alternative


def test_resolved_order_requirement_is_a_capability_decline_with_its_field():
    contract = build_render_contract(
        {"ordering_choice": "chronological"},
        generation_id="generation-1",
        media_snapshot={
            "clip_assignments": [
                {"media_id": "a", "capture": {"capture_time": "2026-10-06T10:00:00Z"}}
            ]
        },
    )
    assert contract is not None and not contract.unresolved
    with pytest.raises(CloudRenderContractError) as exc:
        preflight_cloud_contract({CONTRACT_FIELD: contract.model_dump(mode="json")})
    assert (exc.value.decline_reason, exc.value.field_path) == (
        "capability_unavailable",
        "ordering_choice",
    )


def test_publication_gaps_are_evidence_missing_with_the_field_path():
    duration = _assembly({"target_duration_s": 24, "target_duration_requested": True})
    ready = {"ok": True, "render_status": "ready", "video_path": "jobs/x/output.mp4"}
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(duration, {**ready, "duration_s": 5})
    assert (exc.value.decline_reason, exc.value.field_path) == (
        "evidence_missing",
        "target_duration_s",
    )

    voice = _assembly({"audio_strategy": "voiceover"})
    for variant in (
        ready,  # no receipt at all
        {**ready, "render_receipt": {"verified": True, "narration_applied": False}},
    ):
        with pytest.raises(CloudRenderContractError) as exc:
            verify_cloud_variant(voice, variant)
        assert exc.value.decline_reason == "evidence_missing"
    assert verify_cloud_variant(
        voice, {**ready, "render_receipt": {"verified": True, "narration_applied": True}}
    )


def test_publication_cannot_prove_text_so_it_stays_a_capability_decline():
    with pytest.raises(CloudRenderContractError) as exc:
        verify_cloud_variant(
            _assembly({"closing_title": "The end"}),
            {"ok": True, "render_status": "ready", "video_path": "jobs/x/o.mp4"},
        )
    assert (exc.value.decline_reason, exc.value.field_path) == (
        "capability_unavailable",
        "closing_title",
    )


@pytest.mark.parametrize("adapter", ["cloud_guided_story", "cloud_classic", "cloud_slides"])
def test_declared_cloud_declines_match_preflight_and_publication(adapter):
    from app.services.cloud_render_contract import CLOUD_ADAPTER_DECLARATIONS

    declaration = CLOUD_ADAPTER_DECLARATIONS[adapter]
    ready = {"ok": True, "render_status": "ready", "video_path": "jobs/x/output.mp4"}
    for requirement, decline in declaration.declines.items():
        if requirement in _UNEVIDENCED_STRATEGIES and requirement != "order_required":
            strategy, _path = _UNEVIDENCED_STRATEGIES[requirement]
            with pytest.raises(CloudRenderContractError) as exc:
                preflight_cloud_contract(_assembly(strategy))
        elif requirement == "order_required":
            continue  # resolved-order case asserted above
        elif requirement == "unresolved":
            contract = build_render_contract(
                {"ordering_choice": "chronological"}, generation_id="generation-1"
            )
            with pytest.raises(CloudRenderContractError) as exc:
                preflight_cloud_contract({CONTRACT_FIELD: contract.model_dump(mode="json")})
        elif requirement == "duration_s":
            with pytest.raises(CloudRenderContractError) as exc:
                verify_cloud_variant(
                    _assembly({"target_duration_s": 24, "target_duration_requested": True}),
                    ready,  # no measured duration at all
                )
        else:
            assert requirement == "require_voiceover"
            with pytest.raises(CloudRenderContractError) as exc:
                verify_cloud_variant(_assembly({"audio_strategy": "voiceover"}), ready)
        assert exc.value.decline_reason == decline.reason, (adapter, requirement)
    # What each adapter consumes, the real verifier accepts when the receipt proves it.
    if "duration_s" in declaration.consumes:
        assert verify_cloud_variant(
            _assembly({"target_duration_s": 24, "target_duration_requested": True}),
            {**ready, "duration_s": 24},
        )
    if "require_voiceover" in declaration.consumes:
        assert verify_cloud_variant(
            _assembly({"audio_strategy": "voiceover"}),
            {**ready, "render_receipt": {"verified": True, "narration_applied": True}},
        )


def test_worker_preflight_persists_the_typed_decline_beside_the_unchanged_failure_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "11111111-1111-1111-1111-111111111111"
    assembly = _assembly({"opening_title": "Exact approved title"})
    job = FakeJob(
        job_id=job_id,
        assembly_plan=assembly,
        all_candidates={"clip_paths": ["slot-uploads/clip.mp4"]},
        status="queued",
    )
    patch_job_session(monkeypatch, job)

    generative_build._run_generative_job(job_id)

    assert job.failure_reason == "creator_render_contract_unsupported"
    assert job.assembly_plan["creator_decline"]["decline_reason"] == "capability_unavailable"
    assert job.assembly_plan["creator_decline"]["field_path"] == "opening_title"
    assert job.assembly_plan[CONTRACT_FIELD] == assembly[CONTRACT_FIELD]


def test_published_rejection_and_finalization_carry_the_typed_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        "duration_s": 5,
    }
    generative_build._finalize_job("11111111-1111-1111-1111-111111111111", [result])
    stored = job.assembly_plan["variants"][0]
    assert stored["error_class"] == "creator_render_contract_unverified"
    assert stored["decline_reason"] == "evidence_missing"
    assert stored["field_path"] == "target_duration_s"


def test_a_fresh_retry_drops_its_predecessors_typed_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(
        assembly_plan={
            **_assembly({"target_duration_s": 24, "target_duration_requested": True}),
            "variants": [
                {
                    "variant_id": "original_text",
                    "ok": False,
                    "render_status": "failed",
                    "error": "This edit couldn't keep the confirmed length.",
                    "error_class": "creator_render_contract_unverified",
                    "decline_reason": "evidence_missing",
                    "field_path": "target_duration_s",
                    "alternative": "retry",
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
        {"video_path": "jobs/x/new.mp4", "render_status": "ready", "duration_s": 24},
    )
    stored = job.assembly_plan["variants"][0]
    assert stored["render_status"] == "ready"
    assert not {"decline_reason", "field_path", "alternative", "error_class"} & set(stored)


def test_fail_job_persists_a_typed_decline_without_touching_the_failure_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(assembly_plan={"variants": []}, status="processing")
    patch_job_session(monkeypatch, job)
    assert generative_build._fail_job(
        "11111111-1111-1111-1111-111111111111",
        "I need capture times.",
        failure_reason="phone_plan_unsupported",
        decline={"decline_reason": "needs_choice"},
    )
    assert job.failure_reason == "phone_plan_unsupported"
    assert job.assembly_plan["creator_decline"] == {"decline_reason": "needs_choice"}
    plain = FakeJob(assembly_plan={"variants": []}, status="processing")
    patch_job_session(monkeypatch, plain)
    assert generative_build._fail_job(
        "11111111-1111-1111-1111-111111111111", "x", failure_reason="phone_plan_unsupported"
    )
    assert "creator_decline" not in plain.assembly_plan

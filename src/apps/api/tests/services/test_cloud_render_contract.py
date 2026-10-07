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


_ADAPTER_ARCHETYPE = {
    "cloud_guided_story": "guided_story",
    "cloud_classic": "montage",
    "cloud_slides": "slides",
}


@pytest.mark.parametrize("adapter", sorted(_ADAPTER_ARCHETYPE))
def test_declared_cloud_declines_match_preflight_and_publication(adapter):
    """Every declined requirement refuses with its declared reason, before work AND at
    publication, for the adapter that would render it (KRI-470 PR-E: per adapter)."""
    from app.services.cloud_render_contract import CLOUD_ADAPTER_DECLARATIONS

    declaration = CLOUD_ADAPTER_DECLARATIONS[adapter]
    ready = {
        "ok": True,
        "render_status": "ready",
        "video_path": "jobs/x/output.mp4",
        "resolved_archetype": _ADAPTER_ARCHETYPE[adapter],
    }
    snapshot = {
        "clip_assignments": [{"media_id": "a", "capture": {"capture_time": "2026-10-06T10:00:00Z"}}]
    }
    cases = {
        "exact_texts": ({"opening_title": "Exact words"}, None),
        "audio_source_ids": (_UNEVIDENCED_STRATEGIES["audio_source_ids"][0], None),
        "original_audio": (_UNEVIDENCED_STRATEGIES["original_audio"][0], None),
        "order_required": ({"ordering_choice": "chronological"}, snapshot),
        "duration_s": ({"target_duration_s": 24, "target_duration_requested": True}, None),
        "require_voiceover": ({"audio_strategy": "voiceover"}, None),
    }
    for requirement, decline in declaration.declines.items():
        if requirement == "unresolved":
            contract = build_render_contract(
                {"ordering_choice": "chronological"}, generation_id="generation-1"
            )
            with pytest.raises(CloudRenderContractError) as exc:
                preflight_cloud_contract(
                    {CONTRACT_FIELD: contract.model_dump(mode="json")}, adapter=adapter
                )
            assert exc.value.decline_reason == decline.reason
            continue
        strategy, media = cases[requirement]
        contract = build_render_contract(
            strategy, generation_id="generation-1", media_snapshot=media
        )
        assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
        if requirement not in {"duration_s", "require_voiceover"} or adapter == "cloud_slides":
            with pytest.raises(CloudRenderContractError) as exc:
                preflight_cloud_contract(assembly, adapter=adapter)
            assert exc.value.decline_reason == decline.reason, (adapter, requirement)
        with pytest.raises(CloudRenderContractError) as exc:
            verify_cloud_variant(assembly, ready)
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
    assert job.assembly_plan["creator_decline"] == {
        "decline_reason": "needs_choice",
        "failure_reason": "phone_plan_unsupported",
    }
    plain = FakeJob(assembly_plan={"variants": []}, status="processing")
    patch_job_session(monkeypatch, plain)
    assert generative_build._fail_job(
        "11111111-1111-1111-1111-111111111111", "x", failure_reason="phone_plan_unsupported"
    )
    assert "creator_decline" not in plain.assembly_plan


# --- KRI-470 PR-A review: job-level decline lifecycle ------------------------------


def test_worker_entry_clears_a_stale_job_decline_before_the_run_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "11111111-1111-1111-1111-111111111111"
    job = FakeJob(
        job_id=job_id,
        assembly_plan={
            **_assembly({"pacing": "fast"}),
            "creator_decline": {
                "decline_reason": "capability_unavailable",
                "failure_reason": "creator_render_contract_unsupported",
            },
        },
        all_candidates={"clip_paths": ["slot-uploads/clip.mp4"]},
        status="processing_failed",
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(generative_build.settings, "text_renderer_skia_enabled", False)

    generative_build._run_generative_job(job_id)

    assert job.failure_reason == "skia_disabled"
    assert "creator_decline" not in job.assembly_plan


def test_successful_finalization_clears_the_job_decline(monkeypatch: pytest.MonkeyPatch) -> None:
    job = FakeJob(
        assembly_plan={
            "creator_decline": {"decline_reason": "evidence_missing", "failure_reason": "x"}
        }
    )
    patch_job_session(monkeypatch, job)
    generative_build._finalize_job(
        "11111111-1111-1111-1111-111111111111",
        [
            {
                "variant_id": "v",
                "rank": 1,
                "text_mode": "none",
                "ok": True,
                "render_status": "ready",
                "video_path": "jobs/x/new.mp4",
                "duration_s": 24,
            }
        ],
    )
    assert job.status == "variants_ready"
    assert "creator_decline" not in job.assembly_plan


def test_fail_job_stamps_the_failure_code_the_decline_belongs_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(assembly_plan={"variants": []}, status="processing")
    patch_job_session(monkeypatch, job)
    assert generative_build._fail_job(
        "11111111-1111-1111-1111-111111111111",
        "x",
        failure_reason="phone_plan_unsupported",
        decline={"decline_reason": "needs_choice"},
    )
    assert job.assembly_plan["creator_decline"] == {
        "decline_reason": "needs_choice",
        "failure_reason": "phone_plan_unsupported",
    }


def test_preflight_stamps_its_failure_code_on_the_decline(
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
    generative_build._run_generative_job(job_id)
    assert job.assembly_plan["creator_decline"]["failure_reason"] == job.failure_reason


def test_a_reverified_variant_does_not_keep_the_old_field_path() -> None:
    # No receipt at all: the new decline has no field path of its own.
    assembly = _assembly({"audio_strategy": "voiceover"})
    stale = {
        "variant_id": "v",
        "ok": True,
        "render_status": "ready",
        "video_path": "jobs/x/o.mp4",
        "decline_reason": "capability_unavailable",
        "field_path": "opening_title",
        "alternative": "old alternative",
    }
    rejected = generative_build._reject_unverified_cloud_variant(assembly, stale)
    assert rejected["decline_reason"] == "evidence_missing"
    assert "field_path" not in rejected
    assert rejected["alternative"] != "old alternative"


def test_finalize_merge_does_not_resurrect_a_live_rows_old_decline() -> None:
    live = [
        {
            "variant_id": "v",
            "render_status": "failed",
            "decline_reason": "capability_unavailable",
            "field_path": "opening_title",
            "alternative": "old",
        }
    ]
    finalized = [{"variant_id": "v", "ok": True, "render_status": "ready"}]
    merged = generative_build._merge_finalized_variants(live, finalized)
    assert not {"decline_reason", "field_path", "alternative"} & set(merged[0])


# --- KRI-470 PR-E: per-adapter lift at the worker ----------------------------------


def test_classic_worker_lifts_the_recorded_voice_but_not_camera_audio_or_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classic HONOURS the recorded voice (by archetype), so that early decline is lifted and
    the job proceeds toward ingest. Camera audio and order are not honoured by its matcher
    and song variants, so they still decline before any spend."""
    import app.services.cloud_render_contract as crc

    job_id = "11111111-1111-1111-1111-111111111111"

    class _ReachedIngest(Exception):
        pass

    def run(strategy: dict) -> tuple[FakeJob, list[tuple[str | None, str]], BaseException | None]:
        job = FakeJob(
            job_id=job_id,
            assembly_plan=_assembly(strategy),
            all_candidates={"clip_paths": ["slot-uploads/clip.mp4"], REQUIREMENT_VERSION_FIELD: 1},
            status="queued",
        )
        patch_job_session(monkeypatch, job)
        calls: list[tuple[str | None, str]] = []
        real = crc.preflight_cloud_contract

        def spy(assembly, *, candidates=None, adapter=None):
            try:
                real(assembly, candidates=candidates, adapter=adapter)
            except CloudRenderContractError:
                calls.append((adapter, "declined"))
                raise
            calls.append((adapter, "passed"))

        def ingest(*_a, **_k):
            raise _ReachedIngest

        monkeypatch.setattr(crc, "preflight_cloud_contract", spy)
        monkeypatch.setattr(generative_build, "_ingest_clips", ingest)
        raised: BaseException | None = None
        try:
            generative_build._run_generative_job(job_id)
        except Exception as exc:  # noqa: BLE001 - inspected below
            raised = exc
        return job, calls, raised

    voice, calls, raised = run({"audio_strategy": "voiceover"})
    assert calls == [("cloud_classic", "passed")]  # preflight really ran, for classic, and passed
    assert getattr(voice, "failure_reason", None) != "creator_render_contract_unsupported"
    assert "creator_decline" not in voice.assembly_plan
    assert not isinstance(raised, CloudRenderContractError)

    audio, calls, _ = run({"audio_strategy": "original_audio"})
    assert calls == [("cloud_classic", "declined")]
    assert audio.failure_reason == "creator_render_contract_unsupported"
    assert audio.assembly_plan["creator_decline"]["decline_reason"] == "capability_unavailable"
    assert audio.assembly_plan["creator_decline"]["alternative"]


def test_classic_worker_still_declines_exact_text_for_its_own_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "11111111-1111-1111-1111-111111111111"
    job = FakeJob(
        job_id=job_id,
        assembly_plan=_assembly({"opening_title": "Exact approved title"}),
        all_candidates={"clip_paths": ["slot-uploads/clip.mp4"], REQUIREMENT_VERSION_FIELD: 1},
        status="queued",
    )
    patch_job_session(monkeypatch, job)
    generative_build._run_generative_job(job_id)
    assert job.failure_reason == "creator_render_contract_unsupported"
    assert job.assembly_plan["creator_decline"]["field_path"] == "opening_title"


def test_a_decline_raised_once_the_archetype_is_known_keeps_the_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`check_classic_archetype` raises after analysis; the orchestrator must terminalize it
    with the same code and typed decline as the early preflight, not `unknown`."""
    import contextlib

    import app.services.pipeline_trace as pt

    error = CloudRenderContractError(
        "A talking head edit can't prove this confirmed requirement in the cloud yet.",
        decline_reason="capability_unavailable",
        field_path="montage_audio.preserve_source_audio",
        alternative="Ask for it on your iPhone.",
    )

    def _raise(job_id):
        raise error

    monkeypatch.setattr(generative_build, "_run_generative_job", _raise)
    monkeypatch.setattr(generative_build, "job_heartbeat", lambda _id: contextlib.nullcontext())
    monkeypatch.setattr(
        generative_build,
        "_owned_job_task_fence",
        lambda _id, **_kwargs: contextlib.nullcontext(True),
    )
    monkeypatch.setattr(generative_build, "mark_failed_phase", lambda _id: None)
    monkeypatch.setattr(pt, "pipeline_trace_for", lambda _id: contextlib.nullcontext())
    failed: dict = {}

    def _fail(job_id, detail, **kwargs):
        failed.update(detail=detail, **kwargs)
        return True

    monkeypatch.setattr(generative_build, "_fail_job", _fail)
    generative_build.orchestrate_generative_job.run("11111111-1111-1111-1111-111111111111")

    assert failed["failure_reason"] == "creator_render_contract_unsupported"
    assert failed["decline"]["decline_reason"] == "capability_unavailable"
    assert failed["decline"]["field_path"] == "montage_audio.preserve_source_audio"


# --- KRI-470 PR-E review: evidence lives beside the receipt, never stale ------------


def _evidence_variant(**fields) -> dict:
    return {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "ok": True,
        "render_status": "ready",
        "video_path": "generative-jobs/job/last-good.mp4",
        "render_receipt": {"verified": True, "actual_duration_s": 12.0},
        "cloud_evidence": {"schema_version": 1, "actual_clip_order": ["a", "b"]},
        **fields,
    }


def test_a_new_artifact_without_fresh_evidence_never_passes_on_the_old_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = {
        "clip_assignments": [
            {"media_id": "b", "capture": {"capture_time": "2026-10-06T12:00:00Z"}},
            {"media_id": "a", "capture": {"capture_time": "2026-10-06T10:00:00Z"}},
        ]
    }
    contract = build_render_contract(
        {"ordering_choice": "chronological"}, generation_id="g", media_snapshot=snapshot
    )
    job = FakeJob(
        assembly_plan={
            CONTRACT_FIELD: contract.model_dump(mode="json"),
            "variants": [_evidence_variant()],
        },
        all_candidates={REQUIREMENT_VERSION_FIELD: 1},
    )
    patch_job_session(monkeypatch, job)
    monkeypatch.setattr(
        generative_build, "_attach_variant_posters", lambda result, **_kw: (dict(result), [])
    )
    # a replacement artifact whose patch carries a receipt but no evidence
    assert not generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "guided_story",
        {
            "video_path": "generative-jobs/job/new.mp4",
            "render_status": "ready",
            "render_receipt": {"verified": True, "actual_duration_s": 12.0},
        },
    )
    stored = job.assembly_plan["variants"][0]
    assert stored["render_status"] == "failed"
    assert stored["decline_reason"] == "evidence_missing"
    assert "cloud_evidence" not in stored
    # ...while a replacement that brings its own evidence is accepted
    job.assembly_plan["variants"] = [_evidence_variant()]
    assert generative_build._update_variant_entry(
        "11111111-1111-1111-1111-111111111111",
        "guided_story",
        {
            "video_path": "generative-jobs/job/new2.mp4",
            "render_status": "ready",
            "render_receipt": {"verified": True, "actual_duration_s": 12.0},
            "cloud_evidence": {"schema_version": 1, "actual_clip_order": ["a", "b"]},
        },
    )
    assert job.assembly_plan["variants"][0]["cloud_evidence"]["actual_clip_order"] == ["a", "b"]


def test_finalization_keeps_fresh_evidence_and_drops_a_stale_live_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = FakeJob(assembly_plan={"variants": []})
    patch_job_session(monkeypatch, job)
    fresh = {
        "variant_id": "v",
        "rank": 1,
        "text_mode": "none",
        "ok": True,
        "render_status": "ready",
        "video_path": "jobs/x/new.mp4",
        "cloud_evidence": {"schema_version": 1, "actual_clip_order": ["a"]},
    }
    generative_build._finalize_job("11111111-1111-1111-1111-111111111111", [fresh])
    assert job.assembly_plan["variants"][0]["cloud_evidence"]["actual_clip_order"] == ["a"]
    # legacy shape: no evidence key is invented for a variant that never had one
    legacy = {k: v for k, v in fresh.items() if k != "cloud_evidence"}
    job2 = FakeJob(assembly_plan={"variants": []})
    patch_job_session(monkeypatch, job2)
    generative_build._finalize_job("11111111-1111-1111-1111-111111111111", [legacy])
    assert "cloud_evidence" not in job2.assembly_plan["variants"][0]
    # a live row's stale evidence does not survive a finalized result that has none
    merged = generative_build._merge_finalized_variants(
        [{"variant_id": "v", "cloud_evidence": {"actual_clip_order": ["stale"]}}],
        [legacy],
    )
    assert "cloud_evidence" not in merged[0]


def test_a_staged_replacement_artifact_never_inherits_the_previous_staged_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.speech_cleanup_terminal import peek_required_speech_generation
    from tests.tasks.test_generative_build import (  # noqa: PLC0415
        _patch_speech_rerender_terminal_side_effects,
        _required_speech_rerender_fixture,
    )

    job_id = "11111111-1111-1111-1111-111111111111"
    generation, operation_id, attempt_id = "a" * 32, "b" * 32, "task:0:abc"
    job, result = _required_speech_rerender_fixture(
        job_id, generation=generation, operation_id=operation_id, attempt_id=attempt_id
    )
    job.assembly_plan.update(_assembly({"audio_strategy": "voiceover"}))
    _patch_speech_rerender_terminal_side_effects(monkeypatch, job)
    monkeypatch.setattr(
        generative_build, "_attach_variant_posters", lambda result, **_: (dict(result), [])
    )

    def stage(patch: dict) -> None:
        assert generative_build._update_required_speech_staged_variant(
            job_id,
            variant_id="subtitled",
            generation=generation,
            patch=patch,
            expected_operation_id=operation_id,
            expected_attempt_id=attempt_id,
        )

    stage(
        {
            "video_path": result["video_path"].replace(".mp4", "_a.mp4"),
            "render_status": "ready",
            "render_receipt": {"verified": True, "narration_applied": True},
            "cloud_evidence": {"schema_version": 1, "narration_applied": True},
        }
    )
    staged = peek_required_speech_generation(
        job.assembly_plan, variant_id="subtitled", generation=generation
    )
    assert staged["cloud_evidence"]["narration_applied"] is True
    # a second staged artifact that brings no evidence must not keep the first one's
    stage(
        {
            "video_path": result["video_path"].replace(".mp4", "_b.mp4"),
            "render_status": "ready",
        }
    )
    staged = peek_required_speech_generation(
        job.assembly_plan, variant_id="subtitled", generation=generation
    )
    assert staged.get("cloud_evidence") is None
    assert staged.get("render_receipt") is None

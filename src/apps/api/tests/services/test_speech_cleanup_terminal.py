from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.durable_attempt_cleanup import (
    RENDER_GENERATION_CLEANUP_FIELD,
    CleanupReceiptLocator,
    remove_cleanup_receipt,
)
from app.services.speech_cleanup_terminal import (
    RequiredSpeechOwnershipError,
    classify_required_speech_claim,
    classify_required_speech_resume,
    classify_route_speech_cut_rollback,
    close_required_speech_generation_uploads,
    consume_required_speech_generation,
    mark_required_speech_rendering,
    peek_required_speech_generation,
    reserve_required_speech_generation,
    rotate_required_speech_generation_for_retry,
    stage_required_speech_generation,
    terminalize_required_speech_generations,
)


def test_route_rollback_classifier_requires_exact_unclaimed_owner() -> None:
    generation = uuid.uuid4().hex
    plan = {
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": "operation-a",
            "render_generation_id": generation,
            "finalizer_claim": None,
        }
    }

    assert (
        classify_route_speech_cut_rollback(
            plan,
            variant_id="subtitled",
            operation_id="operation-a",
            generation=generation,
        )
        == "eligible"
    )
    assert (
        classify_route_speech_cut_rollback(
            plan,
            variant_id="subtitled",
            operation_id="superseding-operation",
            generation=generation,
        )
        == "not_owned"
    )


def test_route_rollback_classifier_treats_already_published_generation_as_uncertain() -> None:
    generation = uuid.uuid4().hex
    plan = {
        "speech_cleanup_contract": "required_v1",
        "speech_cut_control": None,
        "variants": [
            {
                "variant_id": "subtitled",
                "render_generation_id": generation,
                "render_status": "ready",
                "video_path": f"generative-jobs/job/render-generations/{generation}/final.mp4",
            }
        ],
    }

    assert (
        classify_route_speech_cut_rollback(
            plan,
            variant_id="subtitled",
            operation_id=uuid.uuid4().hex,
            generation=generation,
        )
        == "enqueue_uncertain"
    )


@pytest.mark.parametrize(
    "worker_evidence",
    [
        {"finalizer_claim": {}},
        {"finalizer_claim": "malformed"},
        {"lock": True},
        {"receipt": True},
        {"terminal": True},
    ],
)
def test_route_rollback_classifier_fails_closed_after_possible_delivery(
    worker_evidence: dict,
) -> None:
    generation = uuid.uuid4().hex
    control = {
        "variant_id": "subtitled",
        "operation_id": "operation-a",
        "render_generation_id": generation,
        "finalizer_claim": worker_evidence.get("finalizer_claim"),
    }
    plan: dict = {"speech_cut_control": control}
    if worker_evidence.get("lock"):
        plan["_speech_cleanup_internal"] = {
            "required_speech_generation_locks": {"subtitled": generation}
        }
    if worker_evidence.get("receipt"):
        plan["_speech_cleanup_internal"] = {
            "render_generation_cleanup_pending": [{"generation": generation}]
        }
    if worker_evidence.get("terminal"):
        plan["_speech_cleanup_internal"] = {
            "terminal_pending": {
                f"subtitled:{generation}": {
                    "variant_id": "subtitled",
                    "render_generation_id": generation,
                }
            }
        }

    assert (
        classify_route_speech_cut_rollback(
            plan,
            variant_id="subtitled",
            operation_id="operation-a",
            generation=generation,
        )
        == "enqueue_uncertain"
    )


def _reserved_plan(job_id: str, generation: str) -> dict:
    plan: dict = {"variants": []}
    reserve_required_speech_generation(
        plan,
        job_id=job_id,
        pending_variant={
            "variant_id": "subtitled",
            "rank": 1,
            "text_mode": "none",
            "render_generation_id": generation,
            "render_status": "pending",
            "ok": False,
        },
        generation=generation,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=35),
    )
    return plan


def test_required_generation_is_private_until_consumed() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan = _reserved_plan(job_id, generation)
    result = {
        "variant_id": "subtitled",
        "rank": 1,
        "text_mode": "none",
        "render_generation_id": generation,
        "render_status": "ready",
        "ok": True,
        "video_path": (f"generative-jobs/{job_id}/render-generations/{generation}/output.mp4"),
    }

    mark_required_speech_rendering(
        plan,
        variant_id="subtitled",
        generation=generation,
        render_started_at="2026-09-01T12:00:00Z",
    )
    stage_required_speech_generation(plan, result=result, generation=generation)
    close_required_speech_generation_uploads(plan, generation=generation)

    # Staging did not leak the ready result into the public variants list.
    assert plan["variants"][0]["render_status"] == "rendering"
    assert "video_path" not in plan["variants"][0]
    assert (
        consume_required_speech_generation(
            plan,
            job_id=job_id,
            variant_id="subtitled",
            generation=generation,
        )
        == result
    )
    assert "_speech_cleanup_internal" not in plan


def test_consume_requires_the_exact_active_cleanup_receipt() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan = _reserved_plan(job_id, generation)
    result = {
        "variant_id": "subtitled",
        "render_generation_id": generation,
        "render_status": "failed",
        "ok": False,
    }
    stage_required_speech_generation(plan, result=result, generation=generation)
    close_required_speech_generation_uploads(plan, generation=generation)
    assert remove_cleanup_receipt(
        plan,
        CleanupReceiptLocator(
            field=RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=generation,
        ),
    )

    with pytest.raises(RequiredSpeechOwnershipError, match="active_cleanup_receipt_missing"):
        consume_required_speech_generation(
            plan,
            job_id=job_id,
            variant_id="subtitled",
            generation=generation,
        )


def test_peek_returns_copy_without_consuming_generation() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan = _reserved_plan(job_id, generation)
    result = {
        "variant_id": "subtitled",
        "render_generation_id": generation,
        "render_status": "ready",
        "ok": True,
    }
    stage_required_speech_generation(plan, result=result, generation=generation)
    before = copy.deepcopy(plan)

    peeked = peek_required_speech_generation(
        plan,
        variant_id="subtitled",
        generation=generation,
    )
    peeked["ok"] = False

    assert plan == before
    assert (
        plan["_speech_cleanup_internal"]["staged_render_results"][f"subtitled:{generation}"]["ok"]
        is True
    )


def test_wrong_generation_cannot_stage_or_consume() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan = _reserved_plan(job_id, generation)
    other = uuid.uuid4().hex

    with pytest.raises(RequiredSpeechOwnershipError, match="result_generation_mismatch"):
        stage_required_speech_generation(
            plan,
            result={
                "variant_id": "subtitled",
                "render_generation_id": generation,
            },
            generation=other,
        )
    with pytest.raises(RequiredSpeechOwnershipError, match="generation_lock_mismatch"):
        consume_required_speech_generation(
            plan,
            job_id=job_id,
            variant_id="subtitled",
            generation=other,
        )


def test_terminalizer_fails_initial_generation_and_retains_closed_cleanup_debt() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    prefix = f"generative-jobs/{job_id}/render-generations/{generation}/"
    plan = _reserved_plan(job_id, generation)
    mark_required_speech_rendering(
        plan,
        variant_id="subtitled",
        generation=generation,
        render_started_at="2026-09-01T12:00:00Z",
    )
    # Simulate a provisional-path leak on the public row and a complete private
    # stage. Recovery must publish neither one.
    plan["variants"][0]["video_path"] = f"{prefix}provisional.mp4"
    stage_required_speech_generation(
        plan,
        generation=generation,
        result={
            "variant_id": "subtitled",
            "render_generation_id": generation,
            "render_status": "ready",
            "ok": True,
            "video_path": f"{prefix}staged.mp4",
        },
    )
    close_required_speech_generation_uploads(plan, generation=generation)
    original = copy.deepcopy(plan)

    outcome = terminalize_required_speech_generations(plan, job_id=job_id)

    assert plan == original  # pure helper
    assert outcome.status == "terminalized"
    assert outcome.terminalized_count == 1
    variant = outcome.plan["variants"][0]
    assert variant["render_status"] == "failed"
    assert variant["ok"] is False
    assert "video_path" not in variant
    internal = outcome.plan["_speech_cleanup_internal"]
    assert "required_speech_generation_locks" not in internal
    assert "staged_render_results" not in internal
    assert internal["render_generation_cleanup_pending"] == [
        {
            **original["_speech_cleanup_internal"]["render_generation_cleanup_pending"][0],
            "upload_state": "closed",
        }
    ]


def test_terminalizer_restores_exact_safe_speech_cut_last_good() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    prior = {
        "variant_id": "subtitled",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
        "video_path": f"generative-jobs/{job_id}/last-good.mp4",
    }
    plan = _reserved_plan(job_id, generation)
    plan["variants"][0]["video_path"] = f"generative-jobs/{job_id}/inherited-last-good.mp4"
    plan.update(
        {
            "silence_cut_disabled": False,
            "speech_cut_control": {
                "variant_id": "subtitled",
                "operation_id": uuid.uuid4().hex,
                "render_generation_id": generation,
                "prior_disabled": True,
                "finalizer_claim": None,
            },
            "speech_cut_previous_variant": copy.deepcopy(prior),
            "speech_cut_previous_variants": [copy.deepcopy(prior)],
        }
    )
    close_required_speech_generation_uploads(plan, generation=generation)

    outcome = terminalize_required_speech_generations(plan, job_id=job_id)

    assert outcome.status == "terminalized"
    assert outcome.restored_last_good is True
    assert outcome.plan["variants"] == [prior]
    assert outcome.plan["silence_cut_disabled"] is True
    assert outcome.plan["speech_cut_control"] is None
    assert outcome.plan["speech_cut_previous_variant"] is None
    assert outcome.plan["speech_cut_previous_variants"] is None
    receipt = outcome.plan["_speech_cleanup_internal"]["render_generation_cleanup_pending"][0]
    assert receipt["generation"] == generation
    assert receipt["upload_state"] == "closed"


def test_terminalizer_rolls_back_required_control_before_worker_reservation() -> None:
    """Route-commit ownership is terminalizable before an upload owner exists."""

    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    public = {
        "variant_id": "subtitled",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
        "video_path": f"generative-jobs/{job_id}/last-good.mp4",
        "media_overlays": [{"id": "last-good"}],
    }
    sibling = {
        "variant_id": "talking_head",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
    }
    # Creator speech+editor bundles use the singular field as the desired
    # generation's editor-lane input; only the vector is public rollback state.
    working = {
        **public,
        "render_generation_id": generation,
        "render_status": "rendering",
        "ok": False,
        "media_overlays": [{"id": "new-editor-lane"}],
    }
    plan = {
        "speech_cleanup_contract": "required_v1",
        "silence_cut_disabled": False,
        "variants": [copy.deepcopy(public), copy.deepcopy(sibling)],
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": uuid.uuid4().hex,
            "render_generation_id": generation,
            "prior_disabled": True,
            "finalizer_claim": None,
        },
        "speech_cut_previous_variant": working,
        "speech_cut_previous_variants": [copy.deepcopy(public), copy.deepcopy(sibling)],
    }
    original = copy.deepcopy(plan)

    superseded = terminalize_required_speech_generations(
        plan,
        job_id=job_id,
        expected_operation_id="older-operation",
    )
    assert superseded.status == "blocked"
    assert superseded.reason == "speech_cut_operation_mismatch"
    assert superseded.plan is plan
    assert plan == original

    outcome = terminalize_required_speech_generations(
        plan,
        job_id=job_id,
        error="cancelled before worker reservation",
    )

    assert plan == original
    assert outcome.status == "terminalized"
    assert outcome.terminalized_count == 1
    assert outcome.restored_last_good is True
    assert outcome.plan["variants"] == [public, sibling]
    assert outcome.plan["silence_cut_disabled"] is True
    assert outcome.plan["speech_cut_control"] is None
    assert outcome.plan["speech_cut_previous_variant"] is None
    assert outcome.plan["speech_cut_previous_variants"] is None
    assert outcome.plan["speech_cut_last_error"] == "cancelled before worker reservation"


def test_required_worker_reservation_adopts_exact_route_control_generation() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    public = {
        "variant_id": "subtitled",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
    }
    plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [copy.deepcopy(public)],
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": uuid.uuid4().hex,
            "render_generation_id": generation,
            "prior_disabled": False,
            "finalizer_claim": None,
        },
        "speech_cut_previous_variant": {
            **public,
            "render_generation_id": generation,
            "render_status": "rendering",
            "ok": False,
        },
        "speech_cut_previous_variants": [copy.deepcopy(public)],
    }

    reserve_required_speech_generation(
        plan,
        job_id=job_id,
        pending_variant={
            "variant_id": "subtitled",
            "render_generation_id": generation,
            "render_status": "pending",
            "ok": False,
        },
        generation=generation,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )

    assert plan["variants"] == [public]
    internal = plan["_speech_cleanup_internal"]
    assert internal["required_speech_generation_locks"] == {"subtitled": generation}
    assert (
        internal["working_render_variants"][f"subtitled:{generation}"]["render_generation_id"]
        == generation
    )
    assert internal["render_generation_cleanup_pending"][0]["generation"] == generation

    close_required_speech_generation_uploads(plan, generation=generation)
    terminalized = terminalize_required_speech_generations(
        plan,
        job_id=job_id,
        error="cancelled after worker reservation",
    )
    assert terminalized.status == "terminalized"
    assert terminalized.plan["variants"] == [public]
    assert terminalized.plan["speech_cut_control"] is None


def test_required_worker_reservation_rejects_superseding_control_generation() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    superseding_generation = uuid.uuid4().hex
    public = {
        "variant_id": "subtitled",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
    }
    plan = {
        "speech_cleanup_contract": "required_v1",
        "variants": [copy.deepcopy(public)],
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": uuid.uuid4().hex,
            "render_generation_id": superseding_generation,
            "prior_disabled": False,
            "finalizer_claim": None,
        },
        "speech_cut_previous_variant": copy.deepcopy(public),
        "speech_cut_previous_variants": [copy.deepcopy(public)],
    }
    original = copy.deepcopy(plan)

    with pytest.raises(
        RequiredSpeechOwnershipError,
        match="speech_cut_pre_reservation_owner_mismatch",
    ):
        reserve_required_speech_generation(
            plan,
            job_id=job_id,
            pending_variant={
                "variant_id": "subtitled",
                "render_generation_id": generation,
                "render_status": "pending",
                "ok": False,
            },
            generation=generation,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )

    assert plan == original


def test_terminalizer_blocks_atomically_when_cleanup_debt_is_missing() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan = _reserved_plan(job_id, generation)
    assert remove_cleanup_receipt(
        plan,
        CleanupReceiptLocator(
            field=RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=generation,
        ),
    )
    original = copy.deepcopy(plan)

    outcome = terminalize_required_speech_generations(plan, job_id=job_id)

    assert outcome.status == "blocked"
    assert outcome.plan is plan
    assert plan == original


def test_terminalizer_never_relinquishes_a_fresh_writing_receipt() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan = _reserved_plan(job_id, generation)
    original = copy.deepcopy(plan)

    outcome = terminalize_required_speech_generations(plan, job_id=job_id)

    assert outcome.status == "blocked"
    assert outcome.plan is plan
    assert plan == original
    receipt = plan["_speech_cleanup_internal"]["render_generation_cleanup_pending"][0]
    assert receipt["upload_state"] == "writing"


def test_terminalizer_can_close_an_expired_writing_receipt() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan = _reserved_plan(job_id, generation)
    receipt = plan["_speech_cleanup_internal"]["render_generation_cleanup_pending"][0]
    receipt["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()

    outcome = terminalize_required_speech_generations(plan, job_id=job_id)

    assert outcome.status == "terminalized"
    receipt = outcome.plan["_speech_cleanup_internal"]["render_generation_cleanup_pending"][0]
    assert receipt["upload_state"] == "closed"


def test_terminalizer_blocks_unsafe_last_good_snapshot_without_partial_clear() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    prefix = f"generative-jobs/{job_id}/render-generations/{generation}/"
    plan = _reserved_plan(job_id, generation)
    unsafe_prior = {
        "variant_id": "subtitled",
        "render_status": "ready",
        "video_path": f"{prefix}not-last-good.mp4",
    }
    plan.update(
        {
            "speech_cut_control": {
                "variant_id": "subtitled",
                "operation_id": uuid.uuid4().hex,
                "prior_disabled": False,
                "finalizer_claim": None,
            },
            "speech_cut_previous_variant": unsafe_prior,
            "speech_cut_previous_variants": [unsafe_prior],
        }
    )
    original = copy.deepcopy(plan)

    outcome = terminalize_required_speech_generations(plan, job_id=job_id)

    assert outcome.status == "blocked"
    assert plan == original


def _resumable_plan(job_id: str, generation: str) -> tuple[dict, dict]:
    plan = _reserved_plan(job_id, generation)
    prefix = f"generative-jobs/{job_id}/render-generations/{generation}/"
    result = {
        "variant_id": "subtitled",
        "rank": 1,
        "text_mode": "none",
        "music_track_id": None,
        "render_generation_id": generation,
        "render_status": "ready",
        "ok": True,
        "video_path": f"{prefix}output.mp4",
        "poster_path": f"{prefix}poster.jpg",
        "output_url": "https://storage.example/signed",
        "_speech_cleanup_outcome_context": {
            "analysis_attempt_id": "trace-1",
            "analysis_view": "full_clip",
            "detector_version": "mixed-gap-v1",
            "source_tag": "0123456789abcdef",
            "selected_plan": "candidate",
            "candidate_status": "ready",
            "output_removal_count": 1,
            "output_removed_ms": 572,
        },
    }
    stage_required_speech_generation(plan, result=result, generation=generation)
    close_required_speech_generation_uploads(plan, generation=generation)
    return plan, result


def test_resume_classifier_requires_exact_generation_objects_and_context() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan, result = _resumable_plan(job_id, generation)
    checked: list[str] = []

    decision = classify_required_speech_resume(
        plan,
        job_id=job_id,
        variant_id="subtitled",
        expected_music_track_id=None,
        expected_analysis_view="full_clip",
        expected_detector_version="mixed-gap-v1",
        object_exists=lambda path: checked.append(path) or True,
    )

    assert decision.status == "resumable"
    assert decision.generation == generation
    assert decision.staged_result == result
    assert checked == sorted([result["poster_path"], result["video_path"]])


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda plan, result: plan["_speech_cleanup_internal"]["staged_render_results"][
                f"subtitled:{result['render_generation_id']}"
            ].update(video_path="generative-jobs/fixed.mp4"),
            "artifact_prefix_mismatch",
        ),
        (
            lambda plan, result: plan["_speech_cleanup_internal"]["terminal_pending"].clear(),
            "terminal_context_missing",
        ),
        (
            lambda plan, result: plan["_speech_cleanup_internal"]["terminal_pending"][
                f"subtitled:{result['render_generation_id']}"
            ].update(detector_version="old"),
            "terminal_context_mismatch",
        ),
    ],
)
def test_resume_classifier_rotates_unprovable_stage(mutation, reason) -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan, result = _resumable_plan(job_id, generation)
    mutation(plan, result)

    decision = classify_required_speech_resume(
        plan,
        job_id=job_id,
        variant_id="subtitled",
        expected_music_track_id=None,
        expected_analysis_view="full_clip",
        expected_detector_version="mixed-gap-v1",
        object_exists=lambda _path: True,
    )

    assert decision.status == "rotate"
    assert decision.reason == reason


def test_retry_rotation_closes_cleanup_debt_and_scrubs_old_prefix() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    prefix = f"generative-jobs/{job_id}/render-generations/{generation}/"
    plan, _result = _resumable_plan(job_id, generation)
    plan["variants"][0]["video_path"] = f"{prefix}provisional.mp4"

    rotate_required_speech_generation_for_retry(
        plan,
        job_id=job_id,
        variant_id="subtitled",
        generation=generation,
    )

    assert "render_generation_id" not in plan["variants"][0]
    assert "video_path" not in plan["variants"][0]
    internal = plan["_speech_cleanup_internal"]
    assert "required_speech_generation_locks" not in internal
    assert "staged_render_results" not in internal
    assert "terminal_pending" not in internal
    assert internal["render_generation_cleanup_pending"][0]["upload_state"] == "closed"


def test_retry_rotation_never_relinquishes_a_fresh_writing_lease() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    plan = _reserved_plan(job_id, generation)

    decision = classify_required_speech_resume(
        plan,
        job_id=job_id,
        variant_id="subtitled",
        expected_music_track_id=None,
        expected_analysis_view="full_clip",
        expected_detector_version="mixed-gap-v1",
        object_exists=lambda _path: True,
    )

    assert decision.status == "blocked"
    assert decision.reason == "uploads_still_active"
    assert decision.retry_after_s is not None
    assert 0 < decision.retry_after_s <= 35 * 60
    with pytest.raises(RequiredSpeechOwnershipError, match="generation_uploads_still_active"):
        rotate_required_speech_generation_for_retry(
            plan,
            job_id=job_id,
            variant_id="subtitled",
            generation=generation,
        )


def test_claim_classifier_shares_release_expiry_and_same_task_semantics() -> None:
    base = {
        "attempt_id": "attempt-1",
        "task_id": "task-1",
        "retry_number": 0,
        "claimed_at_epoch_s": 100.0,
    }

    assert classify_required_speech_claim(base, now_epoch_s=101.0).status == "fresh"
    assert (
        classify_required_speech_claim({**base, "released": True}, now_epoch_s=101.0).status
        == "released"
    )
    assert classify_required_speech_claim(base, now_epoch_s=2000.0, ttl_s=10.0).status == "expired"
    assert (
        classify_required_speech_claim(
            base,
            now_epoch_s=101.0,
            task_id="task-1",
            retry_number=1,
        ).status
        == "same_task_retry"
    )
    assert (
        classify_required_speech_claim(
            {**base, "claimed_at_epoch_s": "bad"}, now_epoch_s=101.0
        ).status
        == "malformed"
    )


def test_terminalizer_blocks_fresh_unreleased_claim_but_accepts_release() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    prior = {
        "variant_id": "subtitled",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
        "video_path": f"generative-jobs/{job_id}/last-good.mp4",
    }
    plan = _reserved_plan(job_id, generation)
    operation_id = uuid.uuid4().hex
    plan.update(
        {
            "speech_cut_control": {
                "variant_id": "subtitled",
                "operation_id": operation_id,
                "render_generation_id": generation,
                "prior_disabled": False,
                "finalizer_claim": {
                    "operation_id": operation_id,
                    "attempt_id": "attempt-1",
                    "task_id": "task-1",
                    "retry_number": 0,
                    "claimed_at_epoch_s": 100.0,
                    "render_generation_id": generation,
                },
            },
            "speech_cut_previous_variant": copy.deepcopy(prior),
            "speech_cut_previous_variants": [copy.deepcopy(prior)],
        }
    )

    blocked = terminalize_required_speech_generations(plan, job_id=job_id, now_epoch_s=101.0)
    assert blocked.status == "blocked"
    close_required_speech_generation_uploads(plan, generation=generation)
    owned = terminalize_required_speech_generations(
        plan,
        job_id=job_id,
        now_epoch_s=101.0,
        expected_operation_id=operation_id,
        expected_attempt_id="attempt-1",
    )
    assert owned.status == "terminalized"
    plan["speech_cut_control"]["finalizer_claim"]["released"] = True
    released = terminalize_required_speech_generations(plan, job_id=job_id, now_epoch_s=101.0)
    assert released.status == "terminalized"
    assert released.plan["variants"] == [prior]


def test_rerender_generation_stays_private_through_stage_and_rollback() -> None:
    job_id = str(uuid.uuid4())
    generation = uuid.uuid4().hex
    prior = {
        "variant_id": "subtitled",
        "rank": 1,
        "text_mode": "none",
        "render_generation_id": uuid.uuid4().hex,
        "render_status": "ready",
        "ok": True,
        "video_path": f"generative-jobs/{job_id}/last-good.mp4",
    }
    operation_id = uuid.uuid4().hex
    plan = {
        "variants": [copy.deepcopy(prior)],
        "speech_cut_control": {
            "variant_id": "subtitled",
            "operation_id": operation_id,
            "render_generation_id": generation,
            "prior_disabled": False,
            "finalizer_claim": None,
        },
        "speech_cut_previous_variant": copy.deepcopy(prior),
        "speech_cut_previous_variants": [copy.deepcopy(prior)],
    }
    reserve_required_speech_generation(
        plan,
        job_id=job_id,
        pending_variant={
            "variant_id": "subtitled",
            "rank": 1,
            "text_mode": "none",
            "render_generation_id": generation,
            "render_status": "pending",
            "ok": False,
        },
        generation=generation,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=35),
    )
    mark_required_speech_rendering(
        plan,
        variant_id="subtitled",
        generation=generation,
        render_started_at="2026-09-01T12:00:00Z",
    )
    result = {
        "variant_id": "subtitled",
        "rank": 1,
        "text_mode": "none",
        "render_generation_id": generation,
        "render_status": "ready",
        "ok": True,
        "video_path": (f"generative-jobs/{job_id}/render-generations/{generation}/output.mp4"),
    }
    stage_required_speech_generation(plan, result=result, generation=generation)
    close_required_speech_generation_uploads(plan, generation=generation)

    assert plan["variants"] == [prior]
    internal = plan["_speech_cleanup_internal"]
    key = f"subtitled:{generation}"
    assert internal["working_render_variants"][key] == result
    assert (
        peek_required_speech_generation(
            plan,
            variant_id="subtitled",
            generation=generation,
        )
        == result
    )

    terminalized = terminalize_required_speech_generations(plan, job_id=job_id)

    assert terminalized.status == "terminalized"
    assert terminalized.plan["variants"] == [prior]
    assert "working_render_variants" not in terminalized.plan["_speech_cleanup_internal"]

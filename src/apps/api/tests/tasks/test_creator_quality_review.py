"""Offline Stage 2 creator-review coordinator tests."""

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.video_grader import GradeBand, GradeVerdict
from app.tasks import creator_quality_review as cqr


def _session(**values):
    defaults = {
        "id": "session-1",
        "creator_id": "creator-1",
        "plan_item_id": "item-1",
        "ownership_epoch": 3,
        "revision": 7,
        "last_review": None,
        "active_plan": {
            "plan_hash": "a" * 64,
            "edit_plan": {"context_hash": "c" * 64},
        },
        "manifest_hash": "b" * 64,
        "target_job_id": "job-1",
        "target_variant_id": "variant-1",
        "target_generation_id": "generation-1",
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _verdict(band=GradeBand.ESCALATE):
    return GradeVerdict(
        band=band,
        scores={"hook_strength": 3, "text_legibility_and_timing": 2},
        avg=2.5,
        confidence=0.8,
        reasoning="The opening is generic.",
        evidence=[
            {
                "dimension": "hook_strength",
                "kind": "structure",
                "start_s": 0.0,
                "end_s": 2.4,
                "observation": "The opening starts with an establishing shot and no open loop.",
            },
            {
                "dimension": "text_legibility_and_timing",
                "kind": "caption",
                "start_s": 1.1,
                "end_s": 2.8,
                "observation": "The caption crosses a bright area and is difficult to read.",
            },
        ],
        raw_response='{"scores": {}}',
    )


def test_queue_is_idempotent_and_uses_stable_task_id(monkeypatch):
    session = _session()
    apply_async = Mock()
    monkeypatch.setattr(cqr.quality_review_creator_session, "apply_async", apply_async)

    assert cqr.queue_creator_quality_review(
        session, job_id="job-1", variant_id="variant-1", render_generation_id="generation-1"
    )
    assert not cqr.queue_creator_quality_review(
        session, job_id="job-1", variant_id="variant-1", render_generation_id="generation-1"
    )
    apply_async.assert_called_once()
    assert apply_async.call_args.kwargs["task_id"].startswith("creator-review-")
    assert session.last_review["status"] == "pending"


def test_pending_review_can_be_republished_after_commit(monkeypatch):
    session = _session(phase="awaiting_feedback")
    apply_async = Mock()
    monkeypatch.setattr(cqr.quality_review_creator_session, "apply_async", apply_async)

    assert cqr.queue_creator_quality_review(
        session, job_id="job-1", variant_id="variant-1", render_generation_id="generation-1"
    )
    apply_async.reset_mock()
    assert cqr.queue_creator_quality_review(
        session, job_id="job-1", variant_id="variant-1", render_generation_id="generation-1"
    )
    apply_async.assert_called_once()


def test_review_payload_has_timestamped_evidence_and_one_inert_revision():
    payload = cqr.build_review_payload(
        _session(),
        job_id="job-1",
        variant_id="variant-1",
        generation_id="generation-1",
        verdict=_verdict(),
    )

    assert payload["decision"] == "revise"
    assert len(payload["evidence"]) == 2
    assert payload["evidence"][0]["start_s"] < payload["evidence"][0]["end_s"]
    assert payload["proposed_revision"]["evidence_ids"] == [
        row["evidence_id"] for row in payload["evidence"]
    ]
    assert payload["context_hash"] == "c" * 64
    assert payload["review_mode"] == "mixed"
    assert "objective_tag" not in payload


def test_review_payload_marks_only_an_objective_failing_dimension_eligible():
    verdict = GradeVerdict(
        band=GradeBand.ESCALATE,
        scores={
            "hook_strength": 4,
            "text_legibility_and_timing": 2,
            "looks_filmed_not_templated": 4,
            "overall_quality": 4,
        },
        avg=3.5,
        confidence=0.9,
        reasoning="Captions are difficult to read.",
        evidence=[
            {
                "dimension": "text_legibility_and_timing",
                "kind": "caption",
                "start_s": 1.0,
                "end_s": 2.0,
                "observation": "The caption loses contrast over the bright wall.",
            }
        ],
    )

    payload = cqr.build_review_payload(
        _session(),
        job_id="job-1",
        variant_id="variant-1",
        generation_id="generation-1",
        verdict=verdict,
    )

    assert payload["review_mode"] == "objective"
    assert payload["objective_tag"] == "objective_quality"
    assert payload["allowlist_action"] == "caption_legibility"


def test_review_payload_never_labels_a_taste_only_failure_objective():
    verdict = GradeVerdict(
        band=GradeBand.ESCALATE,
        scores={
            "hook_strength": 2,
            "text_legibility_and_timing": 5,
            "looks_filmed_not_templated": 3,
            "overall_quality": 3,
        },
        avg=3.25,
        confidence=0.95,
        reasoning="The edit feels generic.",
        evidence=[
            {
                "dimension": "looks_filmed_not_templated",
                "kind": "visual",
                "start_s": 3.0,
                "end_s": 4.5,
                "observation": "The repeated crop and hold feel formulaic.",
            }
        ],
    )

    payload = cqr.build_review_payload(
        _session(),
        job_id="job-1",
        variant_id="variant-1",
        generation_id="generation-1",
        verdict=verdict,
    )

    assert payload["review_mode"] == "taste"
    assert payload["allowlist_action"] is None
    assert "objective_tag" not in payload


def test_review_payload_pins_evidence_backed_transition_boundary():
    verdict = GradeVerdict(
        band=GradeBand.ESCALATE,
        scores={"transition_continuity": 2},
        avg=2.0,
        confidence=0.95,
        reasoning="The second cut flashes.",
        evidence=[
            {
                "dimension": "transition_continuity",
                "kind": "timing",
                "start_s": 2.8,
                "end_s": 3.2,
                "observation": "A visible flash appears at the second shot boundary.",
            }
        ],
    )

    payload = cqr.build_review_payload(
        _session(),
        job_id="job-1",
        variant_id="variant-1",
        generation_id="generation-1",
        verdict=verdict,
        variant={
            "ai_timeline": {
                "slots": [{"duration_s": 1.0}, {"duration_s": 2.0}, {"duration_s": 2.0}]
            }
        },
    )

    assert payload["allowlist_action"] == "transition_fallback"
    assert payload["boundary_index"] == 1
    assert payload["objective_tag"] == "objective_quality"


def test_review_payload_pins_only_treatment_active_at_evidence_time():
    verdict = GradeVerdict(
        band=GradeBand.ESCALATE,
        scores={"optional_overlay_sfx_quality": 2},
        avg=2.0,
        confidence=0.95,
        reasoning="The effect fires late.",
        evidence=[
            {
                "dimension": "optional_overlay_sfx_quality",
                "kind": "audio",
                "start_s": 4.0,
                "end_s": 4.8,
                "observation": "A sound effect lands after the visible action.",
            }
        ],
    )

    payload = cqr.build_review_payload(
        _session(),
        job_id="job-1",
        variant_id="variant-1",
        generation_id="generation-1",
        verdict=verdict,
        variant={"sound_effects": [{"id": "sfx-4", "at_s": 4.4, "duration_s": 0.2}]},
    )

    assert payload["allowlist_action"] == "remove_optional_treatment"
    assert payload["treatment"] == "sfx"
    assert payload["treatment_id"] == "sfx-4"


def test_review_payload_pins_already_validated_speech_candidate():
    verdict = GradeVerdict(
        band=GradeBand.ESCALATE,
        scores={"speech_cut_integrity": 2},
        avg=2.0,
        confidence=0.95,
        reasoning="A repeated phrase remains.",
        evidence=[
            {
                "dimension": "speech_cut_integrity",
                "kind": "audio",
                "start_s": 5.0,
                "end_s": 6.0,
                "observation": "The speaker restarts the same phrase.",
            }
        ],
    )
    variant = {
        "speech_cut_candidates": [
            {
                "candidate_id": "cut-1",
                "start_s": 5.1,
                "end_s": 5.9,
                "status": "pending",
                "source": "retake_review",
            }
        ]
    }

    payload = cqr.build_review_payload(
        _session(),
        job_id="job-1",
        variant_id="variant-1",
        generation_id="generation-1",
        verdict=verdict,
        variant=variant,
    )

    assert payload["allowlist_action"] == "speech_cut"
    assert payload["candidate_id"] == "cut-1"
    assert payload["expected_cut_revision"]


def test_review_payload_fails_closed_when_context_pin_is_missing():
    session = _session(active_plan={"plan_hash": "a" * 64})
    with pytest.raises(ValueError, match="context hash"):
        cqr.build_review_payload(
            session,
            job_id="job-1",
            variant_id="variant-1",
            generation_id="generation-1",
            verdict=_verdict(),
        )


def test_review_payload_rejects_missing_timestamped_evidence():
    verdict = _verdict()
    verdict.evidence = []
    with pytest.raises(ValueError, match="timestamped grader evidence"):
        cqr.build_review_payload(
            _session(),
            job_id="job-1",
            variant_id="variant-1",
            generation_id="generation-1",
            verdict=verdict,
        )


def test_stale_target_never_persists_review_or_agent_run(monkeypatch):
    persist_run = Mock()
    monkeypatch.setattr(
        cqr,
        "claim_exact_review",
        lambda *args, **kwargs: None,
    )
    db = Mock()

    assert not cqr.run_quality_review(
        session_id="session-1",
        job_id="job-1",
        variant_id="variant-1",
        render_generation_id="old-generation",
        db_factory=lambda: db,
        reviewer=lambda _: _verdict(GradeBand.AUTO_PASS),
        persist_run=persist_run,
    )

    persist_run.assert_not_called()


def test_persist_rechecks_current_job_generation_before_writing():
    session_id = uuid.uuid4()
    job_id = uuid.uuid4()
    session = _session(
        id=session_id,
        creator_id=uuid.uuid4(),
        plan_item_id=uuid.uuid4(),
        target_job_id=job_id,
        target_variant_id="variant-1",
        target_generation_id="generation-1",
    )
    session.last_review = {
        "status": "running",
        "review_key": cqr.review_key(str(session_id), str(job_id), "variant-1", "generation-1"),
    }
    job = SimpleNamespace(
        id=job_id,
        user_id=session.creator_id,
        content_plan_item_id=session.plan_item_id,
        content_plan_ownership_epoch=session.ownership_epoch,
        status="variants_ready",
        assembly_plan={
            "variants": [
                {
                    "variant_id": "variant-1",
                    "render_status": "ready",
                    "render_generation_id": "generation-2",
                }
            ]
        },
    )

    class FakeDb:
        def __init__(self):
            self.committed = False

        def get(self, model, identifier, with_for_update=False):
            if model.__name__ == "CreatorAgentSession":
                return session
            if model.__name__ == "Job":
                return job
            return None

        def commit(self):
            self.committed = True

    db = FakeDb()
    assert not cqr.persist_review_if_current(
        db,
        session_id=str(session_id),
        job_id=str(job_id),
        variant_id="variant-1",
        generation_id="generation-1",
        payload={"status": "pass"},
    )
    assert not db.committed


def test_grader_failure_is_visible_and_fail_open(monkeypatch):
    session = _session()
    persist_run = Mock()
    mark_unavailable = Mock()
    monkeypatch.setattr(
        cqr,
        "claim_exact_review",
        lambda *args, **kwargs: (session, "opaque-video-path", {}),
    )
    monkeypatch.setattr(cqr, "mark_review_unavailable", mark_unavailable)

    cqr.run_quality_review(
        session_id="session-1",
        job_id="job-1",
        variant_id="variant-1",
        render_generation_id="generation-1",
        db_factory=lambda: Mock(),
        reviewer=Mock(side_effect=RuntimeError("grader unavailable")),
        persist_run=persist_run,
    )

    mark_unavailable.assert_called_once()
    assert mark_unavailable.call_args.kwargs["code"] == "review_failed"
    assert persist_run.call_args.kwargs["outcome"] == "failed"

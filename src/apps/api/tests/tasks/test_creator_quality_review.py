"""Offline Stage 2 creator-review coordinator tests."""

from types import SimpleNamespace
from unittest.mock import Mock

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
        "active_plan": {"plan_hash": "a" * 64},
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


def test_stale_target_never_persists_review_or_agent_run(monkeypatch):
    persist_run = Mock()
    monkeypatch.setattr(
        cqr,
        "claim_exact_review",
        lambda *args, **kwargs: None,
    )
    db = Mock()

    cqr.run_quality_review(
        session_id="session-1",
        job_id="job-1",
        variant_id="variant-1",
        render_generation_id="old-generation",
        db_factory=lambda: db,
        reviewer=lambda _: _verdict(GradeBand.AUTO_PASS),
        persist_run=persist_run,
    )

    persist_run.assert_not_called()


def test_grader_failure_is_visible_and_fail_open(monkeypatch):
    session = _session()
    persist_run = Mock()
    mark_unavailable = Mock()
    monkeypatch.setattr(
        cqr,
        "claim_exact_review",
        lambda *args, **kwargs: (session, "opaque-video-path"),
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

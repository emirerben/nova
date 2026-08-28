from __future__ import annotations

import uuid
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from billiard.exceptions import SoftTimeLimitExceeded

from app.pipeline.speech_cut_state import cut_revision, make_candidate
from app.routes.generative_jobs import rollback_speech_cut_dispatch
from app.tasks import generative_build as gb


class _Session:
    def __init__(self, job) -> None:
        self.job = job
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, *_args, **_kwargs):
        return self.job

    def commit(self) -> None:
        self.commits += 1


def _candidate() -> dict:
    return make_candidate(
        start_s=4.0,
        end_s=5.0,
        reason="possible abandoned take",
        source="retake_review",
        preview="start again",
        source_fingerprint="source-a",
        transcript_hash="transcript-a",
    )


def _prior_variant() -> dict:
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_status": "ready",
        "ok": True,
        "video_path": "generative-jobs/job/last-good.mp4",
        "output_url": "https://storage/last-good.mp4",
        "speech_cut_candidates": [_candidate()],
        "speech_cut_forced_removals": [],
        "speech_cuts_disabled": False,
        "silence_cut": {"removed": [{"start_s": 1.0, "end_s": 2.0, "reason": "silence"}]},
    }


def _inflight_job(
    *,
    prior_disabled: bool = False,
    operation_id: str = "operation-a",
    attempt_id: str | None = "attempt-a",
):
    prior = _prior_variant()
    current = deepcopy(prior)
    current.update(
        {
            "render_status": "rendering",
            "ok": False,
            "video_path": "generative-jobs/job/new.mp4",
            "output_url": "https://storage/new.mp4",
            "render_generation_id": "generation-new",
        }
    )
    current["speech_cut_candidates"][0]["status"] = "applying"
    current["silence_cut"]["removed"].append(
        {"start_s": 4.0, "end_s": 5.0, "reason": "retake_review"}
    )
    candidate_id = current["speech_cut_candidates"][0]["candidate_id"]
    request = {
        "operation": "apply_speech_cut_candidate",
        "candidate_id": candidate_id,
        "removed": {
            "start_s": 4.0,
            "end_s": 5.0,
            "reason": "retake_review",
            "candidate_id": candidate_id,
        },
        "time_saved_s": 1.0,
        "revision": "in-flight-revision",
        "operation_id": operation_id,
    }
    control = {
        "variant_id": "subtitled",
        "forced_removals": [request["removed"]],
        "desired_disabled": False,
        "prior_disabled": prior_disabled,
        "operation": request,
        "operation_id": operation_id,
        "finalizer_claim": (
            {
                "operation_id": operation_id,
                "attempt_id": attempt_id,
                "claimed_at_epoch_s": 100.0,
            }
            if attempt_id
            else None
        ),
    }
    return SimpleNamespace(
        status="processing",
        assembly_plan={
            "silence_cut_disabled": False,
            "speech_cut_control": control,
            "speech_cut_previous_variant": prior,
            "variants": [current],
        },
    )


def test_enqueue_rollback_restores_exact_last_good_state() -> None:
    job = _inflight_job(prior_disabled=True)
    prior = deepcopy(job.assembly_plan["speech_cut_previous_variant"])

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        rollback_speech_cut_dispatch(job, "broker unavailable")

    assert job.status == "variants_ready"
    assert job.assembly_plan["variants"] == [prior]
    assert job.assembly_plan["silence_cut_disabled"] is True
    assert job.assembly_plan["speech_cut_control"] is None
    assert job.assembly_plan["speech_cut_previous_variant"] is None
    assert "speech_cut_last_receipt" not in job.assembly_plan["variants"][0]


def test_worker_failure_restores_prior_video_state_and_emits_no_receipt(monkeypatch) -> None:
    job = _inflight_job(prior_disabled=True)
    prior = deepcopy(job.assembly_plan["speech_cut_previous_variant"])
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        gb._restore_failed_speech_cut_rerender(
            str(uuid.uuid4()),
            "render exploded",
            expected_operation_id="operation-a",
            expected_attempt_id="attempt-a",
        )

    assert session.commits == 1
    assert job.status == "variants_ready"
    restored = job.assembly_plan["variants"][0]
    assert {key: restored.get(key) for key in prior} == prior
    assert job.assembly_plan["silence_cut_disabled"] is True
    assert job.assembly_plan["speech_cut_last_error"] == "render exploded"
    assert "speech_cut_last_receipt" not in job.assembly_plan["variants"][0]
    assert job.assembly_plan["variants"][0]["speech_cut_last_error"] == {
        "operation_id": "operation-a",
        "message": "render exploded",
    }


def test_worker_failure_restores_all_sibling_variants_byte_for_byte(monkeypatch) -> None:
    job = _inflight_job()
    sibling = {
        "variant_id": "sibling",
        "render_status": "ready",
        "video_path": "generative-jobs/job/sibling.mp4",
        "motion_scenes": [{"id": "creator-block-1"}],
    }
    prior_variants = [deepcopy(job.assembly_plan["speech_cut_previous_variant"]), sibling]
    job.assembly_plan["speech_cut_previous_variants"] = deepcopy(prior_variants)
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        gb._restore_failed_speech_cut_rerender(
            str(uuid.uuid4()),
            "compose failed",
            expected_operation_id="operation-a",
            expected_attempt_id="attempt-a",
        )

    restored = job.assembly_plan["variants"]
    assert restored[1] == sibling
    assert restored[0] == {
        **prior_variants[0],
        "speech_cut_last_error": {
            "operation_id": "operation-a",
            "message": "compose failed",
        },
    }


def test_winning_publish_is_the_only_completed_receipt_boundary(monkeypatch) -> None:
    job = _inflight_job()
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    before = job.assembly_plan["variants"][0]
    assert before["speech_cut_candidates"][0]["status"] == "applying"
    assert "speech_cut_last_receipt" not in before

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        gb._publish_speech_cut_rerender(
            str(uuid.uuid4()),
            expected_operation_id="operation-a",
            expected_attempt_id="attempt-a",
        )

    published = job.assembly_plan["variants"][0]
    receipt = published["speech_cut_last_receipt"]
    assert published["speech_cut_candidates"][0]["status"] == "accepted"
    assert receipt["status"] == "applied"
    assert receipt["render_generation_id"] == "generation-new"
    assert receipt["revision"] == cut_revision(published)
    assert job.assembly_plan["speech_cut_control"] is None
    assert job.assembly_plan["speech_cut_previous_variant"] is None


def test_publish_commits_rollback_clear_before_reconciling_pending_poster(monkeypatch) -> None:
    job = _inflight_job()
    old_poster = (
        "generative-jobs/job/last-good.mp4.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    )
    replacement = job.assembly_plan["variants"][0]["video_path"]
    receipt = {"old_path": old_poster, "replacement_path": replacement}
    job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] = [receipt]
    job.assembly_plan["speech_cut_previous_variants"] = [
        deepcopy(job.assembly_plan["speech_cut_previous_variant"])
    ]
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)
    reconciled: list[tuple[str, list[str]]] = []

    def _assert_committed_then_reconcile(job_id: str, paths: list[str]) -> None:
        assert session.commits == 1
        assert job.assembly_plan["speech_cut_previous_variant"] is None
        assert job.assembly_plan["speech_cut_previous_variants"] is None
        reconciled.append((job_id, list(paths)))

    monkeypatch.setattr(gb, "_reconcile_retired_variant_posters", _assert_committed_then_reconcile)
    job_id = str(uuid.uuid4())

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        gb._publish_speech_cut_rerender(
            job_id,
            expected_operation_id="operation-a",
            expected_attempt_id="attempt-a",
        )

    assert reconciled == [(job_id, [old_poster])]
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [receipt]


def test_publish_cleanup_failure_keeps_committed_poster_receipt(monkeypatch) -> None:
    job = _inflight_job()
    old_poster = (
        "generative-jobs/job/last-good.mp4.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    )
    replacement = job.assembly_plan["variants"][0]["video_path"]
    receipt = {"old_path": old_poster, "replacement_path": replacement}
    job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] = [receipt]
    job.assembly_plan["speech_cut_previous_variants"] = [
        deepcopy(job.assembly_plan["speech_cut_previous_variant"])
    ]
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    def _fail_cleanup(_job_id):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(gb, "reconcile_video_poster_cleanup_receipts", _fail_cleanup)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        gb._publish_speech_cut_rerender(
            str(uuid.uuid4()),
            expected_operation_id="operation-a",
            expected_attempt_id="attempt-a",
        )

    assert session.commits == 1
    assert job.assembly_plan["speech_cut_previous_variant"] is None
    assert job.assembly_plan["speech_cut_previous_variants"] is None
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [receipt]


def test_speech_cut_intermediate_finalize_propagates_cleanup_soft_timeout(monkeypatch) -> None:
    job = _inflight_job()
    current = job.assembly_plan["variants"][0]
    old_poster = (
        "generative-jobs/job/new.mp4.poster.backfill-11111111-1111-4111-8111-111111111111.jpg"
    )
    current["poster_path"] = old_poster
    finalized = {**current, "poster_path": None}
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)
    monkeypatch.setattr(
        gb,
        "reconcile_video_poster_cleanup_receipts",
        lambda _job_id: (_ for _ in ()).throw(SoftTimeLimitExceeded()),
    )

    with pytest.raises(SoftTimeLimitExceeded):
        gb._set_status(
            str(uuid.uuid4()),
            "variants_ready",
            {"variants": [finalized]},
            merge_finalized_variants=True,
            expected_speech_cut_operation_id="operation-a",
            expected_speech_cut_attempt_id="attempt-a",
        )

    # The intermediate write is durable, but the exception must reach the task
    # wrapper before compose/publish so it can restore the last-good snapshot.
    assert session.commits == 1
    assert job.status == "variants_ready"
    assert job.assembly_plan[gb.VIDEO_POSTER_BACKFILL_CLEANUP_FIELD] == [
        {
            "old_path": old_poster,
            "replacement_path": current["video_path"],
        }
    ]


def test_duplicate_delivery_cannot_claim_or_publish_over_winner(monkeypatch) -> None:
    job = _inflight_job(attempt_id=None)
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)
    monkeypatch.setattr(gb.time, "time", lambda: 500.0)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        assert gb._claim_speech_cut_finalize(
            str(uuid.uuid4()),
            "operation-a",
            "attempt-winner",
            task_id="celery-task",
            retry_number=0,
        )
        assert not gb._claim_speech_cut_finalize(
            str(uuid.uuid4()),
            "operation-a",
            "attempt-duplicate",
            task_id="celery-task",
            retry_number=0,
        )
        try:
            gb._publish_speech_cut_rerender(
                str(uuid.uuid4()),
                expected_operation_id="operation-a",
                expected_attempt_id="attempt-duplicate",
            )
        except RuntimeError as exc:
            assert "superseded" in str(exc)
        else:  # pragma: no cover - publication must be token gated
            raise AssertionError("duplicate attempt published")
        gb._publish_speech_cut_rerender(
            str(uuid.uuid4()),
            expected_operation_id="operation-a",
            expected_attempt_id="attempt-winner",
        )

    assert job.assembly_plan["speech_cut_control"] is None
    assert job.assembly_plan["variants"][0]["speech_cut_last_receipt"]["status"] == "applied"


def test_stale_claim_can_be_recovered_after_hard_time_limit(monkeypatch) -> None:
    job = _inflight_job(attempt_id="dead-worker")
    job.assembly_plan["speech_cut_control"]["finalizer_claim"]["claimed_at_epoch_s"] = 10.0
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)
    monkeypatch.setattr(
        gb.time,
        "time",
        lambda: 10.0 + gb._SPEECH_CUT_FINALIZER_CLAIM_TTL_S + 1.0,
    )

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        assert gb._claim_speech_cut_finalize(str(uuid.uuid4()), "operation-a", "recovered-worker")

    claim = job.assembly_plan["speech_cut_control"]["finalizer_claim"]
    assert claim["attempt_id"] == "recovered-worker"


def test_transient_failure_retry_can_reclaim_even_if_release_failed(monkeypatch) -> None:
    job = _inflight_job(attempt_id="attempt-one")
    job.assembly_plan["speech_cut_control"]["finalizer_claim"].update(
        {"task_id": "celery-task", "retry_number": 0}
    )
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        assert gb._claim_speech_cut_finalize(
            str(uuid.uuid4()),
            "operation-a",
            "attempt-two",
            task_id="celery-task",
            retry_number=1,
        )

    control = job.assembly_plan["speech_cut_control"]
    assert control["finalizer_claim"]["attempt_id"] == "attempt-two"
    assert job.assembly_plan["speech_cut_previous_variant"]["video_path"].endswith("last-good.mp4")


def test_stale_worker_cannot_rollback_winning_attempt(monkeypatch) -> None:
    job = _inflight_job(attempt_id="winner")
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        restored = gb._restore_failed_speech_cut_rerender(
            str(uuid.uuid4()),
            "late failure",
            expected_operation_id="operation-a",
            expected_attempt_id="stale-worker",
        )

    assert restored is False
    assert job.assembly_plan["speech_cut_control"] is not None
    assert job.assembly_plan["variants"][0]["render_status"] == "rendering"


def test_publication_never_clears_control_when_target_variant_is_missing(monkeypatch) -> None:
    job = _inflight_job(attempt_id="winner")
    job.assembly_plan["variants"] = []
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        try:
            gb._publish_speech_cut_rerender(
                str(uuid.uuid4()),
                expected_operation_id="operation-a",
                expected_attempt_id="winner",
            )
        except RuntimeError as exc:
            assert "variant disappeared" in str(exc)
        else:  # pragma: no cover - the operation must remain recoverable
            raise AssertionError("missing target published")

    assert job.assembly_plan["speech_cut_control"] is not None


def test_publication_rejects_false_receipt_when_forced_cut_was_not_rendered(
    monkeypatch,
) -> None:
    job = _inflight_job(attempt_id="winner")
    job.assembly_plan["variants"][0]["silence_cut"]["removed"] = [
        {"start_s": 1.0, "end_s": 2.0, "reason": "silence"}
    ]
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        try:
            gb._publish_speech_cut_rerender(
                str(uuid.uuid4()),
                expected_operation_id="operation-a",
                expected_attempt_id="winner",
            )
        except RuntimeError as exc:
            assert "did not apply" in str(exc)
        else:  # pragma: no cover - a receipt must prove the rendered cut
            raise AssertionError("unrendered cut published")

    assert job.assembly_plan["speech_cut_control"] is not None
    assert "speech_cut_last_receipt" not in job.assembly_plan["variants"][0]


def test_restore_publication_rejects_residual_rendered_cuts(monkeypatch) -> None:
    job = _inflight_job(attempt_id="winner")
    control = job.assembly_plan["speech_cut_control"]
    control.update(
        {
            "desired_disabled": True,
            "forced_removals": [],
            "operation": {
                "operation": "restore_original_timing",
                "operation_id": "operation-a",
                "revision": "restore-revision",
            },
        }
    )
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    with patch("sqlalchemy.orm.attributes.flag_modified"):
        try:
            gb._publish_speech_cut_rerender(
                str(uuid.uuid4()),
                expected_operation_id="operation-a",
                expected_attempt_id="winner",
            )
        except RuntimeError as exc:
            assert "residual cuts" in str(exc)
        else:  # pragma: no cover - restore receipts require an uncut render
            raise AssertionError("residual cuts published as restored")

    assert job.assembly_plan["speech_cut_control"] is not None


def test_speech_cut_rerender_never_redispatches_first_generation_suggestions() -> None:
    with (
        patch.object(gb, "_maybe_autoplace_after_finalize") as overlay,
        patch.object(gb, "_maybe_visual_blocks_after_finalize") as visual,
        patch.object(gb, "_maybe_sfx_autoplace_after_finalize") as sfx,
    ):
        gb._dispatch_post_finalize_suggestion_chains("job-a", speech_cut_rerender=True)

    overlay.assert_not_called()
    visual.assert_not_called()
    sfx.assert_not_called()


def test_subtitled_compose_reburns_captions_before_publication(monkeypatch) -> None:
    job = _inflight_job(attempt_id="winner")
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    def _accept_caption_reburn(*_args, terminal_state, **_kwargs) -> None:
        terminal_state["accepted"] = True

    with (
        patch.object(gb, "_update_variant_entry", return_value=True) as update,
        patch.object(
            gb, "_run_reburn_narrated_captions", side_effect=_accept_caption_reburn
        ) as reburn,
        patch.object(gb, "_assert_speech_cut_finalize_claim") as claim,
    ):
        gb._compose_speech_cut_rerender(
            str(uuid.uuid4()),
            expected_operation_id="operation-a",
            expected_attempt_id="winner",
        )

    update.assert_called_once()
    reburn.assert_called_once()
    claim.assert_called_once_with(str(update.call_args.args[0]), "operation-a", "winner")


def test_talking_head_compose_reburns_text_before_publication(monkeypatch) -> None:
    job = _inflight_job(attempt_id="winner")
    variant = job.assembly_plan["variants"][0]
    variant.update(
        {
            "resolved_archetype": "talking_head",
            "intro_text": "Pinned hook",
            "text_mode": "agent_text",
            "style_set_id": "default",
        }
    )
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)

    with (
        patch.object(gb, "_update_variant_entry", return_value=True) as update,
        patch.object(gb, "_reburn_text_on_base", return_value={"video_path": "new.mp4"}) as reburn,
        patch.object(gb, "_will_reapply_media_layers", return_value=False),
        patch.object(gb, "_assert_speech_cut_finalize_claim") as claim,
    ):
        gb._compose_speech_cut_rerender(
            str(uuid.uuid4()),
            expected_operation_id="operation-a",
            expected_attempt_id="winner",
        )

    assert update.call_count == 2
    assert reburn.call_args.kwargs["agent_text"] == "Pinned hook"
    claim.assert_called_once()


def test_timing_rerender_pins_existing_creator_hook_and_style(monkeypatch) -> None:
    job = _inflight_job(attempt_id="winner")
    prior = job.assembly_plan["speech_cut_previous_variant"]
    prior.update(
        {
            "text_mode": "agent_text",
            "style_set_id": "creator-style",
            "intro_text": "Keep my hook",
            "intro_highlight_word": "hook",
            "intro_text_color": "#FFF3A6",
            "intro_behind_subject": True,
            "intro_layout": "cluster",
            "intro_mode": "cluster",
            "intro_placement": "top",
            "intro_word_roles": [{"word": "hook", "role": "hero"}],
            "sequence_quote": "Keep my sequence",
            "user_style_knobs": {"energy": "calm"},
        }
    )
    session = _Session(job)
    monkeypatch.setattr(gb, "_sync_session", lambda: session)
    result = {
        "variant_id": "subtitled",
        "intro_text": "Regenerated hook",
        "style_set_id": "different-style",
        "silence_cut": {"removed": []},
    }

    merged = gb._merge_speech_cut_prior_state(
        str(uuid.uuid4()),
        result,
        expected_operation_id="operation-a",
        expected_attempt_id="winner",
    )

    for field in (
        "text_mode",
        "style_set_id",
        "intro_text",
        "intro_highlight_word",
        "intro_text_color",
        "intro_behind_subject",
        "intro_layout",
        "intro_mode",
        "intro_placement",
        "intro_word_roles",
        "sequence_quote",
        "user_style_knobs",
    ):
        assert merged[field] == prior[field]

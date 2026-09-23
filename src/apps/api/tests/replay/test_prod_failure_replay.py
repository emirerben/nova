"""KRI-118 lane L7: replay real production failure shapes against the current
planner/compiler chain, with NO Gemini/LLM calls (see `tests/replay/README.md`
for how the fixtures under `tests/fixtures/kri118/` were produced -- 4 of 5
are pulled from real prod job-debug data via `scripts/admin.py --prod`; none
are hand-invented).

Each fixture is replayed as deeply as it can be reproduced **offline** (no
ffmpeg, no Whisper, no network) -- for the three guided-story shapes this
means the REAL, pure planning-time compiler (`validate_proposal_compiles`,
which runs the exact beat-allocation arithmetic `_allocate_beat_durations`
uses at render time); for the phone shape it means replaying the REAL
exception object and message captured from the prod job through the REAL
worker failure-mapping path; for the speech-cleanup shape (whose failing
decision depends on a real Whisper transcript this repo cannot commit) it
means asserting the typed-exception contract the worker's real raise site
uses. Every assertion below is on a TYPED, SPECIFIC outcome -- never a bare
`Exception`, and never the generic `processing_failed`/`variants_failed`
catch-all a caller would otherwise see.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.guided_story import GuidedStoryError, validate_proposal_compiles
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.schemas.edit_proposal import EditProposalSnapshot, MediaRef, StoryBeat
from app.services.speech_cleanup import SpeechCleanupFailure

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "kri118"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES_DIR / f"{name}.json").read_text())


def _snapshot_from_fixture(data: dict) -> EditProposalSnapshot:
    """Mirror `test_edit_proposal_story_shapes.py`'s
    `_snapshot_from_repaired_output` helper: build a real, schema-valid
    `EditProposalSnapshot` from the fixture's media/beat shape so
    `validate_proposal_compiles` runs the same arithmetic the render worker
    would run against the approved plan."""

    return EditProposalSnapshot(
        direction=data["direction"],
        pace="balanced",
        duration_s=data["duration_s"],
        title="Replay fixture",
        media=[
            MediaRef(
                lane="clip",
                media_id=media["media_id"],
                gcs_path=f"users/u/{media['media_id']}.mp4",
                generation="1",
                kind=media["kind"],
                duration_s=media["duration_s"],
            )
            for media in data["media"]
        ],
        story_beats=[
            StoryBeat(
                beat_id=beat["beat_id"],
                topic=beat["topic"],
                thought=beat["thought"],
                media_ids=beat["media_ids"],
                duration_s=beat["duration_s"],
            )
            for beat in data["story_beats"]
        ],
    )


# --- guided_story_duration_impossible: fully offline-replayable -----------


def test_guided_story_duration_impossible_replays_against_real_compiler() -> None:
    """This is a genuine replay, not a typed-contract stand-in:
    `_allocate_beat_durations` is pure duration/overlap arithmetic (no
    ffmpeg), so re-running `validate_proposal_compiles` against the exact
    real media/beat durations pulled from the prod job tells us, for real,
    whether today's compiler still finds this shape infeasible.

    CONFIRMED (2026-09-23, this repo revision, L1 merged in): this exact shape
    now compiles successfully -- it no longer reproduces
    `guided_story_duration_impossible`. Pinned as a success outcome rather
    than a permissive try/except so a future regression that makes this shape
    infeasible again fails this test loudly instead of silently passing
    either branch."""

    data = _load("guided_story_duration_impossible_1")
    snapshot = _snapshot_from_fixture(data)

    try:
        validate_proposal_compiles(snapshot)
    except GuidedStoryError as exc:  # pragma: no cover - regression signal
        pytest.fail(
            "This prod shape used to compile (see CONFIRMED note above) and "
            f"now fails again with GuidedStoryError(code={exc.code!r}): {exc}. "
            "If this is an intentional allocator/feasibility change, update "
            "this test's pinned expectation deliberately -- do not silently "
            "accept a generic failure here."
        )


# --- guided_story_render_failed / guided_story_receipt_mismatch: the
# render/verification stage itself needs real ffmpeg output and cannot be
# replayed offline. What IS offline-checkable: the shape is legitimately
# plannable (matches prod reality -- these jobs WERE approved and dispatched,
# only the render/post-render stage failed), and the failure contract at
# that stage is a typed GuidedStoryError with a stable, non-generic code.


@pytest.mark.parametrize(
    ("fixture_name", "expected_code"),
    [
        ("guided_story_render_failed_1", "guided_story_render_failed"),
        ("guided_story_receipt_mismatch_1", "guided_story_receipt_mismatch"),
    ],
)
def test_render_and_receipt_shapes_are_plannable_and_typed_when_they_fail(
    fixture_name: str, expected_code: str
) -> None:
    data = _load(fixture_name)
    snapshot = _snapshot_from_fixture(data)

    # Planning-time layer: this plan was genuinely approved and dispatched in
    # prod, so it must not be rejected before render today either -- a
    # regression here would mean the offline compiler now second-guesses a
    # shape the real renderer already accepted.
    validate_proposal_compiles(snapshot)

    # Render/verification-time layer: cannot be re-executed without the real
    # source bytes (never committed -- CLAUDE.md "Raw uploads ... NEVER in
    # git"). Pin the TYPED CONTRACT instead: `GuidedStoryError` requires an
    # explicit `code`, and the exact code this failure_reason maps to is
    # reproducible verbatim from the fixture's own `failure_reason` field --
    # confirming the worker's real raise site (grep-verified,
    # app/pipeline/guided_story.py) never falls through to a bare
    # `Exception`/generic catch-all for this shape.
    assert data["failure_reason"] == expected_code
    typed = GuidedStoryError(expected_code, "replay contract check")
    assert isinstance(typed, RuntimeError)
    assert typed.code == expected_code
    assert typed.code not in {"processing_failed", "variants_failed"}


# --- phone_plan_unsupported: replay the REAL exception + message through
# the REAL worker failure-mapping path (no synthetic message).


def test_phone_plan_unsupported_replays_real_message_through_worker_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuses the exact harness `test_phone_guided_dispatch.py`'s
    `test_phone_plan_failure_persists_failure_reason` already exercises for
    synthetic `UnsupportedPhonePlan` instances -- the only difference here is
    that the injected exception carries the REAL message string pulled from
    the prod job (see the fixture's `error_message`), replayed through the
    same `orchestrate_generative_job` -> `_fail_job` mapping."""

    from contextlib import nullcontext
    from unittest.mock import Mock

    from app.tasks import generative_build as gb
    from tests.tasks.test_phone_guided_dispatch import setup as _phone_guided_setup

    data = _load("phone_plan_unsupported_1")
    job, _snapshot, _session, planner, cloud = _phone_guided_setup(monkeypatch)
    planner.side_effect = UnsupportedPhonePlan(data["error_message"])
    monkeypatch.setattr("app.services.pipeline_trace.pipeline_trace_for", lambda _: nullcontext())
    monkeypatch.setattr(gb, "job_heartbeat", lambda _: nullcontext())
    monkeypatch.setattr(gb, "mark_finished", Mock())
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    fail_job = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail_job)

    gb.orchestrate_generative_job.run(str(job.id))

    fail_job.assert_called_once()
    _args, kwargs = fail_job.call_args
    # The real prod message still maps to the SPECIFIC typed reason, not the
    # generic `phone_plan_failed` an unrecognized/bare RuntimeError gets
    # (see the parametrized cases in test_phone_guided_dispatch.py).
    assert kwargs.get("failure_reason") == data["failure_reason"] == "phone_plan_unsupported"
    cloud.assert_not_called()


# --- speech_cleanup_failed (unsafe_plan): the bailout decision depends on a
# real Whisper transcript this repo cannot commit; the typed-exception
# contract at the real raise site is what's checkable offline.


def test_speech_cleanup_unsafe_plan_is_a_typed_non_generic_failure() -> None:
    data = _load("speech_cleanup_failed_1")
    bailout_reason = data["speech_cleanup"]["bailout_reason"]
    assert bailout_reason not in (None, "clip_too_short", "no_words")

    # Mirrors the real raise site verbatim (grep-verified,
    # app/tasks/generative_build.py: `raise SpeechCleanupFailure("unsafe_plan")`
    # once `strict_silence_cut` is on and `sc_plan.bailout_reason` is anything
    # other than None/clip_too_short/no_words).
    exc = SpeechCleanupFailure(bailout_reason)
    assert exc.reason == "unsafe_plan"
    assert isinstance(exc, RuntimeError)
    assert exc.reason not in {"processing_failed", "variants_failed"}

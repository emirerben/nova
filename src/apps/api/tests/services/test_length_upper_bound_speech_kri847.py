"""KRI-847: the speech-montage nudge does not call a shorter result a shortfall of "at most N".

``plan_speech_montage`` tells the creator "came out about Xs, shorter than the ~Ns you asked
for" when a voice-pinned plan lands under half the target. For "at most 30 seconds" a short
result is fine, so that nudge must not fire; an exact 30 s still gets it (guard).
"""

# ruff: noqa: F811  (the imported `world` fixture is used by name in the tests below)
from __future__ import annotations

import contextlib

from app.services.speech_montage_planning import plan_speech_montage
from tests.services.test_phone_speech_montage_job import world  # noqa: F401  (the job fixture)
from tests.services.test_speech_montage_planning import _others, _plan, _speaker, _Stub


def _resolve(**kwargs):
    talk, words = _speaker("talk")
    stub = _Stub(_plan({"kind": "speech", "clip_ref": "c1", "quote": "never rush a good espresso"}))
    return plan_speech_montage(
        creator_request="Use my voice over the clips",
        candidates=[talk, *_others(2)],
        run_planner=stub,
        load_words=lambda _c: (words, "en"),
        target_duration_s=30,
        voice_media_ids=["talk"],
        **kwargs,
    )


def test_guard_an_exact_length_far_under_the_target_is_still_reported():
    resolution = _resolve()

    assert any("shorter than the ~30s" in a for a in resolution.adjustments)


def test_a_result_shorter_than_an_at_most_is_not_reported_as_a_shortfall():
    resolution = _resolve(target_is_ceiling=True)

    assert resolution.status == "ready"
    assert not any("you asked for" in a for a in resolution.adjustments)
    assert not any("shorter than" in a for a in resolution.adjustments)


# ------------------------------------------------ the job tells the planner when N is a ceiling


def _job_flag(world, monkeypatch, bound: str) -> bool:
    """Run the real phone speech-montage job with an approved 30 s length and return the
    ``target_is_ceiling`` it hands ``plan_speech_montage``."""
    import app.services.phone_speech_montage_job as job_module
    from app.services.creator_render_contract import CreatorRenderContractError
    from app.services.length_policy_state import stamp_length_policy
    from tests.services.test_phone_speech_montage_job import (
        JOB_ID,
        _contract,
        _plan,
        _words,
    )

    contract = _contract(world, audio_source_ids=("talk",), original_audio="require", duration_s=30)
    stamped = stamp_length_policy(
        world.snapshot, {}, contract, mode="log_only", duration_bound=bound
    )
    world.snapshot.update(stamped)
    world.job.assembly_plan.update(stamped)
    seen: dict = {}
    real = job_module.plan_speech_montage

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(job_module, "plan_speech_montage", spy)
    # The route builds exactly N; whether it then pins or declines is not this ticket's concern.
    with contextlib.suppress(CreatorRenderContractError):
        job_module.run_phone_speech_montage_job(
            JOB_ID,
            world.snapshot,
            world.candidates,
            ownership_epoch=3,
            run_planner=lambda _input: _plan(
                {"kind": "speech", "clip_ref": "c1", "quote": "never rush a good espresso"}
            ),
            load_words=lambda _c: (_words(), "en"),
        )
    return seen["target_is_ceiling"]


def test_the_job_passes_an_approved_ceiling_to_the_planner(world, monkeypatch):
    assert _job_flag(world, monkeypatch, "at_most") is True


def test_guard_the_job_passes_an_exact_length_as_not_a_ceiling(world, monkeypatch):
    assert _job_flag(world, monkeypatch, "exact") is False

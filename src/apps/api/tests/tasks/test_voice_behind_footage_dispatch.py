"""KRI-479: the phone dispatcher sends a stamped voice-behind-footage plan to the composer.

Through the REAL dispatcher fork (`_run_generative_job_impl`); only the render entries are
replaced by recorders. Failure modes written first:

* the stamped plan still goes to the speech-excerpt lane (the lane that re-decides what the
  approved plan already decided -- the KRI-469 bug);
* the flip leaks to an UNSTAMPED twin (legacy behaviour must be unchanged);
* the shadow comparison reports a `route_mismatch` for a case the dispatcher handles itself;
* a plan that merely mentions a voice (`excerpts`, or no `voice_mode`) is rerouted.
"""

from __future__ import annotations

import pytest

from tests.tasks.test_route_shadow_dispatch import Harness, _job, _strategy

VOICE_STRATEGY = {
    "audio_strategy": "original_audio",
    "montage_audio": {"preserve_source_audio": True, "source_media_ids": ["c0"]},
    "voice_mode": "continuous",
}


def _harness(monkeypatch, *, stamped=True, **strategy) -> Harness:  # noqa: ANN001, ANN003
    job = _job(
        platform="phone", strategy=_strategy(**{**VOICE_STRATEGY, **strategy}), stamped=stamped
    )
    h = Harness(monkeypatch, job)
    h.voice = h._recorder("voice_behind_footage", result=True)
    monkeypatch.setattr(
        "app.services.phone_speech_montage_job.run_phone_voice_behind_footage_job", h.voice
    )
    h.speech.side_effect = lambda *_a, **_k: h.calls.append("speech") or True
    return h


def test_a_stamped_voice_plan_is_composed_not_sent_to_the_speech_lane(monkeypatch) -> None:
    h = _harness(monkeypatch)
    h.run()
    assert h.calls == ["voice_behind_footage"]
    assert h.mismatches == [], "the shadow label must agree with the resolver for a flipped case"
    _args, kwargs = h.voice.call_args
    assert kwargs["ownership_epoch"] is None


def test_the_unstamped_twin_keeps_the_legacy_speech_lane(monkeypatch) -> None:
    h = _harness(monkeypatch, stamped=False)
    h.run()
    assert h.calls == ["speech"]
    assert h.mismatches == []


@pytest.mark.parametrize("voice_mode", ["excerpts", None])
def test_a_stamped_plan_without_a_continuous_voice_still_takes_the_speech_lane(
    monkeypatch, voice_mode
) -> None:
    h = _harness(monkeypatch, voice_mode=voice_mode)
    h.run()
    assert h.calls == ["speech"]
    assert h.mismatches == []


def test_several_named_voices_never_pick_one_for_the_creator(monkeypatch) -> None:
    h = _harness(
        monkeypatch,
        montage_audio={"preserve_source_audio": True, "source_media_ids": ["c0", "c1"]},
    )
    h.run()
    assert h.calls == ["speech"]


def test_a_resolver_fault_on_a_stamped_voice_plan_is_a_retryable_typed_decline(monkeypatch) -> None:
    h = _harness(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("resolver down")

    monkeypatch.setattr("app.services.render_route.resolve_route", boom)
    failures = []
    monkeypatch.setattr(
        "app.tasks.generative_build._fail_job", lambda *a, **k: failures.append((a, k)) or True
    )
    monkeypatch.setattr("app.tasks.generative_build.mark_failed_phase", lambda *_a: None)
    h.run()
    assert h.calls == [], "never the speech lane, which would re-decide the approved plan"
    decline = failures[0][1]["decline"]
    assert (decline["decline_reason"], decline["field_path"]) == ("evidence_missing", "voice_mode")


def test_a_resolver_fault_on_any_other_plan_keeps_its_legacy_lane(monkeypatch) -> None:
    h = _harness(monkeypatch, voice_mode="excerpts")

    def boom(*_a, **_k):
        raise RuntimeError("resolver down")

    monkeypatch.setattr("app.services.render_route.resolve_route", boom)
    h.run()
    assert h.calls == ["speech"]


def test_the_composer_runner_returning_false_is_a_typed_decline_not_a_silent_reroute(
    monkeypatch,
) -> None:
    h = _harness(monkeypatch)
    h.voice.side_effect = lambda *_a, **_k: h.calls.append("voice_behind_footage") and False
    failures = []
    monkeypatch.setattr(
        "app.tasks.generative_build._fail_job", lambda *a, **k: failures.append((a, k)) or True
    )
    monkeypatch.setattr("app.tasks.generative_build.mark_failed_phase", lambda *_a: None)
    h.run()
    assert h.calls == ["voice_behind_footage"]  # never falls through to speech or unified
    assert failures and failures[0][1]["decline"]["decline_reason"] == "capability_unavailable"

"""KRI-7 — the server half of AI zoom placement: agent proposes, server disposes."""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.camera_emphasis import (
    CameraEmphasisCandidate,
    CameraEmphasisInput,
    CameraEmphasisOutput,
    RawCameraEmphasis,
)
from app.services.camera_emphasis import (
    build_camera_candidates,
    materialize_camera_emphasis,
    max_emphases_for,
    plan_camera_emphasis,
)

CUES: list[dict[str, Any]] = [
    {"text": "so anyway", "start_s": 0.0, "end_s": 1.0},
    {"text": "this one costs ten times less", "start_s": 1.2, "end_s": 4.0},
    {"text": "um", "start_s": 4.1, "end_s": 4.2},
    {"text": "and that is the whole trick", "start_s": 6.0, "end_s": 9.0},
    {"text": "thanks for watching", "start_s": 20.0, "end_s": 22.0},
]


def pick(index: int, style: str = "zoom_in", strength: str = "standard") -> RawCameraEmphasis:
    return RawCameraEmphasis(candidate_index=index, style=style, strength=strength)


# ── candidates ──────────────────────────────────────────────────────────────


def test_candidates_skip_blanks_and_twitch_length_phrases() -> None:
    candidates = build_camera_candidates(CUES, duration_s=30.0)
    assert [c["text"] for c in candidates] == [
        "so anyway",
        "this one costs ten times less",
        "and that is the whole trick",
        "thanks for watching",
    ]
    assert [c["index"] for c in candidates] == [0, 1, 2, 3]


def test_preset_picks_are_offered_as_a_hint_not_an_order() -> None:
    candidates = build_camera_candidates(
        CUES,
        preset_intents=[{"start_s": 2.0, "role": "list_item"}],
        duration_s=30.0,
    )
    hinted = [c for c in candidates if c["preset_pick"]]
    assert [c["text"] for c in hinted] == ["this one costs ten times less"]
    assert hinted[0]["role"] == "list_item"


def test_candidates_are_clamped_to_the_clip_duration() -> None:
    candidates = build_camera_candidates(
        [{"text": "runs past the end", "start_s": 4.0, "end_s": 99.0}], duration_s=5.0
    )
    assert candidates[0]["end_s"] == 5.0


# ── disposal ────────────────────────────────────────────────────────────────


def test_zoom_in_holds_across_its_phrase_and_pulse_stays_short() -> None:
    candidates = build_camera_candidates(CUES, duration_s=30.0)
    intents = materialize_camera_emphasis(
        [pick(1), pick(3, style="pulse")], candidates=candidates, duration_s=30.0
    )
    assert [(i["start_s"], i["end_s"], i["easing"]) for i in intents] == [
        (1.2, 4.0, "ease_in_hold"),  # the whole phrase
        (20.0, 21.2, "sine_pulse"),  # one accent, not the whole phrase
    ]


def test_a_hold_never_outstays_the_emphasis_ceiling() -> None:
    candidates = build_camera_candidates(
        [{"text": "a very long sentence indeed", "start_s": 0.0, "end_s": 12.0}], duration_s=30.0
    )
    (intent,) = materialize_camera_emphasis([pick(0)], candidates=candidates, duration_s=30.0)
    assert intent["end_s"] == 4.0


def test_an_index_the_caller_never_offered_is_dropped() -> None:
    candidates = build_camera_candidates(CUES, duration_s=30.0)
    assert materialize_camera_emphasis([pick(99)], candidates=candidates, duration_s=30.0) == []


def test_back_to_back_emphases_are_spaced_out() -> None:
    candidates = build_camera_candidates(
        [
            {"text": "first", "start_s": 0.0, "end_s": 1.0},
            {"text": "immediately after", "start_s": 1.1, "end_s": 2.5},
        ],
        duration_s=30.0,
    )
    intents = materialize_camera_emphasis(
        [pick(0), pick(1)], candidates=candidates, duration_s=30.0
    )
    assert [i["start_s"] for i in intents] == [0.0]


def test_only_one_moment_may_be_the_strongest() -> None:
    candidates = build_camera_candidates(CUES, duration_s=60.0)
    intents = materialize_camera_emphasis(
        [pick(1, strength="strong"), pick(3, strength="strong")],
        candidates=candidates,
        duration_s=60.0,
    )
    assert [i["intensity"] for i in intents] == [0.08, 0.06]


def test_count_scales_with_duration() -> None:
    assert max_emphases_for(8.0) == 1
    assert max_emphases_for(25.0) == 2
    assert max_emphases_for(300.0) == 4
    assert max_emphases_for(0.0) == 0
    candidates = build_camera_candidates(CUES, duration_s=25.0)
    intents = materialize_camera_emphasis(
        [pick(0), pick(1), pick(2), pick(3)], candidates=candidates, duration_s=25.0
    )
    assert len(intents) == 2


# ── planning, end to end ────────────────────────────────────────────────────


PRESET = [{"event_id": "preset-1", "start_s": 1.2, "end_s": 2.4, "role": "list_item"}]


def test_kill_switch_falls_back_to_the_preset_picks(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "camera_emphasis_ai_enabled", False)
    assert (
        plan_camera_emphasis(job_id="job", cues=CUES, preset_intents=PRESET, duration_s=30.0)
        == PRESET
    )


def test_a_failing_agent_never_costs_the_render_its_camera_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "camera_emphasis_ai_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())

    class Boom:
        def __init__(self, _client: object) -> None:
            pass

        def run(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("gemini is having a day")

    monkeypatch.setattr("app.agents.camera_emphasis.CameraEmphasisAgent", Boom)
    assert (
        plan_camera_emphasis(job_id="job", cues=CUES, preset_intents=PRESET, duration_s=30.0)
        == PRESET
    )


def test_agent_picks_replace_the_preset_picks(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "camera_emphasis_ai_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    seen: dict[str, CameraEmphasisInput] = {}

    class Stub:
        def __init__(self, _client: object) -> None:
            pass

        def run(self, payload: CameraEmphasisInput, **_kwargs: object) -> CameraEmphasisOutput:
            seen["input"] = payload
            return CameraEmphasisOutput(emphases=[pick(2, strength="strong")])

    monkeypatch.setattr("app.agents.camera_emphasis.CameraEmphasisAgent", Stub)
    intents = plan_camera_emphasis(job_id="job", cues=CUES, preset_intents=PRESET, duration_s=30.0)
    assert [(i["start_s"], i["end_s"], i["easing"]) for i in intents] == [
        (6.0, 9.0, "ease_in_hold")
    ]
    # The agent is told what the preset wanted and how many effects survive.
    payload = seen["input"]
    assert payload.max_effects == 3
    assert [c.preset_pick for c in payload.candidates] == [False, True, False, False]
    assert isinstance(payload.candidates[0], CameraEmphasisCandidate)


def test_an_empty_answer_is_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Footage with no standout moment ships with no zoom — not with the preset's."""
    from app.config import settings

    monkeypatch.setattr(settings, "camera_emphasis_ai_enabled", True)
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())

    class Empty:
        def __init__(self, _client: object) -> None:
            pass

        def run(self, *_args: object, **_kwargs: object) -> CameraEmphasisOutput:
            return CameraEmphasisOutput(emphases=[])

    monkeypatch.setattr("app.agents.camera_emphasis.CameraEmphasisAgent", Empty)
    assert (
        plan_camera_emphasis(job_id="job", cues=CUES, preset_intents=PRESET, duration_s=30.0) == []
    )

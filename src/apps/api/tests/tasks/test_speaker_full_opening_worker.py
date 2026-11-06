"""Qendresa item 16 (S70, S77, S79): she starts full screen, then drops into the corner -- through
the REAL worker (approved strategy -> the item's ready pool -> layout -> the phone recipe)."""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)
from tests.tasks.test_speaker_background_on_the_word import (
    GREEN3_WORDS,
    ON_THE_WORD_BACKGROUND,
    _green3_job,
)

ASKED = {
    **ON_THE_WORD_BACKGROUND,
    "speaker_frame": "small_bottom_right",
    "full_screen_opening": True,
}


def _variant(job):
    plan = job.assembly_plan or {}
    return next(v for v in plan["variants"] if v.get("variant_id") == "subtitled")


def _run(monkeypatch, background, *, enabled=True, protocol=41, token=True):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", enabled)
    job = _green3_job(monkeypatch, background)
    features = [f for f in settings.phone_render_verified_features if f != "speakerFrameWindows"]
    if token:
        features.append("speakerFrameWindows")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    if protocol is not None:
        job.all_candidates["creator_client_protocol"] = protocol
    gb._run_generative_job(str(job.id))
    return job


def _at_a_word_gap(x):
    return not any(s + 1e-3 < x < e - 1e-3 for _t, s, e in GREEN3_WORDS)


def test_she_starts_full_screen_then_the_corner(monkeypatch):
    job = _run(monkeypatch, ASKED)
    recipe = _recipe(job)
    layer = recipe.speaker_background
    assert layer is not None and layer.windows
    # Waits on the compile support in phone_subtitled_plan._compile_background (worker B).
    spans = layer.full_screen_windows
    assert spans is not None and len(spans) == 1 and spans[0].start == 0.0
    x = spans[0].end
    assert 2.0 <= x <= 8.0 and _at_a_word_gap(x)
    assert layer.windows[0].start == pytest.approx(x, abs=1e-3)
    assert all(w.start >= x - 1e-3 for w in layer.windows)
    assert layer.speaker_frame is not None  # the corner stays for the rest of the edit
    receipt = _variant(job)["phone_lane_receipt"]
    assert receipt["background"]["full_screen_opening_s"] == pytest.approx(x, abs=0.01)
    assert not any(d.get("part") == "full_screen_opening" for d in receipt["dropped"])


def test_an_old_build_gets_todays_corner_from_zero_and_an_honest_note(monkeypatch):
    plain = _run(monkeypatch, {k: v for k, v in ASKED.items() if k != "full_screen_opening"})
    old = _run(monkeypatch, ASKED, protocol=35)
    layer = _recipe(old).speaker_background
    assert layer is not None and not layer.full_screen_windows
    assert layer.windows[0].start == 0.0
    assert [(w.asset_id, w.start, w.end) for w in layer.windows] == [
        (w.asset_id, w.start, w.end) for w in _recipe(plain).speaker_background.windows
    ]
    receipt = _variant(old)["phone_lane_receipt"]
    assert "full_screen_opening_s" not in receipt["background"]
    assert {"lane": "background", "part": "full_screen_opening", "reason": "app_too_old"} in (
        receipt["dropped"]
    )


def test_token_not_verified_says_unavailable(monkeypatch):
    job = _run(monkeypatch, ASKED, token=False)
    layer = _recipe(job).speaker_background
    assert layer is not None and not layer.full_screen_windows
    dropped = _variant(job)["phone_lane_receipt"]["dropped"]
    assert {"lane": "background", "part": "full_screen_opening", "reason": "unavailable"} in dropped


def test_flag_off_recipe_and_receipt_are_byte_identical(monkeypatch):
    plain = _run(
        monkeypatch,
        {k: v for k, v in ASKED.items() if k != "full_screen_opening"},
        enabled=False,
    )
    asked = _run(monkeypatch, ASKED, enabled=False)
    assert json.dumps(_recipe(asked).model_dump(mode="json"), sort_keys=True) == json.dumps(
        _recipe(plain).model_dump(mode="json"), sort_keys=True
    )
    assert _variant(asked)["phone_lane_receipt"] == _variant(plain)["phone_lane_receipt"]


def test_the_request_carries_the_opening_only_while_the_flag_is_on(monkeypatch):
    candidates = {"creator_strategy": {"background_media": {**ASKED, "full_screen_opening_s": 6.0}}}
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", True)
    request = gb._strategy_background_request(candidates)
    assert request["full_screen_opening"] is True and request["full_screen_opening_s"] == 6.0
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", False)
    off = gb._strategy_background_request(candidates)
    assert "full_screen_opening" not in off and "full_screen_opening_s" not in off

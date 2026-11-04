"""Qendresa item 18 (F2, F12-F14): the recordings behind her are aimed at their readable text
through the REAL worker (approved strategy -> ready pool -> bound pictures that carry the free
text measure -> compile -> the phone recipe)."""

from __future__ import annotations

import json

from app.config import settings
from app.services.background_text_focus import TextColumn, focus_for_text_column
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)
from tests.tasks.test_speaker_background_on_the_word import (
    ON_THE_WORD_BACKGROUND,
    _green3_job,
)

COLUMN = TextColumn(
    left=0.18, right=0.60, line_pitch=26 / 1080, source_width=1920, source_height=1080
)
AUTO = focus_for_text_column(COLUMN, source_w=1920, source_h=1080, canvas_w=1080, canvas_h=1920)
ASKED = {**ON_THE_WORD_BACKGROUND, "speaker_frame": "small_bottom_right"}


def _variant(job):
    plan = job.assembly_plan or {}
    return next(v for v in plan["variants"] if v.get("variant_id") == "subtitled")


def _run(
    monkeypatch,
    background,
    *,
    enabled=True,
    protocol=39,
    token=True,
    column: TextColumn | None = COLUMN,
):
    import app.services.phone_visuals as phone_visuals_mod
    from app.services.phone_sources import PhoneVisualBinding

    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)
    monkeypatch.setattr(settings, "background_focus_enabled", enabled)
    job = _green3_job(monkeypatch, background)
    plain_bind = phone_visuals_mod.bind_phone_visual_assets

    def bind(open_, *, job_id, pins):
        out = []
        for b in plain_bind(open_, job_id=job_id, pins=pins):
            fields = b.model_dump()
            out.append(PhoneVisualBinding(**fields, text_column=column))
        return tuple(out)

    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind)
    features = [f for f in settings.phone_render_verified_features if f != "backgroundFocus"]
    if token:
        features.append("backgroundFocus")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    if protocol is not None:
        job.all_candidates["creator_client_protocol"] = protocol
    gb._run_generative_job(str(job.id))
    return job


def _dump(job) -> str:
    return json.dumps(_recipe(job).model_dump(mode="json"), sort_keys=True)


def _foci(job):
    layer = _recipe(job).speaker_background
    assert layer is not None and layer.windows
    return [w.focus for w in layer.windows]


def test_every_window_is_aimed_at_the_measured_text(monkeypatch):
    assert AUTO is not None
    job = _run(monkeypatch, ASKED)
    foci = _foci(job)
    assert all(f is not None for f in foci)
    assert all((f.x, f.y, f.zoom) == (AUTO.x, AUTO.y, AUTO.zoom) for f in foci)
    receipt = _variant(job)["phone_lane_receipt"]
    assert "focus" not in receipt["background"]  # nothing named by the creator
    assert "backgroundFocus" in _recipe(job).required_capabilities


def test_a_named_side_wins_for_x_and_zoom_still_comes_from_the_measure(monkeypatch):
    job = _run(monkeypatch, {**ASKED, "focus": "left"})
    foci = _foci(job)
    assert all(f is not None and f.x == 0.0 and f.zoom == AUTO.zoom for f in foci)
    assert _variant(job)["phone_lane_receipt"]["background"]["focus"] == "left"


def test_a_named_zoom_wins_for_zoom_and_x_comes_from_the_measure(monkeypatch):
    job = _run(monkeypatch, {**ASKED, "focus_zoom": 1.2})
    foci = _foci(job)
    assert all(f is not None and f.zoom == 1.2 and f.x == AUTO.x for f in foci)
    assert _variant(job)["phone_lane_receipt"]["background"]["focus_zoom"] == 1.2


def test_the_request_carries_focus_only_while_the_flag_is_on(monkeypatch):
    candidates = {
        "creator_strategy": {"background_media": {**ASKED, "focus": "right", "focus_zoom": 2.0}}
    }
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    request = gb._strategy_background_request(candidates)
    assert request["focus"] == "right" and request["focus_zoom"] == 2.0
    bare = gb._strategy_background_request({"creator_strategy": {"background_media": ASKED}})
    assert "focus" not in bare and "focus_zoom" not in bare
    monkeypatch.setattr(settings, "background_focus_enabled", False)
    off = gb._strategy_background_request(candidates)
    assert "focus" not in off and "focus_zoom" not in off


def test_an_old_build_gets_no_focus_anywhere(monkeypatch):
    job = _run(monkeypatch, {**ASKED, "focus": "left"}, protocol=38)
    assert all(f is None for f in _foci(job))
    assert "focus" not in _dump(job)
    assert "backgroundFocus" not in _recipe(job).required_capabilities


def test_no_stamp_gets_no_focus(monkeypatch):
    job = _run(monkeypatch, ASKED, protocol=None)
    assert all(f is None for f in _foci(job))


def test_token_not_verified_gets_no_focus(monkeypatch):
    job = _run(monkeypatch, ASKED, token=False)
    assert all(f is None for f in _foci(job))


def test_flag_off_recipe_is_byte_identical(monkeypatch):
    plain = _run(monkeypatch, ASKED, enabled=False, column=None)
    measured = _run(monkeypatch, {**ASKED, "focus": "left", "focus_zoom": 2.0}, enabled=False)
    assert "focus" not in _dump(plain)
    assert _dump(measured) == _dump(plain)
    assert _variant(measured)["phone_lane_receipt"] == _variant(plain)["phone_lane_receipt"]


# ------------------------------------------------------------------ the prompt


def test_the_prompt_teaches_focus_only_when_taught(monkeypatch):
    from app.agents import main_creator
    from app.services import creator_capabilities as capabilities
    from tests.kria.test_speaker_background_chat import _manifest, _open_windows, _render_prompt

    section = main_creator._BACKGROUND_FOCUS_PROMPT_SECTION
    _open_windows(monkeypatch)
    monkeypatch.setattr(settings, "background_focus_enabled", True)
    monkeypatch.setattr(capabilities, "background_focus_open", lambda _m: True, raising=False)
    on = _render_prompt(_manifest())
    assert on.count(section) == 1
    assert "background_media.focus_zoom" in section and "speaker_frame" in section

    monkeypatch.setattr(capabilities, "background_focus_open", lambda _m: False, raising=False)
    assert section not in _render_prompt(_manifest())

    monkeypatch.setattr(capabilities, "background_focus_open", lambda _m: True, raising=False)
    monkeypatch.setattr(settings, "background_focus_enabled", False)
    off = _render_prompt(_manifest())
    assert section not in off
    assert on.replace("\n" + section, "") == off

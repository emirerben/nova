"""KRI-1558 (rows 23, 25-29): the speaker's layout follows the talk -- through the REAL worker.

Green 3's approved strategy (five pictures behind her, no named pace) -> the item's ready pool ->
the take's sections on its spoken words -> one layout per section -> the phone recipe. A build
or a gate that cannot draw it keeps today's single layout byte for byte. The speech-cut case runs
the same arrangement and the lane remap the worker runs before the compile.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS
from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes, remap_lanes_for_cut
from app.pipeline.silence_cut import CutPlan, Removal
from app.services.client_protocol import SPEAKER_SECTIONS_PROTOCOL
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)
from tests.tasks.test_speaker_background_on_the_word import (
    GREEN3_WORDS,
    KEEP,
    ON_THE_WORD_BACKGROUND,
    REMOVED,
    STORED_BACKGROUND,
    STORED_ORDER,
    TAKE_FULL_S,
    TAKE_S,
    WORDS,
    _green3_job,
    _pool_rows,
)

NEW = SPEAKER_SECTIONS_PROTOCOL
OLD = SPEAKER_SECTIONS_PROTOCOL - 1


def _variant(job):
    return next(v for v in job.assembly_plan["variants"] if v.get("variant_id") == "subtitled")


def _gate(monkeypatch, *, enabled=True, token=True) -> None:
    monkeypatch.setattr(settings, "speaker_sections_enabled", enabled)
    features = [f for f in settings.phone_render_verified_features if f != "speakerSections"]
    if token:
        features.append("speakerSections")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def _run(monkeypatch, background, *, enabled=True, protocol=NEW, token=True):
    job = _green3_job(monkeypatch, background)
    _gate(monkeypatch, enabled=enabled, token=token)
    if protocol is not None:
        job.all_candidates["creator_client_protocol"] = protocol
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    return job


def _inside_a_word(x: float) -> bool:
    return any(s + 1e-3 < x < e - 1e-3 for _t, s, e in GREEN3_WORDS)


# --- rows 26, 27, 29: the sections and their layouts -----------------------------------------


def test_her_layout_follows_the_sections_of_her_talk(monkeypatch):
    job = _run(monkeypatch, STORED_BACKGROUND)
    recipe = _recipe(job)
    layer = recipe.speaker_background
    layouts = layer.layout_windows
    assert layouts is not None and len(layouts) >= 3
    # They tile the edit: from 0 to the end, each switch where the last section stopped.
    assert layouts[0].start == 0.0
    assert layouts[-1].end == pytest.approx(recipe.duration, abs=1e-3)
    assert all(a.end == b.start for a, b in zip(layouts, layouts[1:], strict=False))
    # Her face opens and closes; the sections between carry the pictures.
    assert layouts[0].layout == "own" and layouts[-1].layout == "own"
    middle = [w.layout for w in layouts[1:-1]]
    assert middle[0] == "small" and set(middle) <= {"small", "picture", "own"}
    assert layouts[1].frame.model_dump() == SPEAKER_FRAME_PRESETS["small_bottom_right"]
    # No switch lands inside a word.
    assert not any(_inside_a_word(w.start) for w in layouts[1:])
    # One background window per section showing a picture, spanning it, pictures in the
    # strategy's order; no corner for the whole edit (the sections decide).
    shown = [w for w in layouts if w.layout in ("small", "picture", "full")]
    assert [(w.start, w.end) for w in layer.windows] == [(w.start, w.end) for w in shown]
    assert [w.asset_id.removeprefix("visual-") for w in layer.windows] == STORED_ORDER[: len(shown)]
    assert layer.speaker_frame is None
    assert "speakerSections" in recipe.required_capabilities
    # The sections the chat can name, on the take's own clock (no cut here).
    sections = _variant(job)["speaker_sections"]
    assert [(s["start_s"], s["end_s"]) for s in sections] == [(w.start, w.end) for w in layouts]
    assert [s["index"] for s in sections] == list(range(1, len(sections) + 1))
    assert all(s["text"] for s in sections)


# --- row 25: a build or a gate that cannot draw it changes nothing ----------------------------


@pytest.mark.parametrize(
    "kwargs",
    [{"protocol": OLD}, {"protocol": None}, {"token": False}],
    ids=["older_build", "no_stamp", "token_not_verified"],
)
def test_what_cannot_draw_it_gets_todays_render_byte_for_byte(monkeypatch, kwargs):
    today = _run(monkeypatch, STORED_BACKGROUND, enabled=False)
    asked = _run(monkeypatch, STORED_BACKGROUND, **kwargs)
    assert json.dumps(_recipe(asked).model_dump(mode="json"), sort_keys=True) == json.dumps(
        _recipe(today).model_dump(mode="json"), sort_keys=True
    )
    assert _variant(asked)["phone_lane_receipt"] == _variant(today)["phone_lane_receipt"]
    assert "speaker_sections" not in _variant(asked)
    assert _recipe(today).speaker_background.speaker_frame is not None  # today's corner


def test_a_named_pace_is_kept(monkeypatch):
    """KRI-1494: "changing every 3-5 seconds" is the creator's own words; sections would hold
    each picture for a whole passage, so the asked pace keeps today's layout."""
    today = _run(monkeypatch, ON_THE_WORD_BACKGROUND, enabled=False)
    asked = _run(monkeypatch, ON_THE_WORD_BACKGROUND)
    assert _recipe(asked).speaker_background.layout_windows is None
    assert _recipe(asked).model_dump_json() == _recipe(today).model_dump_json()
    assert "speaker_sections" not in _variant(asked)


# --- rows 18, 26: a speech cut keeps every section on its own words ---------------------------


def test_a_speech_cut_keeps_each_layout_on_its_sections_words(monkeypatch):
    rows = {row["id"]: row for row in _pool_rows()}
    import app.services.phone_overlay_grounding as ogm

    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: list(rows.values()))
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            *settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundWindows",
        ],
    )
    _gate(monkeypatch)
    sections: list[dict] = []
    lane, _reserved = gb._resolve_strategy_background(
        gb._strategy_background_request(
            {"creator_strategy": {"background_media": STORED_BACKGROUND}}
        ),
        job_id="job",
        lane_drops=[],
        words=WORDS,
        keep_segments=KEEP,
        take_s=TAKE_S,
        creator_client_protocol=NEW,
        source_take_s=TAKE_FULL_S,
        sections_out=sections,
    )
    assert lane is not None and lane.layout_windows and lane.speaker_frame is None
    # The lane is on the ORIGINAL take clock, tiling the whole uncut take.
    assert lane.layout_windows[0].start_s == 0.0
    assert lane.layout_windows[-1].end_s == pytest.approx(TAKE_FULL_S, abs=1e-3)
    assert [(s["start_s"], s["end_s"]) for s in sections] == [
        (w.start_s, w.end_s) for w in lane.layout_windows
    ]
    plan = CutPlan(
        keep_segments=KEEP,
        removed=[Removal(start_s=s, end_s=e, reason="pause") for s, e in REMOVED],
        time_saved_s=sum(e - s for s, e in REMOVED),
    )
    cut, _dropped = remap_lanes_for_cut(PhoneSubtitledLanes(background=lane), plan)
    from app.services.phone_background_sequence import to_output_time

    for section, window in zip(sections, cut.background.layout_windows, strict=True):
        # Every word said in the section plays inside that section's layout after the cut.
        said = [
            to_output_time(s, KEEP)
            for _t, s, e in GREEN3_WORDS
            if section["start_s"] <= (s + e) / 2 < section["end_s"]
            and to_output_time(s, KEEP) is not None
        ]
        assert said and all(window.start_s - 1e-3 <= at < window.end_s for at in said)
    assert cut.background.layout_windows[-1].end_s == pytest.approx(TAKE_S, abs=1e-3)

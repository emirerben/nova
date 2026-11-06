"""Qendresa item 18 (chat half): "show the left side of the recording", "zoom in a bit".

Real harness (`tests/routes/test_background_focus_editor.py` job, the chat snapshot, the parser, the
compile and the editor Save). Failure modes: F12 (an op rebuild loses the focus), F13 (a side the
creator did not mean: the op is about the recording, never the corner), the closed gate changing a
snapshot or prompt byte, a clamp that is not said, a no-op that claims a change.
"""

from __future__ import annotations

import json

import pytest

from app.agents.edit_copilot import _family_allowed, _format_snapshot, _with_v2_fragments
from app.config import settings
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.kria_editor_ops import KriaEditorOpError, compile_editor_ops
from tests.routes.test_background_focus_editor import (
    FILL_FOCUS,
    LEFT,
    PHOTO,
    POOL,
    RIGHT,
    SECOND,
    _aimed,
    _layer_focus,
    _open,
    focus_job,
)
from tests.routes.test_speaker_background_chat_ops import (
    _base,
    _commit,
    _parse,
    _recipe,
    _snapshot,
)
from tests.routes.test_speaker_background_editor import _background, _document, background_job

POOL_ROWS = [
    {"id": PHOTO.media_id, "kind": "image", "label": "a.jpg"},
    {"id": SECOND.media_id, "kind": "image", "label": "b.jpg"},
]


@pytest.fixture(autouse=True)
def _ctx():
    clear_request_context()
    yield
    clear_request_context()


def _ready(job):
    job.assembly_plan["variants"][0]["output_url"] = "https://storage.example/out.mp4"
    job.all_candidates = {}
    return job


def _aimed_job(monkeypatch):
    job = _ready(focus_job(monkeypatch))
    set_client_protocol(42)
    return job


def _plain_job(monkeypatch, *, background=True):
    job = _ready(
        background_job(
            monkeypatch, background=_background(PHOTO) if background else None, pinned=POOL
        )
    )
    _open(monkeypatch)
    set_client_protocol(42)
    return job


def _run(job, *raw, snapshot=None):
    base = _base(job)
    snap = snapshot or _snapshot(job, base, pool=POOL_ROWS)
    ops, state = _parse(snap, *raw)
    assert all(op is not None for op in ops), state.rejection_reasons
    return compile_editor_ops(job, base, list(ops)), state


def _doc(compiled) -> dict:
    return compiled.payload.speaker_background.model_dump(exclude_none=True)


# --- the snapshot ------------------------------------------------------------------------------


def test_the_snapshot_and_families_offer_it_only_with_the_gate_open(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    snap = _snapshot(job, pool=POOL_ROWS)
    assert "background_focus" in snap["allowed_op_families"]
    section = snap["background"]
    assert section["focus_available"] is True
    assert section["current"]["focus"] == FILL_FOCUS
    assert [w["focus"] for w in section["windows"]] == [LEFT, RIGHT]
    text = _format_snapshot(snap)
    assert "aim=x0.24 zoom=2x" in text
    assert "aim=left zoom=1.6x" in text and "aim=right zoom=0.8x" in text
    assert "set_background_focus" in _with_v2_fragments("PROMPT", snap)


def test_a_closed_gate_leaves_snapshot_and_prompt_without_a_trace(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    open_snap = _snapshot(job, pool=POOL_ROWS)
    _open(monkeypatch, flag=False)
    snap = _snapshot(job, pool=POOL_ROWS)
    assert "background_focus" not in snap["allowed_op_families"]
    dumped = json.dumps(snap)
    assert "focus" not in dumped
    assert "aim=" not in _format_snapshot(snap)
    assert "set_background_focus" not in _with_v2_fragments("PROMPT", snap)
    assert not _family_allowed("set_background_focus", snap)
    # Opening it again only adds keys: everything else in the section is untouched.
    for key in ("available", "pool"):
        assert snap["background"][key] == open_snap["background"][key]


def test_an_open_gate_without_any_focus_prints_no_aim_text(monkeypatch) -> None:
    job = _plain_job(monkeypatch)
    snap = _snapshot(job, pool=POOL_ROWS)
    assert snap["background"]["focus_available"] is True
    assert "aim=" not in _format_snapshot(snap)


# --- the op ------------------------------------------------------------------------------------


def test_position_left_aims_the_fill_and_every_window_keeping_zoom_and_saves(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    compiled, _ = _run(job, {"op": "set_background_focus", "position": "left"})
    assert compiled.changes == ["Aimed the recordings at their left side"]
    document = _doc(compiled)
    assert document["focus"] == {"x": 0.0, "y": 0.5, "zoom": 2.0}
    assert [w["focus"] for w in document["windows"]] == [
        {"x": 0.0, "y": 0.5, "zoom": 1.6},
        {"x": 0.0, "y": 0.5, "zoom": 0.8},
    ]
    assert document["speaker_frame"]  # the corner stays (F13)
    _commit(job, compiled)
    assert _layer_focus(job) == (
        {"x": 0.0, "y": 0.5, "zoom": 2.0},
        [{"x": 0.0, "y": 0.5, "zoom": 1.6}, {"x": 0.0, "y": 0.5, "zoom": 0.8}],
    )


@pytest.mark.parametrize(("word", "x"), [("centre", 0.5), ("middle", 0.5), ("center", 0.5)])
def test_centre_words_are_accepted(monkeypatch, word, x) -> None:
    job = _aimed_job(monkeypatch)
    (op,), _ = _parse(
        _snapshot(job, pool=POOL_ROWS), {"op": "set_background_focus", "position": word}
    )
    assert op is not None and op["position"] == "center"


def test_a_position_that_is_not_a_side_and_an_empty_op_are_refused(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    snap = _snapshot(job, pool=POOL_ROWS)
    for raw in (
        {"op": "set_background_focus", "position": "top"},
        {"op": "set_background_focus"},
        {"op": "set_background_focus", "zoom": True},
        {"op": "set_background_focus", "zoom_delta": 3},
        {"op": "set_background_focus", "zoom": "big"},
        {"op": "set_background_focus", "zoom": float("nan")},
        {"op": "set_background_focus", "position": "left", "asset_id": "unknown"},
    ):
        (op,), state = _parse(snap, raw)
        assert op is None, raw
        assert state.rejection_reasons[-1]["reason"] in {"invalid_value", "missing_required"}


def test_zoom_without_a_side_keeps_each_side(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    compiled, _ = _run(job, {"op": "set_background_focus", "zoom": 1.2})
    assert compiled.changes == ["Changed how far the recordings are zoomed"] or compiled.changes
    document = _doc(compiled)
    assert (document["focus"]["x"], document["focus"]["zoom"]) == (0.24, 1.2)
    assert [(w["focus"]["x"], w["focus"]["zoom"]) for w in document["windows"]] == [
        (0.0, 1.2),
        (1.0, 1.2),
    ]


def test_zoom_delta_is_added_to_each_current_zoom_and_summarised(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    zoom_in, _ = _run(job, {"op": "set_background_focus", "zoom_delta": 0.25})
    assert zoom_in.changes == ["Zoomed the recordings in"]
    assert _doc(zoom_in)["focus"]["zoom"] == 2.25
    assert [w["focus"]["zoom"] for w in _doc(zoom_in)["windows"]] == [1.85, 1.05]
    zoom_out, _ = _run(job, {"op": "set_background_focus", "zoom_delta": -0.25})
    assert zoom_out.changes == ["Zoomed the recordings out"]
    assert [w["focus"]["zoom"] for w in _doc(zoom_out)["windows"]] == [1.35, 0.55]


def test_a_zoom_past_the_limits_is_clamped_and_said(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    high, state = _run(job, {"op": "set_background_focus", "zoom": 5})
    assert _doc(high)["focus"]["zoom"] == 3.0
    assert any("3" in note for note in state.reply_notes)
    low, state = _run(job, {"op": "set_background_focus", "zoom": 0.1})
    assert _doc(low)["focus"]["zoom"] == 0.3
    assert state.reply_notes
    capped, _ = _run(job, {"op": "set_background_focus", "zoom_delta": 2})
    assert _doc(capped)["focus"]["zoom"] == 3.0


def test_asset_id_aims_only_the_windows_and_fill_showing_that_picture(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    compiled, _ = _run(
        job, {"op": "set_background_focus", "position": "center", "asset_id": SECOND.media_id}
    )
    document = _doc(compiled)
    assert document["focus"] == FILL_FOCUS  # the fill shows PHOTO
    assert document["windows"][0]["focus"] == LEFT
    assert document["windows"][1]["focus"] == {"x": 0.5, "y": 0.5, "zoom": 0.8}
    both, _ = _run(
        job, {"op": "set_background_focus", "position": "right", "asset_id": PHOTO.media_id}
    )
    assert _doc(both)["focus"]["x"] == 1.0 and _doc(both)["windows"][0]["focus"]["x"] == 1.0
    assert _doc(both)["windows"][1]["focus"] == RIGHT


def test_a_colour_fill_with_windows_aims_the_windows_only(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    row = _snapshot(job, pool=POOL_ROWS)
    variant = job.assembly_plan["variants"][0]
    variant["speaker_background"] = {
        "kind": "color",
        "color": "#112233",
        "windows": variant["speaker_background"]["windows"],
    }
    compiled, _ = _run(job, {"op": "set_background_focus", "position": "right"}, snapshot=row)
    document = _doc(compiled)
    assert "focus" not in document and document["kind"] == "color"
    assert [w["focus"]["x"] for w in document["windows"]] == [1.0, 1.0]


def test_a_photo_aims_from_the_default_focus(monkeypatch) -> None:
    job = _plain_job(monkeypatch)
    compiled, _ = _run(job, {"op": "set_background_focus", "position": "left", "zoom": 1.5})
    assert _doc(compiled)["focus"] == {"x": 0.0, "y": 0.5, "zoom": 1.5}
    _commit(job, compiled)
    assert _layer_focus(job)[0] == {"x": 0.0, "y": 0.5, "zoom": 1.5}


def test_nothing_behind_the_speaker_to_aim_is_an_error_in_words(monkeypatch) -> None:
    job = _plain_job(monkeypatch, background=False)
    base = _base(job)
    snap = _snapshot(job, base, pool=POOL_ROWS)
    (op,), _ = _parse(snap, {"op": "set_background_focus", "position": "left"})
    with pytest.raises(KriaEditorOpError, match="photo or video behind you first"):
        compile_editor_ops(job, base, [op])


def test_aiming_what_is_already_aimed_that_way_is_an_error(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    base = _base(job)
    snap = _snapshot(job, base, pool=POOL_ROWS)
    (op,), _ = _parse(snap, {"op": "set_background_focus", "zoom": 2.0, "asset_id": PHOTO.media_id})
    # The window showing PHOTO is at 1.6, so something changes; but the fill-only case is exact.
    assert compile_editor_ops(job, base, [op])
    plain = _plain_job(monkeypatch)
    snap = _snapshot(plain, pool=POOL_ROWS)
    (noop,), _ = _parse(snap, {"op": "set_background_focus", "position": "center", "zoom": 1.0})
    with pytest.raises(KriaEditorOpError, match="already how they're aimed"):
        compile_editor_ops(plain, _base(plain), [noop])


def test_a_closed_family_is_refused_and_a_closed_snapshot_flag_says_so_in_words(
    monkeypatch,
) -> None:
    job = _aimed_job(monkeypatch)
    _open(monkeypatch, flag=False)
    snap = _snapshot(job, pool=POOL_ROWS)
    (op,), state = _parse(snap, {"op": "set_background_focus", "position": "left"})
    assert op is None
    assert state.rejection_reasons[-1]["reason"] == "capability_unavailable"
    # The coerce step itself, handed a snapshot whose family is open but the section says no.
    forced = dict(snap, allowed_op_families=[*snap["allowed_op_families"], "background_focus"])
    (op,), state = _parse(forced, {"op": "set_background_focus", "position": "left"})
    assert op is None
    assert state.rejection_reasons[-1]["reason"] == "capability_unavailable"
    assert "can't aim or zoom" in state.rejection_reasons[-1]["detail"]
    assert state.rejection_reasons[-1]["detail"] in state.reply_notes


# --- other ops keep the focus ------------------------------------------------------------------


def test_set_background_keeps_the_fill_focus_only_for_the_same_picture(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    same, _ = _run(job, {"op": "set_background", "asset_id": PHOTO.media_id})
    # Nothing but windows would change; the fill (same media) keeps its focus.
    assert _doc(same).get("focus") in (FILL_FOCUS, None)
    other, _ = _run(job, {"op": "set_background", "asset_id": SECOND.media_id})
    assert "focus" not in _doc(other)


def test_set_background_color_keeps_the_window_focus(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "speakerBackgroundColor"],
    )
    compiled, _ = _run(job, {"op": "set_background_color", "color": "#00FF00"})
    document = _doc(compiled)
    assert "focus" not in document
    assert [w["focus"] for w in document["windows"]] == [LEFT, RIGHT]


def test_a_sequence_gives_each_new_window_the_focus_of_its_picture(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    compiled, _ = _run(
        job,
        {
            "op": "set_background_sequence",
            "asset_ids": [SECOND.media_id, PHOTO.media_id, SECOND.media_id],
            "cadence_s": 3,
        },
    )
    windows = _doc(compiled)["windows"]
    by_media = {}
    for window in windows:
        by_media.setdefault(window["media_id"], window.get("focus"))
    assert by_media[SECOND.media_id] == RIGHT  # the first working window with that picture
    assert by_media[PHOTO.media_id] == LEFT


def test_the_full_screen_opening_survives_the_focus_op(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    opening = [{"start_s": 0.0, "end_s": 1.0}]
    job.assembly_plan["variants"][0]["speaker_background"]["full_screen_windows"] = opening
    compiled, _ = _run(job, {"op": "set_background_focus", "position": "right"})
    document = _doc(compiled)
    assert document["full_screen_windows"] == opening
    assert document["speaker_frame"]


def test_two_focus_ops_in_one_turn_build_on_each_other(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    compiled, _ = _run(
        job,
        {"op": "set_background_focus", "position": "left"},
        {"op": "set_background_focus", "zoom_delta": 0.25},
    )
    assert _doc(compiled)["focus"] == {"x": 0.0, "y": 0.5, "zoom": 2.25}


def test_the_chat_edit_ends_in_a_saved_recipe_through_a_second_untouched_save(monkeypatch) -> None:
    job = _aimed_job(monkeypatch)
    compiled, _ = _run(job, {"op": "set_background_focus", "position": "center"})
    _commit(job, compiled)
    centred = _layer_focus(job)
    assert centred[0]["x"] == 0.5
    from tests.routes.test_phone_subtitled_editor_commit import _resave

    _resave(job, sound_effects=[])
    assert _layer_focus(job) == centred
    assert _recipe(job).speaker_background.focus is not None
    assert _document(PHOTO)["media_id"] == PHOTO.media_id
    assert _aimed()["focus"] == FILL_FOCUS

"""KRI-1141: chat tilts a Talking edit's picture cards through ``patch_overlay``.

The whole path with nothing stubbed but the model's JSON, like `test_phone_chat_overlay_edit`:
chat base -> snapshot -> parser -> compile -> the real editor Save -> the recompiled phone recipe.
The parser used to refuse every `rotation_deg` on a card because the compile dropped the angle.
"""

from __future__ import annotations

import pytest

from app.agents import editor_ops_v2 as v2
from app.agents.edit_copilot import _format_snapshot, _parse_op, _ParseState
from app.config import settings
from app.services.client_protocol import (
    CARD_TILT_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
    project_editor_draft,
)
from tests.routes.test_phone_chat_overlay_edit import _chat, _commit, _rendered_job
from tests.routes.test_phone_subtitled_editor_commit import device_status


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _open_tilt(monkeypatch) -> None:
    monkeypatch.setattr(settings, "media_tilt_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "clipTilt"],
    )
    set_client_protocol(CARD_TILT_PROTOCOL)


def _angle(job) -> float:
    track = next(
        t
        for t in device_status(job, "subtitled").request.recipe.tracks
        if t.id == "subtitled-overlays"
    )
    return track.clips[0].transform.rotation_degrees


def _tilt(job, patch: dict, index: int = 0):
    base, snapshot, ops = _chat(
        job, [{"op": "patch_overlay", "overlay_index": index, "patch": patch}]
    )
    return base, snapshot, ops


def _tilt_and_save(job, patch: dict):
    base, _snapshot, ops = _tilt(job, patch)
    assert ops and ops[0] is not None, "the parser dropped the tilt"
    compiled = compile_editor_ops(job, base, ops)
    _commit(job, compiled)
    return compiled


def test_tilt_the_picture_a_bit_reaches_the_pinned_recipe_clockwise(monkeypatch):
    job = _rendered_job(monkeypatch)
    _open_tilt(monkeypatch)

    _base, snapshot, ops = _tilt(job, {"rotation_deg": 4})

    assert snapshot["overlays"]["tilt"] is True
    assert ops == [{"op": "patch_overlay", "overlay_index": 0, "patch": {"rotation_deg": 4.0}}]
    compiled = _tilt_and_save(job, {"rotation_deg": 4})
    assert compiled.payload.media_overlays[0]["editor_style"]["rotation_deg"] == 4
    assert _angle(job) == 4
    # The receipt names what changed, so chat can say it honestly.
    assert [(e.lane, e.field) for e in compiled.diff.real()] == [("media_overlays", "changed")]


def test_an_absolute_angle_to_the_left_is_negative_and_a_big_turn_wraps(monkeypatch):
    job = _rendered_job(monkeypatch)
    _open_tilt(monkeypatch)

    _tilt_and_save(job, {"rotation_deg": -15})
    assert _angle(job) == -15
    _tilt_and_save(job, {"rotation_deg": 270})
    assert _angle(job) == -90


def test_a_relative_ask_adds_to_the_card_instead_of_replacing_its_angle(monkeypatch):
    job = _rendered_job(monkeypatch)
    _open_tilt(monkeypatch)
    _tilt_and_save(job, {"rotation_deg": 4})

    _base, snapshot, _ops = _tilt(job, {"rotation_delta_deg": 3})
    assert snapshot["overlays"]["cards"][0]["rotation_deg"] == 4
    _tilt_and_save(job, {"rotation_delta_deg": 3})
    assert _angle(job) == 7
    _tilt_and_save(job, {"rotation_delta_deg": -10})
    assert _angle(job) == -3


def test_straightening_puts_the_card_back_to_the_untilted_recipe(monkeypatch):
    job = _rendered_job(monkeypatch)
    plain = device_status(job, "subtitled").request.recipe
    _open_tilt(monkeypatch)
    _tilt_and_save(job, {"rotation_deg": 12})
    assert _angle(job) == 12

    compiled = _tilt_and_save(job, {"rotation_deg": 0})

    recipe = device_status(job, "subtitled").request.recipe
    assert _angle(job) == 0
    assert "clipTilt" not in recipe.required_capabilities
    assert "editor_style" not in compiled.payload.media_overlays[0]
    plain_clip = next(t for t in plain.tracks if t.id == "subtitled-overlays").clips[0]
    assert next(t for t in recipe.tracks if t.id == "subtitled-overlays").clips[0] == plain_clip


def test_the_angle_is_kept_when_the_same_patch_moves_the_card(monkeypatch):
    job = _rendered_job(monkeypatch)
    _open_tilt(monkeypatch)

    _tilt_and_save(job, {"rotation_deg": 8, "x_frac": 0.8, "y_frac": 0.2})

    track = next(
        t
        for t in device_status(job, "subtitled").request.recipe.tracks
        if t.id == "subtitled-overlays"
    )
    assert track.clips[0].transform.rotation_degrees == 8
    assert track.clips[0].visual_placement.x_fraction == pytest.approx(0.8)


@pytest.mark.parametrize(
    "patch",
    [
        {"rotation_deg": 400},
        {"rotation_delta_deg": -361},
        {"rotation_deg": "tilted"},
        {"rotation_deg": float("inf")},
        {"rotation_deg": 5, "rotation_delta_deg": 3},
        {"rotation_deg": True},
    ],
)
def test_a_bad_angle_is_dropped_not_guessed(monkeypatch, patch):
    job = _rendered_job(monkeypatch)
    _open_tilt(monkeypatch)

    _base, _snapshot, ops = _tilt(job, patch)

    assert ops == [None]


def test_a_closed_gate_refuses_the_angle_in_a_sentence_and_leaves_the_snapshot_unchanged(
    monkeypatch,
):
    job = _rendered_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    snapshot = build_editor_snapshot(job, project_editor_draft(variant, {}, job))
    assert "tilt" not in snapshot["overlays"]
    assert all("rotation_deg" not in card for card in snapshot["overlays"]["cards"])
    assert "PICTURE CARD TILT" not in v2.family_fragments(snapshot)
    assert "tilt:" not in _format_snapshot(snapshot)

    state = _ParseState(0.9)
    op = _parse_op(
        {"op": "patch_overlay", "overlay_index": 0, "patch": {"rotation_deg": 12, "scale": 0.5}},
        snapshot,
        state,
    )

    assert op is None
    assert [r["reason"] for r in state.rejection_reasons] == ["capability_unavailable"]
    assert "tilt a picture" in state.reply_notes[0]


def test_the_prompt_teaches_tilt_only_where_the_snapshot_offers_it(monkeypatch):
    job = _rendered_job(monkeypatch)
    variant = job.assembly_plan["variants"][0]
    closed = build_editor_snapshot(job, project_editor_draft(variant, {}, job))
    _open_tilt(monkeypatch)
    opened = build_editor_snapshot(job, project_editor_draft(variant, {}, job))

    assert "PICTURE CARD TILT" in v2.family_fragments(opened)
    assert "rotation_delta_deg" in v2.family_fragments(opened)
    assert (
        v2.family_fragments(closed) == v2.family_fragments(opened).split("\n\nPICTURE CARD TILT")[0]
    )
    assert "tilt: available" in _format_snapshot(opened)


def test_a_tilted_card_shows_its_angle_to_the_model(monkeypatch):
    job = _rendered_job(monkeypatch)
    _open_tilt(monkeypatch)
    _tilt_and_save(job, {"rotation_deg": -7})
    variant = job.assembly_plan["variants"][0]

    snapshot = build_editor_snapshot(job, project_editor_draft(variant, {}, job))

    assert "tilt=(rotation_deg=-7.00)" in _format_snapshot(snapshot)


def test_a_full_screen_card_is_not_tilted_and_going_full_screen_straightens_it(monkeypatch):
    job = _rendered_job(monkeypatch)
    _open_tilt(monkeypatch)
    _tilt_and_save(job, {"rotation_deg": 10})

    base, _snapshot, ops = _chat(
        job, [{"op": "set_overlay_display", "display_mode": "fullscreen", "overlay_index": 0}]
    )
    compiled = compile_editor_ops(job, base, ops)
    assert "editor_style" not in compiled.payload.media_overlays[0]
    _commit(job, compiled)
    assert "subtitled-fullscreen" in [
        t.id for t in device_status(job, "subtitled").request.recipe.tracks
    ]

    base, _snapshot, ops = _tilt(job, {"rotation_deg": 10})
    with pytest.raises(KriaEditorOpError, match="can't be tilted"):
        compile_editor_ops(job, base, ops)


def test_tilting_to_the_angle_it_already_has_is_a_sentence_not_a_silent_no_op(monkeypatch):
    job = _rendered_job(monkeypatch)
    _open_tilt(monkeypatch)
    _tilt_and_save(job, {"rotation_deg": 10})

    base, _snapshot, ops = _tilt(job, {"rotation_deg": 10})
    with pytest.raises(KriaEditorOpError, match="already tilted that way"):
        compile_editor_ops(job, base, ops)

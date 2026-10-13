"""KRI-661: chat turns a Talking edit's picture cards on top of the speaker and back.

The whole path with nothing stubbed but the model's JSON: chat base (derived from the
pinned recipe) -> snapshot -> parser -> compile -> the real editor Save -> the recompiled
phone recipe. This is the "put my photos on top of the video" ask that used to end in
"that kind of edit isn't available for this draft yet".
"""

from __future__ import annotations

import pytest

from app.agents.edit_copilot import _parse_op, _ParseState
from app.config import settings
from app.routes import generative_jobs as gj
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
    project_editor_draft,
)
from tests.routes.test_phone_subtitled_editor_commit import device_status, phone_job


def _rendered_job(monkeypatch, **kwargs):
    """A finished Talking edit: the variant carries its rendered video, as in prod."""
    monkeypatch.setattr(settings, "media_overlays_enabled", True)
    job = phone_job(monkeypatch, **kwargs)
    job.assembly_plan["variants"][0]["output_url"] = "https://storage.example/out.mp4"
    return job


def _chat(job, raw_ops: list[dict]):
    variant = job.assembly_plan["variants"][0]
    base = project_editor_draft(variant, {}, job)
    snapshot = build_editor_snapshot(job, base)
    state = _ParseState(0.9)
    ops = [_parse_op(raw, snapshot, state) for raw in raw_ops]
    return base, snapshot, ops


def _commit(job, compiled):
    variant = job.assembly_plan["variants"][0]
    payload = compiled.payload.model_copy(
        update={"base_generation": gj.variant_render_baseline(variant)}
    )
    return gj.prepare_editor_commit(job, "subtitled", payload, user_id="owner", plan_item_id="item")


def _track_ids(job) -> list[str]:
    return [t.id for t in device_status(job, "subtitled").request.recipe.tracks]


def test_chat_moves_a_fullscreen_card_on_top_and_saves(monkeypatch):
    job = _rendered_job(monkeypatch, fullscreen=True)
    assert "subtitled-fullscreen" in _track_ids(job)

    base, snapshot, ops = _chat(
        job, [{"op": "set_overlay_display", "display_mode": "pip", "kind": "image"}]
    )
    assert "overlay" in snapshot["allowed_op_families"]
    assert snapshot["overlays"]["counts"] == {
        "total": 1,
        "images": 1,
        "videos": 0,
        "fullscreen": 1,
    }
    assert ops == [{"op": "set_overlay_display", "display_mode": "pip", "kind": "image"}]

    compiled = compile_editor_ops(job, base, ops)
    cards = compiled.payload.media_overlays
    assert [c["display_mode"] for c in cards] == ["pip"]
    # Leaving full screen puts the card in a corner, not over the speaker's face.
    assert (cards[0]["x_frac"], cards[0]["y_frac"]) == (0.74, 0.22)

    _commit(job, compiled)
    track_ids = _track_ids(job)
    assert "subtitled-overlays" in track_ids and "subtitled-fullscreen" not in track_ids
    # The receipt names what changed (one picture), so chat can say it honestly.
    assert [(e.lane, e.field) for e in compiled.diff.real()] == [("media_overlays", "changed")]


def test_chat_can_put_a_card_back_full_screen_and_retime_or_remove_it(monkeypatch):
    job = _rendered_job(monkeypatch)
    base, _snapshot, ops = _chat(
        job, [{"op": "set_overlay_display", "display_mode": "fullscreen", "overlay_index": 0}]
    )
    _commit(job, compile_editor_ops(job, base, ops))
    assert "subtitled-fullscreen" in _track_ids(job)

    base, _snapshot, ops = _chat(
        job,
        [{"op": "patch_overlay", "overlay_index": 0, "patch": {"start_s": 2.0, "end_s": 4.0}}],
    )
    _commit(job, compile_editor_ops(job, base, ops))
    track = next(
        t
        for t in device_status(job, "subtitled").request.recipe.tracks
        if t.id == "subtitled-fullscreen"
    )
    assert track.clips[0].visual_placement.window_start == pytest.approx(2.0)

    base, _snapshot, ops = _chat(job, [{"op": "remove_overlay", "overlay_index": 0}])
    _commit(job, compile_editor_ops(job, base, ops))
    ids = _track_ids(job)
    assert "subtitled-fullscreen" not in ids and "subtitled-overlays" not in ids


def test_chat_refuses_what_save_would_refuse_in_plain_words(monkeypatch):
    job = _rendered_job(monkeypatch)
    # A card that is not there, and a change that changes nothing, are sentences -- not a 422.
    base, _snapshot, ops = _chat(
        job, [{"op": "set_overlay_display", "display_mode": "pip", "kind": "all"}]
    )
    with pytest.raises(KriaEditorOpError, match="already show that way"):
        compile_editor_ops(job, base, ops)
    _base, _snapshot, ops = _chat(job, [{"op": "remove_overlay", "overlay_index": 9}])
    assert ops == [None]  # out-of-range index never reaches compile


def test_overlay_family_is_not_offered_when_the_lane_is_closed(monkeypatch):
    job = _rendered_job(monkeypatch, enable=False)
    _base, snapshot, _ops = _chat(job, [])
    assert "overlay" not in snapshot["allowed_op_families"]
    assert "overlays" not in snapshot


def _two_cards(job):
    """The derived base plus a second photo card (fullscreen 5-7s) so windows can collide."""
    variant = job.assembly_plan["variants"][0]
    base = project_editor_draft(variant, {}, job)
    second = {**base["media_overlays"][0], "id": "card-2", "start_s": 5.0, "end_s": 7.0}
    base["media_overlays"].append(second)
    return base


def _run(job, base, raw_ops):
    snapshot = build_editor_snapshot(job, base)
    ops = [_parse_op(raw, snapshot, _ParseState(0.9)) for raw in raw_ops]
    assert None not in ops
    return compile_editor_ops(job, base, ops)


def test_chat_says_so_instead_of_stacking_two_full_screen_pictures(monkeypatch):
    job = _rendered_job(monkeypatch)
    base = _two_cards(job)
    base["media_overlays"][0].update(display_mode="pip", start_s=4.0, end_s=6.0)
    base["media_overlays"][1]["display_mode"] = "fullscreen"
    with pytest.raises(KriaEditorOpError, match="same time"):
        _run(
            job,
            base,
            [{"op": "set_overlay_display", "display_mode": "fullscreen", "overlay_index": 0}],
        )


def test_chat_refuses_a_full_screen_window_the_render_would_silently_drop(monkeypatch):
    job = _rendered_job(monkeypatch)
    base = project_editor_draft(job.assembly_plan["variants"][0], {}, job)
    for patch in ({"start_s": 2.0, "end_s": 2.1}, {"start_s": 50.0, "end_s": 53.0}):
        with pytest.raises(KriaEditorOpError, match="third of a second"):
            _run(
                job,
                base,
                [
                    {
                        "op": "patch_overlay",
                        "overlay_index": 0,
                        "patch": {**patch, "display_mode": "fullscreen"},
                    }
                ],
            )


def test_chat_keeps_a_card_where_the_creator_put_it(monkeypatch):
    job = _rendered_job(monkeypatch)
    base = project_editor_draft(job.assembly_plan["variants"][0], {}, job)
    base["media_overlays"][0].update(display_mode="pip", x_frac=0.9, y_frac=0.1, scale=0.2)
    compiled = _run(
        job,
        base,
        [
            {"op": "set_overlay_display", "display_mode": "fullscreen", "overlay_index": 0},
            {"op": "set_overlay_display", "display_mode": "pip", "overlay_index": 0},
        ],
    )
    card = compiled.payload.media_overlays[0]
    assert (card["x_frac"], card["y_frac"], card["scale"]) == (0.9, 0.1, 0.2)


def test_the_full_screen_kill_switch_stops_chat_and_save(monkeypatch):
    job = _rendered_job(monkeypatch)
    base = project_editor_draft(job.assembly_plan["variants"][0], {}, job)
    monkeypatch.setattr(settings, "phone_subtitled_fullscreen_overlays_enabled", False)
    with pytest.raises(KriaEditorOpError, match="turned off"):
        _run(
            job,
            base,
            [{"op": "set_overlay_display", "display_mode": "fullscreen", "overlay_index": 0}],
        )

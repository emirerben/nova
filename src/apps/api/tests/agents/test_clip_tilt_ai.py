"""KRI-1137 PR C: the chat copilot tilts clips through ``patch_slots.tilt``.

Each test is one way the feature can go wrong. The Save model, the phone writers and the
server definition of the ``tilt`` clip capability belong to PR B, so these tests open the
capability the way the crop tests do (a monkeypatched capability map) and read the rows
the compiler hands to the Save model.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from app.agents import editor_ops_v2 as v2
from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput, _format_snapshot
from app.agents.editor_ops_v2 import timeline
from app.services import kria_editor_ops
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
)
from app.services.kria_editor_ops_diff import describe_diff
from tests.services.test_kria_editor_ops import _job, _variant

OPEN = {
    "text_elements": True,
    "timeline": True,
    "clips": {"tilt": {"editable": True, "reason": None}},
}
CLOSED = {"text_elements": True, "timeline": True, "clips": {}}


def _caps(monkeypatch: pytest.MonkeyPatch, caps: dict[str, Any]) -> None:
    monkeypatch.setattr("app.services.kria_editor_ops._editor_capabilities", lambda *_: caps)


def _three_clips(**row: Any) -> dict:
    """a.mp4 (video), b.jpg (photo), c.png (photo); ``row`` is merged into the second."""
    variant = _variant()
    slots = variant["ai_timeline"]["slots"]
    slots[1].update(row)
    slots.append(
        {**slots[1], "slot_id": "slot-3", "clip_index": 2, "source_gcs_path": "users/u/c.png"}
        | {k: v for k, v in row.items() if k != "tilt"}
    )
    return variant


def _parse(snapshot: dict, *ops: dict, reply: str = "Done, tilted it.", utterance: str = "tilt"):
    raw = json.dumps({"intent": "edit", "ops": list(ops), "confidence": 0.95, "reply": reply})
    return EditCopilotAgent(ModelClient()).parse(
        raw, EditCopilotInput(utterance=utterance, variant_snapshot=snapshot)
    )


def _tilt(selector: dict, tilt: Any) -> dict:
    return {"op": "patch_slots", "selector": selector, "patch": {"tilt": tilt}}


def _compile(monkeypatch, variant: dict, *ops: dict):
    """(draft, rows): the draft and the slot rows the Save model is built from."""
    seen: dict[str, list[dict]] = {}
    real = kria_editor_ops._timeline_models

    def spy(rows):
        # The first call builds the Save payload; a later one projects the "before" state.
        seen.setdefault("rows", copy.deepcopy(rows))
        return real(rows)

    monkeypatch.setattr(kria_editor_ops, "_timeline_models", spy)
    draft = compile_editor_ops(_job(variant), variant, list(ops))
    return draft, seen["rows"]


def _snapshot(monkeypatch, variant: dict, caps: dict = OPEN) -> dict:
    _caps(monkeypatch, caps)
    return build_editor_snapshot(_job(variant), variant)


def _said(draft) -> list[str]:
    return describe_diff(draft.diff.entries, live_text_count=draft.diff.live_text_count)


# ── The ask lands on the right clip with the right sign ───────────────────────


def test_tilt_the_second_clip_ten_degrees_right(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant)
    op = _tilt({"slot_indexes": [1]}, {"rotation_deg": 10})

    parsed = _parse(snapshot, op)
    assert parsed.ops == [_tilt({"slot_indexes": [1]}, {"rotation_deg": 10.0})]

    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert [r.get("tilt") for r in rows] == [None, {"rotation_deg": 10.0}, None]
    assert _said(draft) == ["Tilted clip 2 10 degrees right"]


def test_left_is_negative_and_the_receipt_says_left(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"rotation_deg": -10}))

    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": -10.0}
    assert _said(draft) == ["Tilted clip 2 10 degrees left"]


def test_all_photos_with_a_blurred_background(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(
        snapshot,
        _tilt({"media_kind": "image"}, {"rotation_deg": -6, "backdrop": {"kind": "blur"}}),
    )

    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    want = {"rotation_deg": -6.0, "backdrop": {"kind": "blur"}}
    assert [r.get("tilt") for r in rows] == [None, want, want]
    assert _said(draft) == ["Tilted 2 clips 6 degrees left, blurred background"]


def test_an_angle_past_a_half_turn_wraps_to_the_same_direction_on_screen(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"rotation_deg": 270}))

    _draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": -90.0}


@pytest.mark.parametrize("angle", [400, -361, float("inf"), "10", True, None])
def test_an_unusable_angle_is_refused_in_plain_words(monkeypatch, angle):
    snapshot = _snapshot(monkeypatch, _three_clips())
    parsed = _parse(
        snapshot, _tilt({"slot_indexes": [1]}, {"rotation_deg": angle}), reply="Tilted clip 2."
    )

    assert parsed.ops == []
    assert "Tilted" not in parsed.reply
    assert "tilt" in parsed.reply.lower()


# ── Relative asks add to what the clip already has ────────────────────────────


def test_a_relative_ask_adds_to_the_current_angle_and_keeps_the_background(monkeypatch):
    now = {"rotation_deg": 10.0, "backdrop": {"kind": "blur"}}
    variant = _three_clips(tilt=now)
    snapshot = _snapshot(monkeypatch, variant)
    assert snapshot["slots"][1]["tilt"] == now
    assert "tilt" not in snapshot["slots"][0]

    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"rotation_delta_deg": 5}))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)

    assert rows[1]["tilt"] == {"rotation_deg": 15.0, "backdrop": {"kind": "blur"}}
    assert _said(draft) == ["Tilted clip 2 15 degrees right, blurred background"]


def test_a_relative_ask_over_clips_with_different_angles_adds_to_each(monkeypatch):
    variant = _three_clips()
    variant["ai_timeline"]["slots"][1]["tilt"] = {"rotation_deg": 10.0}
    variant["ai_timeline"]["slots"][2]["tilt"] = {"rotation_deg": -20.0}
    snapshot = _snapshot(monkeypatch, variant)

    parsed = _parse(snapshot, _tilt({"media_kind": "image"}, {"rotation_delta_deg": 5}))
    _draft, rows = _compile(monkeypatch, variant, *parsed.ops)

    assert [r["tilt"]["rotation_deg"] for r in rows[1:]] == [15.0, -15.0]


def test_a_relative_ask_past_a_half_turn_wraps(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 175.0})
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"rotation_delta_deg": 10}))

    _draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": -175.0}


def test_an_absolute_angle_replaces_instead_of_adding(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 10.0, "backdrop": {"kind": "blur"}})
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"rotation_deg": 4}))

    _draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": 4.0, "backdrop": {"kind": "blur"}}


def test_an_absolute_and_a_relative_angle_together_are_ambiguous(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips())
    parsed = _parse(
        snapshot, _tilt({"slot_indexes": [1]}, {"rotation_deg": 4, "rotation_delta_deg": 5})
    )
    assert parsed.ops == []


# ── Reset and the background on its own ───────────────────────────────────────


def test_straightening_removes_the_tilt(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 10.0, "backdrop": {"kind": "blur"}})
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, None))
    assert parsed.ops == [_tilt({"slot_indexes": [1]}, None)]

    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] is None
    assert _said(draft) == ["Straightened clip 2"]


def test_straightening_a_clip_that_is_already_upright_says_so(monkeypatch):
    variant = _three_clips()
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, None))
    with pytest.raises(KriaEditorOpError, match="already"):
        compile_editor_ops(_job(variant), variant, parsed.ops)


def test_a_background_alone_keeps_the_angle_and_a_blank_one_removes_it(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 8.0})
    snapshot = _snapshot(monkeypatch, variant)
    color = {"kind": "color", "color": "#112233"}

    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"backdrop": color}))
    _draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": 8.0, "backdrop": color}

    variant = _three_clips(tilt={"rotation_deg": 8.0, "backdrop": color})
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"backdrop": {"kind": "blank"}}))
    draft, rows = _compile(monkeypatch, variant, *parsed.ops)
    assert rows[1]["tilt"] == {"rotation_deg": 8.0}
    assert _said(draft) == ["Changed the background behind clip 2: plain"]


# ── The model cannot write a backdrop the wire would reject ───────────────────


@pytest.mark.parametrize(
    "backdrop",
    [
        {"kind": "sparkle"},
        {"kind": "color"},
        {"kind": "color", "color": "red"},
        {"kind": "color", "color": "#12345"},
        {"kind": "blur", "color": "#112233"},
        {"kind": "blur", "radius": 40},
        {},
        "blur",
        None,
        ["blur"],
    ],
)
def test_an_invalid_backdrop_never_reaches_the_draft(monkeypatch, backdrop):
    snapshot = _snapshot(monkeypatch, _three_clips())
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"backdrop": backdrop}))
    assert parsed.ops == []
    assert "Done, tilted it." not in parsed.reply


def test_an_empty_or_unknown_tilt_patch_is_refused(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips())
    for tilt in ({}, {"angle": 10}, {"rotation_deg": 10, "zoom": 2}, 10, "10"):
        parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, tilt))
        assert parsed.ops == []
        assert "Done, tilted it." not in parsed.reply


# ── A clip with a look cannot be tilted ───────────────────────────────────────


def test_a_clip_with_a_look_refuses_a_tilt_in_a_plain_sentence(monkeypatch):
    variant = _three_clips(look_preset="golden_hour")
    snapshot = _snapshot(monkeypatch, variant)
    parsed = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"rotation_deg": 10}))
    assert parsed.ops  # the parser cannot see a look it was not shown; the compiler decides

    with pytest.raises(KriaEditorOpError, match="Clip 2 has a look"):
        compile_editor_ops(_job(variant), variant, parsed.ops)


def test_a_look_and_a_tilt_in_one_patch_are_refused(monkeypatch):
    variant = _three_clips()
    _caps(monkeypatch, OPEN)
    op = {
        "op": "patch_slots",
        "selector": {"slot_indexes": [1]},
        "patch": {"look_preset": "golden_hour", "tilt": {"rotation_deg": 10}},
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(_job(variant), variant, [op])


def test_straightening_a_clip_with_a_look_is_allowed(monkeypatch):
    variant = _three_clips(look_preset="golden_hour", tilt={"rotation_deg": 10.0})
    _caps(monkeypatch, OPEN)
    _draft, rows = _compile(monkeypatch, variant, _tilt({"slot_indexes": [1]}, None))
    assert rows[1]["tilt"] is None


def test_a_card_photo_turns_but_its_background_stays(monkeypatch):
    variant = _three_clips(layout="supporting_card")
    snapshot = _snapshot(monkeypatch, variant)

    turn = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"rotation_deg": 5}))
    _draft, rows = _compile(monkeypatch, variant, *turn.ops)
    assert rows[1]["tilt"] == {"rotation_deg": 5.0}

    paint = _parse(snapshot, _tilt({"slot_indexes": [1]}, {"backdrop": {"kind": "blur"}}))
    with pytest.raises(KriaEditorOpError, match="card"):
        compile_editor_ops(_job(variant), variant, paint.ops)


# ── A closed gate changes nothing the model sees and refuses the ask ──────────


def test_a_closed_gate_leaves_the_snapshot_and_the_prompt_as_they_were(monkeypatch):
    variant = _three_clips(tilt={"rotation_deg": 10.0})
    snapshot = _snapshot(monkeypatch, variant, CLOSED)

    assert "tilt" not in snapshot["timeline_patch_capabilities"]
    assert all("tilt" not in slot for slot in snapshot["slots"])
    assert "tilt=" not in _format_snapshot(snapshot)
    assert "CLIP TILT" not in v2.family_fragments(snapshot)

    opened = _snapshot(monkeypatch, variant, OPEN)
    assert "CLIP TILT" in v2.family_fragments(opened)
    assert v2.family_fragments(opened).startswith(v2.family_fragments(snapshot))


def test_a_closed_gate_refuses_the_ask_instead_of_claiming_it(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips(), CLOSED)
    parsed = _parse(
        snapshot,
        _tilt({"slot_indexes": [1]}, {"rotation_deg": 10}),
        reply="Tilted clip 2 ten degrees to the right.",
    )

    assert parsed.ops == []
    assert parsed.outcome == "unsupported"
    assert "Tilted" not in parsed.reply
    assert "can't" in parsed.reply


def test_a_closed_gate_also_stops_an_op_that_skips_the_parser(monkeypatch):
    variant = _three_clips()
    _caps(monkeypatch, CLOSED)
    with pytest.raises(KriaEditorOpError, match="isn't available"):
        compile_editor_ops(
            _job(variant), variant, [_tilt({"slot_indexes": [1]}, {"rotation_deg": 10})]
        )


def test_a_snapshot_without_the_capability_map_cannot_tilt(monkeypatch):
    snapshot = _snapshot(monkeypatch, _three_clips())
    snapshot.pop("timeline_patch_capabilities")
    assert _parse(snapshot, _tilt({"slot_indexes": [1]}, {"rotation_deg": 10})).ops == []


# ── What the open prompt teaches ──────────────────────────────────────────────


def test_the_open_prompt_pins_the_sign_the_units_and_the_relative_rule(monkeypatch):
    fragment = v2.read_fragment("clip_tilt.txt")

    assert "clockwise" in fragment
    assert "right" in fragment and "left" in fragment and "negative" in fragment
    assert "rotation_delta_deg" in fragment
    assert "slot_indexes" in fragment and "0-based" in fragment
    assert "blank" in fragment and "blur" in fragment and "never invent" in fragment.lower()
    assert "patch_text" in fragment  # text rotation is a different op


def test_the_tilt_patch_key_is_part_of_the_timeline_patch_contract():
    assert "tilt" in timeline._PATCH_KEYS


# ── Picture cards: a gate-closed edit says so instead of dropping the angle ───


def _talking_card_snapshot() -> dict:
    return {
        "editor_ops_version": 2,
        "allowed_op_families": ["overlay"],
        "render_destination": "device",
        "total_duration_s": 10.0,
        "slots": [],
        "text_bars": [],
        "overlays": {
            "cards": [
                {
                    "index": 0,
                    "id": "card-1",
                    "kind": "image",
                    "start_s": 1.0,
                    "end_s": 4.0,
                    "x_frac": 0.74,
                    "y_frac": 0.22,
                    "scale": 0.36,
                    "position": "custom",
                    "display_mode": "pip",
                }
            ],
            "counts": {"total": 1, "images": 1, "videos": 0, "fullscreen": 0},
            "asset_pool": [],
            "pending_suggestions": [],
        },
    }


def test_rotating_a_picture_card_is_refused_not_silently_dropped():
    parsed = _parse(
        _talking_card_snapshot(),
        {
            "op": "patch_overlay",
            "overlay_index": 0,
            "patch": {"rotation_deg": 12, "scale": 0.5},
        },
        reply="Tilted the card and made it bigger.",
    )

    assert parsed.ops == []
    assert parsed.outcome == "unsupported"
    assert "Tilted" not in parsed.reply
    assert "tilt a picture" in parsed.reply.lower()


def test_moving_a_picture_card_still_works():
    parsed = _parse(
        _talking_card_snapshot(),
        {"op": "patch_overlay", "overlay_index": 0, "patch": {"scale": 0.5}},
    )
    assert parsed.ops == [{"op": "patch_overlay", "overlay_index": 0, "patch": {"scale": 0.5}}]

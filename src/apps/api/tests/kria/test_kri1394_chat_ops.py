"""KRI-1394: the editor actions chat gained, judged on a phone snapshot (real builder, parser,
compiler and Save model). The failure modes, written before the code:

set_user_song (the creator's own song)
  U1 a lip-sync song's start or end moves (the Save answers user_song_lipsync_locked)
  U2 a window shorter than MIN_PLAYABLE_SONG_S is drafted (user_song_window_out_of_range)
  U3 a start later than the song's last playable second is drafted, or a start past the plan's
     end (where the video stops the song, not a creator's end) is refused
  U4 the op runs on an edit with no song, or where the editor offers no song controls
  U5 a volume outside 0..1 reaches the Save (it 422s)
  U6 a removal carries other fields
  U7 a song already removed in the draft is changed again
  U8 a second turn drops the first turn's song change, or `removed: false` un-removes it
patch_caption_line (one line's size and place)
  C1 a cue id the snapshot does not list
  C2 values outside the phone's clamps reach the Save (size 36-160, x 0.05-0.95, y 0.10-0.90)
  C3 both or neither of cue_id and all
  C4 the editor offers no caption style scope (older build) and the op still runs
  C5 "every line" leaves a line's own value of that key, or a cue's own size
  C6 a line loses its other override keys
  C7 the next turn does not see the line's new place
set_layer_order (which picture is on top)
  L1 a layer id the snapshot does not list
  L2 equal z values: the order is not the drawn one, and z is not dense after the move
  L3 a z past the top
  L4 a move that changes nothing claims a change
  L5 a full-screen card is "moved"
  L6 moving a picture block touches the cards' z
remove_full_screen_opening
  O1 there is no opening, O2 the full-opening gate is closed, O3 the ending window, fill or corner
  go with it
set_video_shape
  S1 an axis the editor keeps closed, S2 the same value claims a change
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.routes.generative_jobs import EditorCommitRequest
from app.services import kria_editor_ops as editor
from tests.evals.request_following.author_action_fixtures import (
    BACKGROUND_OPEN,
    BACKGROUND_POOL,
    CAPTION_SCOPE_CAPS,
    FULL_OPENING_OPEN,
    LIPSYNC_CAPS,
    SHAPE_CAPS,
    SONG_CAPS,
    Scenario,
    _cues,
    _full_opening,
    _song,
    _stacked_cards,
    _stacked_pictures,
    _talking,
    build,
)


def _run(op: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return build(Scenario("test", "", [], [op], **kwargs))


def _refused(result: dict[str, Any]) -> list[dict[str, str]]:
    probe = result["probe"]
    assert not probe["parsed_ops"], probe
    return probe["parse_reasons"]


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    probe = result["probe"]
    assert probe["parsed_ops"] and probe.get("compiles") is True, probe
    payload = editor.editor_payload_dump(result["compiled"].payload)
    EditorCommitRequest.model_validate(payload)  # what the runtime hands the Save
    return payload


def _compile_error(result: dict[str, Any]) -> str:
    probe = result["probe"]
    assert probe["parsed_ops"] and probe.get("compiles") is False, probe
    return probe["compile_error"]


BACKGROUND_SONG = {"variant": _song("background"), "caps": SONG_CAPS}
LIPSYNC_SONG = {"variant": _song("lipsync"), "caps": LIPSYNC_CAPS}


@pytest.mark.parametrize(
    "op",
    [
        {"op": "set_user_song", "window_start_s": 5.0},
        {"op": "set_user_song", "window_end_s": 18.0},
    ],
)
def test_u1_lipsync_window_is_refused_in_the_saves_words(op, prod_profile) -> None:
    reasons = _refused(_run(op, **LIPSYNC_SONG))
    assert reasons[0]["reason"] == "capability_unavailable"
    assert "Lip-sync keeps the song where you filmed it" in reasons[0]["detail"]


@pytest.mark.parametrize(
    ("op", "why"),
    [
        ({"op": "set_user_song", "window_start_s": 10.0, "window_end_s": 10.5}, "U2"),
        ({"op": "set_user_song", "window_end_s": 0.4}, "U2 against the stored start"),
        ({"op": "set_user_song", "window_start_s": 59.5}, "U3"),
    ],
)
def test_u2_u3_a_window_under_the_playable_minimum_is_refused(op, why, prod_profile) -> None:
    reasons = _refused(_run(op, **BACKGROUND_SONG))
    assert reasons[0]["reason"] == "invalid_value", why


def test_u3_a_start_past_the_videos_window_is_not_an_end(prod_profile) -> None:
    # The plan's end (23 s) is where a 23 s video stops the song, not a limit: the song
    # re-windows from the new start. Only an end the creator set limits a start.
    payload = _payload(_run({"op": "set_user_song", "window_start_s": 30.0}, **BACKGROUND_SONG))
    assert payload["user_song"]["window_start_s"] == 30.0

    def creator_end(variant: dict[str, Any]) -> None:
        _song("background")(variant)
        variant["user_song_edit"] = {"window_end_s": 20.0}

    reasons = _refused(
        _run({"op": "set_user_song", "window_start_s": 30.0}, variant=creator_end, caps=SONG_CAPS)
    )
    assert reasons[0]["reason"] == "invalid_value"


def test_u4_no_song_or_no_controls_refuses_in_plain_words(prod_profile) -> None:
    for kwargs in ({"caps": SONG_CAPS}, {"variant": _song("background")}):
        result = _run({"op": "set_user_song", "volume": 0.5}, **kwargs)
        assert "user_song" not in result["probe"]["families"]
        reasons = _refused(result)
        assert reasons[0]["reason"] == "capability_unavailable"


def _half_volume_song(variant: dict[str, Any]) -> None:
    _song("background")(variant)
    variant["_phone_editor_plan_v1"]["user_song"]["volume"] = 0.5


@pytest.mark.parametrize(("asked", "saved"), [(1.7, 1.0), (-0.3, 0.0), (0.333, 0.33)])
def test_u5_volume_is_clamped_like_the_slider(asked, saved, prod_profile) -> None:
    payload = _payload(
        _run({"op": "set_user_song", "volume": asked}, variant=_half_volume_song, caps=SONG_CAPS)
    )
    assert payload["user_song"]["volume"] == saved


def test_u6_removal_carries_nothing_else(prod_profile) -> None:
    payload = _payload(_run({"op": "set_user_song", "remove": True, "volume": 0.2}, **LIPSYNC_SONG))
    assert payload["user_song"] == {"removed": True}


def test_u7_u8_turns_merge_and_a_removed_song_stays_removed(prod_profile) -> None:
    first = _payload(_run({"op": "set_user_song", "volume": 0.5}, **BACKGROUND_SONG))
    second = _payload(_run({"op": "set_user_song", "window_start_s": 4.0}, **BACKGROUND_SONG))
    merged = editor.merge_editor_draft(first, second)
    assert merged["user_song"]["volume"] == 0.5
    assert merged["user_song"]["window_start_s"] == 4.0

    removed = _payload(_run({"op": "set_user_song", "remove": True}, **BACKGROUND_SONG))
    after = editor.merge_editor_draft(removed, second)
    assert after["user_song"]["removed"] is True

    def removed_in_draft(variant: dict[str, Any]) -> None:
        _song("background")(variant)
        variant.update(editor.project_editor_draft(variant, removed))

    reasons = _refused(
        _run({"op": "set_user_song", "volume": 0.9}, variant=removed_in_draft, caps=SONG_CAPS)
    )
    assert "already taken off" in reasons[0]["detail"]


def test_an_end_at_the_songs_length_means_no_end(prod_profile) -> None:
    payload = _payload(_run({"op": "set_user_song", "window_end_s": 75.0}, **BACKGROUND_SONG))
    assert payload["user_song"]["window_end_s"] == 60.0


CAPTIONS = {"variant": _cues, "caps": CAPTION_SCOPE_CAPS}


def test_c1_unknown_cue_is_a_stale_target(prod_profile) -> None:
    reasons = _refused(
        _run({"op": "patch_caption_line", "cue_id": "cue-9", "size_px": 90}, **CAPTIONS)
    )
    assert reasons[0]["reason"] == "stale_target"


def test_c2_values_are_clamped_to_the_phone_bounds(prod_profile) -> None:
    payload = _payload(
        _run(
            {
                "op": "patch_caption_line",
                "cue_id": "cue-2",
                "size_px": 400,
                "position_x_frac": -1,
                "position_y_frac": 0.97,
            },
            **CAPTIONS,
        )
    )
    own = payload["caption_meta"]["appearance"]["line_overrides"]["cue-2"]
    assert own == {"size_px": 160, "position_x_frac": 0.05, "position_y_frac": 0.9}


@pytest.mark.parametrize(
    "op",
    [
        {"op": "patch_caption_line", "cue_id": "cue-1", "all": True, "size_px": 90},
        {"op": "patch_caption_line", "size_px": 90},
        {"op": "patch_caption_line", "cue_id": "cue-1"},
    ],
)
def test_c3_one_target_and_one_value_are_required(op, prod_profile) -> None:
    assert _refused(_run(op, **CAPTIONS))[0]["reason"] == "invalid_value"


def test_c4_no_style_scope_no_op(prod_profile) -> None:
    result = _run(
        {"op": "patch_caption_line", "cue_id": "cue-1", "size_px": 90},
        variant=_cues,
        caps={"caption_editor_style": True},
    )
    assert "caption_lines" not in result["probe"]["families"]
    assert _refused(result)[0]["reason"] == "capability_unavailable"


def _scoped_cues(variant: dict[str, Any]) -> None:
    _cues(variant)
    variant["caption_cues"][0]["size_px"] = 120
    variant["caption_editor_style"] = {
        "alignment": "center",
        "line_overrides": {
            "cue-1": {"size_px": 140, "position_y_frac": 0.3},
            "cue-2": {"position_x_frac": 0.2, "position_y_frac": 0.4},
        },
    }


def test_c5_every_line_drops_each_lines_own_value(prod_profile) -> None:
    payload = _payload(
        _run(
            {"op": "patch_caption_line", "all": True, "size_px": 70, "position_y_frac": 0.8},
            variant=_scoped_cues,
            caps=CAPTION_SCOPE_CAPS,
        )
    )
    meta = payload["caption_meta"]
    assert meta["size_px"] == 70
    assert meta["appearance"]["position_y_frac"] == 0.8
    assert meta["appearance"]["line_overrides"] == {"cue-2": {"position_x_frac": 0.2}}
    assert all("size_px" not in cue for cue in payload["caption_cues"])
    assert [cue["id"] for cue in payload["caption_cues"]] == ["cue-1", "cue-2", "cue-3"]


def test_c6_c7_a_line_keeps_its_other_keys_and_the_next_turn_sees_it(prod_profile) -> None:
    payload = _payload(
        _run(
            {"op": "patch_caption_line", "cue_id": "cue-2", "size_px": 96},
            variant=_scoped_cues,
            caps=CAPTION_SCOPE_CAPS,
        )
    )
    own = payload["caption_meta"]["appearance"]["line_overrides"]
    assert own["cue-2"] == {"position_x_frac": 0.2, "position_y_frac": 0.4, "size_px": 96}
    assert own["cue-1"] == {"size_px": 140, "position_y_frac": 0.3}

    def drafted(variant: dict[str, Any]) -> None:
        _scoped_cues(variant)
        variant.update(editor.project_editor_draft(variant, payload))

    snapshot = _run(
        {"op": "patch_caption_line", "cue_id": "cue-3", "size_px": 50},
        variant=drafted,
        caps=CAPTION_SCOPE_CAPS,
    )["snapshot"]
    assert snapshot["captions"]["line_placement"]["lines"]["cue-2"]["size_px"] == 96


def _cards_with_tie(variant: dict[str, Any]) -> None:
    _stacked_cards(variant)
    for card in variant["media_overlays"]:
        card["z"] = 0
    variant["media_overlays"].append(
        {
            "id": "card-0",
            "kind": "image",
            "start_s": 2.0,
            "end_s": 6.0,
            "x_frac": 0.3,
            "y_frac": 0.3,
            "scale": 0.3,
            "display_mode": "fullscreen",
        }
    )


def _z(payload: dict[str, Any], key: str = "media_overlays") -> dict[str, Any]:
    return {row["id"]: row.get("z") for row in payload[key]}


def test_l1_unknown_layer_is_a_stale_target(prod_profile) -> None:
    reasons = _refused(
        _run(
            {"op": "set_layer_order", "layer": "card-9", "direction": "front"},
            variant=_stacked_cards,
        )
    )
    assert reasons[0]["reason"] == "stale_target"


def test_l2_l3_ties_follow_the_drawn_order_and_z_comes_out_dense(prod_profile) -> None:
    result = _run({"op": "set_layer_order", "layer": "card-1", "z": 9}, variant=_cards_with_tie)
    # Equal z: start_s decides, as the recipe draws them (card-1 starts at 1 s, card-2 at 2 s).
    assert result["snapshot"]["layer_order"]["cards"] == ["card-1", "card-2"]
    assert _z(_payload(result)) == {"card-1": 1, "card-2": 0, "card-0": None}


def test_l4_a_move_to_where_it_is_claims_nothing(prod_profile) -> None:
    error = _compile_error(
        _run(
            {"op": "set_layer_order", "layer": "card-1", "direction": "front"},
            variant=_stacked_cards,
        )
    )
    assert "already there" in error


def test_l5_a_full_screen_card_has_no_layer(prod_profile) -> None:
    reasons = _refused(
        _run(
            {"op": "set_layer_order", "layer": "card-0", "direction": "back"},
            variant=_cards_with_tie,
        )
    )
    assert reasons[0]["reason"] == "capability_unavailable"


def test_l6_moving_a_picture_never_touches_the_cards(prod_profile) -> None:
    def both(variant: dict[str, Any]) -> None:
        _stacked_cards(variant)
        _stacked_pictures(variant)

    payload = _payload(
        _run(
            {"op": "set_layer_order", "layer": "photo-1", "direction": "front"},
            variant=both,
            caps={"visual_blocks": True},
        )
    )
    assert "media_overlays" not in payload
    assert _z(payload, "visual_blocks") == {"photo-1": 1, "photo-2": 0}


OPENING = {"context": {"background_pool": BACKGROUND_POOL}}


def test_o1_o2_no_opening_or_a_closed_gate_is_refused(prod_profile) -> None:
    no_opening = _run(
        {"op": "remove_full_screen_opening"}, variant=_talking, patches=FULL_OPENING_OPEN, **OPENING
    )
    assert "no full-screen opening" in _refused(no_opening)[0]["detail"]
    closed = _run(
        {"op": "remove_full_screen_opening"},
        variant=_full_opening,
        patches=BACKGROUND_OPEN,
        **OPENING,
    )
    assert "isn't available" in _refused(closed)[0]["detail"]


def test_o3_only_the_opening_goes(prod_profile) -> None:
    payload = _payload(
        _run(
            {"op": "remove_full_screen_opening"},
            variant=_full_opening,
            patches=FULL_OPENING_OPEN,
            **OPENING,
        )
    )
    background = payload["speaker_background"]
    assert background["media_id"] == "bg-2"
    assert background["full_screen_windows"] == [{"start_s": 20.0, "end_s": 23.0}]


def test_s1_s2_closed_axes_and_unchanged_shapes(prod_profile) -> None:
    fit_only = copy.deepcopy(SHAPE_CAPS)
    fit_only["orientation"] = {"editable": False, "reason": "orientation_unsupported"}
    reasons = _refused(
        _run({"op": "set_video_shape", "orientation": "landscape"}, variant=_talking, caps=fit_only)
    )
    assert reasons[0]["reason"] == "capability_unavailable"
    error = _compile_error(
        _run({"op": "set_video_shape", "landscape_fit": "fill"}, variant=_talking, caps=SHAPE_CAPS)
    )
    assert "already has that shape" in error

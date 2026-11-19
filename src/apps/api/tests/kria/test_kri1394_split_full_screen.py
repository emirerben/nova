"""KRI-1394: "Full screen from / until playhead" from chat, judged on a phone snapshot (real
builder, parser, compiler and Save model). The planner itself is proven equal to the phone's in
``test_split_full_screen_parity.py``; these are the failure modes of the op around it, written
before the op:

F1 the op is offered where the phone cannot plan it, or where this port does not move what the
   phone moves (no split screen, a narrated or Talking source edit, a section under 1 s), so the
   prompt changes for an edit that cannot use it
F2 a section the snapshot does not list
F3 a moment closer than 0.5 s to the section's start or end, or outside it
F4 a panel the section does not have
F5 a named panel with no footage on that side is quietly swapped for another panel (the phone's
   fallback for a tap; for a named request it is a silent override)
F6 another clip or timeline op in the same bundle, before or after, whose lanes would follow a
   different rebase
F7 text the phone re-times lies over the section (or after it when time is lost) and the draft
   keeps it where the phone would not; text before the section blocks nothing
F8 footage runs out and the lanes after the lost time stay behind, a lane inside it survives, or
   an untouched lane is sent to the Save
F9 nothing is lost and a lane is sent anyway (the phone sends only changed sections)
F10 the Save model refuses the compiled sections
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import settings
from app.routes.generative_jobs import EditorCommitRequest
from app.services import kria_editor_ops as editor
from tests.evals.request_following.author_action_fixtures import Scenario, _split, build

OP = "make_split_full_screen"


def _run(op: dict[str, Any] | list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    ops = op if isinstance(op, list) else [op]
    kwargs.setdefault("variant", _split)
    kwargs.setdefault("needs_split", True)
    return build(Scenario("split_screen_full_screen", "", [], ops, **kwargs))


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    probe = result["probe"]
    assert probe["parsed_ops"] and probe.get("compiles") is True, probe
    payload = editor.editor_payload_dump(result["compiled"].payload)
    EditorCommitRequest.model_validate(payload)
    return payload


def _reasons(result: dict[str, Any]) -> list[dict[str, str]]:
    probe = result["probe"]
    assert not probe["parsed_ops"], probe
    return probe["parse_reasons"]


def _compile_error(result: dict[str, Any]) -> str:
    probe = result["probe"]
    assert probe["parsed_ops"] and probe.get("compiles") is False, probe
    return probe["compile_error"]


def _op(**fields: Any) -> dict[str, Any]:
    return {"op": OP, "section_id": "duo", "at_s": 2.0, "side": "from", **fields}


def _room(variant: dict[str, Any]) -> None:
    """duo lasts 6 s but its clips 3 s: footage after 3 s runs out when a panel goes full screen."""
    _split(variant)
    variant["user_timeline"]["split_screen_sections"][0]["duration_s"] = 6.0


def test_f1_offered_only_where_the_phone_plans_it(
    prod_profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(_op())["snapshot"]["split_full_screen"] == [
        {
            "section_id": "duo",
            "start_s": 0.0,
            "end_s": 3.0,
            "at_s_min": 0.5,
            "at_s_max": 2.5,
            "panel_count": 2,
        }
    ]

    def narrated(variant: dict[str, Any]) -> None:
        _split(variant)
        variant["resolved_archetype"] = "narrated"

    def talking(variant: dict[str, Any]) -> None:
        _split(variant)
        variant["user_timeline"]["slots"][0]["slot_id"] = "native-phone-talking-source"

    def short(variant: dict[str, Any]) -> None:
        _split(variant)
        timeline = variant["user_timeline"]
        timeline["split_screen_sections"][0]["duration_s"] = 0.9
        for row in timeline["slots"][:2]:
            row["duration_s"] = 0.9

    for kwargs in (
        {"needs_split": False},
        {"variant": narrated},
        {"variant": talking},
        {"variant": short},
    ):
        with monkeypatch.context() as scoped:
            if kwargs.get("needs_split") is False:
                # Production has verified splitScreen (live phone-render-config), so a case
                # about a build without it has to take it away.
                scoped.setattr(
                    settings,
                    "phone_render_verified_features",
                    [f for f in settings.phone_render_verified_features if f != "splitScreen"],
                )
            result = _run(_op(), **kwargs)
        assert "split_full_screen" not in result["snapshot"], kwargs
        reasons = _reasons(result)
        assert reasons[0]["reason"] == "capability_unavailable", kwargs
        assert "can't go full screen from chat" in reasons[0]["detail"]


def test_f2_a_section_the_snapshot_does_not_list(prod_profile) -> None:
    assert _reasons(_run(_op(section_id="trio")))[0]["reason"] == "stale_target"


@pytest.mark.parametrize("at_s", [0.4, 2.6, 9.0, "2"])
def test_f3_a_moment_too_close_to_an_edge_or_outside(at_s, prod_profile) -> None:
    assert _reasons(_run(_op(at_s=at_s)))[0]["reason"] == "invalid_value"


@pytest.mark.parametrize("panel", [2, -1, True])
def test_f4_a_panel_the_section_does_not_have(panel, prod_profile) -> None:
    assert _reasons(_run(_op(panel=panel)))[0]["reason"] == "invalid_value"


def test_f5_a_named_panel_without_footage_there_is_refused_not_swapped(prod_profile) -> None:
    def bottom_ends_early(variant: dict[str, Any]) -> None:
        _split(variant)
        variant["user_timeline"]["slots"][1]["duration_s"] = 1.5

    error = _compile_error(_run(_op(panel=1), variant=bottom_ends_early))
    assert "Panel 1 has no footage" in error
    payload = _payload(_run(_op(), variant=bottom_ends_early))
    run = [row for row in payload["timeline_slots"] if row.get("parent_segment_id") == "slot-1"]
    assert [row["clip_index"] for row in run] == [0], "without a panel the top panel fills it"


@pytest.mark.parametrize(
    "ops",
    [
        [_op(), {"op": "set_clip_duration", "slot_index": 3, "duration_s": 2.0}],
        [{"op": "set_clip_duration", "slot_index": 3, "duration_s": 2.0}, _op()],
    ],
    ids=["after", "before"],
)
def test_f6_another_timeline_op_in_the_bundle_is_refused(ops, prod_profile) -> None:
    probe = _run(ops)["probe"]
    assert probe.get("compiles") is False, probe
    assert "on its own" in probe["compile_error"]


def _texts(*rows: dict[str, Any]):
    def variant(value: dict[str, Any]) -> None:
        _room(value)
        value["text_elements"] = [dict(row) for row in rows]

    return variant


def test_f7_text_the_phone_retimes_over_the_section_is_refused(prod_profile) -> None:
    over = {"id": "t1", "text": "Harbour", "start_s": 1.0, "end_s": 2.5}
    error = _compile_error(_run(_op(panel=1), variant=_texts(over)))
    assert "Text is timed over the split screen" in error
    caption_bar = {**over, "source_params": {"source": "caption_cue"}}
    assert _payload(_run(_op(panel=1), variant=_texts(caption_bar)))

    def before(value: dict[str, Any]) -> None:
        _split(value)
        timeline = value["user_timeline"]
        timeline["slots"].insert(0, timeline["slots"].pop(2))  # a 3.5 s clip plays first
        value["text_elements"] = [{"id": "t1", "text": "Pier", "start_s": 0.5, "end_s": 3.0}]

    payload = _payload(_run({**_op(), "at_s": 5.5}, variant=before))
    assert "text_elements" not in payload, "text before the section stays where it is"


def test_f7_text_after_the_section_blocks_only_when_time_is_lost(prod_profile) -> None:
    after = {"id": "t1", "text": "Pier", "start_s": 9.0, "end_s": 10.0}
    assert _payload(_run(_op(panel=1), variant=_split_with_text(after)))
    error = _compile_error(_run(_op(panel=1), variant=_texts(after)))
    assert "Text is timed over the split screen" in error


def _split_with_text(*rows: dict[str, Any]):
    def variant(value: dict[str, Any]) -> None:
        _split(value)
        value["text_elements"] = [dict(row) for row in rows]

    return variant


def _lanes(value: dict[str, Any]) -> None:
    _room(value)
    value["caption_cues"] = [
        {"id": "inside", "text": "gone", "start_s": 4.0, "end_s": 5.0},
        {
            "id": "after",
            "text": "two words",
            "start_s": 7.0,
            "end_s": 8.0,
            "words": [
                {"text": "two", "start_s": 7.0, "end_s": 7.5},
                {"text": "words", "start_s": 7.5, "end_s": 8.0},
            ],
        },
    ]
    value["sound_effects"] = [
        {"id": "hit", "at_s": 10.0, "effect_id": "hit_01"},
        {"id": "early", "start_s": 0.5, "end_s": 1.0, "effect_id": "whoosh_01"},
    ]
    value["media_overlays"] = [{"id": "card", "start_s": 2.5, "end_s": 3.5, "kind": "image"}]
    value["drawings"] = [{"id": "ink", "start_s": 12.0, "end_s": 13.0, "strokes": []}]


def test_f8_lost_time_moves_the_lanes_after_it_and_drops_those_inside(prod_profile) -> None:
    result = _run(_op(panel=1), variant=_lanes)
    payload = _payload(result)
    sections = payload["split_screen_sections"]
    assert [(s["id"], s["duration_s"]) for s in sections] == [("duo", 2.0)]
    full = [r for r in payload["timeline_slots"] if r.get("parent_segment_id") == "slot-2"]
    assert [(r["in_s"], r["duration_s"]) for r in full] == [(2.0, 1.0)], "footage ends at 3 s"
    cues = {cue["id"]: cue for cue in payload["caption_cues"]}
    assert set(cues) == {"after"}, "a caption over the lost 3 s goes with it"
    assert (cues["after"]["start_s"], cues["after"]["end_s"]) == (4.0, 5.0)
    assert [(w["start_s"], w["end_s"]) for w in cues["after"]["words"]] == [(4.0, 4.5), (4.5, 5.0)]
    sounds = {row["id"]: row for row in payload["sound_effects"]}
    assert sounds["hit"]["at_s"] == 7.0 and (
        sounds["early"]["start_s"],
        sounds["early"]["end_s"],
    ) == (
        0.5,
        1.0,
    )
    assert [(r["start_s"], r["end_s"]) for r in payload["media_overlays"]] == [(2.5, 3.0)]
    assert [(r["start_s"], r["end_s"]) for r in payload["drawings"]] == [(9.0, 10.0)]
    for lane in ("text_elements", "camera_effects", "speaker_background", "visual_blocks"):
        assert payload.get(lane) is None, f"{lane} did not change and is not sent"


def test_f9_no_lane_is_sent_when_nothing_is_lost(prod_profile) -> None:
    def lanes_no_room(value: dict[str, Any]) -> None:
        _lanes(value)
        value["user_timeline"]["split_screen_sections"][0]["duration_s"] = 3.0

    payload = _payload(_run(_op(panel=1), variant=lanes_no_room))
    for lane in ("caption_cues", "sound_effects", "media_overlays", "drawings", "text_elements"):
        assert payload.get(lane) is None, lane
    assert payload["timeline_slots"] and payload["split_screen_sections"]


def test_f10_the_save_reads_the_sections_the_phone_writes(prod_profile) -> None:
    payload = _payload(_run(_op(side="until", at_s=1.0)))
    members = [r for r in payload["timeline_slots"] if r.get("split_section_id") == "duo"]
    assert sorted((r["split_panel_index"], r["split_offset_s"]) for r in members) == [
        (0, 0.0),
        (1, 0.0),
    ]
    assert payload["split_screen_sections"][0]["duration_s"] == 2.0
    first = payload["timeline_slots"][0]
    assert first["slot_id"] == "slot-1" and first.get("split_section_id") is None

"""Text placement never fails a render (KRI-1152).

KRI-1035 assumed only long titles fail to fit. Job 07efa44c failed with the same
"cannot fit clearly" decline under a saved `bottom_left` style, so these cases
cross every text role with every saved position and obstacle set.
"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_text_style import resolve_phone_text_style, text_role

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures/phone_text_fit/job_07efa44c.json").read_text()
)

ROLE_IDS = {
    "title": "guided-title",
    "caption": "caption-0",
    "label": "label-0",
    "pinned": "pinned-0",
    "lyric": "song-caption-0",
}
POSITIONS = ["top_left", "top", "top_right", "middle", "bottom_left", "bottom", "bottom_right"]
SHORT = "Kadıköy'ü istediler"
LONG = "Devlet Fenerbahçe'ye bir stat teklif etti ve karşılığında Kadıköy'ü yani kimliğini istedi"

_FRAME = (0.0, 0.0, 1.0, 1.0)
OBSTACLE_SETS = {
    "watermark_only": [],
    "face_band": [{"start_s": 0, "end_s": 60, "box": (0.28, 0.35, 0.72, 0.58), "kind": "face"}],
    "caption_lane": [
        {"start_s": 0, "end_s": 60, "box": (0.05, 0.52, 0.74, 0.72), "kind": "caption"}
    ],
    "face_and_caption": [
        {"start_s": 0, "end_s": 60, "box": (0.28, 0.35, 0.72, 0.58), "kind": "face"},
        {"start_s": 0, "end_s": 60, "box": (0.05, 0.52, 0.74, 0.72), "kind": "caption"},
    ],
    "whole_frame": [
        {"start_s": 0, "end_s": 60, "box": _FRAME, "kind": "face"},
        {"start_s": 0, "end_s": 60, "box": _FRAME, "kind": "caption"},
        {"start_s": 0, "end_s": 60, "box": _FRAME, "kind": "text"},
    ],
}


def row(role, text=SHORT, **extra):
    base = dict(
        id=ROLE_IDS[role],
        text=text,
        start_s=0,
        end_s=4,
        position="custom",
        x_frac=0.5,
        y_frac=0.16 if role == "title" else 0.78,
        font_family="Inter",
        size_px=76,
        color="#FFFFFF",
        stroke_width=0,
        shadow_enabled=False,
        alignment="center",
        max_width_frac=0.8,
        effect="static",
    )
    base.update(extra)
    return base


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "app.services.pipeline_trace.record_pipeline_event",
        lambda stage, name, data=None, **_: recorded.append((name, data or {})),
    )
    return recorded


@pytest.mark.parametrize("obstacle_set", sorted(OBSTACLE_SETS))
@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("role", sorted(ROLE_IDS))
def test_a_saved_position_never_fails_a_role(role, position, obstacle_set, events):
    source = row(role)
    rows = resolve_phone_text_style(
        [source],
        preferences={"all_text": {"position": position}},
        requested={},
        excluded_boxes=deepcopy(OBSTACLE_SETS[obstacle_set]),
    )
    assert len(rows) == 1
    assert rows[0]["text"] == source["text"]
    assert text_role(rows[0]) == role
    assert 0 <= rows[0]["x_frac"] <= 1 and 0 <= rows[0]["y_frac"] <= 1


@pytest.mark.parametrize("obstacle_set", ["face_and_caption", "whole_frame"])
@pytest.mark.parametrize("role", sorted(ROLE_IDS))
def test_long_text_never_fails_a_role(role, obstacle_set, events):
    rows = resolve_phone_text_style(
        [row(role, LONG)],
        preferences={"all_text": {"position": "bottom_left"}},
        requested={},
        excluded_boxes=deepcopy(OBSTACLE_SETS[obstacle_set]),
    )
    assert rows[0]["text"] == LONG


def test_saved_position_is_soft_when_its_spot_is_taken(events):
    rows = resolve_phone_text_style(
        [row("caption")],
        preferences={"all_text": {"position": "bottom_left"}},
        requested={},
        excluded_boxes=deepcopy(OBSTACLE_SETS["face_and_caption"]),
    )
    assert rows[0]["y_frac"] < 0.5 or rows[0]["x_frac"] > 0.3
    assert not [name for name, _ in events if name == "text_fit_relaxed"]


def test_a_position_requested_this_turn_stays_fixed_and_leaves_a_receipt(events):
    rows = resolve_phone_text_style(
        [row("caption")],
        preferences={"all_text": {"position": "top"}},
        requested={"all_text": {"position": "bottom_left"}},
        excluded_boxes=deepcopy(OBSTACLE_SETS["face_and_caption"]),
    )
    assert rows[0]["alignment"] == "left"
    assert rows[0]["y_frac"] > 0.5
    receipts = [data for name, data in events if name == "text_fit_relaxed"]
    assert len(receipts) == 1
    assert receipts[0]["row_id"] == "caption-0"
    assert receipts[0]["role"] == "caption"
    assert receipts[0]["text"] == SHORT
    assert receipts[0]["requested_position"] == "bottom_left"
    assert {item["kind"] for item in receipts[0]["obstacles"]} >= {"caption"}


def test_the_lowest_priority_obstacle_gives_way_first(events):
    rows = resolve_phone_text_style(
        [row("caption")],
        preferences={},
        requested={"all_text": {"position": "bottom_left"}},
        excluded_boxes=deepcopy(OBSTACLE_SETS["face_and_caption"]),
    )
    receipt = next(data for name, data in events if name == "text_fit_relaxed")
    assert "face" not in receipt["overlaps"]
    assert rows[0]["text"] == SHORT


def test_replay_of_job_07efa44c_produces_styled_rows(events):
    from app.services import phone_text_styling as styling

    policy = deepcopy(FIXTURE["policy"])
    policy["selected_fonts"] = {"caption": "Inter", "title": "Inter", "label": "Inter"}
    policy["selection_roles"] = ["caption", "title", "label", "pinned"]
    policy["_environment_sampled"] = True
    policy["_background_frames"] = []
    policy["_face_boxes"] = [{**face, "kind": "face"} for face in deepcopy(FIXTURE["face_boxes"])]
    policy["_caption_boxes"] = [
        {"start_s": 0.0, "end_s": 15.0, "box": (0.05, 0.55, 0.74, 0.72), "kind": "caption"}
    ]
    rows = [
        {
            "position": "custom",
            "font_family": "Inter",
            "color": "#FFFFFF",
            "stroke_width": 0,
            "shadow_enabled": False,
            "effect": "static",
            **source,
        }
        for source in deepcopy(FIXTURE["rows"])
    ]
    with styling.phone_text_policy_scope(policy):
        look = styling.generation_caption_look(FIXTURE["cues"], canvas=PORTRAIT)
        styled = styling.generation_style_rows(rows, canvas=PORTRAIT)
    assert look is not None
    assert [r["id"] for r in styled] == [r["id"] for r in rows]
    assert [r["text"] for r in styled] == [r["text"] for r in rows]


def stack_rows(count=3, text="Kadıköy'ü istediler ve stat teklifi geldi"):
    from app.pipeline.text_stack_layout import HEADING_SIZES, STACK_LAYOUT_PARAM

    return [
        row(
            "pinned",
            text,
            id=f"guided-pinned-{index}",
            x_frac=0.08,
            y_frac=0.8,
            alignment="left",
            size_px=HEADING_SIZES[min(index + 1, 3)],
            source_params={
                STACK_LAYOUT_PARAM: {
                    "corner": "bottom_left",
                    "heading_level": min(index + 1, 3),
                    "stack_id": "pin-0",
                    "explicit_size": False,
                }
            },
        )
        for index in range(count)
    ]


@pytest.mark.parametrize("obstacle_set", sorted(OBSTACLE_SETS))
@pytest.mark.parametrize("position", ["top_left", "bottom_left", "bottom_right"])
def test_a_text_group_never_fails_under_a_saved_position(position, obstacle_set, events):
    rows = resolve_phone_text_style(
        stack_rows(),
        preferences={"all_text": {"position": position}},
        requested={},
        excluded_boxes=deepcopy(OBSTACLE_SETS[obstacle_set]),
    )
    assert [r["id"] for r in rows] == [f"guided-pinned-{i}" for i in range(3)]


def test_a_text_group_that_fits_nowhere_is_kept_with_a_receipt(events):
    rows = resolve_phone_text_style(
        stack_rows(count=8, text=LONG),
        preferences={"all_text": {"position": "bottom_left"}},
        requested={},
        excluded_boxes=deepcopy(OBSTACLE_SETS["whole_frame"]),
    )
    assert len(rows) == 8
    receipts = [data for name, data in events if name == "text_fit_relaxed"]
    assert receipts and {data["rung"] for data in receipts} == {"text_group"}
    assert {data["row_id"] for data in receipts} <= {r["id"] for r in rows}


def test_a_text_group_keeps_the_position_requested_this_turn(events):
    rows = resolve_phone_text_style(
        stack_rows(),
        preferences={},
        requested={"all_text": {"position": "top_right"}},
        excluded_boxes=[{"start_s": 0, "end_s": 60, "box": (0.4, 0.05, 1.0, 0.45), "kind": "face"}],
    )
    assert {r["alignment"] for r in rows} == {"right"}
    assert max(r["y_frac"] for r in rows) < 0.5
    receipts = [data for name, data in events if name == "text_fit_relaxed"]
    assert receipts and {data["requested_position"] for data in receipts} == {"top_right"}


def test_a_text_group_gives_way_to_a_face_before_the_device_watermark(events):
    rows = resolve_phone_text_style(
        stack_rows(),
        preferences={"all_text": {"position": "bottom_left"}},
        requested={},
        excluded_boxes=[],
    )
    receipts = [data for name, data in events if name == "text_fit_relaxed"]
    assert not [data for data in receipts if "watermark" in data["overlaps"]]
    assert [r["id"] for r in rows] == [f"guided-pinned-{i}" for i in range(3)]


def test_a_pin_stays_in_the_corner_the_creator_confirmed(events):
    pin = row(
        "pinned", "Maltepe stadı teklifi, 24 Ekim", x_frac=0.06, y_frac=0.86, alignment="left"
    )
    rows = resolve_phone_text_style(
        [pin],
        preferences={},
        requested={},
        excluded_boxes=[
            {"start_s": 0, "end_s": 60, "box": (0.0, 0.40, 1.0, 0.80), "kind": "caption"}
        ],
    )
    assert rows[0]["alignment"] == "left"
    assert rows[0]["y_frac"] > 0.5
    receipts = [data for name, data in events if name == "text_fit_relaxed"]
    assert [data["row_id"] for data in receipts] == ["pinned-0"]


@pytest.mark.parametrize("box", [(0.35, 0.05, 0.65, 0.45), (0.3, 0.05, 0.7, 0.45)])
def test_a_title_that_fit_before_keeps_its_place(box, events):
    rows = resolve_phone_text_style(
        [row("title")],
        preferences={"all_text": {"position": "top"}},
        requested={},
        excluded_boxes=[{"start_s": 0, "end_s": 60, "box": box, "kind": "face"}],
    )
    assert rows[0]["alignment"] == "center"
    assert rows[0]["size_px"] == 76
    assert rows[0]["y_frac"] > 0.6
    assert not [name for name, _ in events if name == "text_fit_relaxed"]


def test_the_resolved_event_names_only_the_rows_it_relaxed(events):
    from app.pipeline.phone_text_style import phone_text_measurement_scope

    with phone_text_measurement_scope():
        resolve_phone_text_style(
            [row("caption")],
            requested={"all_text": {"position": "bottom_left"}},
            excluded_boxes=deepcopy(OBSTACLE_SETS["face_and_caption"]),
        )
        resolve_phone_text_style([row("title")], preferences={}, requested={})
    resolved = [data for name, data in events if name == "phone_text_style_resolved"]
    assert resolved[0]["text_fit_relaxed_ids"] == ["caption-0"]
    assert resolved[1]["text_fit_relaxed_ids"] == []


@pytest.mark.parametrize("cue_count", [8, 40])
def test_caption_placement_does_not_starve_the_rows_styled_after_it(cue_count, events):
    from app.services import phone_text_styling as styling

    cues = [
        {
            "text": f"Bu {i}. cümle Kadıköy'ü istediler ve stat teklifi geldi",
            "start_s": float(i),
            "end_s": float(i + 1),
        }
        for i in range(cue_count)
    ]

    def policy():
        base = deepcopy(FIXTURE["policy"])
        base.update(
            selected_fonts={"caption": "Inter", "title": "Inter", "label": "Inter"},
            selection_roles=["caption", "title", "label", "pinned"],
            _environment_sampled=True,
            _background_frames=[],
            _caption_boxes=[],
            _face_boxes=[{"start_s": 0, "end_s": 60, "box": (0.2, 0.3, 0.8, 0.75), "kind": "face"}],
        )
        return base

    def style_rows(with_captions):
        rows = [
            {
                "position": "custom",
                "font_family": "Inter",
                "color": "#FFFFFF",
                "stroke_width": 0,
                "shadow_enabled": False,
                "effect": "static",
                **source,
            }
            for source in deepcopy(FIXTURE["rows"])
        ]
        events.clear()
        with styling.phone_text_policy_scope(policy()):
            if with_captions:
                styling.generation_caption_look(cues, canvas=PORTRAIT)
            styled = styling.generation_style_rows(rows, canvas=PORTRAIT)
        relaxed = {data["row_id"] for name, data in events if name == "text_fit_relaxed"}
        return styled, relaxed

    alone, alone_relaxed = style_rows(False)
    after_captions, after_relaxed = style_rows(True)
    row_ids = {r["id"] for r in alone}
    assert after_relaxed & row_ids == alone_relaxed & row_ids
    for expected, actual in zip(alone, after_captions, strict=True):
        for key in ("alignment", "x_frac", "y_frac", "size_px", "max_width_frac"):
            assert actual[key] == expected[key], (actual["id"], key)


def test_guided_story_faces_are_tagged_as_faces(monkeypatch):
    from types import SimpleNamespace

    from app.pipeline import phone_guided_text_placement as placement
    from app.services import phone_text_styling as styling

    region = SimpleNamespace(
        start_s=0.0, end_s=5.0, box=SimpleNamespace(left=0.3, top=0.3, right=0.7, bottom=0.5)
    )
    monkeypatch.setattr(
        placement, "build_phone_face_sampler", lambda *_a, **_k: lambda *_b: ([region], {})
    )
    seen = {}
    monkeypatch.setattr(
        styling, "generation_style_rows", lambda rows, **kw: seen.update(kw) or rows
    )
    plan = SimpleNamespace(
        output_orientation="portrait",
        direction="",
        model_dump=lambda **_: {
            "text_elements": [{"id": "guided-title", "text": "x", "start_s": 0.0, "end_s": 3.0}]
        },
    )
    styling.style_guided_generation(plan, [object()], job_id="job")
    assert [box["kind"] for box in seen["excluded_boxes"]] == ["face"]

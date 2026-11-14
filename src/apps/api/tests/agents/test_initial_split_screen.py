"""Replay structured creator output into the same editable section operation."""

from copy import deepcopy

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.services import kria_editor_ops as editor
from tests.services.test_kria_editor_ops import _job, _variant


def test_initial_request_keeps_panel_source_identity_and_clocks(monkeypatch):
    from app.services.initial_split_screen import apply_initial_split_screen

    variant = _variant()
    variant.update(render_destination="device", editor_timeline_mode="authored")
    rows = variant["ai_timeline"]["slots"]
    rows[0].update(media_id="content", duration_s=4, in_s=0, speaker_subject=False)
    rows[1].update(
        media_id="face",
        duration_s=4,
        in_s=0,
        source_duration_s=20,
        media_kind="video",
        slot_id="native-phone-talking-source-a",
    )
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: True)
    request = CreativeStrategy.model_validate(
        {
            "split_screen": {
                "panels": [
                    {"media_id": "content", "source_start_s": 1},
                    {"media_id": "face", "source_start_s": 7},
                ],
                "duration_s": 4,
            }
        }
    ).split_screen
    result = apply_initial_split_screen(_job(variant), variant, request, source_rows=rows)
    members = result["user_timeline"]["slots"]
    assert [r["speaker_subject"] for r in members] == [False, True]
    assert all(not r.get("parent_segment_id") for r in members)
    assert [r["clip_index"] for r in members] == [0, 1]
    assert [r["in_s"] for r in members] == [1, 7]
    assert [r["split_panel_index"] for r in members] == [0, 1]
    assert result["user_timeline"]["split_screen_sections"][0]["duration_s"] == 4
    assert "user_timeline" not in variant


def test_initial_request_rejects_wrong_media_and_unavailable_capability(monkeypatch):
    from app.services.initial_split_screen import apply_initial_split_screen

    variant = _variant()
    rows = variant["ai_timeline"]["slots"]
    rows[0]["media_id"], rows[1]["media_id"] = "content", "face"
    request = {"panels": [{"media_id": "wrong-footage"}, {"media_id": "face"}]}
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: True)
    before = deepcopy(variant)
    with pytest.raises(editor.KriaEditorOpError, match="source"):
        apply_initial_split_screen(_job(variant), variant, request, source_rows=rows)
    assert variant == before
    request["panels"][0]["media_id"] = "content"
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: False)
    with pytest.raises(editor.KriaEditorOpError, match="unavailable"):
        apply_initial_split_screen(_job(variant), variant, request, source_rows=rows)


@pytest.mark.parametrize(
    "utterance", ["Put content above and my face below.", "Üstte içerik, altta yüzüm olsun."]
)
def test_creator_transport_replay_retains_structured_panel_request(utterance):
    import json

    from app.agents._schemas.creator_agent import CapabilityAvailability, CreatorMediaRef
    from app.agents.main_creator import MainCreatorAgent
    from tests.agents.test_main_creator_agent import _input, _raw

    input = _input()
    manifest = input.capability_manifest.model_copy(
        update={
            "media": [
                CreatorMediaRef(media_id="content", kind="video"),
                CreatorMediaRef(media_id="face", kind="video"),
            ],
            "capabilities": {
                **input.capability_manifest.capabilities,
                "split_screen": CapabilityAvailability(available=True),
            },
        }
    )
    input = input.model_copy(update={"user_message": utterance, "capability_manifest": manifest})
    raw = json.loads(_raw(audio_strategy="original_audio", selected=["content", "face"]))
    raw["action"]["strategy"]["split_screen"] = {
        "panels": [{"media_id": "content"}, {"media_id": "face"}],
        "duration_s": 4,
    }
    output = MainCreatorAgent(None).parse(json.dumps(raw), input)
    assert [p.media_id for p in output.action.strategy.split_screen.panels] == ["content", "face"]
    assert "source_start_s" in MainCreatorAgent(None).render_prompt(input)


def test_creation_policy_refuses_unavailable_and_unknown_panel_sources():
    from app.agents._schemas.creator_agent import CapabilityAvailability
    from app.services.creator_capabilities import compile_strategy_to_plan
    from app.services.creator_errors import CreatorCapabilityError, CreatorStrategyError
    from tests.agents.test_main_creator_agent import _manifest

    manifest = _manifest()
    request = {"panels": [{"media_id": manifest.media[0].media_id}, {"media_id": "unknown-source"}]}
    strategy = CreativeStrategy(split_screen=request)
    with pytest.raises(CreatorCapabilityError, match="unavailable"):
        compile_strategy_to_plan(manifest, strategy)
    manifest.capabilities["split_screen"] = CapabilityAvailability(available=True)
    with pytest.raises(CreatorStrategyError, match="source"):
        compile_strategy_to_plan(manifest, strategy)


def test_wrong_content_control_rejects_swapped_known_sources(monkeypatch):
    from app.services.initial_split_screen import apply_initial_split_screen

    variant = _variant()
    variant.update(render_destination="device", editor_timeline_mode="authored")
    rows = variant["ai_timeline"]["slots"]
    rows[0]["media_id"], rows[1]["media_id"] = "content", "face"
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: True)
    expected_sources_top_to_bottom = [0, 1]
    wrong_request = {"panels": [{"media_id": "face"}, {"media_id": "content"}], "duration_s": 2}
    result = apply_initial_split_screen(_job(variant), variant, wrong_request, source_rows=rows)
    members = sorted(result["user_timeline"]["slots"], key=lambda row: row["split_panel_index"])
    assert len(members) == 2
    assert result["user_timeline"]["split_screen_sections"][0]["duration_s"] == 2
    assert [row["clip_index"] for row in members] != expected_sources_top_to_bottom


# Several clips in one panel (KRI-1420). Failure modes, each pinned below:
# * a later clip of a panel is dropped, or every clip is stretched to the whole section and
#   stacks on its neighbour;
# * offsets leave a gap or overlap instead of running back to back;
# * a clip with no duration takes the whole section and starves its siblings;
# * a clip that runs past the section end is kept long (the section fails to lay out) or dropped
#   without a word;
# * a panel with both `media_id` and `clips`, or with neither, is accepted;
# * a later clip naming footage the creator never approved gets through policy;
# * the single `media_id` panel the model writes today stops working.
def _three_sources(monkeypatch):
    variant = _variant()
    variant.update(render_destination="device", editor_timeline_mode="authored")
    rows = variant["ai_timeline"]["slots"]
    for row, media_id in zip(rows, ("clip-a", "clip-b"), strict=False):
        row.update(
            media_id=media_id, duration_s=4, in_s=0, source_duration_s=20, media_kind="video"
        )
    sources = [
        *rows,
        {
            **rows[0],
            "slot_id": "source-c",
            "clip_index": 2,
            "media_id": "clip-c",
        },
    ]
    monkeypatch.setattr(editor, "split_screen_available", lambda *args: True)
    return variant, sources


def _members(result):
    return sorted(
        result["user_timeline"]["slots"],
        key=lambda row: (row["split_panel_index"], row["split_offset_s"]),
    )


def _a_then_b_on_top_c_below(**top):
    return {
        "panels": [
            {"clips": [{"media_id": "clip-a"}, {"media_id": "clip-b"}], **top},
            {"media_id": "clip-c"},
        ],
        "duration_s": 4,
    }


def test_panel_lays_its_clips_back_to_back_and_keeps_the_single_source_panel(monkeypatch):
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.kria.split_screen import layout_split_timeline
    from app.services.initial_split_screen import apply_initial_split_screen

    variant, sources = _three_sources(monkeypatch)
    request = CreativeStrategy.model_validate(
        {
            "split_screen": {
                "panels": [
                    {
                        "clips": [
                            {"media_id": "clip-a", "duration_s": 1.5},
                            {"media_id": "clip-b", "source_start_s": 3, "duration_s": 2.5},
                        ]
                    },
                    {"media_id": "clip-c", "source_start_s": 2},
                ],
                "duration_s": 4,
            }
        }
    ).split_screen
    result = apply_initial_split_screen(_job(variant), variant, request, source_rows=sources)
    top_a, top_b, bottom = _members(result)
    assert [(r["media_id"], r["split_panel_index"]) for r in (top_a, top_b, bottom)] == [
        ("clip-a", 0),
        ("clip-b", 0),
        ("clip-c", 1),
    ]
    assert (top_a["split_offset_s"], top_a["duration_s"]) == (0, 1.5)
    assert (top_b["split_offset_s"], top_b["duration_s"], top_b["in_s"]) == (1.5, 2.5, 3)
    assert (bottom["split_offset_s"], bottom["duration_s"], bottom["in_s"]) == (0, 4, 2)
    assert len({r["slot_id"] for r in result["user_timeline"]["slots"]}) == 3
    assert result["user_timeline"]["split_screen_sections"][0]["duration_s"] == 4
    assert (
        layout_split_timeline(
            result["user_timeline"]["slots"], result["user_timeline"]["split_screen_sections"]
        )[2]
        == 4
    )
    assert result["duration_s"] == 4
    assert top_a["transition_after"] == "cut"


def test_clips_without_a_duration_share_the_time_the_others_leave(monkeypatch):
    from app.services.initial_split_screen import apply_initial_split_screen

    variant, sources = _three_sources(monkeypatch)
    equal = apply_initial_split_screen(
        _job(variant), variant, _a_then_b_on_top_c_below(), source_rows=sources
    )
    assert [(r["split_offset_s"], r["duration_s"]) for r in _members(equal)[:2]] == [
        (0, 2),
        (2, 2),
    ]
    request = _a_then_b_on_top_c_below()
    request["panels"][0]["clips"][0]["duration_s"] = 3
    leftover = apply_initial_split_screen(_job(variant), variant, request, source_rows=sources)
    assert [(r["split_offset_s"], r["duration_s"]) for r in _members(leftover)[:2]] == [
        (0, 3),
        (3, 1),
    ]


def test_a_clip_past_the_section_end_is_cut_and_one_with_no_room_is_refused(monkeypatch):
    from app.services.initial_split_screen import apply_initial_split_screen

    variant, sources = _three_sources(monkeypatch)
    request = _a_then_b_on_top_c_below()
    request["panels"][0]["clips"][0]["duration_s"] = 3
    request["panels"][0]["clips"][1]["duration_s"] = 3
    cut = apply_initial_split_screen(_job(variant), variant, request, source_rows=sources)
    assert [r["duration_s"] for r in _members(cut)[:2]] == [3, 1]
    request["panels"][0]["clips"] = [
        {"media_id": "clip-a", "duration_s": 3.95},
        {"media_id": "clip-b", "duration_s": 1},
    ]
    with pytest.raises(editor.KriaEditorOpError, match="section"):
        apply_initial_split_screen(_job(variant), variant, request, source_rows=sources)


def test_section_length_defaults_to_the_longest_panel_of_explicit_clips(monkeypatch):
    from app.services.initial_split_screen import apply_initial_split_screen

    variant, sources = _three_sources(monkeypatch)
    request = _a_then_b_on_top_c_below()
    del request["duration_s"]
    request["panels"][0]["clips"][0]["duration_s"] = 5
    request["panels"][0]["clips"][1]["duration_s"] = 4
    result = apply_initial_split_screen(_job(variant), variant, request, source_rows=sources)
    assert result["user_timeline"]["split_screen_sections"][0]["duration_s"] == 9
    assert [r["duration_s"] for r in _members(result)[:2]] == [5, 4]


def test_a_panel_names_either_one_source_or_a_clip_list():
    from pydantic import ValidationError

    from app.agents._schemas.split_screen import InitialSplitScreen

    ok = {"clips": [{"media_id": "a"}]}
    for bad in (
        {},
        {"media_id": "a", "clips": [{"media_id": "b"}]},
        {"clips": []},
        {"clips": [{"media_id": "a"}], "source_start_s": 1},
        {"clips": [{"media_id": "a", "duration_s": 0.01}]},
        {"clips": [{"media_id": "a"}] * 9},
    ):
        with pytest.raises(ValidationError):
            InitialSplitScreen.model_validate({"panels": [bad, {"media_id": "b"}]})
    InitialSplitScreen.model_validate({"panels": [ok, {"media_id": "b"}]})


def test_policy_checks_every_clip_of_every_panel():
    from app.agents._schemas.creator_agent import CapabilityAvailability
    from app.services.creator_capabilities import compile_strategy_to_plan
    from app.services.creator_errors import CreatorStrategyError
    from tests.agents.test_main_creator_agent import _manifest

    manifest = _manifest()
    manifest.capabilities["split_screen"] = CapabilityAvailability(available=True)
    known = manifest.media[0].media_id
    strategy = CreativeStrategy(
        split_screen={
            "panels": [
                {"clips": [{"media_id": known}, {"media_id": "never-approved"}]},
                {"media_id": known},
            ]
        }
    )
    with pytest.raises(CreatorStrategyError, match="source"):
        compile_strategy_to_plan(manifest, strategy)


def test_creator_replay_a_then_b_on_top_c_below_reaches_the_editable_section(monkeypatch):
    import json

    from app.agents._schemas.creator_agent import CapabilityAvailability, CreatorMediaRef
    from app.agents.main_creator import MainCreatorAgent
    from app.kria.split_screen import layout_split_timeline
    from app.services.initial_split_screen import apply_initial_split_screen
    from tests.agents.test_main_creator_agent import _input, _raw

    input = _input()
    manifest = input.capability_manifest.model_copy(
        update={
            "media": [
                CreatorMediaRef(media_id=m, kind="video") for m in ("clip-a", "clip-b", "clip-c")
            ],
            "capabilities": {
                **input.capability_manifest.capabilities,
                "split_screen": CapabilityAvailability(available=True),
            },
        }
    )
    input = input.model_copy(
        update={
            "user_message": "A then B on top, C on the bottom.",
            "capability_manifest": manifest,
        }
    )
    raw = json.loads(_raw(audio_strategy="original_audio", selected=["clip-a", "clip-b", "clip-c"]))
    raw["action"]["strategy"]["split_screen"] = _a_then_b_on_top_c_below()
    output = MainCreatorAgent(None).parse(json.dumps(raw), input)
    request = output.action.strategy.split_screen
    assert [[c.media_id for c in p.sequence] for p in request.panels] == [
        ["clip-a", "clip-b"],
        ["clip-c"],
    ]
    assert "clips" in MainCreatorAgent(None).render_prompt(input)
    variant, sources = _three_sources(monkeypatch)
    result = apply_initial_split_screen(_job(variant), variant, request, source_rows=sources)
    assert [(r["media_id"], r["split_panel_index"]) for r in _members(result)] == [
        ("clip-a", 0),
        ("clip-b", 0),
        ("clip-c", 1),
    ]
    layout_split_timeline(
        result["user_timeline"]["slots"], result["user_timeline"]["split_screen_sections"]
    )

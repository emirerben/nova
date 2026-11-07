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

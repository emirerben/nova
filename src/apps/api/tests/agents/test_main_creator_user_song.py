"""KRI-374 lane C: the Main Creator learns about an uploaded song.

The song section of the prompt is rendered ONLY when the manifest carries a
usable song. The no-song hashes below are the KRI-506 v46 rebaseline for the
same `_input()` fixture with `creator_montage_shapes_enabled` off.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.agents import main_creator as main_creator_module
from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    ProposeStrategy,
    UserSongFacts,
)
from app.agents.main_creator import (
    MAIN_CREATOR_PROMPT_VERSION,
    MainCreatorAgent,
    MainCreatorInput,
)
from app.config import settings
from tests.agents.test_main_creator_agent import _input, _manifest

# (clip_intents_enabled, brief_enabled) -> sha256 of the KRI-506 v46 render without a song.
PRE_CHANGE_PROMPT_SHA = {
    (True, False): "47fdf325d8c20b5851bf5f88aca7c4609349f12d697bba36da23f6e6be9dc8f5",
    (True, True): "b7d2b67861fdd19dc9c3d9a64cc2a505a0026ea32c76afbb9a25735fcc31549b",
    (False, False): "8818499503544a04696940bc5c752dbb51739ded19e70fc4abe0e1f20b0e62d5",
    (False, True): "cfa4f20b7698078a0b5c8c3f2dfcafe4219253951053a0176e7e04268407073b",
}


def _render(agent_input: MainCreatorInput) -> str:
    return MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]


def _song_input() -> MainCreatorInput:
    manifest = _manifest().model_copy(
        update={
            "has_user_song": True,
            "user_song": UserSongFacts(duration_s=187.4, has_lyrics=True),
            "capabilities": {
                **_manifest().capabilities,
                "phone_source_audio": CapabilityAvailability(available=True),
                "user_song": CapabilityAvailability(available=True),
            },
        }
    )
    return _input().model_copy(update={"capability_manifest": manifest})


@pytest.mark.parametrize(("clip_intents", "brief"), sorted(PRE_CHANGE_PROMPT_SHA))
def test_prompt_without_a_song_is_byte_identical_to_the_pre_change_render(
    monkeypatch, clip_intents, brief
) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", clip_intents)
    monkeypatch.setattr(settings, "creator_montage_shapes_enabled", False)

    prompt = _render(_input().model_copy(update={"brief_enabled": brief}))

    assert (
        hashlib.sha256(prompt.encode()).hexdigest() == PRE_CHANGE_PROMPT_SHA[(clip_intents, brief)]
    )
    assert "UPLOADED SONG" not in prompt
    assert "$user_song_section" not in prompt
    assert "user_song" not in prompt


def test_prompt_with_a_song_teaches_the_song_rules() -> None:
    prompt = _render(_song_input())

    assert main_creator_module._USER_SONG_PROMPT_SECTION in prompt
    assert "$user_song_section" not in prompt
    for cue in ("lip sync", "lip-sync", "lipsync", "singing along", "mouthing the words"):
        assert cue in prompt
    assert 'set\n`audio_strategy` to "user_song"' in prompt
    assert '"lipsync"' in prompt and '"background"' in prompt
    assert "EVERYTHING ELSE" in prompt
    assert "concert" in prompt and "dancing" in prompt
    assert "`summary` MUST say" in prompt
    # The section lives inside the strategy rules, before the JSON envelope.
    assert prompt.index("UPLOADED SONG") < prompt.index("Return ONLY JSON in this envelope")


def test_song_section_is_the_only_difference_when_a_song_is_attached() -> None:
    plain = _input()
    with_song = _song_input()
    song_prompt = _render(with_song)
    # Same everything-else: removing the section restores the plain render except
    # for the manifest JSON, which now (legitimately) carries the song facts.
    assert song_prompt.replace("\n" + main_creator_module._USER_SONG_PROMPT_SECTION, "") != (
        _render(plain)
    )
    assert '"has_user_song":true' in song_prompt
    assert '"user_song":{"duration_s":187.4,"has_lyrics":true}' in song_prompt


def test_prompt_version_is_bumped_and_wired_into_the_spec() -> None:
    assert MAIN_CREATOR_PROMPT_VERSION == "2026-10-07-v46"
    assert MainCreatorAgent.spec.prompt_version == MAIN_CREATOR_PROMPT_VERSION


def _raw(**strategy_update) -> str:
    strategy = {
        "direction": "fast_montage",
        "edit_format": "montage",
        "audio_strategy": "user_song",
        "render_program": "guided",
        "selected_media_ids": [],
        "target_duration_s": 24,
        "rationale": "Cut the clips to the creator's own song.",
        **strategy_update,
    }
    return json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": strategy,
                "summary": "I'll use your song as the background music.",
            }
        }
    )


def test_parse_reads_the_model_authored_song_sync() -> None:
    agent_input = _song_input()
    for sync in ("lipsync", "background"):
        output = MainCreatorAgent(None).parse(_raw(song_sync=sync), agent_input)  # type: ignore[arg-type]
        assert isinstance(output.action, ProposeStrategy)
        assert output.action.strategy.audio_strategy == "user_song"
        assert output.action.strategy.song_sync == sync


def test_parse_reads_lip_sync_spellings_and_tolerates_junk() -> None:
    agent_input = _song_input()
    spelled = MainCreatorAgent(None).parse(_raw(song_sync="lip-sync"), agent_input)  # type: ignore[arg-type]
    assert isinstance(spelled.action, ProposeStrategy)
    assert spelled.action.strategy.song_sync == "lipsync"
    junk = MainCreatorAgent(None).parse(_raw(song_sync="karaoke"), agent_input)  # type: ignore[arg-type]
    assert isinstance(junk.action, ProposeStrategy)
    assert junk.action.strategy.song_sync is None  # repaired later, with a notice


def test_parse_never_trusts_model_authored_resolved_song_takes() -> None:
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(song_sync="lipsync", resolved_song_takes=[{"media_id": "x", "delta_s": 4.2}]),
        _song_input(),
    )
    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.resolved_song_takes is None
    assert "resolved_song_takes" not in output.model_dump_json()


def test_parse_of_a_strategy_without_song_fields_stays_byte_identical() -> None:
    agent_input = _input()
    raw = _raw(audio_strategy="licensed_music")
    output = MainCreatorAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]
    dumped = output.model_dump_json()
    assert "song_sync" not in dumped and "resolved_song_takes" not in dumped


def test_parse_of_user_song_without_a_usable_song_retries_with_a_rule() -> None:
    agent = MainCreatorAgent(None)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="invalid output"):
        agent.parse(_raw(song_sync="background"), _input())
    assert "manifest.user_song" in agent.schema_clarification()

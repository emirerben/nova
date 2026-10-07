"""KRI-479: `voice_mode` is a Creator-agent strategy field.

Failure modes pinned here (written before the code):

* the model's value silently defaults to None somewhere between the raw reply and the
  persisted strategy (the clip_metadata parse()-threading trap) -- `parse` must carry it;
* unknown spellings crash the whole reply instead of reading as "unset" (KRI-129);
* the teaching text leaks into prompts that cannot render a camera-audio montage
  (byte-identity of every other prompt is pinned by test_main_creator_user_song);
* a stray value (no camera-audio montage) reaches the render contract as a promise;
* an unused field changes stored strategies / hashes.
"""

from __future__ import annotations

import json

import pytest

from app.agents import main_creator as main_creator_module
from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    MontageAudioPlan,
    ProposeStrategy,
)
from app.agents.main_creator import MainCreatorAgent
from tests.agents.test_main_creator_agent import _input, _manifest

VOICE_ID = "clip-00-11111111-1111-1111-1111-111111111111"


def _phone_input():
    manifest = _manifest().model_copy(
        update={
            "capabilities": {
                **_manifest().capabilities,
                "phone_source_audio": CapabilityAvailability(available=True),
            }
        }
    )
    return _input().model_copy(update={"capability_manifest": manifest})


def _raw(**strategy_update) -> str:
    strategy = {
        "direction": "fast_montage",
        "edit_format": "montage",
        "audio_strategy": "original_audio",
        "montage_audio": {"preserve_source_audio": True, "source_media_ids": [VOICE_ID]},
        "render_program": "guided",
        "selected_media_ids": [],
        "target_duration_s": 30,
        "rationale": "Play the talk-to-camera voice over a montage of the rest.",
        **strategy_update,
    }
    return json.dumps(
        {"action": {"kind": "propose_strategy", "strategy": strategy, "summary": "Voice behind."}}
    )


def _parse(raw: str, agent_input=None):
    return MainCreatorAgent(None).parse(raw, agent_input or _phone_input())  # type: ignore[arg-type]


def test_parse_carries_the_model_authored_voice_mode() -> None:
    for mode in ("continuous", "excerpts"):
        output = _parse(_raw(voice_mode=mode))
        assert isinstance(output.action, ProposeStrategy)
        assert output.action.strategy.voice_mode == mode


def test_parse_reads_spelling_noise_and_treats_junk_as_unset() -> None:
    noisy = _parse(_raw(voice_mode=" Continuous "))
    assert isinstance(noisy.action, ProposeStrategy)
    assert noisy.action.strategy.voice_mode == "continuous"
    junk = _parse(_raw(voice_mode="karaoke"))
    assert isinstance(junk.action, ProposeStrategy)
    assert junk.action.strategy.voice_mode is None


def test_an_absent_voice_mode_changes_nothing_in_the_stored_strategy() -> None:
    output = _parse(_raw())
    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.voice_mode is None
    assert "voice_mode" not in output.model_dump_json()
    assert "voice_mode" not in CreativeStrategy().model_dump_json()
    # A set value round-trips through the persisted JSON form.
    kept = CreativeStrategy(voice_mode="continuous").model_dump(mode="json", exclude_none=True)
    assert kept["voice_mode"] == "continuous"
    assert CreativeStrategy.model_validate(kept).voice_mode == "continuous"


def test_voice_mode_is_not_in_any_derived_json_schema() -> None:
    """The Kria `apply_strategy` tool schema stays byte-identical (SkipJsonSchema)."""
    assert "voice_mode" not in json.dumps(CreativeStrategy.model_json_schema())


def test_prompt_teaches_voice_mode_only_when_the_phone_can_keep_source_audio() -> None:
    plain = MainCreatorAgent(None).render_prompt(_input())  # type: ignore[arg-type]
    phone = MainCreatorAgent(None).render_prompt(_phone_input())  # type: ignore[arg-type]
    assert "VOICE MODE" not in plain and "voice_mode" not in plain
    assert main_creator_module._VOICE_MODE_PROMPT_SECTION in phone
    assert "$voice_mode_section" not in phone
    flat = " ".join(phone.split())
    for cue in ('"continuous"', '"excerpts"', "exactly that one clip", "own picture is not shown"):
        assert cue in flat
    # The section lives inside the strategy rules, before the JSON envelope.
    assert phone.index("VOICE MODE") < phone.index("Return ONLY JSON in this envelope")


def _compile(strategy: CreativeStrategy):
    from app.services import creator_capabilities
    from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest

    creator_capabilities.settings.guided_edit_capability_enabled = True
    manifest = resolve_creator_manifest(
        item_id="item-1",
        edit_format="montage",
        media=[{"media_id": f"m{i}", "kind": "video", "duration_s": 12.0 + i} for i in range(4)],
    )
    return compile_strategy_to_plan(manifest, strategy).strategy


@pytest.fixture(autouse=True)
def _restore_flag():
    from app.services import creator_capabilities

    before = creator_capabilities.settings.guided_edit_capability_enabled
    yield
    creator_capabilities.settings.guided_edit_capability_enabled = before


def test_policy_keeps_voice_mode_under_a_camera_audio_montage() -> None:
    strategy = CreativeStrategy(
        audio_strategy="original_audio",
        voice_mode="continuous",
        montage_audio=MontageAudioPlan(preserve_source_audio=True, source_media_ids=["m0"]),
    )
    assert _compile(strategy).voice_mode == "continuous"


@pytest.mark.parametrize(
    "update",
    [
        {"montage_audio": None},
        {"montage_audio": MontageAudioPlan(preserve_source_audio=False)},
        {"audio_strategy": "licensed_music", "montage_audio": None},
        {"audio_strategy": "voiceover"},
        {"edit_format": "subtitled"},
    ],
)
def test_policy_drops_a_stray_voice_mode_instead_of_promising_it(update) -> None:
    from app.agents._schemas.creator_policy import repair_creator_voice_mode

    base = {
        "audio_strategy": "original_audio",
        "voice_mode": "continuous",
        "montage_audio": MontageAudioPlan(preserve_source_audio=True, source_media_ids=["m0"]),
    }
    repaired, notices = repair_creator_voice_mode(CreativeStrategy(**{**base, **update}))
    assert repaired.voice_mode is None
    assert notices == []  # a vestigial field is dropped silently (KRI-129)


def test_a_model_added_story_shape_does_not_demote_a_continuous_voice() -> None:
    """KRI-469's recorded strategy carried `archetype: day_vlog` next to the voice."""
    from app.agents._schemas.creator_policy import repair_creator_voice_mode

    strategy = CreativeStrategy(
        audio_strategy="original_audio",
        voice_mode="continuous",
        archetype="day_vlog",
        montage_audio=MontageAudioPlan(preserve_source_audio=True, source_media_ids=["m0"]),
    )
    repaired, notices = repair_creator_voice_mode(strategy)
    assert repaired.voice_mode == "continuous" and repaired.archetype is None
    assert len(notices) == 1 and "day-vlog" in notices[0]  # not silent: the creator is told


def test_the_cleared_story_shape_reaches_the_creator_through_the_plan_notices() -> None:
    from app.agents._schemas.creator_policy import VOICE_MODE_SHAPE_NOTICE
    from app.services import creator_capabilities
    from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest

    before = creator_capabilities.settings.guided_edit_capability_enabled
    creator_capabilities.settings.guided_edit_capability_enabled = True
    try:
        manifest = resolve_creator_manifest(
            item_id="item-1",
            edit_format="montage",
            media=[{"media_id": f"m{i}", "kind": "video", "duration_s": 12.0} for i in range(4)],
        )
        plan = compile_strategy_to_plan(
            manifest,
            CreativeStrategy(
                audio_strategy="original_audio",
                voice_mode="continuous",
                archetype="day_vlog",
                montage_audio=MontageAudioPlan(preserve_source_audio=True, source_media_ids=["m0"]),
            ),
        )
    finally:
        creator_capabilities.settings.guided_edit_capability_enabled = before
    assert plan.strategy.voice_mode == "continuous" and plan.strategy.archetype is None
    assert VOICE_MODE_SHAPE_NOTICE in plan.notices


def test_the_prompt_says_plainly_when_not_to_set_continuous() -> None:
    phone = MainCreatorAgent(None).render_prompt(_phone_input())  # type: ignore[arg-type]
    phone = " ".join(phone.split())
    for cue in (
        "Leaving it null is the safe default",
        'Do NOT set "continuous"',
        "pick the best quote, line or moment",
        "talking-head or subtitled edit",
        "the request is ambiguous",
        "Never invent a clip as the voice",
    ):
        assert cue in phone, cue

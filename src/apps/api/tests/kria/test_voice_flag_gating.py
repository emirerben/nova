"""KRI-479 review P2-5/P2-6: nothing advertises the voice-behind-footage shape unless it can render.

With `KRIA_PLAN_AUTHORITY_ENABLED` off (new jobs are unstamped, so the route never runs) or
`SPEECH_EXCERPT_MONTAGE_ENABLED` off (the camera-audio renderer is unavailable), the clarification
gate asks no voice question, `duration_vs_count` keeps its legacy exemption for `montage_audio`,
`voice_mode` is repaired away and the Main Creator prompt does not teach it.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.creator_agent import CapabilityAvailability, CreativeStrategy
from app.agents.main_creator import MainCreatorAgent
from app.config import settings
from app.services.choice_questions import ChoiceCapability, collect_conflicts
from tests.agents.test_main_creator_agent import _input, _manifest
from tests.kria.test_choice_conflict_gate import _brief, _gate, _order, _timing
from tests.kria.test_choice_voice_gate import _voice_plan, _voice_rows

FLAGS = ("kria_plan_authority_enabled", "speech_excerpt_montage_enabled")


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


def test_the_prompt_teaches_voice_mode_only_while_the_route_can_render(monkeypatch):
    assert "VOICE MODE" in MainCreatorAgent(None).render_prompt(_phone_input())  # type: ignore[arg-type]
    for flag in FLAGS:
        monkeypatch.setattr(settings, flag, False)
        prompt = MainCreatorAgent(None).render_prompt(_phone_input())  # type: ignore[arg-type]
        assert "VOICE MODE" not in prompt and "voice_mode" not in prompt, flag
        monkeypatch.setattr(settings, flag, True)


@pytest.mark.parametrize("flag", FLAGS)
def test_voice_mode_is_repaired_away_when_the_route_is_off(monkeypatch, flag):
    from app.services import creator_capabilities
    from app.services.creator_capabilities import compile_strategy_to_plan, resolve_creator_manifest

    monkeypatch.setattr(creator_capabilities.settings, "guided_edit_capability_enabled", True)
    manifest = resolve_creator_manifest(
        item_id="i",
        edit_format="montage",
        media=[{"media_id": f"m{i}", "kind": "video", "duration_s": 9.0} for i in range(3)],
    )
    strategy = CreativeStrategy(
        audio_strategy="original_audio",
        voice_mode="continuous",
        montage_audio={"preserve_source_audio": True, "source_media_ids": ["m0"]},
    )
    assert compile_strategy_to_plan(manifest, strategy).strategy.voice_mode == "continuous"
    monkeypatch.setattr(settings, flag, False)
    assert compile_strategy_to_plan(manifest, strategy).strategy.voice_mode is None


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", FLAGS)
async def test_no_voice_question_is_asked_when_the_route_is_off(monkeypatch, flag):
    rows, brief = _voice_rows(voice_s=20.0), _brief(_timing(30), _order())
    asked = await _gate(monkeypatch, _voice_plan(30), rows=rows, brief=brief)
    assert asked.plan.choice_question["kind"] == "voice_vs_duration"
    monkeypatch.setattr(settings, flag, False)
    quiet = await _gate(monkeypatch, _voice_plan(30), rows=rows, brief=brief)
    assert quiet.plan.mode == "act", quiet.plan.response


def test_the_collector_itself_needs_the_route_flag_and_keeps_the_legacy_exemption():
    strategy = CreativeStrategy(
        edit_format="montage",
        audio_strategy="original_audio",
        voice_mode="continuous",
        montage_audio={"preserve_source_audio": True, "source_media_ids": ["talk"]},
        target_duration_s=20,
        target_duration_requested=True,
    )
    snapshot = {"clip_assignments": _voice_rows(voice_s=60.0, pictures=30)}
    brief = _brief(_timing(20))
    off = collect_conflicts(strategy, brief, snapshot, ChoiceCapability())
    assert off == [], "route not available: legacy exemption for montage_audio, no voice question"
    on = collect_conflicts(strategy, brief, snapshot, ChoiceCapability(voice_route=True))
    assert [c.kind for c in on] == ["duration_vs_count"]

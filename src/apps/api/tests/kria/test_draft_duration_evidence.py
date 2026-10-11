"""A proposed strategy's target is never evidence of executed duration."""

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement, plan_facts_from_strategy


@pytest.mark.parametrize(
    ("edit_format", "audio_strategy"),
    [
        ("subtitled", "original_audio"),
        ("narrated", "voiceover"),
        ("montage", "original_audio"),
        ("montage", "user_song"),
    ],
)
def test_strategy_target_is_not_duration_evidence(edit_format, audio_strategy):
    requirement = BriefRequirement(
        id="duration",
        kind="timing",
        scope="global",
        description="make it 24 seconds",
        facts={"duration_s": 24},
    )

    facts = plan_facts_from_strategy(
        {
            "edit_format": edit_format,
            "audio_strategy": audio_strategy,
            "target_duration_s": 24,
        }
    )

    assert facts.duration_s is None
    receipt = check_requirement(requirement, facts)
    assert receipt.status == "partial"
    assert receipt.reason == "I can't confirm this draft's length yet."


def test_concrete_render_duration_within_tolerance_is_met():
    requirement = BriefRequirement(
        id="duration",
        kind="timing",
        scope="global",
        description="make it 24 seconds",
        facts={"duration_s": 24},
    )

    receipt = check_requirement(
        requirement,
        PlanFacts(edit_format="subtitled", audio_strategy="original_audio", duration_s=25.9),
    )

    assert receipt.status == "met"

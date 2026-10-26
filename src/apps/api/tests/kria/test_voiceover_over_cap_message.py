"""A voiceover longer than the 120 s edit cap must not blame the footage.

Prod 2026-10-26: a 150 s voiceover over 164 s of footage was told "the footage can provide
only 164 seconds without reuse. Add longer footage." The footage was long enough; the edit
cap (120 s) was the limit.
"""

from app.services.choice_questions import _source_coverage
from app.services.length_planning import build_length_plan


def _card(voiceover_s: float, footage_s: float):
    strategy = {"edit_format": "montage", "audio_strategy": "voiceover", "has_voiceover": True}
    media = [{"media_id": f"v{i}", "kind": "video", "duration_s": footage_s / 2} for i in range(2)]
    plan = build_length_plan(
        {**strategy, "voiceover_duration_s": voiceover_s},
        {"clip_assignments": media},
        selected_ids=["v0", "v1"],
    )
    card = _source_coverage(
        {**strategy, "voiceover_duration_s": voiceover_s},
        None,
        [],
        None,
        snapshot={"length_plan": plan},
    )
    return plan, card


def test_voiceover_over_the_cap_is_not_blamed_on_footage() -> None:
    _, card = _card(150.0, 164.0)

    assert card is not None
    assert "164" not in card.reason
    assert "120" in card.reason
    assert "shorter" in card.reason.lower()


def test_voiceover_that_footage_cannot_cover_still_asks_for_footage() -> None:
    _, card = _card(100.0, 60.0)

    assert card is not None
    assert "60 seconds" in card.reason
    assert "longer footage" in card.reason.lower()

"""KRI-1327 (Çiğdem S3, job 7416c94b): with three talking takes the draft said "Done: Show the
Nar picture full screen when I mention it" x4, the render skipped every lane
(``lanes_skipped: multiple_speakers``) and the review said "Have a look at these in the video".

Failure modes (written before the code):

R1  A draft where several clips may talk says "Done" for a photo on a word while the lane that
    places it is closed for several speakers.
R2  With the multi-take photo lane open, a full-screen photo is wrongly refused (or a corner
    card / closing photo, which still needs one speaker's frames, is promised).
R3  A one-clip Talking draft is touched.
R4  The finished video of a several-speaker render with no beat receipt says "have a look".
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    check_requirement,
    is_format_limit,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
)
from app.services import phone_rollout
from tests.kria.test_kri840_fullscreen_beat import _GRILL, _manifest

# S3's three takes, as the brief binding's media snapshot held them: every one heard speech.
_TAKES = ("takeB", "takeA", "takeC")
_SNAPSHOT = {
    "clip_assignments": [
        {
            "media_id": media_id,
            "kind": "video",
            "duration_s": seconds,
            "analysis": {"understanding": {"speech": {"has_speech": True}}},
        }
        for media_id, seconds in zip(_TAKES, (6.99, 20.1, 7.15), strict=True)
    ]
}
R3 = BriefRequirement(
    id="r3",
    kind="style",
    scope="global",
    description="Show the Nar picture full screen when I mention it",
    facts={"triggers": ["Nar"], "display_mode": "fullscreen"},
)


def _beat(display_mode: str = "fullscreen") -> dict:
    return {
        "beat_id": "nar",
        "trigger": "Nar",
        "visual_id": f"asset-{_GRILL}",  # a Visual the draft resolves
        "occurrence": "first",
        "visual_role": "photo",
        "display_mode": display_mode,
    }


def _facts(display_mode: str = "fullscreen", *, takes: int = 3):
    strategy = CreativeStrategy.model_validate(
        {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
            "reaction_beats": [_beat(display_mode)],
        }
    ).model_dump(mode="json", exclude_none=True)
    snapshot = {"clip_assignments": _SNAPSHOT["clip_assignments"][:takes]}
    return replace(
        plan_facts_from_strategy(strategy, manifest=_manifest()),
        phone_talking=True,
        media_snapshot=snapshot,
    )


@pytest.fixture
def lane(monkeypatch):
    def _set(open_: bool) -> None:
        monkeypatch.setattr(
            phone_rollout, "phone_talking_multi_speaker_beats_supported", lambda: open_
        )

    return _set


def test_three_takes_with_the_lane_closed_never_say_done(lane):
    lane(False)
    receipt = check_requirement(R3, _facts())
    assert receipt.status == "not_possible", receipt
    assert is_format_limit(receipt.reason)
    assert "several of your clips talk" in receipt.reason


def test_three_takes_with_the_lane_open_keep_the_full_screen_photo(lane):
    lane(True)
    assert check_requirement(R3, _facts()).status == "met"


def test_a_corner_card_with_three_takes_is_refused_even_with_the_lane_open(lane):
    lane(True)
    corner = R3.model_copy(update={"facts": {"triggers": ["Nar"], "display_mode": "pip"}})
    receipt = check_requirement(corner, _facts("pip"))
    assert receipt.status == "not_possible"
    assert "only show full screen" in receipt.reason


def test_one_take_is_untouched(lane):
    lane(False)
    assert check_requirement(R3, _facts(takes=1)).status == "met"


def test_the_review_of_a_several_speaker_render_without_photos_says_so():
    variant = {
        "ok": True,
        "render_status": "ready",
        "resolved_archetype": "subtitled",
        "phone_talking_multi_clip": {"version": 1, "lanes_skipped": "multiple_speakers"},
    }
    receipt = check_requirement(R3, plan_facts_from_phone_variant(variant))
    assert receipt.status == "not_possible", receipt

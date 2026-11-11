"""A refusal names the limit that actually failed, never photo/video timing nobody asked for.

A single phone video, montage, a hook request and its approval: approval readiness answered
"I can't keep that exact photo and video timing in this edit", though no timing was asked for.
"""

from __future__ import annotations

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import (
    MixedMediaTimingUnavailableError,
    PhoneFormatUnavailableError,
)
from app.kria.strategy_policy import (
    RefusedStrategy,
    _refusal_question,
    check_strategy_for_runtime_v2,
)
from app.schemas.edit_proposal import MixedMediaTimingProfile, recognize_mixed_media_timing
from app.services import creator_capabilities as capabilities

CLIP = "analysis-proxy-ios-hook-1.mp4"
HOOK_WORDS = "Add a hook that fits this clip\nUse this wording"
TIMING_WORDS = "photo and video timing"


def _montage(**update) -> CreativeStrategy:
    fields = {
        "edit_format": "montage",
        "render_program": "guided",
        "selected_media_ids": [CLIP],
        "opening_title": "A Long Way Here",
        "mixed_media_timing": recognize_mixed_media_timing(HOOK_WORDS),
    }
    return CreativeStrategy(**{**fields, **update})


def test_hook_words_ask_for_no_timing() -> None:
    assert recognize_mixed_media_timing(HOOK_WORDS) is None


def test_unverified_phone_clip_is_not_called_a_timing_problem(prod_profile) -> None:
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-hook",
        edit_format="montage",
        media=[{"media_id": CLIP, "kind": "video", "duration_s": 15.0}],
        phone_source_media_ids=[],
        phone_rendering_allowed=True,
    )

    checked = check_strategy_for_runtime_v2(
        manifest, _montage(), ask_before_simplifying=True, ask_about_stated_settings=True
    )

    assert isinstance(checked, RefusedStrategy)
    assert TIMING_WORDS not in checked.question
    assert checked.code != "mixed_media_timing_unavailable"


def test_phone_format_limit_is_not_called_a_timing_problem() -> None:
    refused = _refusal_question(
        PhoneFormatUnavailableError("phone sources require a supported edit format"), _montage()
    )

    assert TIMING_WORDS not in refused.question


def test_requested_timing_still_gets_the_timing_question() -> None:
    asked = _montage(
        mixed_media_timing=MixedMediaTimingProfile(
            image_hold="very_fast", video_hold="longer", boundary_style="cut"
        )
    )

    refused = _refusal_question(
        MixedMediaTimingUnavailableError("mixed-media timing requires the guided proposal"), asked
    )

    assert TIMING_WORDS in refused.question
    assert refused.code == "mixed_media_timing_unavailable"

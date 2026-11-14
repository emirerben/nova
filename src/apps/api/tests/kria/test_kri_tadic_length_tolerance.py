"""A length the creator never stated never blocks a video (prod thread aa36d140, 2026-11-14).

One 14.03 s clip, "add these lyrics". The Main Creator planned 14.005 s (the clip's own length,
no length in any creator message); the server's check made it ~14 s and counted the 0.005 s as
a lost creator ask, so every turn ended "Your clips add up to about 14 seconds, so the video
can't be 14.005 seconds. Should I make a simpler version?", and "Yes" replanned into it again.

Ways it could go wrong, written down before the code (each has a case below):

1. A model-chosen length a hair over the clips (14.005 on 14.03 s) still refuses.
2. A model-chosen length far over the clips (24 s default on 14 s) refuses instead of
   clamping to what the clips can run.
3. The clamped strategy is not a fixed point: approval readiness re-runs the same check on
   the drafted strategy and finds a change ("This renderer would change the proposed edit").
4. A stated length the clips really can't reach (20 s on 14 s) is now dropped silently.
5. A stated length within tolerance (14 s on 13.98 s) still refuses.
6. The tolerance is too wide: a stated 10 s cut to 9.4 s (more than max(0.5 s, 5 %)) passes.
7. Clips left out of a stated length keep their old message (which clips are in, and why).
8. The stated marker is trusted from the model: only the server stamp
   (``target_duration_requested`` / ``creator_duration_s``) makes a length stated.
"""

from __future__ import annotations

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import (
    CheckedStrategy,
    RefusedStrategy,
    check_strategy_for_runtime_v2,
)
from app.services import creator_capabilities as capabilities

CLIP = "analysis-proxy-ios-01.mp4"


def _manifest(durations: list[float]):
    clips = [f"analysis-proxy-ios-{n:02d}.mp4" for n in range(1, len(durations) + 1)]
    return capabilities.resolve_creator_manifest(
        item_id="item-tadic",
        edit_format="montage",
        media=[
            {"media_id": clip, "kind": "video", "duration_s": seconds}
            for clip, seconds in zip(clips, durations, strict=True)
        ],
        phone_source_media_ids=clips,
        phone_rendering_allowed=True,
    )


def _strategy(
    seconds: float, *, stated: float | None = None, selected: list[str] | None = None
) -> CreativeStrategy:
    return CreativeStrategy(
        edit_format="montage",
        audio_strategy="original_audio",
        selected_media_ids=selected if selected is not None else [CLIP],
        media_scope="selected",
        target_duration_s=seconds,
        video_reuse_policy="once",
        target_duration_requested=True if stated is not None else None,
        creator_duration_s=stated,
    )


def _check(manifest, strategy: CreativeStrategy, language: str = "en"):  # noqa: ANN001
    with reply_language_for(language):
        return check_strategy_for_runtime_v2(
            manifest, strategy, ask_before_simplifying=True, ask_about_stated_settings=True
        )


def _fixed_point(manifest, checked: CheckedStrategy) -> None:  # noqa: ANN001
    again = _check(manifest, checked.strategy)
    assert isinstance(again, CheckedStrategy), getattr(again, "question", again)
    assert again.strategy.model_dump(mode="json") == checked.strategy.model_dump(mode="json")


def test_the_incidents_model_chosen_length_passes(prod_profile) -> None:
    manifest = _manifest([14.033333])
    for language in ("en", "tr"):
        checked = _check(manifest, _strategy(14.005), language)
        assert isinstance(checked, CheckedStrategy), checked.question
        # The clip runs 14.005 s, so the whole-second cap does not cut it to 14.
        assert checked.strategy.target_duration_s == 14.005
        _fixed_point(manifest, checked)


def test_a_model_chosen_length_far_over_the_clips_clamps_silently(prod_profile) -> None:
    manifest = _manifest([14.033333])
    checked = _check(manifest, _strategy(24))
    assert isinstance(checked, CheckedStrategy), checked.question
    assert checked.strategy.target_duration_s <= 14.033333
    _fixed_point(manifest, checked)


def test_a_stated_length_the_clips_cant_reach_still_says_so(prod_profile) -> None:
    manifest = _manifest([14.033333])
    refused = _check(manifest, _strategy(20, stated=20))
    assert isinstance(refused, RefusedStrategy)
    assert refused.fields == ("target_duration_s",)
    assert refused.question == (
        "Your clips add up to about 14 seconds, so the video can't be 20 seconds. "
        "Should I make a simpler version?"
    )


def test_a_stated_length_within_tolerance_passes(prod_profile) -> None:
    manifest = _manifest([13.98])
    checked = _check(manifest, _strategy(14, stated=14))
    assert isinstance(checked, CheckedStrategy), checked.question
    _fixed_point(manifest, checked)


def test_a_stated_length_cut_past_the_tolerance_still_asks(prod_profile) -> None:
    # 0.6 s short of 10 s: over both 0.5 s and 5 %.
    manifest = _manifest([9.4])
    refused = _check(manifest, _strategy(10, stated=10))
    assert isinstance(refused, RefusedStrategy)
    assert refused.fields == ("target_duration_s",)


def test_clips_left_out_of_a_stated_length_keep_their_message(prod_profile) -> None:
    manifest = _manifest([4.0, 5.0, 6.0])
    refused = _check(manifest, _strategy(30, stated=30, selected=[CLIP]))
    assert isinstance(refused, RefusedStrategy)
    assert refused.question == (
        "Only clip 1 is in this edit and it runs about 4 seconds, so the video can't be "
        "30 seconds. Should I make a simpler version?"
    )
    assert refused.agreed == "I made it 4 seconds instead of 30."

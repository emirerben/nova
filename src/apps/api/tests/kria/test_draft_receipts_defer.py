"""KRI-190: a strategy draft is not judged on what the unified montage planner writes later."""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    UNIFIED_SETTLED_KINDS,
    defers_to_unified_montage,
    requirements_to_check_at_draft,
)

USER = uuid.uuid4()
PROXY = "users/u/creation-threads/t/analysis-proxy-ios-A.mp4"
CLOUD = "users/u/creation-threads/t/original-clip.mp4"


def _reqs() -> list[BriefRequirement]:
    return [
        BriefRequirement(id="r1", kind="text", scope="title", description="a title with the run"),
        BriefRequirement(id="r2", kind="text", scope="per_clip", description="place on each clip"),
        BriefRequirement(id="r3", kind="order", scope="global", description="as I filmed them"),
        BriefRequirement(id="r4", kind="timing", scope="global", description="fast but readable"),
        BriefRequirement(id="r5", kind="audio", scope="global", description="no music"),
        BriefRequirement(id="r6", kind="style", scope="global", description="warm tones"),
    ]


def _strategy(**overrides) -> dict:
    return {"edit_format": "montage", "audio_strategy": "original_audio", **overrides}


def _check(strategy=None, *, clips=(PROXY,), item_format="montage", user=USER):
    return [
        r.id
        for r in requirements_to_check_at_draft(
            _reqs(),
            creator_id=user,
            strategy=_strategy() if strategy is None else strategy,
            item_edit_format=item_format,
            clip_paths=clips,
        )
    ]


@pytest.fixture
def unified_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", True)
    monkeypatch.setattr(settings, "montage_unified_plan_user_ids", [])


def test_the_unified_planner_settles_text_order_and_timing_but_not_audio_or_style(unified_on):
    assert UNIFIED_SETTLED_KINDS == {"text", "order", "timing"}
    assert _check() == ["r5", "r6"]


def test_with_the_flag_off_every_requirement_is_still_checked_at_draft_time(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", False)
    monkeypatch.setattr(settings, "montage_unified_plan_user_ids", [])
    assert _check() == ["r1", "r2", "r3", "r4", "r5", "r6"]


@pytest.mark.parametrize("fmt", ["montage", "day_vlog", "single_hero"])
def test_a_phone_montage_family_draft_defers(unified_on, fmt):
    assert _check(_strategy(edit_format=fmt)) == ["r5", "r6"]


@pytest.mark.parametrize("fmt", ["subtitled", "narrated", "slides"])
def test_other_formats_are_judged_at_draft_time(unified_on, fmt):
    assert len(_check(_strategy(edit_format=fmt), item_format=fmt)) == 6


def test_the_item_format_is_used_when_the_strategy_names_none(unified_on):
    assert _check(_strategy(edit_format=None), item_format="montage") == ["r5", "r6"]
    assert len(_check(_strategy(edit_format=None), item_format="subtitled")) == 6


@pytest.mark.parametrize("audio", ["voiceover", "narration", "anything_else", None, ""])
def test_the_voiceover_lane_is_judged_because_the_worker_leaves_the_unified_planner(
    unified_on, audio
):
    """Approval maps only original_audio / licensed_music out of the voiceover lane; the
    worker then takes the plain lane, which builds no receipts, so a deferred requirement
    would be judged nowhere. Whether a voiceover is recorded yet does not matter."""
    assert len(_check(_strategy(audio_strategy=audio))) == 6


@pytest.mark.parametrize("audio", ["original_audio", "licensed_music"])
def test_both_non_voiceover_audio_strategies_defer_even_if_a_voiceover_exists(unified_on, audio):
    assert _check(_strategy(audio_strategy=audio)) == ["r5", "r6"]


def test_clips_that_are_not_phone_proxies_render_in_the_cloud_and_are_judged(unified_on):
    """No phone sources means no unified planner and no render-time receipts."""
    assert len(_check(clips=(CLOUD,))) == 6
    assert len(_check(clips=())) == 6
    assert _check(clips=(CLOUD, PROXY)) == ["r5", "r6"]


def test_an_account_the_phone_gate_excludes_is_judged(unified_on, monkeypatch):
    monkeypatch.setattr(settings, "phone_render_user_ids", [uuid.uuid4()])
    assert len(_check()) == 6
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    assert len(_check()) == 6


def test_the_allowlist_alone_turns_deferral_on_for_that_account(unified_on, monkeypatch):
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", False)
    monkeypatch.setattr(settings, "montage_unified_plan_user_ids", [str(USER)])
    assert _check() == ["r5", "r6"]
    assert len(_check(user=uuid.uuid4())) == 6


def test_a_missing_strategy_defers_nothing(unified_on):
    reqs = requirements_to_check_at_draft(
        _reqs(), creator_id=USER, strategy=None, item_edit_format="montage", clip_paths=(PROXY,)
    )
    assert len(reqs) == 6
    assert (
        defers_to_unified_montage(
            creator_id=USER, edit_format="montage", audio_strategy=None, clip_paths=(PROXY,)
        )
        is False
    )

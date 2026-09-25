"""KRI-190: a strategy draft is not judged on what the unified montage planner writes later."""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    UNIFIED_SETTLED_KINDS,
    defers_to_unified_montage,
    requirements_for_draft_receipts,
)

USER = uuid.uuid4()


def _reqs() -> list[BriefRequirement]:
    return [
        BriefRequirement(id="r1", kind="text", scope="title", description="a title with the run"),
        BriefRequirement(id="r2", kind="text", scope="per_clip", description="place on each clip"),
        BriefRequirement(id="r3", kind="order", scope="global", description="as I filmed them"),
        BriefRequirement(id="r4", kind="timing", scope="global", description="fast but readable"),
        BriefRequirement(id="r5", kind="audio", scope="global", description="no music"),
        BriefRequirement(id="r6", kind="style", scope="global", description="warm tones"),
    ]


@pytest.fixture
def unified_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", True)
    monkeypatch.setattr(settings, "montage_unified_plan_user_ids", [])


def test_the_unified_planner_settles_text_order_and_timing_but_not_audio_or_style():
    assert UNIFIED_SETTLED_KINDS == {"text", "order", "timing"}
    kept = requirements_for_draft_receipts(_reqs(), defers_to_render=True)
    assert [r.id for r in kept] == ["r5", "r6"]


def test_without_deferral_every_requirement_is_still_checked_at_draft_time():
    reqs = _reqs()
    assert requirements_for_draft_receipts(reqs, defers_to_render=False) == reqs


@pytest.mark.parametrize("edit_format", ["montage", "day_vlog", "single_hero"])
def test_a_phone_montage_family_draft_defers_when_the_unified_plan_is_on(unified_on, edit_format):
    assert defers_to_unified_montage(creator_id=USER, edit_format=edit_format) is True


@pytest.mark.parametrize("edit_format", ["subtitled", "narrated", "slides", "", None])
def test_other_formats_are_judged_at_draft_time(unified_on, edit_format):
    assert defers_to_unified_montage(creator_id=USER, edit_format=edit_format) is False


def test_a_voiceover_keeps_the_plain_lane_so_the_draft_is_judged(unified_on):
    assert (
        defers_to_unified_montage(creator_id=USER, edit_format="montage", has_voiceover=True)
        is False
    )


def test_flag_off_or_not_a_phone_account_is_the_unchanged_behaviour(
    unified_on, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", False)
    assert defers_to_unified_montage(creator_id=USER, edit_format="montage") is False
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", True)
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    assert defers_to_unified_montage(creator_id=USER, edit_format="montage") is False


def test_the_allowlist_alone_turns_deferral_on_for_that_account(
    unified_on, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", False)
    monkeypatch.setattr(settings, "montage_unified_plan_user_ids", [str(USER)])
    assert defers_to_unified_montage(creator_id=USER, edit_format="montage") is True
    assert defers_to_unified_montage(creator_id=uuid.uuid4(), edit_format="montage") is False

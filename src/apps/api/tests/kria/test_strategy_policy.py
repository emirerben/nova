"""KRI-142: runtime-v2 strategies go through the same server compile as v1.

Every case runs under the production flag profile against a phone manifest, the
shape the iOS app plans against.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.agents._schemas.creator_policy import GUIDED_VOICEOVER_EXECUTION_CONTRACT
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import PlanFacts, check_requirement, reply_from_receipts
from app.kria.strategy_policy import (
    CAPTIONS_KEPT_NOTICE,
    GUIDED_VOICEOVER_DOWNGRADE_NOTICE,
    CheckedStrategy,
    RefusedStrategy,
    check_strategy_for_runtime_v2,
)
from app.services import creator_capabilities as capabilities
from tests._prod_profile import PROD_NARRATION_IDENTITY

CLIPS = ["analysis-proxy-ios-1.mp4", "analysis-proxy-ios-2.mp4", "analysis-proxy-ios-3.mp4"]
PHOTOS = ["asset-photo-1", "asset-photo-2"]


def _talking_manifest():
    return capabilities.resolve_creator_manifest(
        item_id="item-talking",
        edit_format="subtitled",
        media=[{"media_id": CLIPS[0], "kind": "video", "duration_s": 40.0}],
        phone_source_media_ids=[CLIPS[0]],
        phone_rendering_allowed=True,
    )


def _narrated_manifest():
    return capabilities.resolve_creator_manifest(
        item_id="item-narrated",
        edit_format="narrated_planned",
        media=[{"media_id": media_id, "kind": "video"} for media_id in CLIPS]
        + [{"media_id": media_id, "kind": "image"} for media_id in PHOTOS],
        phone_source_media_ids=CLIPS,
        phone_rendering_allowed=True,
        has_voiceover=True,
        narration=dict(PROD_NARRATION_IDENTITY),
    )


def _talking(**update) -> CreativeStrategy:
    return CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=[CLIPS[0]],
        **update,
    )


def _narrated(**update) -> CreativeStrategy:
    return CreativeStrategy(
        edit_format="narrated_planned",
        audio_strategy="voiceover",
        render_program="native",
        selected_media_ids=CLIPS,
        **update,
    )


def test_plain_talking_strategy_passes_unchanged(prod_profile) -> None:
    checked = check_strategy_for_runtime_v2(_talking_manifest(), _talking(caption_style="karaoke"))

    assert isinstance(checked, CheckedStrategy)
    assert checked.notices == ()
    assert checked.strategy.caption_style == "karaoke"
    assert checked.strategy.selected_media_ids == [CLIPS[0]]


def test_title_on_talking_edit_is_asked_about_not_silently_dropped(prod_profile) -> None:
    refused = check_strategy_for_runtime_v2(_talking_manifest(), _talking(opening_title="Top 3"))

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "title_unavailable"
    assert "captions" in refused.question
    assert refused.question.endswith("?")


def test_title_on_phone_voiceover_edit_renders(prod_profile) -> None:
    """KRI-455: the phone voiceover compiler burns the title like the cloud."""
    checked = check_strategy_for_runtime_v2(
        _narrated_manifest(), _narrated(opening_title="Cacio e pepe in 10 minutes")
    )

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title == "Cacio e pepe in 10 minutes"


def test_title_on_phone_voiceover_edit_is_asked_about_when_switched_off(
    prod_profile, monkeypatch
) -> None:
    monkeypatch.setattr(capabilities.settings, "phone_narrated_title_enabled", False)

    refused = check_strategy_for_runtime_v2(
        _narrated_manifest(), _narrated(opening_title="Barcelona")
    )

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "title_unavailable"
    assert "iPhone" in refused.question


def test_title_on_cloud_voiceover_edit_still_renders(prod_profile) -> None:
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-narrated-cloud",
        edit_format="narrated_planned",
        media=[{"media_id": media_id, "kind": "video"} for media_id in CLIPS],
        has_voiceover=True,
        narration=dict(PROD_NARRATION_IDENTITY),
    )

    checked = check_strategy_for_runtime_v2(manifest, _narrated(opening_title="Barcelona"))

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title == "Barcelona"


@pytest.mark.parametrize(
    ("manifest_factory", "strategy_factory"),
    [(_talking_manifest, _talking), (_narrated_manifest, _narrated)],
)
def test_no_captions_is_repaired_with_a_notice(
    prod_profile, manifest_factory, strategy_factory
) -> None:
    checked = check_strategy_for_runtime_v2(
        manifest_factory(), strategy_factory(caption_style="none")
    )

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.caption_style == "auto"
    assert CAPTIONS_KEPT_NOTICE in checked.notices


def test_guided_voiceover_is_downgraded_to_the_native_voiceover_edit(prod_profile) -> None:
    """v2 dispatch always bypasses the guided gate, which refuses this contract
    with `proposal_replan_required` -- a render that could never start."""

    checked = check_strategy_for_runtime_v2(
        _narrated_manifest(),
        CreativeStrategy(
            edit_format="narrated_planned",
            audio_strategy="voiceover",
            media_scope="all",
            execution_contract=GUIDED_VOICEOVER_EXECUTION_CONTRACT,
            render_program="guided",
            selected_media_ids=[*CLIPS, *PHOTOS],
        ),
    )

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.execution_contract is None
    assert checked.strategy.render_program == "native"
    assert checked.strategy.selected_media_ids == CLIPS
    assert GUIDED_VOICEOVER_DOWNGRADE_NOTICE in checked.notices


def test_talking_length_receipt_says_the_whole_take_is_kept() -> None:
    req = BriefRequirement(
        id="len",
        kind="timing",
        scope="global",
        description="make it 20 seconds",
        facts={"duration_s": 20},
    )
    receipt = check_requirement(
        req, PlanFacts(clip_ids=(CLIPS[0],), duration_s=20.0, edit_format="subtitled")
    )

    assert receipt.status == "partial"
    assert "whole take" in (receipt.reason or "")


def test_receipts_reply_keeps_policy_notices_when_it_replaces_the_summary() -> None:
    req = BriefRequirement(
        id="len",
        kind="timing",
        scope="global",
        description="make it 20 seconds",
        facts={"duration_s": 20},
    )
    receipt = check_requirement(
        req, PlanFacts(clip_ids=(CLIPS[0],), duration_s=20.0, edit_format="subtitled")
    )

    reply = reply_from_receipts(
        CreativeBrief(version=1, requirements=[req]),
        [receipt],
        summary="Here is your edit. " + CAPTIONS_KEPT_NOTICE,
        notices=(CAPTIONS_KEPT_NOTICE,),
    )

    assert reply.startswith("Not everything you asked for made it in:")
    assert reply.endswith(CAPTIONS_KEPT_NOTICE)

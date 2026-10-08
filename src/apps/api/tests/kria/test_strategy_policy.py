"""KRI-142: runtime-v2 strategies go through the same server compile as v1.

Every case runs under the production flag profile against a phone manifest, the
shape the iOS app plans against.
"""

from __future__ import annotations

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, CreatorEditSnapshot
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


def test_title_on_phone_talking_edit_renders_for_its_confirmed_seconds(prod_profile) -> None:
    """KRI-467: the "3 sourdough mistakes" chat. The phone Talking compiler
    draws the title as an editable text row, held for the seconds the creator
    named, instead of asking to drop it."""
    checked = check_strategy_for_runtime_v2(
        _talking_manifest(),
        _talking(
            caption_style="karaoke",
            opening_title="3 sourdough mistakes",
            opening_title_duration_s=2.0,
        ),
    )

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.opening_title == "3 sourdough mistakes"
    assert checked.strategy.opening_title_duration_s == 2.0
    assert checked.strategy.caption_style == "karaoke"


def test_title_on_phone_talking_edit_is_asked_about_when_switched_off(
    prod_profile, monkeypatch
) -> None:
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_title_enabled", False)

    refused = check_strategy_for_runtime_v2(_talking_manifest(), _talking(opening_title="Top 3"))

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "title_unavailable"
    assert refused.question == (
        "Talking edits show your words as captions, so I can't add a title on "
        "top yet. Should I make it without the title?"
    )


def test_title_on_cloud_talking_edit_is_still_asked_about(prod_profile) -> None:
    """The cloud subtitled renderer has no title lane yet: ask, never drop."""
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-talking-cloud",
        edit_format="subtitled",
        media=[{"media_id": CLIPS[0], "kind": "video", "duration_s": 40.0}],
    )

    refused = check_strategy_for_runtime_v2(manifest, _talking(opening_title="Top 3"))

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


def _talking_visuals_manifest():
    return capabilities.resolve_creator_manifest(
        item_id="item-greek-yogurt",
        edit_format="subtitled",
        media=[{"media_id": CLIPS[0], "kind": "video", "duration_s": 32.5}]
        + [{"media_id": media_id, "kind": "image"} for media_id in PHOTOS],
        phone_source_media_ids=[CLIPS[0]],
        phone_rendering_allowed=True,
    )


def test_closing_text_on_phone_talking_edit_renders_with_the_rest(prod_profile) -> None:
    """KRI-514: the "Greek Yogurt & Smoothie Video" chat. "End on the toast
    photo with a 'MY PICK' badge" used to stop the whole edit -- photo pop-ins,
    sounds and all -- with "can't show your own text on each shot or at the
    end yet". The phone Talking text lane now draws it on the closing photo."""
    checked = check_strategy_for_runtime_v2(
        _talking_visuals_manifest(),
        _talking(
            reaction_beats=[
                {
                    "beat_id": "yogurt",
                    "trigger": "Greek yogurt",
                    "visual_id": PHOTOS[0],
                    "visual_role": "photo",
                    "sound": "pop",
                }
            ],
            closing_media={"visual_id": PHOTOS[1]},
            closing_title="MY PICK",
        ),
        ask_before_simplifying=True,
    )

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.closing_title == "MY PICK"
    assert checked.strategy.closing_media is not None
    assert checked.strategy.closing_media.visual_id == PHOTOS[1]
    assert [beat.beat_id for beat in checked.strategy.reaction_beats or []] == ["yogurt"]


_MY_PICK_QUESTION = (
    'This kind of edit can\'t show "MY PICK" at the end yet. Should I make everything '
    "else and leave that text out?"
)


def test_closing_text_on_phone_talking_edit_is_asked_about_when_switched_off(
    prod_profile, monkeypatch
) -> None:
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_closing_title_enabled", False)

    refused = check_strategy_for_runtime_v2(_talking_manifest(), _talking(closing_title="MY PICK"))

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "shot_text_unavailable"
    assert refused.question == _MY_PICK_QUESTION


def test_closing_text_on_cloud_talking_edit_names_the_text(prod_profile) -> None:
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-talking-cloud",
        edit_format="subtitled",
        media=[{"media_id": CLIPS[0], "kind": "video", "duration_s": 40.0}],
    )

    refused = check_strategy_for_runtime_v2(manifest, _talking(closing_title="MY PICK"))

    assert isinstance(refused, RefusedStrategy)
    assert refused.question == _MY_PICK_QUESTION


def test_shot_labels_are_still_asked_about_on_a_talking_edit(prod_profile) -> None:
    refused = check_strategy_for_runtime_v2(
        _talking_manifest(), _talking(shot_labels=["Day 1"], closing_title="MY PICK")
    )

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "shot_text_unavailable"
    assert refused.question == (
        "This kind of edit can't show your own text on each shot or at the end yet. "
        "Should I make everything else and leave that text out?"
    )


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


# --- KRI-519: photos timed to a phone voiceover ------------------------------


def test_phone_voiceover_keeps_photo_pop_ins_instead_of_asking(prod_profile) -> None:
    """Stress kit N3: "When I say the medal, show my medal photo" on an iPhone
    Voiceover edit is a reaction beat that survives the v2 check untouched."""
    manifest = _narrated_manifest()
    assert manifest.capabilities[capabilities.CAPABILITY_REACTION_BEATS].available is True

    checked = check_strategy_for_runtime_v2(
        manifest,
        _narrated(
            reaction_beats=[
                {"beat_id": "medal", "trigger": "the medal", "visual_id": PHOTOS[0]},
                {
                    "beat_id": "finish",
                    "trigger": "four hours and twelve minutes",
                    "visual_id": PHOTOS[1],
                },
            ]
        ),
        ask_before_simplifying=True,
        ask_about_stated_settings=True,
    )

    assert isinstance(checked, CheckedStrategy)
    assert [beat.visual_id for beat in checked.strategy.reaction_beats] == PHOTOS
    assert checked.notices == ()


def test_simplify_question_never_claims_a_draft_on_the_first_request(prod_profile) -> None:
    guided = CreativeStrategy(
        edit_format="narrated_planned",
        audio_strategy="voiceover",
        media_scope="all",
        execution_contract=GUIDED_VOICEOVER_EXECUTION_CONTRACT,
        render_program="guided",
        selected_media_ids=[*CLIPS, *PHOTOS],
    )

    first = check_strategy_for_runtime_v2(_narrated_manifest(), guided, ask_before_simplifying=True)
    assert isinstance(first, RefusedStrategy)
    assert "draft is unchanged" not in first.question
    assert first.question.endswith("Should I make a simpler version?")

    with_edit = _narrated_manifest().model_copy(
        update={"current_edit": CreatorEditSnapshot(revision=2, status="ready")}
    )
    later = check_strategy_for_runtime_v2(with_edit, guided, ask_before_simplifying=True)
    assert isinstance(later, RefusedStrategy)
    assert "Your current draft is unchanged. Should I make a simpler version?" in later.question


# --- KRI-521: "when I say X, show my video in the corner" on phone Talking ---

KADIKOY_TAKE = "analysis-proxy-ios-987424EF-621F-49BB-8A25-49B780205D08.mp4"
BREWING_VIDEO = "asset-d6f9c9fb-9196-4762-a73a-2a8843e0f23c"


def _kadikoy_manifest():
    """Prod thread 3598097e: one landscape take plus one ready Visuals video."""
    return capabilities.resolve_creator_manifest(
        item_id="item-kadikoy",
        edit_format="subtitled",
        media=[
            {"media_id": KADIKOY_TAKE, "kind": "video", "duration_s": 33.621667},
            {"media_id": BREWING_VIDEO, "kind": "video", "duration_s": 5.033333},
        ],
        catalog=[
            {
                "catalog_id": "657a4e2f14d14d8296008757856c3fbe",
                "kind": "sound_effect",
                "label": "Soft pop",
            }
        ],
        phone_source_media_ids=[KADIKOY_TAKE],
        phone_rendering_allowed=True,
    )


def _kadikoy_strategy(reaction_beats: list[dict] | None = None) -> CreativeStrategy:
    """The beats Main Creator v46 wrote for the T3 prompt (re-run 2026-10-08)."""
    return CreativeStrategy(
        edit_format="subtitled",
        audio_strategy="original_audio",
        render_program="native",
        selected_media_ids=[KADIKOY_TAKE],
        caption_style="editorial",
        optional_treatments=["overlays"],
        reaction_beats=reaction_beats
        or [
            {
                "beat_id": "kahve",
                "trigger": "kahve",
                "occurrence": "every",
                "sound": "657a4e2f14d14d8296008757856c3fbe",
            },
            {
                "beat_id": "ilk-durak",
                "trigger": "İlk durak",
                "visual_id": BREWING_VIDEO,
                "visual_role": "photo",
            },
        ],
    )


def test_video_beat_on_phone_talking_is_kept_not_a_simplify_question(prod_profile) -> None:
    manifest = _kadikoy_manifest()
    assert manifest.capabilities[capabilities.CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS].available

    checked = check_strategy_for_runtime_v2(
        manifest, _kadikoy_strategy(), ask_before_simplifying=True
    )

    assert isinstance(checked, CheckedStrategy)
    beats = {beat.beat_id: beat for beat in checked.strategy.reaction_beats or []}
    assert set(beats) == {"kahve", "ilk-durak"}
    assert beats["ilk-durak"].visual_id == BREWING_VIDEO
    assert not any("photo/sticker" in notice for notice in checked.notices)


def test_video_beat_without_video_cards_names_the_video_not_a_photo(
    prod_profile, monkeypatch
) -> None:
    monkeypatch.setattr(capabilities.settings, "phone_subtitled_video_overlays_enabled", False)
    manifest = _kadikoy_manifest()
    assert capabilities.CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS not in manifest.capabilities

    refused = check_strategy_for_runtime_v2(
        manifest, _kadikoy_strategy(), ask_before_simplifying=True
    )

    assert isinstance(refused, RefusedStrategy)
    assert refused.question.startswith(
        "Videos can't pop up on your words in this edit yet, so the video for "
        '"İlk durak" is left out.'
    )
    assert "photo/sticker" not in refused.question


def test_speaker_take_is_never_a_beat_video(prod_profile) -> None:
    """Only a Visuals video can pop in; the take itself never resolves."""
    strategy = _kadikoy_strategy(
        [{"beat_id": "self", "trigger": "İlk durak", "visual_id": KADIKOY_TAKE}]
    )

    checked = check_strategy_for_runtime_v2(_kadikoy_manifest(), strategy)

    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.reaction_beats is None
    assert any(
        'Couldn\'t find the photo or sticker for "İlk durak" in your Visuals' in notice
        for notice in checked.notices
    )

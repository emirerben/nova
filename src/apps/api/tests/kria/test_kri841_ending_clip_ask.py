"""KRI-841: "add the X video at the very end with its own sound" on a phone Talking edit.

Two prod-shaped kits (the creator's real footage, `tests/kria` replays the brief half):

* Cafe T2 (Turkish): "... En sona Atakan'in ilk isirik videosunu kendi sesiyle ekle." One
  English-speaking take (Caro, 24.6 s) plus three Visuals: two photos and the bite video
  (6.4 s, WITH sound). Expected: the take, then the bite at its own volume; the bite nowhere
  else.
* Don Julio T3 (English): "... At the very end, add the cheers video with its own sound."
  Three FOOTAGE clips (Lucas speaking, the overhead table, the cheers). Needs multi-clip
  Talking; with that flag off a 2nd/3rd footage clip is refused before approval.

What went wrong on main (probe: scratchpad map/brief_probe_841.py + render_ready_probe_841.py):
the ask had NO strategy field. A Visuals video `closing_media` was repaired away ("Couldn't
find the closing photo"); an `order` + `last_clip` requirement fell to `_check_order`'s
"I can't confirm the order this draft uses" (unchecked: "Have a look at these in the video");
a `closing` fact got the photo-flavoured "doesn't end the way you asked". Nothing could say
"Done", so a render that appended nothing (or appended it muted) read exactly like one that did.

Failure modes pinned here (written before the code, through the real checker chain):

 1. the strategy cannot carry the ask at all (no `ending_clip` field);
 2. the field leaks into the model-facing / Kria `apply_strategy` JSON schema, or changes the
    serialization / hash of every strategy that does not use it;
 3. the field is missing from the render-contract field matrix or the "placed media" set, so
    a clip exclusion ("skip the short clips") removes the very clip the edit ends on;
 4. a repair keeps an ending on an edit that cannot render it (montage, voiceover), on a
    photo, on an id that is not in the project, on the only clip, or (footage) while
    multi-clip Talking is off; and keeps the same clip ALSO playing mid-talk as a pop-in;
 5. the draft reply says "Done" for an ask the plan does not carry, or stays silent
    ("Have a look") instead of "Couldn't" before approval, in English or Turkish, whichever
    way the brief recorded it (new `ending_clip` fact, or the old `order` + `last_clip`);
 6. the draft is not held back ("a simpler version?") when the ending is missing;
 7. the finished video is called "Done" from the strategy alone, with no render receipt, or
    with a receipt that says the clip was dropped, or played muted;
 8. a montage `order` + `last_clip` ask changes verdict (ending checks are Talking-only);
 9. the Main Creator never learns the field (prompt), or learns it on an edit that cannot
    render it; the brief extractor never records it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from typing import Any

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    check_requirement,
    defers_caption_words_to_phone_render,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import CheckedStrategy, RefusedStrategy, check_strategy_for_runtime_v2
from app.services import creator_capabilities as capabilities

USER = uuid.uuid4()
FOOTAGE_A = "analysis-proxy-ios-A.mp4"  # Cafe: Caro's take / Don Julio: Lucas speaking
FOOTAGE_B = "analysis-proxy-ios-B.mp4"  # Don Julio: the overhead table (a muted cutaway)
FOOTAGE_C = "analysis-proxy-ios-C.mp4"  # Don Julio: the cheers video
BITE = "asset-bite"  # Cafe: Atakan's first bite, a Visuals video WITH sound
CAFE_PATHS = (f"users/u/creation-threads/t/{FOOTAGE_A}",)
JULIO_PATHS = tuple(
    f"users/u/creation-threads/t/{name}" for name in (FOOTAGE_A, FOOTAGE_B, FOOTAGE_C)
)


# ------------------------------------------------------------------ builders


def _req(rid: str, kind: str, description: str, **facts: Any) -> BriefRequirement:
    return BriefRequirement(id=rid, kind=kind, scope="global", description=description, facts=facts)


# The ask in the two shapes the brief ledger can hold it: the new structured record, and the
# old one (an ordering ask whose `last_clip` names the video) that KRI-841's prod runs got.
CAFE_TR = "En sona Atakan'ın ilk ısırık videosunu kendi sesiyle ekle"
CAFE_EN = "Add Atakan's first-bite video at the very end with its own sound"
JULIO_EN = "At the very end, add the cheers video with its own sound"


def _ending_fact_req(text: str, video: str) -> BriefRequirement:
    return _req("r4", "style", text, ending_clip=video, keep_audio=True)


def _old_way_req(text: str, video: str) -> BriefRequirement:
    return _req("r4", "order", text, last_clip=video)


CAFE_SHAPES = {
    "ending_clip fact (TR)": _ending_fact_req(CAFE_TR, "Atakan'ın ilk ısırık videosu"),
    "ending_clip fact (EN)": _ending_fact_req(CAFE_EN, "Atakan's first-bite video"),
    "order + last_clip (TR)": _old_way_req(CAFE_TR, "Atakan'ın ilk ısırık videosu"),
    "order + last_clip (EN)": _old_way_req(CAFE_EN, "Atakan's first-bite video"),
}
JULIO_SHAPES = {
    "ending_clip fact": _ending_fact_req(JULIO_EN, "the cheers video"),
    "order + last_clip": _old_way_req(JULIO_EN, "the cheers video"),
}

# Cafe T2's other asks (pop-ins on spoken words), exactly as the probe held them.
CAFE_OTHER = [
    _req(
        "r1",
        "style",
        "When she says 'Ecuador', show the Cotopaxi photo as a small card in the corner",
        triggers=["Ecuador"],
    ),
    _req(
        "r2",
        "style",
        "When she says 'traditional Colombian dessert', show the oblea photo full screen",
        triggers=["traditional Colombian dessert"],
    ),
]
CAFE_BEATS = [
    {
        "beat_id": "ecuador",
        "trigger": "Ecuador",
        "visual_id": "asset-cotopaxi",
        "visual_role": "photo",
    },
    {
        "beat_id": "dessert",
        "trigger": "traditional Colombian dessert",
        "visual_id": "asset-oblea",
        "visual_role": "photo",
    },
]


def _phone_settings(monkeypatch: pytest.MonkeyPatch, *, multi_clip: bool) -> None:
    from tests._prod_profile import apply_prod_profile

    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", multi_clip)


def _manifest(media: list[dict[str, Any]], *, edit_format: str = "subtitled"):
    own = [m["media_id"] for m in media if not m["media_id"].startswith("asset-")]
    return capabilities.resolve_creator_manifest(
        item_id="item-841",
        edit_format=edit_format,
        media=media,
        phone_source_media_ids=own,
        phone_rendering_allowed=True,
    )


def _cafe_manifest(edit_format: str = "subtitled"):
    return _manifest(
        [
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 24.6},
            {"media_id": "asset-cotopaxi", "kind": "image", "label": "T2_02_visual.jpg"},
            {"media_id": "asset-oblea", "kind": "image", "label": "T2_03_visual.jpg"},
            {"media_id": BITE, "kind": "video", "duration_s": 6.4, "label": "T2_04_visual.mp4"},
        ],
        edit_format=edit_format,
    )


def _julio_manifest():
    return _manifest(
        [
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9},
            {"media_id": FOOTAGE_B, "kind": "video", "duration_s": 2.0},
            {"media_id": FOOTAGE_C, "kind": "video", "duration_s": 3.4},
        ]
    )


def _strategy(manifest, **extra: Any) -> CreativeStrategy:
    own = [m.media_id for m in manifest.media if not m.media_id.startswith("asset-")]
    data = {
        "direction": "fast_montage",
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "render_program": "native",
        "target_duration_s": 24,
        "selected_media_ids": own,
        "rationale": "Keep the take whole and add what was asked.",
        **extra,
    }
    return CreativeStrategy.model_validate(data)


def _dispatched(manifest, strategy: CreativeStrategy) -> dict[str, Any]:
    """The strategy a draft carries: what the planner's policy check keeps (as the runtime does)."""
    checked = check_strategy_for_runtime_v2(manifest, strategy)
    assert isinstance(checked, CheckedStrategy), getattr(checked, "question", checked)
    return checked.strategy.model_dump(mode="json", exclude_none=True)


def _draft(requirements, strategy: dict[str, Any], manifest, clip_paths, notices=()):
    """What the runtime does with a phone Talking first draft (`tasks/kria_runtime.py`)."""
    kept = requirements_to_check_at_draft(
        requirements,
        creator_id=USER,
        strategy=strategy,
        item_edit_format="subtitled",
        clip_paths=clip_paths,
    )
    facts = plan_facts_from_strategy(strategy, manifest=manifest)
    talking_writer = defers_caption_words_to_phone_render(
        creator_id=USER,
        edit_format="subtitled",
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    )
    facts = dataclasses.replace(facts, phone_talking=talking_writer, caption_words_at_render=True)
    receipts = build_receipts(kept, facts, include_unchecked=True, strict_order=True)
    brief = CreativeBrief(version=1, requirements=kept)
    return receipts, reply_from_receipts(
        brief, receipts, summary="Here's your draft.", notices=notices
    )


def _by_id(receipts):
    return {receipt.requirement_id: receipt for receipt in receipts}


# ------------------------------------------------- 1-3. schema, registrations, placed media


def test_the_strategy_carries_an_ending_clip_and_round_trips_it():
    from app.agents._schemas.reaction_beats import EndingClip

    strategy = CreativeStrategy(
        edit_format="subtitled", ending_clip={"media_id": BITE, "keep_audio": True}
    )
    assert isinstance(strategy.ending_clip, EndingClip)
    dumped = strategy.model_dump(mode="json", exclude_none=True)
    assert dumped["ending_clip"] == {"media_id": BITE, "keep_audio": True}
    assert CreativeStrategy.model_validate(dumped) == strategy
    with pytest.raises(ValueError):
        EndingClip(media_id="")
    with pytest.raises(ValueError):
        EndingClip(media_id=BITE, volume=0.5)  # extra keys are forbidden


# A strategy a prod thread stored before KRI-841 and the sha256 of its canonical JSON on main.
_LEGACY_STRATEGY = {
    "direction": "fast_montage",
    "edit_format": "subtitled",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "target_duration_s": 24,
    "selected_media_ids": [FOOTAGE_A],
    "rationale": "Keep the take whole.",
    "reaction_beats": [{"beat_id": "ecuador", "trigger": "Ecuador", "visual_id": "asset-cotopaxi"}],
    "closing_media": {"visual_id": "asset-oblea"},
}


# Computed once on main (004f38182) from `_LEGACY_STRATEGY`; any drift = hash instability.
_LEGACY_STRATEGY_SHA256 = "57d6b457f2981cd1fb48d6f6c475b2c9e753c01a1212ac4537b11b3ca9148607"


def test_a_strategy_without_an_ending_clip_serializes_and_hashes_exactly_as_before():
    strategy = CreativeStrategy.model_validate(_LEGACY_STRATEGY)
    for dump in (
        strategy.model_dump(mode="json"),
        strategy.model_dump(mode="json", exclude_none=True),
        json.loads(strategy.model_dump_json()),
    ):
        assert "ending_clip" not in dump
    digest = hashlib.sha256(
        json.dumps(
            strategy.model_dump(mode="json", exclude_none=True), sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
    assert digest == _LEGACY_STRATEGY_SHA256


def test_the_ending_clip_stays_out_of_every_model_facing_and_tool_schema():
    from app.kria.registry import ApplyStrategyArguments

    for schema in (
        CreativeStrategy.model_json_schema(),
        ApplyStrategyArguments.model_json_schema(),
    ):
        text = json.dumps(schema)
        assert "ending_clip" not in text
        assert "EndingClip" not in text


def test_the_wire_schema_still_lets_the_model_author_it():
    from app.agents._creator_wire import MODEL_HIDDEN, WireContract, main_creator_schema

    assert "ending_clip" in MODEL_HIDDEN
    contract = WireContract(main_creator_schema())
    strategy = contract.source["$defs"]["CreativeStrategy"]["properties"]
    assert "ending_clip" in strategy


def test_the_render_contract_matrix_classifies_every_ending_clip_path():
    from app.services.creator_render_contract import FIELD_MATRIX

    for path in ("ending_clip", "ending_clip.media_id", "ending_clip.keep_audio"):
        assert path in FIELD_MATRIX, path


def test_a_clip_the_edit_ends_on_counts_as_placed_so_exclusions_never_remove_it():
    from app.kria.clip_scope import placed_media

    strategy = CreativeStrategy(
        edit_format="subtitled", ending_clip={"media_id": FOOTAGE_C, "keep_audio": True}
    )
    assert FOOTAGE_C in placed_media(strategy)


# ----------------------------------------------------------------- 4. the server repair


def _repair(manifest, strategy: CreativeStrategy):
    return capabilities.repair_creator_ending_clip(manifest, strategy)


def test_a_visuals_video_is_kept_on_a_talking_edit_with_its_canonical_id(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    # The model may name the label instead of the id; the dispatched strategy carries the id.
    strategy = _strategy(manifest, ending_clip={"media_id": "t2_04_visual.mp4"})

    repaired, notices = _repair(manifest, strategy)

    assert repaired.ending_clip is not None
    assert repaired.ending_clip.media_id == BITE
    assert repaired.ending_clip.keep_audio is True
    assert notices == []


@pytest.mark.parametrize(
    ("media_id", "why"),
    [
        ("asset-oblea", "photo"),  # a photo has no sound: it is a closing photo, not this
        ("asset-does-not-exist", "unknown"),
    ],
)
def test_an_ending_that_is_not_a_video_in_the_project_is_dropped_with_a_notice(
    monkeypatch, media_id, why
):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    strategy = _strategy(manifest, ending_clip={"media_id": media_id})

    repaired, notices = _repair(manifest, strategy)

    assert repaired.ending_clip is None, why
    assert len(notices) == 1
    assert notices[0].startswith("Couldn't")


def test_an_ending_on_a_montage_edit_is_dropped(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest(edit_format="montage")
    strategy = _strategy(manifest, edit_format="montage", ending_clip={"media_id": BITE})

    repaired, notices = _repair(manifest, strategy)

    assert repaired.ending_clip is None
    assert notices and notices[0].startswith("Couldn't add")


def test_an_ending_on_a_voiceover_edit_is_dropped(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-841",
        edit_format="narrated",
        media=[
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 20.0},
            {"media_id": BITE, "kind": "video", "duration_s": 6.4},
        ],
        phone_source_media_ids=[FOOTAGE_A],
        phone_rendering_allowed=True,
        has_voiceover=True,
    )
    strategy = _strategy(
        manifest, edit_format="narrated", audio_strategy="voiceover", ending_clip={"media_id": BITE}
    )

    repaired, notices = _repair(manifest, strategy)

    assert repaired.ending_clip is None
    assert notices and notices[0].startswith("Couldn't add")


def test_a_footage_clip_ending_needs_multi_clip_talking_and_a_second_clip(monkeypatch):
    # Flag ON, three clips: the cheers clip (a footage clip that is not the first) can end it.
    _phone_settings(monkeypatch, multi_clip=True)
    manifest = _julio_manifest()
    kept, notices = _repair(manifest, _strategy(manifest, ending_clip={"media_id": FOOTAGE_C}))
    assert kept.ending_clip is not None and kept.ending_clip.media_id == FOOTAGE_C
    assert notices == []

    # Flag ON, but it is the project's only clip: nothing to end the talk on.
    solo = _manifest([{"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9}])
    dropped, notices = _repair(solo, _strategy(solo, ending_clip={"media_id": FOOTAGE_A}))
    assert dropped.ending_clip is None
    assert notices and notices[0].startswith("Couldn't add")

    # Flag OFF: another footage clip cannot be appended (and a 2nd footage clip is refused anyway).
    _phone_settings(monkeypatch, multi_clip=False)
    duo = _manifest(
        [
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9},
            {"media_id": FOOTAGE_C, "kind": "video", "duration_s": 3.4},
        ]
    )
    off, notices = _repair(duo, _strategy(duo, ending_clip={"media_id": FOOTAGE_C}))
    assert off.ending_clip is None
    assert notices and notices[0].startswith("Couldn't add")


def test_the_same_clip_is_never_also_a_mid_talk_pop_in(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    strategy = _strategy(
        manifest,
        reaction_beats=[
            *CAFE_BEATS,
            # The bite as a pip card on a spoken word, and as a card WITH a sound.
            {"beat_id": "bite-card", "trigger": "Colombian", "visual_id": BITE},
            {"beat_id": "bite-ding", "trigger": "That's it", "visual_id": BITE, "sound": "ding"},
        ],
        ending_clip={"media_id": BITE},
    )

    repaired, notices = _repair(manifest, strategy)

    assert repaired.ending_clip is not None
    beats = {beat.beat_id: beat for beat in repaired.reaction_beats or ()}
    assert set(beats) == {"ecuador", "dessert", "bite-ding"}
    assert beats["bite-ding"].visual_id is None and beats["bite-ding"].sound == "ding"
    assert all(beat.visual_id != BITE for beat in beats.values())
    assert capabilities.ENDING_CLIP_NOT_ALSO_A_POP_IN_NOTICE in notices


def test_compile_strategy_to_plan_dispatches_the_repaired_ending(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()

    kept = capabilities.compile_strategy_to_plan(
        manifest, _strategy(manifest, ending_clip={"media_id": "T2_04_VISUAL.MP4"})
    )
    assert kept.strategy.ending_clip is not None
    assert kept.strategy.ending_clip.media_id == BITE

    dropped = capabilities.compile_strategy_to_plan(
        manifest, _strategy(manifest, ending_clip={"media_id": "asset-oblea"})
    )
    assert dropped.strategy.ending_clip is None
    assert any(n.startswith("Couldn't add") for n in dropped.notices)


# --------------------------------------------- 5-6. the draft reply (before approval)


@pytest.mark.parametrize("shape", sorted(CAFE_SHAPES))
def test_cafe_t2_draft_says_done_when_the_plan_ends_on_the_bite(monkeypatch, shape):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    strategy = _dispatched(
        manifest,
        _strategy(manifest, reaction_beats=CAFE_BEATS, ending_clip={"media_id": BITE}),
    )
    assert strategy["ending_clip"] == {"media_id": BITE, "keep_audio": True}
    ask = CAFE_SHAPES[shape]

    language = "tr" if "(TR)" in shape else "en"
    with reply_language_for(language):
        receipts, reply = _draft([*CAFE_OTHER, ask], strategy, manifest, CAFE_PATHS)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "met", receipt.reason
    assert receipt.verification == "checked"
    assert not needs_creator_choice(receipt)
    assert [r.requirement_id for r in receipts if needs_creator_choice(r)] == []
    assert "Have a look" not in reply and "Bunlara videoda" not in reply
    assert ask.description in reply


@pytest.mark.parametrize("shape", sorted(k for k in CAFE_SHAPES if k.startswith("ending_clip")))
def test_cafe_t2_draft_says_couldnt_and_holds_when_the_plan_has_no_ending(monkeypatch, shape):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    # The planner never set it (or the repair dropped it): the draft has no ending video.
    strategy = _dispatched(manifest, _strategy(manifest, reaction_beats=CAFE_BEATS))
    assert "ending_clip" not in strategy
    ask = CAFE_SHAPES[shape]

    language = "tr" if "(TR)" in shape else "en"
    with reply_language_for(language):
        receipts, reply = _draft([*CAFE_OTHER, ask], strategy, manifest, CAFE_PATHS)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "not_possible"
    assert receipt.verification == "checked"
    assert needs_creator_choice(receipt)  # the first draft is held back for the creator's choice
    label = "Yapamadım" if language == "tr" else "Couldn't"
    assert f"{label}: {ask.description}" in reply
    assert "Have a look" not in reply
    # It names the video, never the closing-photo wording the old check borrowed.
    assert "video" in (receipt.reason or "").lower()
    assert "photo" not in (receipt.reason or "").lower()


def test_a_repaired_away_ending_is_a_couldnt_that_says_why(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    # The model pointed at the oblea PHOTO: no sound to keep, the repair drops it.
    raw = _strategy(manifest, reaction_beats=CAFE_BEATS, ending_clip={"media_id": "asset-oblea"})
    ask = CAFE_SHAPES["ending_clip fact (EN)"]

    # The chain a draft really goes through is check_strategy -> facts; run the facts on the
    # model's raw strategy too (brief checks repair it again, exactly as approval will).
    receipts, reply = _draft(
        [ask], raw.model_dump(mode="json", exclude_none=True), manifest, CAFE_PATHS
    )

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "not_possible"
    assert needs_creator_choice(receipt)
    assert "photo" in (receipt.reason or "").lower()
    assert "Couldn't" in reply


def test_a_muted_ending_is_not_what_was_asked_for(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    strategy = _dispatched(
        manifest, _strategy(manifest, ending_clip={"media_id": BITE, "keep_audio": False})
    )
    ask = CAFE_SHAPES["ending_clip fact (EN)"]  # asked WITH its own sound

    receipts, reply = _draft([ask], strategy, manifest, CAFE_PATHS)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "not_possible"
    assert "sound" in (receipt.reason or "").lower()


def test_a_silent_ending_asked_silent_is_met(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    strategy = _dispatched(
        manifest, _strategy(manifest, ending_clip={"media_id": BITE, "keep_audio": False})
    )
    ask = _req(
        "r4",
        "style",
        "Add the bite video at the end, muted",
        ending_clip="the bite video",
        keep_audio=False,
    )

    receipts, _ = _draft([ask], strategy, manifest, CAFE_PATHS)

    assert _by_id(receipts)["r4"].status == "met"


@pytest.mark.parametrize("shape", sorted(JULIO_SHAPES))
def test_don_julio_t3_flag_on_a_footage_ending_is_done_at_draft(monkeypatch, shape):
    _phone_settings(monkeypatch, multi_clip=True)
    manifest = _julio_manifest()
    strategy = _dispatched(manifest, _strategy(manifest, ending_clip={"media_id": FOOTAGE_C}))
    assert strategy["ending_clip"]["media_id"] == FOOTAGE_C
    # The overhead clip stays a muted cutaway inside Lucas: only the ending clip is excluded
    # from the pop-ins, never the other clips from the edit.
    assert FOOTAGE_B in strategy["selected_media_ids"]
    ask = JULIO_SHAPES[shape]

    receipts, reply = _draft([ask], strategy, manifest, JULIO_PATHS)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "met", receipt.reason
    assert [r.requirement_id for r in receipts if needs_creator_choice(r)] == []


def test_don_julio_t3_flag_off_is_refused_before_approval(monkeypatch):
    """A 2nd/3rd footage clip is not accepted while multi-clip Talking is off: the honest
    refusal stays, and an ending appended as a footage clip is never silently kept."""
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _julio_manifest()

    checked = check_strategy_for_runtime_v2(
        manifest, _strategy(manifest, ending_clip={"media_id": FOOTAGE_C})
    )

    assert isinstance(checked, RefusedStrategy)
    assert "?" in checked.question  # the creator is asked, before any draft exists


def test_don_julio_flag_off_the_cheers_uploaded_as_a_visual_works_like_the_cafe(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _manifest(
        [
            {"media_id": FOOTAGE_A, "kind": "video", "duration_s": 12.9},
            {"media_id": "asset-cheers", "kind": "video", "duration_s": 3.4, "label": "cheers.mp4"},
        ]
    )
    strategy = _dispatched(manifest, _strategy(manifest, ending_clip={"media_id": "asset-cheers"}))
    ask = JULIO_SHAPES["ending_clip fact"]

    receipts, _ = _draft([ask], strategy, manifest, (JULIO_PATHS[0],))

    assert _by_id(receipts)["r4"].status == "met"


# ------------------------------------------------- 7. the finished video (render-ready)


def _cue(i: int, start: float, end: float, text: str) -> dict[str, Any]:
    return {
        "id": f"c{i}",
        "start_s": start,
        "end_s": end,
        "text": text,
        "words": [{"text": text, "start_s": start, "end_s": end}],
    }


def _variant(ending: dict[str, Any] | None) -> dict[str, Any]:
    """A finished phone Talking variant, in the shape `_run_phone_subtitled_job` persists."""
    variant: dict[str, Any] = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_status": "awaiting_device",
        "duration_s": 31.0,
        "caption_cues": [_cue(0, 0.2, 2.2, "Hi, I'm Karolina"), _cue(1, 22.7, 24.4, "That's it.")],
        "voiceover_caption_style": "sentence",
        "caption_language": "en",
        "phone_lane_receipt": {"applied": [], "dropped": []},
    }
    if ending is not None:
        variant["phone_ending_clip"] = ending
    return variant


def _receipt_row(**overrides: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "requested_media_id": BITE,
        "source": "visual",
        "status": "applied",
        "reason": None,
        "keep_audio": True,
        "volume": 1.0,
        "start_s": 24.6,
        "end_s": 31.0,
        "recipe_duration_s": 31.0,
        **overrides,
    }


def _render_ready(variant: dict[str, Any], ask: BriefRequirement):
    facts = plan_facts_from_phone_variant(variant)
    brief = CreativeBrief(version=1, requirements=[ask])
    receipts = build_receipts(brief.live(), facts, include_unchecked=True)
    return receipts, reply_from_receipts(brief, receipts, summary="Your video is ready.")


@pytest.mark.parametrize("shape", sorted(CAFE_SHAPES))
def test_render_ready_is_done_only_when_the_render_ended_on_the_clip_with_sound(shape):
    ask = CAFE_SHAPES[shape]
    language = "tr" if "(TR)" in shape else "en"

    with reply_language_for(language):
        receipts, reply = _render_ready(_variant(_receipt_row()), ask)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "met", receipt.reason
    assert receipt.verification == "checked"
    assert "Have a look" not in reply and "Bunlara videoda" not in reply
    assert ask.description in reply


@pytest.mark.parametrize("shape", sorted(CAFE_SHAPES))
def test_render_ready_is_a_couldnt_when_the_worker_dropped_the_ending(shape):
    ask = CAFE_SHAPES[shape]
    language = "tr" if "(TR)" in shape else "en"
    dropped = _receipt_row(
        status="dropped", reason="visual_videos_not_verified", volume=0.0, start_s=None, end_s=None
    )

    with reply_language_for(language):
        receipts, reply = _render_ready(_variant(dropped), ask)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "not_possible"
    assert receipt.verification == "checked"
    label = "Yapamadım" if language == "tr" else "Couldn't"
    assert f"{label}: {ask.description}" in reply
    assert "visual_videos_not_verified" not in reply  # plain words, never the machine code


def test_render_ready_negative_control_applied_but_muted_is_not_met():
    ask = CAFE_SHAPES["ending_clip fact (EN)"]  # asked with its own sound

    receipts, reply = _render_ready(_variant(_receipt_row(volume=0.0)), ask)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "not_possible"
    assert "sound" in (receipt.reason or "").lower()
    assert "Couldn't" in reply


def test_render_ready_negative_control_no_receipt_is_never_met():
    ask = CAFE_SHAPES["ending_clip fact (EN)"]

    receipts, reply = _render_ready(_variant(None), ask)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status != "met"
    assert "Couldn't" in reply or "Have a look" in reply


def test_render_ready_silent_ending_asked_silent_is_met():
    ask = _req(
        "r4", "style", "Add the bite at the end, muted", ending_clip="the bite", keep_audio=False
    )

    receipts, _ = _render_ready(_variant(_receipt_row(keep_audio=False, volume=0.0)), ask)

    assert _by_id(receipts)["r4"].status == "met"


# ---------------------------------------------------------- 8. montage controls


def test_a_montage_order_ask_still_goes_through_the_order_check():
    from app.kria.brief_checks import PlanFacts, _check_order

    ask = _old_way_req("End on the short arrival clip", "the short clip where we arrive")
    for facts in (
        PlanFacts(edit_format="montage", video_clip_count=4),
        PlanFacts(edit_format="montage", video_clip_count=4, phone_talking=False),
    ):
        assert check_requirement(ask, facts) == _check_order(ask, facts)


def test_a_talking_edit_with_one_clip_keeps_the_old_unchecked_answer_for_last_clip():
    from app.kria.brief_checks import PlanFacts, _check_order

    ask = _old_way_req("End on the part where I say goodbye", "the part where I say goodbye")
    facts = PlanFacts(edit_format="subtitled", phone_talking=True, video_clip_count=1)

    assert check_requirement(ask, facts) == _check_order(ask, facts)


def test_a_wording_only_change_keeps_the_ending_clip_ask():
    from app.kria.brief import BriefUpdate, apply_updates

    brief = apply_updates(
        None,
        [
            BriefUpdate(
                kind="style",
                scope="global",
                description=CAFE_EN,
                facts={"ending_clip": "Atakan's first-bite video", "keep_audio": True},
            )
        ],
        source_turn_id="t1",
    )
    brief = brief.model_copy(update={"version": 1})
    changed = apply_updates(
        brief,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id=brief.live()[0].id,
                expected_version=1,
                kind="style",
                scope="global",
                description="Add the bite video after the talk, with its own sound",
            )
        ],
        source_turn_id="t2",
    )

    facts = changed.live()[0].facts
    assert facts["ending_clip"] == "Atakan's first-bite video" and facts["keep_audio"] is True


# ------------------------------------------------------- 9. prompts and the extractor


def test_prompt_version_is_bumped_for_the_new_field():
    from app.agents.main_creator import MAIN_CREATOR_PROMPT_VERSION

    assert MAIN_CREATOR_PROMPT_VERSION == "2026-10-31-v107"


def _render_prompt(manifest) -> str:
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput

    agent = MainCreatorAgent(None)
    return agent.render_prompt(
        MainCreatorInput(
            user_message="En sona Atakan'ın ilk ısırık videosunu kendi sesiyle ekle",
            media_context=[{"media_id": BITE, "kind": "video", "filename": "T2_04_visual.mp4"}],
            capability_manifest=manifest,
        )
    )


def test_the_prompt_teaches_ending_clip_on_a_phone_talking_edit_only(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)

    talking = _render_prompt(_cafe_manifest())
    assert "ending_clip" in talking
    assert "keep_audio" in talking
    assert "closing_media" in talking  # photos held over the last seconds stay where they were

    montage = _render_prompt(_cafe_manifest(edit_format="montage"))
    assert "ending_clip" not in montage


def test_the_brief_extractor_records_an_appended_video_as_an_ending_clip_fact():
    from app.agents._schemas.brief_extractor import BriefExtractionInput
    from app.agents.brief_extractor import BriefExtractorAgent
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION

    assert '"ending_clip"' in _BRIEF_PROMPT_SECTION
    assert '"keep_audio"' in _BRIEF_PROMPT_SECTION
    # It is a `style` record, never an ordering ask: `last_clip` on a Talking edit makes the
    # render contract demand an order it cannot verify ("I couldn't tell which clip ... means").
    assert "record it as a `style` requirement with" in _BRIEF_PROMPT_SECTION
    # ... and "end on clip X" orderings keep their `last_clip` record (montage is unchanged).
    assert "last_clip" in _BRIEF_PROMPT_SECTION
    narrow = BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(creator_request="", user_message="x", conversation=[])
    )
    assert '"ending_clip"' in narrow


# ------------------------------- 10. the gates the phone worker applies, said BEFORE approval
#
# `_run_phone_subtitled_job` drops a Visuals ending when media lanes are off
# (`media_lanes_disabled`), when `visualVideos` or `audioMix` is not verified, and a footage
# ending when `audioMix` is not verified or the ask is muted (`muted_footage_ending_unsupported`).
# A draft that only mirrored some of those said "Done" and the finished video "Couldn't".
# The repair, the prompt gate and the draft reply must close the same gates.


def _turn_off(monkeypatch: pytest.MonkeyPatch, gate: str) -> None:
    if gate == "media_lanes":
        monkeypatch.setattr(settings, "phone_subtitled_media_lanes_enabled", False)
        return
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in settings.phone_render_verified_features if f != gate],
    )


GATES = ["media_lanes", "visualVideos", "audioMix"]
# A creator can read each reason; none of them is the machine code.
GATE_WORDS = {
    "media_lanes": "pictures and videos",
    "visualVideos": "videos from Visuals",
    "audioMix": "mix its sound",
}


@pytest.mark.parametrize("gate", GATES)
def test_a_visuals_ending_is_dropped_before_approval_when_the_worker_would_drop_it(
    monkeypatch, gate
):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    strategy = _strategy(manifest, ending_clip={"media_id": BITE})
    # Every gate on: kept (the control).
    kept, notices = _repair(manifest, strategy)
    assert kept.ending_clip is not None and notices == []

    _turn_off(monkeypatch, gate)
    repaired, notices = _repair(manifest, strategy)

    assert repaired.ending_clip is None
    assert len(notices) == 1 and notices[0].startswith("Couldn't add")
    assert GATE_WORDS[gate] in notices[0]
    assert capabilities.ending_clip_problem(manifest, strategy) is not None


@pytest.mark.parametrize("gate", GATES)
def test_the_draft_says_couldnt_before_approval_when_a_gate_is_closed(monkeypatch, gate):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    _turn_off(monkeypatch, gate)
    checked = check_strategy_for_runtime_v2(
        manifest, _strategy(manifest, reaction_beats=CAFE_BEATS, ending_clip={"media_id": BITE})
    )
    assert isinstance(checked, CheckedStrategy)
    dispatched = checked.strategy.model_dump(mode="json", exclude_none=True)
    assert "ending_clip" not in dispatched  # the worker never sees an ending it would drop
    ask = CAFE_SHAPES["ending_clip fact (EN)"]

    receipts, reply = _draft([ask], dispatched, manifest, CAFE_PATHS, notices=list(checked.notices))

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "not_possible" and needs_creator_choice(receipt)
    assert f"Couldn't: {ask.description}" in reply
    assert GATE_WORDS[gate] in reply  # the plain reason reaches the creator, not a bare "no"


@pytest.mark.parametrize("gate", GATES)
def test_the_prompt_stops_teaching_the_ending_when_a_gate_is_closed(monkeypatch, gate):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    assert capabilities.ending_clip_candidates(manifest)
    assert "ending_clip" in _render_prompt(manifest)

    _turn_off(monkeypatch, gate)

    assert capabilities.ending_clip_candidates(manifest) == []
    assert "ending_clip" not in _render_prompt(manifest)


@pytest.mark.parametrize("gate", ["audioMix"])
def test_a_footage_ending_needs_the_audio_mix_too(monkeypatch, gate):
    _phone_settings(monkeypatch, multi_clip=True)
    manifest = _julio_manifest()
    strategy = _strategy(manifest, ending_clip={"media_id": FOOTAGE_C})
    assert _repair(manifest, strategy)[0].ending_clip is not None

    _turn_off(monkeypatch, gate)
    repaired, notices = _repair(manifest, strategy)

    assert repaired.ending_clip is None
    assert notices and GATE_WORDS[gate] in notices[0]
    assert capabilities.ending_clip_candidates(manifest) == []


def test_a_muted_footage_ending_is_dropped_with_a_notice(monkeypatch):
    """The worker refuses it (`muted_footage_ending_unsupported`); a silent Visuals one is fine."""
    _phone_settings(monkeypatch, multi_clip=True)
    manifest = _julio_manifest()

    repaired, notices = _repair(
        manifest, _strategy(manifest, ending_clip={"media_id": FOOTAGE_C, "keep_audio": False})
    )

    assert repaired.ending_clip is None
    assert len(notices) == 1 and "silent" in notices[0]
    muted_ask = _req(
        "r4",
        "style",
        "Add the cheers clip at the end, muted",
        ending_clip="cheers",
        keep_audio=False,
    )
    checked = check_strategy_for_runtime_v2(
        manifest, _strategy(manifest, ending_clip={"media_id": FOOTAGE_C, "keep_audio": False})
    )
    assert isinstance(checked, CheckedStrategy)
    receipts, reply = _draft(
        [muted_ask],
        checked.strategy.model_dump(mode="json", exclude_none=True),
        manifest,
        JULIO_PATHS,
        notices=list(checked.notices),
    )
    assert _by_id(receipts)["r4"].status == "not_possible"
    assert "silent" in reply


def test_a_silent_visuals_ending_is_still_kept(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()

    repaired, notices = _repair(
        manifest, _strategy(manifest, ending_clip={"media_id": BITE, "keep_audio": False})
    )

    assert repaired.ending_clip is not None and repaired.ending_clip.keep_audio is False
    assert notices == []


def test_a_silent_visuals_ending_still_needs_the_audio_mix(monkeypatch):
    """The worker drops any Visuals ending without `audioMix` (it rides the main track), muted or
    not, so a silent ask gets the same early "Couldn't" instead of a Done that the render drops."""
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    _turn_off(monkeypatch, "audioMix")

    repaired, notices = _repair(
        manifest, _strategy(manifest, ending_clip={"media_id": BITE, "keep_audio": False})
    )

    assert repaired.ending_clip is None
    assert notices and GATE_WORDS["audioMix"] in notices[0]


def test_every_ending_notice_has_a_turkish_twin():
    from app.kria.strategy_policy import localize_known_message

    for code, text in capabilities.ENDING_CLIP_NOTICES.items():
        with reply_language_for("tr"):
            assert localize_known_message(text) != text, code
    with reply_language_for("tr"):
        assert (
            localize_known_message(capabilities.ENDING_CLIP_NOT_ALSO_A_POP_IN_NOTICE)
            != capabilities.ENDING_CLIP_NOT_ALSO_A_POP_IN_NOTICE
        )


# Reason codes the worker writes on `phone_ending_clip` that a creator can act on.
WORKER_REASONS = {
    "media_lanes_disabled": ("pictures and videos", "resim ve video"),
    "audio_mix_not_verified": ("mix its sound", "karıştıramıyor"),
    "muted_footage_ending_unsupported": ("silent", "sessiz"),
    "over_length_limit": ("longer than the length you asked for", "istediğin süreden uzun"),
    "not_a_video": ("isn't a video", "video değil"),
    "too_long": ("too long", "çok uzun"),
}


@pytest.mark.parametrize("reason", sorted(WORKER_REASONS))
@pytest.mark.parametrize("language", ["en", "tr"])
def test_render_ready_names_the_workers_reason_in_plain_words(reason, language):
    ask = CAFE_SHAPES["ending_clip fact (EN)"]
    dropped = _receipt_row(status="dropped", reason=reason, volume=0.0, start_s=None, end_s=None)

    with reply_language_for(language):
        receipts, reply = _render_ready(_variant(dropped), ask)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "not_possible"
    english, turkish = WORKER_REASONS[reason]
    assert (english if language == "en" else turkish) in (receipt.reason or "")
    assert reason not in reply  # never the machine code
    assert "I couldn't add it at the end" not in (receipt.reason or "")  # not the generic line


# ---------------------------------------------------------------- 11. review fixes (KRI-841)


def _draft_planned_bite(monkeypatch, requirements):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    strategy = _dispatched(manifest, _strategy(manifest, ending_clip={"media_id": BITE}))
    assert strategy["ending_clip"]["media_id"] == BITE and "closing_media" not in strategy
    return _draft(requirements, strategy, manifest, CAFE_PATHS)


def test_a_closing_photo_ask_is_not_met_by_the_ending_video(monkeypatch):
    """Item 1: the ending video must not make "hold the oblea photo over the last seconds" Done."""
    photo = _req(
        "r5",
        "style",
        "Hold the oblea photo over the last seconds of the talk",
        closing=True,
    )
    video = CAFE_SHAPES["ending_clip fact (EN)"]

    receipts, reply = _draft_planned_bite(monkeypatch, [video, photo])

    by_id = _by_id(receipts)
    assert by_id["r4"].status == "met"
    assert by_id["r5"].status != "met"
    assert by_id["r5"].status == "not_possible"
    assert "Done: Hold the oblea photo" not in reply


def test_a_closing_ask_is_never_claimed_by_the_ending_check_even_beside_an_ending(monkeypatch):
    from app.kria.brief_checks import PlanFacts, _asks_ending_clip

    ask = _req("r5", "style", "End on the sunset shot", closing="the sunset shot")
    planned = PlanFacts(
        edit_format="subtitled",
        phone_talking=True,
        ending_clip_known=True,
        ending_clip=_planned_fact(),
    )
    assert _asks_ending_clip(ask, planned) is False


def _planned_fact():
    from app.kria.brief_checks import EndingClipFact

    return EndingClipFact("planned", keep_audio=True)


def _three_clip_strategy(manifest, **extra: Any) -> CreativeStrategy:
    return _strategy(
        manifest,
        media_scope="selected",
        selected_media_ids=[FOOTAGE_A, FOOTAGE_B],
        **extra,
    )


def test_a_footage_ending_left_out_of_the_selection_is_added_to_it(monkeypatch):
    """Item 2: the worker binds only the selected clips, so a kept ending must be selected."""
    _phone_settings(monkeypatch, multi_clip=True)
    manifest = _julio_manifest()
    strategy = _three_clip_strategy(manifest, ending_clip={"media_id": FOOTAGE_C})

    checked = check_strategy_for_runtime_v2(manifest, strategy)

    assert isinstance(checked, CheckedStrategy)
    dispatched = checked.strategy.model_dump(mode="json", exclude_none=True)
    assert dispatched["ending_clip"]["media_id"] == FOOTAGE_C
    assert FOOTAGE_C in dispatched["selected_media_ids"]
    assert dispatched["selected_media_ids"][:2] == [FOOTAGE_A, FOOTAGE_B]  # order kept, C appended
    receipts, _ = _draft([JULIO_SHAPES["ending_clip fact"]], dispatched, manifest, JULIO_PATHS)
    assert _by_id(receipts)["r4"].status == "met"


def test_the_selection_is_left_alone_when_it_already_holds_the_ending_or_is_everything(
    monkeypatch,
):
    _phone_settings(monkeypatch, multi_clip=True)
    manifest = _julio_manifest()

    inside = _repair(
        manifest,
        _strategy(
            manifest,
            media_scope="selected",
            selected_media_ids=[FOOTAGE_A, FOOTAGE_C],
            ending_clip={"media_id": FOOTAGE_C},
        ),
    )[0]
    assert list(inside.selected_media_ids) == [FOOTAGE_A, FOOTAGE_C]

    everything = _repair(
        manifest,
        _strategy(manifest, media_scope="all", ending_clip={"media_id": FOOTAGE_C}),
    )[0]
    assert everything.ending_clip is not None
    assert list(everything.selected_media_ids) == list(
        _strategy(manifest, media_scope="all").selected_media_ids
    )


def test_a_footage_ending_the_creator_excluded_is_dropped_not_re_added(monkeypatch):
    from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent

    _phone_settings(monkeypatch, multi_clip=True)
    manifest = _julio_manifest()
    skip = ResolvedClipIntent(
        intent_id="skip-cheers",
        op="exclude",
        attribute="the cheers clip",
        status="resolved",
        assignments=[ClipAssignment(media_id=FOOTAGE_C, confidence=0.95)],
    )
    strategy = _three_clip_strategy(
        manifest, ending_clip={"media_id": FOOTAGE_C}, resolved_clip_intents=[skip]
    )

    repaired, notices = _repair(manifest, strategy)

    assert repaired.ending_clip is None
    assert FOOTAGE_C not in repaired.selected_media_ids
    assert len(notices) == 1 and "leave" in notices[0]
    with reply_language_for("tr"):
        from app.kria.strategy_policy import localize_known_message

        assert localize_known_message(notices[0]) != notices[0]


def test_a_talk_internal_last_clip_ask_without_an_ending_keeps_its_old_verdict(monkeypatch):
    """Item 5: "End on me saying thank you" must not become a held "Couldn't"."""
    from app.kria.brief_checks import _check_order

    _phone_settings(monkeypatch, multi_clip=True)
    manifest = _julio_manifest()
    strategy = _dispatched(manifest, _strategy(manifest))  # no ending video planned
    assert "ending_clip" not in strategy
    thanks = _old_way_req("End on me saying thank you", "me saying thank you")

    receipts, reply = _draft([thanks], strategy, manifest, JULIO_PATHS)

    receipt = _by_id(receipts)["r4"]
    assert receipt.verification == "unchecked" and receipt.status == "partial"
    assert not needs_creator_choice(receipt)
    assert "Couldn't" not in reply
    facts = plan_facts_from_strategy(strategy, manifest=manifest)
    assert check_requirement(thanks, facts) == _check_order(thanks, facts)


def test_an_old_shape_last_clip_ask_is_claimed_only_when_the_plan_carries_an_ending(monkeypatch):
    _phone_settings(monkeypatch, multi_clip=False)
    manifest = _cafe_manifest()
    ask = _old_way_req(CAFE_EN, "Atakan's first-bite video")
    with_ending = _dispatched(manifest, _strategy(manifest, ending_clip={"media_id": BITE}))
    without = _dispatched(manifest, _strategy(manifest))

    met, _ = _draft([ask], with_ending, manifest, CAFE_PATHS)
    unclaimed, reply = _draft([ask], without, manifest, CAFE_PATHS)

    assert _by_id(met)["r4"].status == "met"
    assert _by_id(unclaimed)["r4"].verification == "unchecked"
    assert "Couldn't" not in reply


def test_an_ending_clip_fact_on_a_montage_is_unjudged_not_a_closing_photo_couldnt():
    """Item 8b: `_has_checker` says yes for the fact, so every other format must answer neutral."""
    from app.kria.brief_checks import PlanFacts, is_judged

    ask = _req(
        "r4",
        "style",
        "End with the cheers video, with its own sound",
        ending_clip="the cheers video",
        keep_audio=True,
    )
    facts = PlanFacts(
        edit_format="montage",
        ending_clip_known=True,
        reaction_beats=(),
        reaction_beats_available=True,
    )

    receipt = check_requirement(ask, facts)

    assert receipt.status == "partial"
    assert not is_judged(ask, receipt)
    assert not needs_creator_choice(receipt)


# The worker's receipt says whether the ending source has an audio track at all.


@pytest.mark.parametrize("language", ["en", "tr"])
def test_render_ready_a_source_with_no_audio_track_is_not_its_own_sound(language):
    ask = CAFE_SHAPES["ending_clip fact (EN)"]  # asked WITH its own sound

    with reply_language_for(language):
        receipts, reply = _render_ready(_variant(_receipt_row(has_audio=False, volume=1.0)), ask)

    receipt = _by_id(receipts)["r4"]
    assert receipt.status == "not_possible"
    assert ("no sound of its own" if language == "en" else "kendi sesi yok") in (
        receipt.reason or ""
    )
    assert needs_creator_choice(receipt)


@pytest.mark.parametrize("has_audio", [True, None])
def test_render_ready_audio_track_true_or_unknown_lets_the_volume_decide(has_audio):
    ask = CAFE_SHAPES["ending_clip fact (EN)"]

    audible, _ = _render_ready(_variant(_receipt_row(has_audio=has_audio, volume=1.0)), ask)
    muted, _ = _render_ready(_variant(_receipt_row(has_audio=has_audio, volume=0.0)), ask)

    assert _by_id(audible)["r4"].status == "met"
    assert _by_id(muted)["r4"].status == "not_possible"


def test_render_ready_a_silent_ask_is_not_blamed_for_a_source_without_audio():
    ask = _req(
        "r4", "style", "Add the bite at the end, muted", ending_clip="the bite", keep_audio=False
    )

    receipts, _ = _render_ready(
        _variant(_receipt_row(keep_audio=False, has_audio=False, volume=0.0)), ask
    )

    assert _by_id(receipts)["r4"].status == "met"

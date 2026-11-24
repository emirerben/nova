"""Cusco DJI T1 "Good Take" (prod 2026-11-17, thread d3c428b2, job d988bb99): KRI-1523.

Two of the creator's own videos, attached in this order: T1_01 (11.5 s, the false start that ends
"... Ulan neyse.") and T1_02 (19.9 s, the good take). Server 80c96b04c, client build 2016,
protocol 52, phone Talking (`subtitled`). The request: "These two clips are the same line filmed
twice. In the first clip I lose my thread and stop. The second clip is the good take. Use only the
good take and cut my pauses so it's tight. Turkish captions."

What the creator got: the first draft HELD with "Use only the good take: the second clip, not the
first failed take: A Talking video can't do this, so it stays as filmed". After "Make it without
these" both takes played back to back, the false start first.

The Main Creator had understood: its brief row r1 carried the typed fact
`selected_media_ids: [T1_02]`. Nothing on the server read it, so `_talking_honest_receipt` turned
the unjudged `select` row into the format "can't" (the Talking dispatch narrows to the strategy's
`selected_media_ids` all along, `content_plan_build._creator_selected_clip_paths`).

Real: the brief rows exactly as `creative_brief_versions` stored them (r1's status is the stored
"superseded" one before the waiver), the strategy stored on job d988bb99 (the held draft's own
strategy was never stored; this one plays both takes, the worst case), the two media ids and
lengths, and `assess_readiness` under production's switches. No model call.

Failure modes written before the fix (each has a test below):
 1. incident: r1 as stored + a strategy with both takes -> the planner keeps only T1_02 and the
    draft is approvable, saying which clip is used (never "can't");
 2. control: the switch off holds the first draft exactly as prod did;
 3. a strategy that already picks only the good take is "met" with nothing to narrow;
 4. a strategy that also plays the false start elsewhere (an insert, the ending) is never
    narrowed and never "met": "Clip 1 still plays too" holds the draft (never "can't");
 5. an id without the `analysis-proxy-ios-` prefix still names the clip; an id that matches no
    own video (or a photo) names nothing and the hold says how to pick, not "can't";
 6. an untyped "use only the good take" with two videos holds with the same how-to-pick line;
    with one video it stays KRI-1488's "made from the one video you attached";
 7. a montage keeps its own clip-intent path: no narrowing, no new verdict;
 8. photos in the pick survive the narrowing; a dropped hero clip is cleared;
 9. two live rows naming different clips narrow nothing;
10. the finished video is judged by the videos the phone render was made from;
11. Turkish and German chats read the same typed fact and get their own sentence;
12. the brief extractor's prompt never gains the sentence; the Main Creator's does only while the
    switch is on.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.config import settings
from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.clip_scope import apply_chosen_videos
from app.kria.reply_language import reply_language_for
from app.services import creator_capabilities as capabilities
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile

TURN = "5e8b24b2-8141-4dc7-9364-6a7c816e79c9"
FALSE_START = "analysis-proxy-ios-242C1F4D-D699-470D-82CC-7912FA78836F.mp4"
GOOD_TAKE = "analysis-proxy-ios-6123A6AD-6F19-4B22-BFCB-468BAB21C493.mp4"
PHOTO = "asset-0a6e2c68-d764-4fe3-b2d9-c67e03f53b75"

TALKING_CANT = "A Talking video can't do this, so it stays as filmed"
ONLY_CLIP_2 = "Only clip 2 of your 2 is used"
CLIP_1_STILL_PLAYS = "Clip 1 still plays too"
WHICH_CLIP = (
    "I can't tell which clip you mean: remove the clips you don't want from this chat and I'll "
    "make it from the rest"
)
ONE_VIDEO = "This video is made from the one video you attached"
# Switches production has on for the phone Talking path (default off in code); the same list as
# the Çiğdem S1 replay (KRI-1488), read off prod two days before this run.
PROD_ON = (
    "tight_pause_cut_enabled",
    "title_look_enabled",
    "phone_caption_shape_enabled",
    "spoken_list_labels_enabled",
    "talking_card_layout_enabled",
    "creator_cta_ending_enabled",
    "freehand_drawing_enabled",
    "phone_text_styling_enabled",
    "phone_subtitled_title_enabled",
    "phone_subtitled_closing_title_enabled",
    "phone_subtitled_spoken_text_enabled",
    "phone_subtitled_video_overlays_enabled",
    "speech_cleanup_retakes_enabled",
    # Job d988bb99's own variant proves these: two speaker clips (`phone_talking_multi_clip`) and
    # a cleanup per take (`tight_pauses_other_takes`).
    "talking_multi_clip_enabled",
    "talking_multi_take_cleanup_enabled",
)


def _r(rid, kind, description, *, facts=None, status="open"):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope="global",
        description=description,
        literal=None,
        facts=facts or {},
        status=status,
        source_turn_id=TURN,
    )


# Brief v1, row for row as prod stored it (r1 before the creator waived it).
R1 = _r(
    "r1",
    "select",
    "Use only the good take: the second clip, not the first failed take.",
    facts={"selected_media_ids": [GOOD_TAKE]},
)
R2 = _r(
    "r2", "style", "Cut my pauses so it's tight.", facts={"pauses": "tight", "speech_cleanup": True}
)
R3 = _r("r3", "style", "Turkish captions.", facts={"captions": "on", "caption_language": "tr"})
BRIEF = CreativeBrief(version=1, requirements=[R1, R2, R3])

# Job d988bb99's `creator_strategy`, verbatim but for its rationale (the simpler version's).
STRATEGY = {
    "pacing": "balanced",
    "direction": "native",
    "rationale": "Both takes with their original speech and clean Turkish subtitles.",
    "intro_hook": "Your on-camera introduction to the final part of the Qorikancha visit.",
    "edit_format": "subtitled",
    "caption_style": "clean",
    "audio_strategy": "original_audio",
    "render_program": "native",
    "story_structure": [
        "Show the first attempt through the point where you stop.",
        "Continue into the second take in the original upload order.",
        "Keep the second take's complete closing thought.",
    ],
    "target_duration_s": 31.4,
    "selected_media_ids": [FALSE_START, GOOD_TAKE],
    "video_reuse_policy": "once",
    "optional_treatments": [],
}


def _manifest(*, photo: bool = False, edit_format: str = "subtitled", videos=None):
    videos = videos or [(FALSE_START, 11.5), (GOOD_TAKE, 19.9)]
    media = [{"media_id": mid, "kind": "video", "duration_s": s} for mid, s in videos]
    if photo:
        media.append({"media_id": PHOTO, "kind": "image", "label": "IMG_7101.JPG"})
    return capabilities.resolve_creator_manifest(
        item_id="0b1f1d8e-7c55-4d39-8d0c-3e2a6c1f9a10",
        edit_format=edit_format,
        media=media,
        phone_source_media_ids=[mid for mid, _ in videos],
        phone_rendering_allowed=True,
    )


def _prod(monkeypatch, *, fix: bool = True, german: bool = False) -> None:
    apply_prod_profile(monkeypatch)
    monkeypatch.setattr(settings, "kria_german_replies_enabled", german)
    for name in PROD_ON:
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "chosen_clips_ask_enabled", fix)
    monkeypatch.setattr(settings, "single_take_pointer_ask_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))


def _planned(strategy: dict, brief: CreativeBrief = BRIEF, manifest=None) -> dict:
    """The strategy after the planner's clip-scope step, as the draft stores it."""
    narrowed = apply_chosen_videos(
        manifest or _manifest(), CreativeStrategy.model_validate(strategy), brief.live()
    )
    return narrowed.model_dump(mode="json", exclude_none=True)


def _readiness(
    monkeypatch,
    strategy: dict,
    brief: CreativeBrief = BRIEF,
    *,
    fix=True,
    manifest=None,
    german=False,
):
    _prod(monkeypatch, fix=fix, german=german)
    manifest = manifest or _manifest()
    return assess_readiness(
        brief,
        strategy,
        media={"clip_paths": [m.media_id for m in manifest.media if m.kind == "video"]},
        manifest=manifest,
        creator_id=__import__("uuid").UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d"),
        protected_regions=[],
        protected_marks=True,
    )


def _held(report: dict) -> dict[str, str]:
    return {
        row["requirement_id"]: row["reason"]
        for row in report["requirements"]
        if row["disposition"] not in APPROVABLE
    }


def _row(report: dict, rid: str) -> dict:
    return next(row for row in report["requirements"] if row["requirement_id"] == rid)


# ------------------------------------------------------------------ the incident


def test_control_switch_off_holds_the_first_draft_exactly_as_prod_did(monkeypatch):
    _prod(monkeypatch, fix=False)
    assert _planned(STRATEGY)["selected_media_ids"] == [FALSE_START, GOOD_TAKE]
    assert _held(_readiness(monkeypatch, STRATEGY, fix=False)) == {"r1": TALKING_CANT}


def test_only_the_good_take_is_planned_and_the_draft_says_so(monkeypatch):
    _prod(monkeypatch)
    planned = _planned(STRATEGY)
    assert planned["selected_media_ids"] == [GOOD_TAKE]
    assert planned["media_scope"] == "selected"
    report = _readiness(monkeypatch, planned)
    assert _held(report) == {}
    assert (_row(report, "r1")["disposition"], _row(report, "r1")["reason"]) == (
        "executable",
        ONLY_CLIP_2,
    )


def test_a_plan_that_still_plays_both_takes_is_held_honestly(monkeypatch):
    # Readiness judges the stored strategy as it is: both takes play, so never "met".
    report = _readiness(monkeypatch, STRATEGY)
    assert _held(report) == {"r1": CLIP_1_STILL_PLAYS}


# ------------------------------------------------------------------ failure modes


def test_a_plan_with_only_the_good_take_is_met_with_nothing_to_narrow(monkeypatch):
    _prod(monkeypatch)
    only = {**STRATEGY, "selected_media_ids": [GOOD_TAKE]}
    assert _planned(only)["selected_media_ids"] == [GOOD_TAKE]
    assert _row(_readiness(monkeypatch, only), "r1")["reason"] == ONLY_CLIP_2


@pytest.mark.parametrize(
    "placed",
    [
        {"insert_clips": [{"media_id": FALSE_START, "at_phrase": "son kısım"}]},
        {"ending_clip": {"media_id": FALSE_START}},
    ],
)
def test_a_false_start_the_plan_places_elsewhere_is_never_dropped_or_met(monkeypatch, placed):
    _prod(monkeypatch)
    strategy = {**STRATEGY, **placed}
    assert _planned(strategy)["selected_media_ids"] == [FALSE_START, GOOD_TAKE]
    from app.kria.brief_checks import plan_facts_from_strategy

    facts = plan_facts_from_strategy(strategy, manifest=_manifest())
    facts = dataclasses.replace(facts, phone_talking=True)
    receipt = build_receipts([R1], facts)[0]
    assert (receipt.status, receipt.reason) == ("partial", CLIP_1_STILL_PLAYS)


def test_an_id_without_its_proxy_prefix_still_names_the_clip(monkeypatch):
    _prod(monkeypatch)
    tail = GOOD_TAKE.removeprefix("analysis-proxy-ios-")
    brief = CreativeBrief(
        version=1,
        requirements=[R1.model_copy(update={"facts": {"selected_media_ids": [tail]}}), R2, R3],
    )
    planned = _planned(STRATEGY, brief)
    assert planned["selected_media_ids"] == [GOOD_TAKE]
    assert _row(_readiness(monkeypatch, planned, brief), "r1")["reason"] == ONLY_CLIP_2


@pytest.mark.parametrize("named", [["clip_2"], [PHOTO], [GOOD_TAKE, "clip_9"]])
def test_an_id_that_names_no_own_video_holds_with_how_to_pick(monkeypatch, named):
    _prod(monkeypatch)
    brief = CreativeBrief(
        version=1,
        requirements=[R1.model_copy(update={"facts": {"selected_media_ids": named}}), R2, R3],
    )
    manifest = _manifest(photo=True)
    assert _planned(STRATEGY, brief, manifest)["selected_media_ids"] == [FALSE_START, GOOD_TAKE]
    assert _held(_readiness(monkeypatch, STRATEGY, brief, manifest=manifest)) == {"r1": WHICH_CLIP}


@pytest.mark.parametrize(
    "description", ["Use only the good take.", "Sadece iyi çekimi kullan, ilkini değil."]
)
def test_an_untyped_pick_among_two_videos_holds_with_how_to_pick(monkeypatch, description):
    brief = CreativeBrief(version=1, requirements=[_r("r1", "select", description), R2, R3])
    assert _held(_readiness(monkeypatch, STRATEGY, brief)) == {"r1": WHICH_CLIP}


def test_an_untyped_pick_on_one_video_keeps_the_kri1488_answer(monkeypatch):
    _prod(monkeypatch)  # before the manifest: it reads the phone switches when it resolves
    brief = CreativeBrief(version=1, requirements=[_r("r1", "select", "Use my good take."), R2, R3])
    one = {**STRATEGY, "selected_media_ids": [GOOD_TAKE]}
    report = _readiness(monkeypatch, one, brief, manifest=_manifest(videos=[(GOOD_TAKE, 19.9)]))
    assert (_row(report, "r1")["disposition"], _row(report, "r1")["reason"]) == (
        "executable",
        ONE_VIDEO,
    )


def test_a_montage_keeps_its_own_clip_intent_path(monkeypatch):
    _prod(monkeypatch)
    montage = {**STRATEGY, "edit_format": "montage", "render_program": "guided"}
    manifest = _manifest(edit_format="montage")
    assert _planned(montage, manifest=manifest)["selected_media_ids"] == [FALSE_START, GOOD_TAKE]
    from app.kria.brief_checks import plan_facts_from_strategy

    receipt = build_receipts([R1], plan_facts_from_strategy(montage, manifest=manifest))
    assert all(r.reason not in {ONLY_CLIP_2, CLIP_1_STILL_PLAYS, WHICH_CLIP} for r in receipt)


def test_photos_survive_and_a_dropped_hero_is_cleared(monkeypatch):
    _prod(monkeypatch)
    with_photo = {
        **STRATEGY,
        "selected_media_ids": [FALSE_START, GOOD_TAKE, PHOTO],
        "hero_media_id": FALSE_START,
    }
    planned = _planned(with_photo, manifest=_manifest(photo=True))
    assert planned["selected_media_ids"] == [GOOD_TAKE, PHOTO]
    assert "hero_media_id" not in planned


def test_a_pick_the_model_left_out_is_added(monkeypatch):
    _prod(monkeypatch)
    assert _planned({**STRATEGY, "selected_media_ids": [FALSE_START]})["selected_media_ids"] == [
        GOOD_TAKE
    ]


def test_two_rows_naming_different_clips_narrow_nothing(monkeypatch):
    _prod(monkeypatch)
    other = _r("r4", "select", "Use the first clip.", facts={"selected_media_ids": [FALSE_START]})
    brief = CreativeBrief(version=1, requirements=[R1, R2, R3, other])
    assert _planned(STRATEGY, brief)["selected_media_ids"] == [FALSE_START, GOOD_TAKE]


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ([GOOD_TAKE], ("met", ONLY_CLIP_2)),
        ([FALSE_START, GOOD_TAKE], ("partial", CLIP_1_STILL_PLAYS)),
    ],
)
def test_the_finished_video_is_judged_by_what_the_phone_rendered(monkeypatch, sources, expected):
    from app.tasks.kria_runtime import _phone_video_use

    _prod(monkeypatch)
    # Job d988bb99 as stored: the approved media snapshot (both clips attached, in order) and the
    # job's `_phone_sources_v1` (what the phone rendered from).
    job = SimpleNamespace(
        assembly_plan={
            "_phone_sources_v1": [
                {"media_id": media_id, "original": {"kind": "video"}} for media_id in sources
            ],
            "creator_brief_binding": {
                "media_snapshot": {
                    "clip_assignments": [
                        {"media_id": FALSE_START, "kind": "video"},
                        {"media_id": GOOD_TAKE, "kind": "video"},
                    ]
                }
            },
        }
    )
    variant = {"variant_id": "subtitled", "resolved_archetype": "subtitled", "duration_s": 24.86}
    facts = dataclasses.replace(plan_facts_from_phone_variant(variant), **_phone_video_use(job))
    receipt = build_receipts([R1], facts)[0]
    assert (receipt.status, receipt.reason) == expected


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("tr", "2 klibinden sadece 2. klip kullanılıyor"),
        ("de", "Nur Clip 2 von deinen 2 wird verwendet"),
    ],
)
def test_turkish_and_german_chats_read_the_same_fact(monkeypatch, language, expected):
    _prod(monkeypatch, german=True)
    planned = _planned(STRATEGY)
    with reply_language_for(language):
        row = _row(_readiness(monkeypatch, planned, german=True), "r1")
    assert (row["disposition"], row["reason"]) == ("executable", expected)


def test_only_the_main_creator_on_a_several_clip_talking_edit_is_taught_the_fact(monkeypatch):
    from app.agents import brief_extractor
    from app.agents.main_creator import (
        CHOSEN_VIDEOS_BRIEF_SENTENCE,
        brief_prompt_section,
        chosen_videos_taught,
    )

    _prod(monkeypatch)
    assert chosen_videos_taught(_manifest())
    assert brief_prompt_section(chosen_videos_open=True).count(CHOSEN_VIDEOS_BRIEF_SENTENCE) == 1
    assert CHOSEN_VIDEOS_BRIEF_SENTENCE not in brief_prompt_section()
    assert CHOSEN_VIDEOS_BRIEF_SENTENCE not in brief_extractor._NARROW_BRIEF_SECTION
    # One video, or a montage (clip intents own it): every other prompt keeps its bytes.
    assert not chosen_videos_taught(_manifest(videos=[(GOOD_TAKE, 19.9)]))
    assert not chosen_videos_taught(_manifest(edit_format="montage"))
    monkeypatch.setattr(settings, "chosen_clips_ask_enabled", False)
    assert not chosen_videos_taught(_manifest())


def test_a_typed_pick_with_unknown_videos_is_never_called_impossible(monkeypatch):
    # The finished review without a media snapshot: the pick was never shown impossible.
    _prod(monkeypatch)
    variant = {"variant_id": "subtitled", "resolved_archetype": "subtitled", "duration_s": 24.86}
    facts = dataclasses.replace(plan_facts_from_phone_variant(variant), attached_video_count=1)
    receipts = build_receipts([R1], facts)
    assert all(r.reason not in {TALKING_CANT, WHICH_CLIP} for r in receipts)


def test_a_live_use_all_clips_ask_beside_the_pick_narrows_nothing(monkeypatch):
    _prod(monkeypatch)
    every = _r("r4", "select", "Use all my clips.", facts={"all": True})
    brief = CreativeBrief(version=1, requirements=[R1, R2, R3, every])
    assert _planned(STRATEGY, brief)["selected_media_ids"] == [FALSE_START, GOOD_TAKE]


def test_a_typed_pick_of_the_only_video_reads_like_kri1488(monkeypatch):
    _prod(monkeypatch)
    one = _manifest(videos=[(GOOD_TAKE, 19.9)])
    from app.kria.brief_checks import plan_facts_from_strategy

    facts = plan_facts_from_strategy({**STRATEGY, "selected_media_ids": [GOOD_TAKE]}, manifest=one)
    receipt = build_receipts([R1], dataclasses.replace(facts, phone_talking=True))[0]
    assert (receipt.status, receipt.reason) == ("met", ONE_VIDEO)

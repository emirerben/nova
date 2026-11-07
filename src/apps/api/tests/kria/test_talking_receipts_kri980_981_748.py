"""KRI-980 / KRI-981 / KRI-748: what a multi-clip Talking draft says about length and clip sound.

Don Julio lunch T4 (thread 134b43e0): three clips, phone Talking. The waiter speaks (8.23 s);
the cheese (12.0 s) and steak (11.0 s) clips are footage that plays muted over his voice. The
creator asked for "under 10 seconds" and for the cheese and steak clips muted. The draft said
"your take is 31.2s and you asked for at most 10s" (it added the cutaways to the take) and
refused both mutes ("works on a montage, not in this edit") although the edit already plays a
footage clip silent. The clip analysis the draft reads is the same record `speaker_candidates`
reads; ids and durations below are the thread's, the speech flags are what a draft can know.

How this could fail (written before the code):
- the cutaways' seconds still count toward the take (length complaint on an 8.2 s speaker);
- the fix quotes a number when two clips may speak (a guess dressed as a fact);
- a one-clip 31 s take loses its honest notice, or its wording changes;
- a footage clip that may speak is promised muted (a second speaker keeps its sound);
- a clip that may speak (ambient speech, or no speech record yet) is flatly refused with the
  montage limit, which asks for a simpler version and renders nothing (the review of #2162);
- "mute the speaker" / the whole video, or sound ON, is promised as met;
- the flag-on lane (`PHONE_TALKING_CUTAWAY_SOUND_ENABLED`) claims a mute the split refuses;
- the flag-off wording says nothing about what the cutaways do;
- Turkish chats get English copy;
- KRI-748: the "found no long pauses to cut" sentence is printed twice when the brief is linked,
  or vanishes when it is not.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    needs_creator_choice,
    plan_facts_from_phone_variant,
)
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.tasks import kria_runtime

PROXY = "analysis-proxy-ios-"
WAITER = PROXY + "8F66E4F4-87E5-4B4D-B608-E37E79D9C07B.mp4"
CHEESE = PROXY + "93211221-EEFC-4475-BF34-7D23F63894F6.mp4"
STEAK = PROXY + "05866D5C-5C1D-4E1F-B1EF-3C341AE14577.mp4"
MONTAGE_LIMIT = "works on a montage made on your iPhone"
_VERIFIED = [
    "basicComposition",
    "local1080Export",
    "audioMix",
    "visualBlocks",
    "visualVideos",
    "alphaOverlay",
]


def _clip(media_id: str, seconds: float, *, speech: bool | None) -> dict:
    """``speech=None`` is a clip whose analysis has not said anything about speech yet."""
    analysis = {} if speech is None else {"understanding": {"speech": {"has_speech": speech}}}
    return {
        "kind": "video",
        "media_id": media_id,
        "has_audio": True,
        "duration_s": seconds,
        "analysis": analysis,
    }


def _draft(*clips: dict) -> PlanFacts:
    return PlanFacts(
        phone_talking=True,
        edit_format="subtitled",
        media_snapshot={"clip_assignments": list(clips)},
        clip_ids=tuple(c["media_id"] for c in clips),
        video_clip_count=len(clips),
        take_duration_s=sum(c["duration_s"] for c in clips),
    )


def _t4(*, cheese_speech: bool = False) -> PlanFacts:
    return _draft(
        _clip(WAITER, 8.23, speech=True),
        _clip(CHEESE, 12.0, speech=cheese_speech),
        _clip(STEAK, 11.0, speech=False),
    )


def _ask(rid: str, **kw) -> BriefRequirement:
    base = {"id": rid, "kind": "audio", "scope": "global", "description": rid, "facts": {}}
    return BriefRequirement(**{**base, **kw})


def _length(seconds: float = 10) -> BriefRequirement:
    return _ask(
        "len",
        kind="timing",
        description="under 10 seconds",
        facts={"duration_s": seconds, "duration_bound": "at_most"},
    )


def _mute(media_id: str, rid: str) -> BriefRequirement:
    return _ask(rid, scope=f"clip:{media_id}", facts={"clip_sound": "off"})


def _receipts(facts: PlanFacts, *reqs: BriefRequirement) -> dict:
    return {r.requirement_id: r for r in build_receipts(reqs, facts, include_unchecked=True)}


@pytest.fixture(autouse=True)
def _lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_render_verified_features", _VERIFIED)
    monkeypatch.setattr(settings, "phone_talking_cutaway_sound_enabled", False)
    monkeypatch.setattr(settings, "phone_clip_sound_windows_enabled", False)


def _lane_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_talking_cutaway_sound_enabled", True)
    monkeypatch.setattr(settings, "phone_clip_sound_windows_enabled", True)


# -- KRI-981: the take is the speaker, not the cutaways --------------------------------------


def test_the_cutaways_do_not_lengthen_an_8_second_take() -> None:
    receipts = _receipts(_t4(), _length())

    assert "31.2" not in (receipts["len"].reason or "")
    assert "take is" not in (receipts["len"].reason or "")
    assert not needs_creator_choice(receipts["len"])


def test_a_one_clip_31_second_take_keeps_the_existing_notice() -> None:
    receipts = _receipts(_draft(_clip(WAITER, 31.2, speech=True)), _length())

    assert receipts["len"].status == "partial"
    assert receipts["len"].reason == (
        "A Talking edit keeps your whole take, so its length follows your clip; "
        "your take is 31.2s and you asked for at most 10s"
    )


def test_a_long_speaker_among_cutaways_quotes_only_the_speaker() -> None:
    facts = _draft(
        _clip(WAITER, 15.0, speech=True),
        _clip(CHEESE, 12.0, speech=False),
        _clip(STEAK, 11.0, speech=False),
    )

    reason = _receipts(facts, _length())["len"].reason

    assert reason == (
        "A Talking edit keeps the whole talking clip, so its length follows it; "
        "the talking clip is 15s and you asked for at most 10s"
    )
    assert not needs_creator_choice(_receipts(facts, _length())["len"])


def test_two_clips_that_may_speak_get_no_made_up_number() -> None:
    facts = _draft(
        _clip(WAITER, 8.23, speech=True),
        _clip(CHEESE, 12.0, speech=True),
        _clip(STEAK, 11.0, speech=False),
    )

    reason = _receipts(facts, _length())["len"].reason

    assert "I can't tell yet which clip is the talking one" in reason
    assert not any(token in reason for token in ("8.2", "12s", "20.2", "31"))
    # Both clips together fit: nothing to say.
    assert "can't tell" not in (_receipts(facts, _length(25))["len"].reason or "")


def test_the_length_notice_speaks_turkish() -> None:
    facts = _draft(
        _clip(WAITER, 15.0, speech=True),
        _clip(CHEESE, 12.0, speech=False),
    )
    token = bind_reply_language("tr")
    try:
        reason = _receipts(facts, _length())["len"].reason
    finally:
        release_reply_language(token)

    assert "konuşma klibi 15 saniye" in reason
    assert "A Talking edit" not in reason


# -- KRI-980: a footage clip is already muted; a clip that may speak is not promised ----------


@pytest.mark.parametrize("lane", [False, True])
def test_t4_both_mutes_are_met_and_nothing_asks_for_a_simpler_version(
    monkeypatch: pytest.MonkeyPatch, lane: bool
) -> None:
    if lane:
        _lane_on(monkeypatch)
    receipts = _receipts(_t4(), _length(), _mute(CHEESE, "r2"), _mute(STEAK, "r3"))

    for rid in ("r2", "r3"):
        assert receipts[rid].status == "met", receipts[rid].reason
        assert MONTAGE_LIMIT not in receipts[rid].reason
    assert not any(needs_creator_choice(r) for r in receipts.values())


def _t4_unanalysed() -> PlanFacts:
    return _draft(
        _clip(WAITER, 8.23, speech=True),
        _clip(CHEESE, 12.0, speech=None),
        _clip(STEAK, 11.0, speech=None),
    )


@pytest.mark.parametrize("lane", [False, True])
@pytest.mark.parametrize("shape", ["ambient_speech", "no_speech_record"])
def test_a_clip_that_may_speak_gets_a_conditional_receipt_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch, lane: bool, shape: str
) -> None:
    if lane:
        _lane_on(monkeypatch)
    facts = _t4(cheese_speech=True) if shape == "ambient_speech" else _t4_unanalysed()
    receipts = _receipts(facts, _mute(CHEESE, "r2"), _mute(STEAK, "r3"))

    for rid in ("r2", "r3") if shape == "no_speech_record" else ("r2",):
        receipt = receipts[rid]
        assert receipt.status == "partial", receipt.reason
        assert receipt.reason.startswith(
            "If that clip plays as a cutaway over your voice, it plays without its own sound"
        )
        assert "keeps its sound" in receipt.reason
        assert MONTAGE_LIMIT not in receipt.reason
        assert not needs_creator_choice(receipt)
    if shape == "ambient_speech":
        assert receipts["r3"].status == "met"


def test_the_conditional_mute_receipt_speaks_turkish() -> None:
    token = bind_reply_language("tr")
    try:
        receipt = _receipts(_t4_unanalysed(), _mute(CHEESE, "r2"))["r2"]
    finally:
        release_reply_language(token)

    assert receipt.status == "partial"
    assert receipt.reason.startswith("O klip sesinin üzerinde gösterilirse")
    assert "If that clip" not in receipt.reason
    assert not needs_creator_choice(receipt)


@pytest.mark.parametrize("lane", [False, True])
def test_muting_the_one_clip_that_can_be_the_speaker_or_the_whole_video_stays_refused(
    monkeypatch: pytest.MonkeyPatch, lane: bool
) -> None:
    if lane:
        _lane_on(monkeypatch)
    receipts = _receipts(_t4(), _mute(WAITER, "r1"), _ask("r2", facts={"clip_sound": "off"}))

    assert receipts["r1"].status == "not_possible"
    assert receipts["r2"].status == "not_possible"


def test_sound_on_for_a_cutaway_says_what_the_cutaways_do_while_the_lane_is_off() -> None:
    on = _ask("r4", scope=f"clip:{STEAK}", facts={"clip_sound": "on"})

    receipt = _receipts(_t4(), on)["r4"]

    assert receipt.status == "not_possible"
    assert "play without their own sound" in receipt.reason
    assert MONTAGE_LIMIT in receipt.reason


def test_sound_on_is_met_where_the_lane_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _lane_on(monkeypatch)
    on = _ask("r4", scope=f"clip:{STEAK}", facts={"clip_sound": "on"})

    assert _receipts(_t4(), on)["r4"].status == "met"


def test_one_clip_has_no_cutaway_to_mute() -> None:
    alone = _draft(_clip(CHEESE, 12.0, speech=False))

    receipt = _receipts(alone, _mute(CHEESE, "r2"))["r2"]

    assert receipt.status == "not_possible"
    assert MONTAGE_LIMIT in receipt.reason


# -- KRI-748: the nothing-to-cut sentence is said once ----------------------------------------

GENERATION = "d9ff5ff3a2184bfda7719201a4767d7c"
NOTHING = "Speech cleanup found no long pauses to cut"


def _no_change_variant() -> dict:
    return {
        "variant_id": "original_text",
        "render_generation_id": GENERATION,
        "resolved_archetype": "narrated",
        "render_destination": "device",
        "caption_language": "en",
        "silence_cut_outcome": "no_change",
    }


def _review(brief: CreativeBrief | None, language: str = "en") -> str:
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    binding = BriefBinding.create(thread.id, brief).model_dump(mode="json")
    execution = SimpleNamespace(result={"brief_binding": binding})
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={"speech_cleanup_contract": "required_v1"})
    token = bind_reply_language(language)
    try:
        text, _ = kria_runtime._approved_generation_review(
            None, thread, job, _no_change_variant(), execution, "Your voiceover edit is ready."
        )
    finally:
        release_reply_language(token)
    return text


def _cleanup_brief() -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="audio",
                scope="global",
                description="cut the long pauses",
                facts={},
            )
        ],
    )


def test_a_linked_brief_and_the_cleanup_note_say_nothing_to_cut_once() -> None:
    facts = plan_facts_from_phone_variant(_no_change_variant())
    receipt = build_receipts(_cleanup_brief().requirements, facts)[0]
    assert NOTHING in (receipt.reason or "")  # the requirement receipt opens with it

    assert _review(_cleanup_brief()).count(NOTHING) == 1


def test_without_a_brief_the_cleanup_note_still_says_it() -> None:
    text = _review(None)

    assert text.count(NOTHING) == 1


def test_nothing_to_cut_is_said_once_in_turkish_too() -> None:
    from app.kria.brief_checks import _CLEANUP_NOTHING_TO_CUT, _REASON_TR

    assert _review(_cleanup_brief(), "tr").count(_REASON_TR[_CLEANUP_NOTHING_TO_CUT]) == 1


# -- readiness reads the same clips as the draft turn ----------------------------------------


@pytest.mark.parametrize("footage_speech", [False, None, True])
def test_readiness_agrees_with_the_draft_on_t4(
    prod_profile, monkeypatch: pytest.MonkeyPatch, footage_speech: bool | None
) -> None:
    from app.kria.approval_readiness import APPROVABLE, assess_readiness
    from app.services import creator_capabilities as capabilities

    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "talking_multi_clip_enabled", True)
    clips = [
        _clip(WAITER, 8.23, speech=True),
        _clip(CHEESE, 12.0, speech=footage_speech),
        _clip(STEAK, 11.0, speech=footage_speech),
    ]
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-t4",
        edit_format="subtitled",
        media=[{k: c[k] for k in ("media_id", "kind", "duration_s")} for c in clips],
        phone_source_media_ids=[c["media_id"] for c in clips],
        phone_rendering_allowed=True,
    )
    strategy = {
        "direction": "fast_montage",
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "render_program": "native",
        "target_duration_s": 9,
        "selected_media_ids": [c["media_id"] for c in clips],
        "rationale": "Keep the waiter's take whole.",
    }
    brief = CreativeBrief(
        version=1, requirements=[_length(), _mute(CHEESE, "r2"), _mute(STEAK, "r3")]
    )
    media = {
        "clip_paths": [f"users/t4/analysis-proxies/{c['media_id']}" for c in clips],
        "clip_assignments": clips,
    }

    report = assess_readiness(
        brief,
        strategy,
        media=media,
        manifest=manifest,
        creator_id=uuid.UUID("00000000-0000-4000-8000-0000000009e0"),
    )
    rows = {row["requirement_id"]: row for row in report["requirements"]}

    for rid in ("r2", "r3"):
        assert rows[rid]["disposition"] in APPROVABLE, rows[rid]
        assert MONTAGE_LIMIT not in (rows[rid]["reason"] or "")
    assert "take is" not in (rows["len"]["reason"] or "")

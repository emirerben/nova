"""KRI-1598: a general sound-effect ask on a montage cut from what the creator says.

The manifest opens `sfx_ask` by format, before the route is known, so a montage that the
spoken-excerpt writer (or the one-voice-under-footage writer) renders can carry it. Neither
writer has an effects lane, and before this the ask was dropped without a line: no receipt on
the variant, so the finished video's review stayed neutral.

Failure modes, written before the tests:

1. The writer pins the variant with no receipt, so the finished video says nothing about the
   ask (the reported gap).
2. The decline fails the edit instead of rendering it: an optional ask must never cost the video
   (only a NAMED effect is refused before this writer runs, KRI-608).
3. A live "no sound effects" is reported as the format's limit instead of the creator's choice.
4. No ask: the variant gains a receipt (not byte-identical with before).
5. The receipt exists but the review does not read it as a checked "Couldn't" with the real
   sentence, or it holds the video behind "Should I make a simpler version?".
6. A draft that routes to this writer says nothing (or promises effects) before approval, or says
   a different sentence than the finished video will.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    build_receipts,
    is_judged,
    needs_creator_choice,
    plan_facts_from_phone_variant,
)
from app.kria.reply_language import reply_language_for
from app.kria.route_disclosure import ask_declines, with_route_declines
from app.services.phone_sfx_choice import RECEIPT_FIELD
from app.services.sfx_capability import (
    FORMAT_UNSUPPORTED_EN,
    FORMAT_UNSUPPORTED_TR,
    limit_line,
)
from app.services.sfx_chooser import SfxLimit
from tests.kria.test_no_effects_gate import NO_SFX, _binding
from tests.services import test_phone_speech_montage_job as speech
from tests.services import test_phone_voice_behind_footage_job as voice

world = speech.world
_plan, _run = speech._plan, speech._run
ASK = {"request_text": "add sound effects where they fit", "max_placements": 6}
ASK_REQ = BriefRequirement(
    id="s1",
    kind="audio",
    scope="global",
    description="add sound effects where they fit",
    facts={"audio_intent": "sound_effects"},
)


def _excerpt_plan():
    return _plan(
        {"kind": "montage", "duration_s": 3.0},
        {
            "kind": "speech",
            "clip_ref": "c1",
            "quote": "never rush a good espresso",
            "visual": "cutaways",
        },
    )


def _review(variant) -> object:
    """The finished-video verdict on the ask, read the way the approved-render review reads a
    phone variant (`kria_runtime._approved_generation_requirements_review`)."""
    [receipt] = build_receipts([ASK_REQ], plan_facts_from_phone_variant(variant))
    return receipt


def test_a_spoken_excerpt_montage_declines_the_ask_on_its_receipt_and_still_renders(world):
    world.candidates["creator_strategy"] = {"sfx_ask": ASK}

    assert _run(world, _excerpt_plan()) is True

    job = world.job
    assert job.status == "awaiting_device" and job.error_detail is None
    variant = job.assembly_plan["variants"][0]
    receipt = variant[RECEIPT_FIELD]
    assert receipt["declined"] == "format_unsupported" and receipt["placed"] == []
    review = _review(variant)
    assert review.status == "not_possible" and review.stage == "checked"
    assert review.reason == FORMAT_UNSUPPORTED_EN
    assert is_judged(ASK_REQ, review)
    assert not needs_creator_choice(review)


def test_a_live_no_sound_effects_is_the_creators_choice_not_the_formats(world):
    world.candidates["creator_strategy"] = {"sfx_ask": ASK}
    world.snapshot["creator_brief_binding"] = _binding(NO_SFX)

    assert _run(world, _excerpt_plan()) is True

    receipt = world.job.assembly_plan["variants"][0][RECEIPT_FIELD]
    assert receipt["declined"] == "creator_said_no"


def test_no_ask_leaves_the_spoken_excerpt_variant_as_before(world):
    world.candidates["creator_strategy"] = {}

    assert _run(world, _excerpt_plan()) is True

    assert RECEIPT_FIELD not in world.job.assembly_plan["variants"][0]


def test_one_voice_under_footage_declines_the_ask_on_its_receipt(monkeypatch):
    world = voice._world(monkeypatch, strategy=voice._strategy(sfx_ask=ASK))

    assert voice._run(world) is True

    variant = world.job.assembly_plan["variants"][0]
    assert world.job.status == "awaiting_device"
    assert variant[RECEIPT_FIELD]["declined"] == "format_unsupported"
    assert _review(variant).reason == FORMAT_UNSUPPORTED_EN


def test_one_voice_under_footage_without_the_ask_has_no_receipt(monkeypatch):
    world = voice._world(monkeypatch)

    assert voice._run(world) is True

    assert RECEIPT_FIELD not in world.job.assembly_plan["variants"][0]


def test_the_draft_says_the_same_decline_before_approval(monkeypatch):
    """The draft card declines the ask on the spoken-excerpt route with the sentence the finished
    video will say; the guided montage route, which places effects, does not decline it."""
    from app.config import settings

    monkeypatch.setattr(settings, "kria_route_disclosure_enabled", True)
    monkeypatch.setattr(settings, "route_registry_enabled", True)
    neutral = build_receipts([ASK_REQ], plan_facts_from_phone_variant({}))

    [declined] = with_route_declines(neutral, [ASK_REQ], {}, route="speech_excerpt")
    assert declined.status == "not_possible" and declined.reason == FORMAT_UNSUPPORTED_EN
    assert declined.reason == limit_line(SfxLimit.format_unsupported)
    assert is_judged(ASK_REQ, declined) and not needs_creator_choice(declined)
    with reply_language_for("tr"):
        [turkish] = ask_declines([ASK_REQ], {}, route="speech_excerpt")
    assert turkish.text == FORMAT_UNSUPPORTED_TR
    # Negative control: the route that does place effects for the ask never declines it.
    assert ask_declines([ASK_REQ], {}, route="unified_montage") == []
    # And an untyped audio row is not read as the ask (KRI-667: typed facts only).
    untyped = ASK_REQ.model_copy(update={"facts": {}})
    assert ask_declines([untyped], {}, route="speech_excerpt") == []


def test_the_decline_is_a_route_decline_in_both_languages():
    """`is_judged` reads a route decline as a plain answer only when its sentence is listed."""
    from app.kria.route_disclosure import is_route_decline_reason

    assert is_route_decline_reason(FORMAT_UNSUPPORTED_EN)
    assert is_route_decline_reason(FORMAT_UNSUPPORTED_TR)

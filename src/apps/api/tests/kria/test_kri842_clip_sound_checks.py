"""KRI-842: what the draft and the finished video say about ONE clip's own sound.

Café N1: "keep the bite clip's own sound faintly under my voice" and "after my voice ends, let
the last clip play so Caro's 'bye bye' is heard". Before: the Turkish wording had no checker
(silently dropped) and the English wording was "met" by the GLOBAL bed (`_check_voice_bed`
never read the clip scope), so the creator was told "Done" for a mix that did nothing for that
clip.

Ways this could go wrong, written down before the code (each has a case below, or in
`tests/tasks/test_kri842_narrated_clip_sound.py` / `tests/pipeline/test_kri842_clip_sound_plan.py`):

1. A clip-scoped footage-sound ask is "met" by the global bed (draft and finished render).
2. The Turkish wording, which no wording rule reads, is dropped: only the recorded fact decides.
3. The draft promises a tail that cannot exist: the clip is not the last one, the line is not
   in it, the clip is a photo. The creator must read "Couldn't" BEFORE approval.
4. A finished render says met without a receipt of what the recipe plays (or the receipt says
   built for a row that was never built / that an editor Save later broke).
5. A cloud render, an editor turn or the worker's own mid-compile facts claim anything about
   clip sound.
6. The new clip-scope reasons turn a first draft into the "simpler version?" question, or lack
   their Turkish twin.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    is_judged,
    needs_creator_choice,
    plan_facts_from_narrated_alignment,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    reply_from_receipts,
)
from app.kria.narrated_clip_sound_ask import brief_clip_sound_asks, requirement_clip_sound
from app.kria.reply_language import reply_language_for
from tests.tasks.test_kri842_narrated_clip_sound import (
    after_voice_req,
    n1_media_snapshot,
    under_voice_req,
)

CLIP_IDS = ["N1_01", "N1_02", "N1_03", "N1_04", "N1_05", "N1_06", "N1_07", "N1_08", "N1_09"]
STRATEGY = {"edit_format": "narrated_planned", "audio_strategy": "voiceover"}


def draft_facts(strategy=None, **changes) -> PlanFacts:
    facts = plan_facts_from_strategy(strategy or STRATEGY, clip_ids=CLIP_IDS)
    return dataclasses.replace(
        facts, **{"phone_narrated": True, "media_snapshot": n1_media_snapshot(), **changes}
    )


def one(req: BriefRequirement, facts: PlanFacts):
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    return receipt


def rendered_variant(**clip_sound) -> dict:
    return {
        "variant_id": "narrated",
        "resolved_archetype": "narrated",
        "render_destination": "device",
        "voiceover_bed_level": 0.25,
        **({"clip_sound": clip_sound} if clip_sound else {}),
    }


# --- the facts decide, never the words ----------------------------------------------------


def test_the_reader_takes_only_a_recorded_fact_on_a_clip_scope():
    turkish = BriefRequirement(
        id="r9", kind="audio", scope="clip:N1_05", description="kendi sesi hafifçe altta kalsın"
    )
    assert requirement_clip_sound(turkish) is None
    assert requirement_clip_sound(under_voice_req()).mode == "under_voice"
    after = requirement_clip_sound(after_voice_req())
    assert (after.mode, after.triggers) == ("after_voice", ("bye bye",))
    # Not a clip scope, an unknown value, a superseded row: none of them is an ask.
    assert (
        requirement_clip_sound(
            BriefRequirement(
                id="r2", kind="audio", scope="global", facts={"clip_sound": "after_voice"}
            )
        )
        is None
    )
    assert requirement_clip_sound(under_voice_req(clip_sound="loud")) is None
    gone = under_voice_req()
    assert requirement_clip_sound(gone.model_copy(update={"status": "superseded"})) is None
    brief = CreativeBrief(version=1, requirements=[under_voice_req(), after_voice_req(), turkish])
    assert [ask.requirement_id for ask in brief_clip_sound_asks(brief)] == ["r1", "r2"]


def test_both_brief_writers_are_taught_the_fact_with_a_turkish_and_an_english_example():
    from app.agents.brief_extractor import _NARROW_BRIEF_SECTION
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION

    for section in (_BRIEF_PROMPT_SECTION, _NARROW_BRIEF_SECTION):
        assert '{"clip_sound": "under_voice"}' in section
        assert '"clip_sound": "after_voice", "triggers": ["bye bye"]' in section
        assert "Seslendirme net duyulsun" in section
        assert "After my voiceover ends" in section


# --- before approval ----------------------------------------------------------------------


def test_the_draft_says_done_for_both_halves_of_n1():
    under = one(under_voice_req(), draft_facts())
    after = one(after_voice_req(), draft_facts())

    assert (under.status, under.verification) == ("met", "checked")
    assert under.reason == "That clip's own sound plays softly under your voice"
    assert (after.status, after.verification) == ("met", "checked")
    assert after.reason == (
        'That clip plays on with its own sound after your voice ends, so "bye bye" is heard'
    )


def test_the_draft_says_it_in_turkish_for_a_turkish_chat():
    with reply_language_for("tr"):
        under = one(under_voice_req(), draft_facts())
        after = one(after_voice_req(), draft_facts())
        missed = one(after_voice_req(triggers=["see you tomorrow"]), draft_facts())

    assert under.reason == "O klibin kendi sesi seslendirmenin altında kısık çalıyor"
    assert after.reason == (
        'O klip, seslendirme bitince kendi sesiyle devam ediyor, böylece "bye bye" duyuluyor'
    )
    assert missed.status == "not_possible"
    assert "sözünü o klipte bulamadım" in missed.reason


def test_the_draft_says_couldnt_when_the_line_is_not_in_the_last_clip():
    receipt = one(after_voice_req(triggers=["see you tomorrow"]), draft_facts())

    assert (receipt.status, receipt.verification) == ("not_possible", "checked")
    assert receipt.reason == (
        'I couldn\'t find "see you tomorrow" in that clip, so nothing plays on after your voice'
    )
    # A line the creator can fix is a question for them, not a limit of the format.
    assert needs_creator_choice(receipt)


def test_the_draft_says_couldnt_when_the_clip_is_not_the_last_one():
    receipt = one(after_voice_req(media_id="N1_05"), draft_facts())

    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("Only the last clip can play on after your voice")
    assert is_format_limit(receipt.reason) and not needs_creator_choice(receipt)


def test_an_order_intent_that_seats_another_clip_last_wins_over_attachment_order():
    seated = {
        **STRATEGY,
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "position": "last",
                "attribute": "the toast",
                "assignments": [{"media_id": "N1_04"}],
            }
        ],
    }

    receipt = one(after_voice_req(), draft_facts(seated))

    assert receipt.status == "not_possible"
    assert receipt.reason.startswith("Only the last clip can play on")


def test_a_photo_and_an_unknown_clip_have_no_sound_to_play():
    photo = one(under_voice_req(media_id="N1_09"), draft_facts())
    unknown = one(under_voice_req(media_id="N1_99"), draft_facts())

    assert (photo.status, photo.reason) == (
        "not_possible",
        "That clip has no sound of its own to play",
    )
    assert unknown.status == "not_possible" and "isn't one of the video clips" in unknown.reason


def test_a_clip_whose_speech_is_not_analysed_yet_is_partly_not_done():
    snapshot = n1_media_snapshot()
    for row in snapshot["clip_assignments"]:
        row.pop("analysis", None)

    receipt = one(after_voice_req(), draft_facts(media_snapshot=snapshot))

    assert receipt.status == "partial" and receipt.verification == "checked"
    assert receipt.reason.endswith('but I can\'t hear "bye bye" in it yet')


@pytest.mark.parametrize(
    "facts",
    [
        # an editor turn: its receipts come from the before/after diff
        dataclasses.replace(draft_facts(), editor=True),
        # the worker's own mid-compile record (nothing built yet)
        plan_facts_from_narrated_alignment({"steps": []}),
    ],
    ids=["editor", "mid-compile"],
)
def test_nothing_is_claimed_where_the_phone_voiceover_does_not_own_the_ask(facts):
    req = under_voice_req()

    receipt = check_requirement(req, facts)

    assert not is_judged(req, receipt)
    assert build_receipts([req], facts) == []


def test_a_phone_talking_draft_says_plainly_that_it_cannot_do_it():
    """A Talking edit has one speaker clip and no voiceover to play a clip's sound under."""
    facts = dataclasses.replace(
        plan_facts_from_strategy({"edit_format": "subtitled"}, clip_ids=["N1_05"]),
        phone_talking=True,
    )

    receipt = one(under_voice_req(), facts)

    assert receipt.status == "not_possible"
    assert receipt.reason == "A Talking video can't do this, so it stays as filmed"


# --- a voiceover edit the phone Voiceover compiler does not render (D) ----------------------

NEEDS_PHONE_VOICEOVER = (
    "Playing one clip's own sound under or after your voice only works in a voiceover edit "
    "made on your iPhone, not in this one"
)


def test_a_voiceover_draft_that_is_not_a_phone_voiceover_says_couldnt_before_approval():
    """A montage with a recorded voiceover (or a cloud render) cannot play one clip's sound
    under or after the voice: it used to be dropped silently, the original bug class."""
    facts = plan_facts_from_strategy(
        {"edit_format": "montage", "audio_strategy": "voiceover"}, clip_ids=CLIP_IDS
    )
    assert not facts.phone_narrated

    receipt = one(under_voice_req(), facts)
    after = one(after_voice_req(), facts)

    assert (receipt.status, receipt.verification, receipt.reason) == (
        "not_possible",
        "checked",
        NEEDS_PHONE_VOICEOVER,
    )
    assert after.status == "not_possible"
    # The format's own limit, said plainly: never the simpler-version question.
    assert is_format_limit(receipt.reason) and not needs_creator_choice(receipt)
    with reply_language_for("tr"):
        turkish = one(under_voice_req(), facts)
    assert turkish.reason.startswith("Bir klibin kendi sesini seslendirmenin altında")
    assert is_format_limit(turkish.reason)


def test_a_finished_phone_montage_voiceover_says_couldnt_too():
    facts = plan_facts_from_phone_variant(
        {
            "variant_id": "voiceover_only",
            "resolved_archetype": "voiceover",
            "render_destination": "device",
        }
    )

    receipt = one(after_voice_req(), facts)

    assert (receipt.status, receipt.reason) == ("not_possible", NEEDS_PHONE_VOICEOVER)


def test_an_edit_with_no_voiceover_does_not_judge_the_ask():
    """No voice, nothing to be "under" or "after": the ask stays unjudged, as before."""
    facts = plan_facts_from_strategy(
        {"edit_format": "montage", "audio_strategy": "original_audio"}, clip_ids=CLIP_IDS
    )

    assert build_receipts([under_voice_req()], facts) == []


# --- the finished video -------------------------------------------------------------------


def built_receipt() -> dict:
    return {
        "version": 1,
        "under_voice": [
            {"requirement_id": "r1", "media_id": "N1_05", "status": "built", "gain": 0.25}
        ],
        "after_voice": {
            "requirement_id": "r2",
            "media_id": "N1_08",
            "status": "built",
            "line": "bye bye",
        },
        "not_built": [],
    }


def test_a_finished_video_is_judged_from_its_own_receipt():
    facts = plan_facts_from_phone_variant(rendered_variant(**built_receipt()))

    under = one(under_voice_req(), facts)
    after = one(after_voice_req(), facts)

    assert (under.status, under.reason) == (
        "met",
        "That clip's own sound plays softly under your voice",
    )
    assert after.status == "met" and '"bye bye" is heard' in after.reason


@pytest.mark.parametrize(
    ("code", "text"),
    [
        ("line_not_found", 'couldn\'t find "bye bye" in that clip'),
        ("not_last_clip", "Only the last clip can play on after your voice"),
        ("no_room", "no footage left after the part under your voice"),
        ("no_sound", "no sound of its own"),
        ("edited", "You changed the cut in the editor"),
    ],
)
def test_a_tail_that_was_not_built_is_a_couldnt_with_the_plain_reason(code, text):
    record = built_receipt()
    record["after_voice"] = {
        "requirement_id": "r2",
        "media_id": "N1_08",
        "status": "not_possible",
        "reason_code": code,
        "line": "bye bye",
    }

    receipt = one(after_voice_req(), plan_facts_from_phone_variant(rendered_variant(**record)))

    assert (receipt.status, receipt.verification) == ("not_possible", "checked")
    assert text in receipt.reason


def test_a_finished_video_with_no_receipt_never_says_met():
    """FM13/FM12: a render that recorded nothing built nothing."""
    for facts in (
        plan_facts_from_phone_variant(rendered_variant()),
        plan_facts_from_phone_variant(rendered_variant(under_voice=[], after_voice=None)),
    ):
        receipt = one(under_voice_req(), facts)
        assert receipt.status == "not_possible"
        assert receipt.reason == "I couldn't play that clip's own sound in this edit"


def test_a_finished_video_is_not_judged_by_the_global_bed_level():
    facts = plan_facts_from_phone_variant(
        {**rendered_variant(**built_receipt()), "voiceover_bed_level": 0.0}
    )

    assert one(under_voice_req(), facts).status == "met"  # the receipt, not the level


def test_the_finished_review_lists_both_asks_in_one_reply():
    brief = CreativeBrief(version=3, requirements=[under_voice_req(), after_voice_req()])
    facts = plan_facts_from_phone_variant(rendered_variant(**built_receipt()))
    receipts = build_receipts(brief.live(), facts, include_unchecked=True)

    text = reply_from_receipts(brief, receipts, summary="Ready.")

    assert text.count("Done:") == 2


# --- a clip-scoped footage-sound ask with no recorded fact (Done-when #2) -----------------

ENGLISH_BITE = BriefRequirement(
    id="r5",
    kind="audio",
    scope="clip:N1_05",
    description="Keep the clip sound low under the voiceover on the bite clip",
)
EVERY_CLIP = "Every clip's own sound plays softly under your voice, not just this one"


def test_a_clip_scoped_bed_ask_is_never_met_by_the_global_bed_on_a_draft():
    """FM3 (draft): red on main -- `_check_voice_bed` never read `req.scope`."""
    receipt = one(ENGLISH_BITE, draft_facts(voiceover_bed_level=0.25))

    assert receipt.status == "partial"
    assert receipt.reason == EVERY_CLIP
    # The format's own limit: said plainly, never the simpler-version question.
    assert is_format_limit(receipt.reason) and not needs_creator_choice(receipt)


def test_a_clip_scoped_bed_ask_is_never_met_by_the_global_bed_on_a_finished_video():
    """FM3 (render): the finished video's "about 25%" no longer reads as this clip's."""
    facts = plan_facts_from_phone_variant(rendered_variant())

    receipt = one(ENGLISH_BITE, facts)

    assert facts.voiceover_bed_level == 0.25
    assert (receipt.status, receipt.reason) == ("partial", EVERY_CLIP)


def test_with_no_bed_at_all_the_clip_scoped_ask_says_no_clip_sound_plays():
    facts = plan_facts_from_phone_variant({**rendered_variant(), "voiceover_bed_level": 0.0})

    receipt = one(ENGLISH_BITE, facts)

    assert (receipt.status, receipt.reason) == (
        "partial",
        "No clip's own sound plays under your voice",
    )


def test_the_clip_scope_reasons_have_turkish_twins_that_stay_format_limits():
    with reply_language_for("tr"):
        receipt = one(ENGLISH_BITE, draft_facts(voiceover_bed_level=0.25))

    assert receipt.reason == (
        "Her klibin kendi sesi seslendirmenin altında kısık çalıyor, yalnızca bu klibinki değil"
    )
    assert is_format_limit(receipt.reason)


def test_a_whole_edit_bed_ask_keeps_its_old_answer():
    """The global ask ("keep the crowd noise quiet under my voice") is not clip-scoped."""
    req = BriefRequirement(
        id="r6",
        kind="audio",
        scope="global",
        description="Keep the crowd noise quiet under my voiceover",
    )

    receipt = one(req, plan_facts_from_phone_variant(rendered_variant()))

    assert receipt.status == "met"
    assert "about 25% volume" in receipt.reason


def test_a_clip_scoped_mute_ask_is_met_when_no_clip_plays_under_the_voice():
    """E: a regression of the clip-scope rule. With a bed of 0 every clip is silent under the
    voice, so "mute that clip's sound" is true of that clip too. Keep / low asks stay unmet."""
    from app.kria.brief_checks import _wants_bed_muted, _wants_bed_under_voice

    mute = BriefRequirement(
        id="r7",
        kind="audio",
        scope="clip:N1_05",
        description="Mute the clip sound on the bite clip",
    )
    assert _wants_bed_muted(mute) and not _wants_bed_under_voice(mute)
    no_bed = {**rendered_variant(), "voiceover_bed_level": 0.0}

    muted = one(mute, plan_facts_from_phone_variant(no_bed))
    still_there = one(mute, plan_facts_from_phone_variant(rendered_variant()))
    keep_low = one(ENGLISH_BITE, plan_facts_from_phone_variant(no_bed))

    assert muted.status == "met"
    assert still_there.status == "partial"
    assert still_there.reason.startswith("The footage sound still plays")
    assert is_format_limit(still_there.reason)
    assert keep_low.status == "partial"  # "keep it low" is never met by an absent bed
    assert keep_low.reason == "No clip's own sound plays under your voice"

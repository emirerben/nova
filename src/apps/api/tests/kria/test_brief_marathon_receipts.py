"""A Voiceover edit's photo pop-ins and sound mix are judged, not "Couldn't verify" (KRI-537).

Thread 55a99da8 / job 890eca28 (iPhone Voiceover, marathon): the brief asked for clean
captions, the long pauses cut, the crowd noise kept quiet under the voiceover, and two
photos shown when the voiceover says "the medal" / "four hours and twelve minutes". The
render placed both photos, yet the reply said "Couldn't verify" for three of the five
because:

1. the brief filed the photo asks as kind `timing`, which only the duration checker read;
2. the brief rewrites asks into third person ("when voiceover says ..."), which the pop-in
   cue did not know; and
3. nothing judged "keep the crowd noise quiet under the voiceover".

These pin the deterministic receipts, at draft time (from the strategy) and at render time
(from the rendered phone variant's `phone_beat_receipt` and `voiceover_bed_level`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    CreativeStrategy,
    ResolvedCreatorManifest,
)
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (  # noqa: PLC2701
    BeatFact,
    PlanFacts,
    _cue_triggers,
    _named_triggers,
    _wants_beats,
    _wants_cleanup,
    build_receipts,
    check_requirement,
    is_format_limit,
    is_judged,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.reply_language import reply_language_for

_MEDAL = "asset-be13-medal.jpg"
_WATCH = "asset-fe79-watch.jpg"


def _req(rid: str, kind: str, description: str, scope: str = "global", **facts: Any):
    return BriefRequirement(id=rid, kind=kind, scope=scope, description=description, facts=facts)


# The marathon brief, verbatim (every `facts` was empty).
R1 = _req("r1", "style", "clean captions")
R2 = _req("r2", "audio", "cut long pauses and the restart of the kilometer thirty sentence")
R3 = _req("r3", "audio", "keep crowd noise quiet under the voiceover")
R4 = _req("r4", "timing", "show medal photo when voiceover says the medal")
R5 = _req("r5", "timing", "show watch photo when voiceover says four hours and twelve minutes")
MARATHON = [R1, R2, R3, R4, R5]


def _manifest() -> ResolvedCreatorManifest:
    return ResolvedCreatorManifest(
        item_id=str(uuid.uuid4()),
        edit_format="narrated_planned",
        render_program="guided",
        media=[
            {"media_id": "clip-a", "kind": "video"},
            {"media_id": _MEDAL, "kind": "image", "label": "medal photo"},
            {"media_id": _WATCH, "kind": "image", "label": "watch photo"},
        ],
        capabilities={
            "dispatch_render": CapabilityAvailability(available=True),
            "reaction_beats": CapabilityAvailability(available=True),
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def _strategy() -> dict[str, Any]:
    return CreativeStrategy.model_validate(
        {
            "edit_format": "narrated_planned",
            "audio_strategy": "voiceover",
            "caption_style": "clean",
            "reaction_beats": [
                {
                    "beat_id": "medal",
                    "trigger": "the medal",
                    "visual_id": _MEDAL,
                    "visual_role": "photo",
                },
                {
                    "beat_id": "watch-time",
                    "trigger": "four hours and twelve minutes",
                    "visual_id": _WATCH,
                    "visual_role": "photo",
                },
            ],
        }
    ).model_dump(mode="json", exclude_none=True)


def _draft_facts(**changes: Any) -> PlanFacts:
    facts = plan_facts_from_strategy(
        _strategy(),
        manifest=_manifest(),
        speech_cleanup_offered=True,
    )
    return PlanFacts(**{**facts.__dict__, **changes}) if changes else facts


_PHONE_RECEIPT: dict[str, Any] = {
    "placed": [
        {
            "at_s": 25.432,
            "end_s": 28.432,
            "beat_id": "medal",
            "trigger": "the medal",
            "visual_label": "340BCD6C-6C1B-4F78-A6D2-B68BF3B4255D-N3_04_visual.jpg",
        },
        {
            "at_s": 23.262,
            "end_s": 25.432,
            "beat_id": "watch-time",
            "trigger": "four hours and twelve minutes",
            "visual_label": "02937924-17F3-4509-8DC0-BC7FEF699401-N3_05_visual.jpg",
        },
    ],
    "closing": {"badge": "none", "status": "none"},
    "matcher": "phrase",
    "version": 1,
    "unplaced": [],
    "face_sampling": "skipped",
}


def _variant(**changes: Any) -> dict[str, Any]:
    variant: dict[str, Any] = {
        "phone_beat_receipt": _PHONE_RECEIPT,
        "voiceover_bed_level": 0.25,
        "resolved_archetype": "narrated",
        "caption_language": "en",
        "render_generation_id": "720266e0bc584a09af74bf6fbf167f99",
    }
    variant.update(changes)
    return variant


def _brief(*reqs: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(reqs))


# ------------------------------------------------------------------ draft time


def test_the_marathon_brief_is_fully_judged_at_draft_time() -> None:
    facts = _draft_facts()
    assert facts.audio_strategy == "voiceover"
    assert [b.trigger for b in facts.reaction_beats or ()] == [
        "the medal",
        "four hours and twelve minutes",
    ]

    receipts = {r.requirement_id: r for r in build_receipts(MARATHON, facts)}

    assert set(receipts) == {"r1", "r2", "r3", "r4", "r5"}
    assert receipts["r1"].status == "met"
    # The cleanup line is still the honest "choose Clean up speech" partly. KRI-541: "the
    # restart of the kilometer thirty sentence" is a retake, which cleanup never cuts.
    assert receipts["r2"].status == "partial"
    assert receipts["r2"].reason == (
        "Choose Clean up speech when you approve and the long pauses are cut; "
        "a retake or a specific line isn't cut automatically yet, so trim that in the editor"
    )
    assert receipts["r3"].status == "met"
    assert receipts["r3"].reason == "Your footage sound plays under your voice"
    assert receipts["r4"].status == "met"
    assert receipts["r5"].status == "met"
    # A draft has no timings yet, so the pop-in receipts carry no reason.
    assert receipts["r4"].reason is None
    assert all(not needs_creator_choice(r) for r in receipts.values())


def test_the_marathon_reply_has_nothing_unverified() -> None:
    receipts = build_receipts(MARATHON, _draft_facts(), include_unchecked=True)
    reply = reply_from_receipts(_brief(*MARATHON), receipts)

    assert "Couldn't verify" not in reply
    assert "Done: show medal photo when voiceover says the medal" in reply
    assert "Done: show watch photo when voiceover says four hours and twelve minutes" in reply
    assert "Done: keep crowd noise quiet under the voiceover" in reply
    assert "Partly: cut long pauses" in reply
    assert "Choose Clean up speech when you approve" in reply


def test_all_five_requirements_are_still_checked_at_draft_on_a_narrated_draft() -> None:
    kept = requirements_to_check_at_draft(
        MARATHON,
        creator_id=uuid.uuid4(),
        strategy=_strategy(),
        item_edit_format="narrated_planned",
    )
    assert [r.id for r in kept] == ["r1", "r2", "r3", "r4", "r5"]


# ------------------------------------------------------------------ render time


def test_the_rendered_variant_carries_the_beats_the_bed_and_the_archetype() -> None:
    facts = plan_facts_from_phone_variant(_variant())

    assert facts.editor is True  # text lanes still come from the editor payload
    assert facts.reaction_beats_available is True
    assert [(b.trigger, b.at_s) for b in facts.reaction_beats or ()] == [
        ("the medal", 25.432),
        ("four hours and twelve minutes", 23.262),
    ]
    assert facts.reaction_beats[0].visual_id.endswith("N3_04_visual.jpg")  # type: ignore[index]
    assert facts.unheard_beat_triggers == ()
    assert facts.voiceover_bed_level == 0.25
    assert facts.audio_strategy == "voiceover"
    assert facts.closing_visual_id is None


def test_the_render_receipts_give_the_times_and_the_mix() -> None:
    facts = plan_facts_from_phone_variant(_variant())
    receipts = {r.requirement_id: r for r in build_receipts(MARATHON, facts)}

    assert receipts["r4"].status == "met"
    assert receipts["r4"].reason == 'Pop-in on "the medal" at 25.4 s'
    assert receipts["r5"].status == "met"
    assert "at 23.3 s" in (receipts["r5"].reason or "")
    assert receipts["r3"].status == "met"
    assert "25%" in (receipts["r3"].reason or "")
    assert all(not needs_creator_choice(r) for r in receipts.values())


def test_an_ask_naming_no_words_lists_every_placement_in_time_order() -> None:
    facts = plan_facts_from_phone_variant(_variant())
    req = _req("r9", "style", "pop up my photos at the exact words")

    receipt = check_requirement(req, facts)

    assert receipt.status == "met"
    assert receipt.reason == (
        'Pop-ins on "four hours and twelve minutes" at 23.3 s and "the medal" at 25.4 s'
    )


def test_a_beat_the_render_never_heard_is_told_as_that() -> None:
    receipt_data = {
        **_PHONE_RECEIPT,
        "placed": [_PHONE_RECEIPT["placed"][1]],
        "unplaced": [{"beat_id": "medal", "trigger": "the medal", "reason": "no_spoken_match"}],
    }
    facts = plan_facts_from_phone_variant(_variant(phone_beat_receipt=receipt_data))
    assert facts.unheard_beat_triggers == ("the medal",)

    medal = check_requirement(R4, facts)
    watch = check_requirement(R5, facts)

    # The only word this ask names was never said: nothing of it landed.
    assert medal.status == "not_possible"
    assert medal.reason == "I never heard the medal in your voice."
    # The other beat, which did land, is not blamed for it.
    assert watch.status == "met"
    assert "at 23.3 s" in (watch.reason or "")
    assert needs_creator_choice(medal.model_copy(update={"verification": "checked"}))


def test_a_beat_that_was_heard_but_had_no_room_is_not_called_unheard() -> None:
    receipt_data = {
        **_PHONE_RECEIPT,
        "placed": [_PHONE_RECEIPT["placed"][1]],
        "unplaced": [{"beat_id": "medal", "trigger": "the medal", "reason": "no_safe_spot"}],
    }
    facts = plan_facts_from_phone_variant(_variant(phone_beat_receipt=receipt_data))
    assert facts.unheard_beat_triggers == ()
    assert facts.beat_room_drops == (("the medal", "no_room"),)

    # KRI-547: the medal pop-in was heard but had no room on screen. That used to read
    # "met" (the other word's pop-in judged as a whole) -- a Done for a photo that never
    # showed. Like a never-heard word, the dropped pop-in now owns the verdict and says why.
    no_room = (
        "There was no room on screen for the medal without covering your face or the captions."
    )
    receipt = check_requirement(R4, facts)
    assert receipt.status == "not_possible"
    assert receipt.reason == no_room

    # A word the creator quoted gets the same honest reason, not "No pop-in for the medal".
    quoted = _req("q1", "timing", 'show the medal photo when voiceover says "the medal"')
    named = check_requirement(quoted, facts)
    assert named.status == "not_possible"
    assert named.reason == no_room


def test_nothing_placed_and_nothing_heard_is_not_possible() -> None:
    receipt_data = {
        **_PHONE_RECEIPT,
        "placed": [],
        "unplaced": [
            {"beat_id": "medal", "trigger": "the medal", "reason": "never_heard"},
            {"beat_id": "watch-time", "trigger": "four hours and twelve minutes"},
        ],
    }
    facts = plan_facts_from_phone_variant(_variant(phone_beat_receipt=receipt_data))

    medal = check_requirement(R4, facts)
    both = check_requirement(_req("r9", "style", "pop up my photos at the exact words"), facts)

    assert medal.status == "not_possible"
    assert medal.reason == "I never heard the medal in your voice."
    assert both.status == "not_possible"
    assert both.reason == ("I never heard the medal, four hours and twelve minutes in your voice.")


def test_a_variant_without_a_beat_receipt_leaves_beats_unknown() -> None:
    facts = plan_facts_from_phone_variant({"voiceover_bed_level": 0.25})

    assert facts.reaction_beats is None
    assert facts.reaction_beats_available is None
    assert facts.audio_strategy == "voiceover"
    receipt = check_requirement(R4, facts)
    assert receipt.status == "partial"
    assert not is_judged(R4, receipt)  # "can't check the pop-ins": neutral, never a failure


def test_a_manual_receipt_and_junk_input_claim_nothing() -> None:
    manual = plan_facts_from_phone_variant({"phone_beat_receipt": {"matcher": "manual"}})
    assert manual.reaction_beats is None
    assert manual.audio_strategy is None
    assert plan_facts_from_phone_variant(None) == PlanFacts()
    assert plan_facts_from_phone_variant("nope") == PlanFacts()  # type: ignore[arg-type]
    assert (
        plan_facts_from_phone_variant({"voiceover_bed_level": "loud"}).voiceover_bed_level is None
    )


def test_a_closing_shot_is_read_off_the_receipt() -> None:
    placed = plan_facts_from_phone_variant(
        _variant(
            phone_beat_receipt={
                **_PHONE_RECEIPT,
                "closing": {"status": "placed", "visual_label": "finish.jpg", "badge": "none"},
            }
        )
    )
    missed = plan_facts_from_phone_variant(
        _variant(
            phone_beat_receipt={
                **_PHONE_RECEIPT,
                "closing": {"status": "unplaced", "reason": "visual_not_in_pool", "badge": "none"},
            }
        )
    )
    assert placed.closing_visual_id == "finish.jpg"
    assert missed.closing_visual_id is None
    assert missed.closing_requested is True


# ------------------------------------------------------------------ routing guards


def test_a_length_ask_keeps_the_duration_checker() -> None:
    req = _req("t1", "timing", "keep it under 30 s", duration_s=30)
    assert not _wants_beats(req)
    receipt = check_requirement(req, _draft_facts())
    assert receipt.status == "partial"
    assert receipt.reason == "A voiceover edit runs as long as your voiceover"


def test_a_length_ask_with_a_pop_in_cue_is_still_a_length_ask() -> None:
    req = _req("t1", "timing", "keep it under 30 s, pop up the photos", duration_s=30)
    assert not _wants_beats(req)


def test_whole_take_keeps_the_whole_take_checker() -> None:
    req = _req("t2", "timing", "keep my whole take")
    assert not _wants_beats(req)
    receipt = check_requirement(req, PlanFacts(edit_format="subtitled", video_clip_count=1))
    assert receipt.status == "met"
    unknown = check_requirement(req, PlanFacts())
    assert unknown.reason == "I can't confirm this draft keeps your whole take."


def test_a_title_hold_ask_keeps_the_text_span_checker() -> None:
    req = _req("t3", "timing", "keep the title up for the whole video", scope="title")
    assert not _wants_beats(req)
    receipt = check_requirement(req, PlanFacts())
    assert receipt.reason == "I can't verify this timing automatically."
    assert not is_judged(req, receipt)


def test_a_timing_ask_with_no_number_and_no_cue_stays_unverifiable() -> None:
    req = _req("t4", "timing", "fast but readable")
    receipt = check_requirement(req, _draft_facts())
    assert receipt.reason == "I can't verify this timing automatically."
    assert not is_judged(req, receipt)


def test_the_pause_cut_ask_still_goes_to_the_cleanup_checker() -> None:
    receipt = check_requirement(R2, _draft_facts(speech_cleanup_enabled=True))
    # KRI-541: R2 also names "the restart of the kilometer thirty sentence", a retake the
    # cleanup never cuts: still the cleanup checker, an honest format-limit "Partly".
    assert receipt.status == "partial"
    assert receipt.reason.startswith("Speech cleanup cuts the long pauses")
    assert "trim that in the editor" in receipt.reason
    assert is_format_limit(receipt.reason)
    offered = check_requirement(R2, _draft_facts())
    assert offered.status == "partial"
    assert is_format_limit(offered.reason)


# ------------------------------------------------------------------ cue wording


@pytest.mark.parametrize(
    "text",
    [
        "show medal photo when voiceover says the medal",
        "show the photo when the voiceover says the medal",
        "pop the flag up when my voice-over mentions Berlin",
        "show a sticker when the narration says pasta",
        "show the map when the narration mentions Lisbon",
        "flash the photo when the narration names the finish line",
        "show the photo when the narration talks about the medal",
        "put the watch on screen when the narrator says four hours",
        "show the clip when the recording says medal",
        "show the shot when the audio says medal",
        "show the shot when the script says medal",
        "show the photo as soon as I say medal",
        "show the photo the moment I say medal",
        "When I say medal, show the photo",
        "when you hear medal show the photo",
        "fotoğrafı madalya dediğimde göster",
        "fotoğrafı madalya dediğinde göster",
        "fotografi madalya dedigimde goster",
        "madalya deyince fotoğraf çıksın",
        "madalya denince fotoğraf çıksın",
        "madalyadan bahsettiğinde fotoğraf çıksın",
        "madalya söylediğinde fotoğraf çıksın",
        "madalya dendiğinde fotoğraf çıksın",
        "madalya geçtiğinde fotoğraf çıksın",
        "madalya geçince fotoğraf çıksın",
    ],
)
@pytest.mark.parametrize("kind", ["timing", "audio", "style", "select"])
def test_third_person_and_turkish_cues_are_pop_in_asks(text: str, kind: str) -> None:
    assert _wants_beats(_req("c1", kind, text))


@pytest.mark.parametrize(
    "text",
    [
        "cut long pauses and the restart of the kilometer thirty sentence",
        "clean captions",
        "keep my voice clear",
        "captions when the voice is loud",
        "keep crowd noise quiet under the voiceover",
        "make the voiceover louder",
        "as I said, keep it short",
    ],
)
def test_other_asks_are_not_pop_in_asks(text: str) -> None:
    assert not _wants_beats(_req("c2", "audio", text))


def test_the_cue_words_are_a_guess_kept_apart_from_the_named_triggers() -> None:
    assert _named_triggers(R4) == []  # nothing quoted, no facts: nothing named for certain
    assert _cue_triggers(R4) == ["the medal"]
    assert _cue_triggers(R5) == ["four hours and twelve minutes"]
    assert _cue_triggers(
        _req("c3", "style", "When I say four hours and twelve minutes, show the watch photo")
    ) == ["four hours and twelve minutes"]
    assert _cue_triggers(
        _req("c4", "style", "when I say the finish line and show the medal photo")
    ) == ["the finish line"]
    assert _cue_triggers(_req("c5", "style", "When I say the word medal, show it")) == ["medal"]
    # The creator's own spelling is kept.
    assert _cue_triggers(_req("c5b", "style", "Show it when I say Kırmızı Balık")) == [
        "Kırmızı Balık"
    ]


@pytest.mark.parametrize(
    "text",
    [
        "pop up a photo when I say any food name",
        "show stickers when I say each country",
        "pop up a photo when I mention a place",
        "show the photo when I say it",
        "show the photo when I say I",
    ],
)
def test_a_described_or_trivial_phrase_names_no_cue_trigger(text: str) -> None:
    assert _cue_triggers(_req("c6", "style", text)) == []


def test_quotes_and_facts_still_win_over_the_cue_words() -> None:
    quoted = _req("c8", "style", 'show the photo when I say "medal" and then relax')
    assert _named_triggers(quoted) == ["medal"]
    fact = _req("c9", "style", "show the photo when I say the medal", triggers=["finish"])
    assert _named_triggers(fact) == ["finish"]


def test_a_pop_in_ask_with_a_word_the_draft_lacks_is_still_named_missing() -> None:
    facts = PlanFacts(
        reaction_beats_available=True,
        reaction_beats=(BeatFact(trigger="the medal", visual_id="x"),),
    )
    quoted = _req("c10", "timing", 'show the flag when voiceover says "Berlin"')
    receipt = check_requirement(quoted, facts)
    assert receipt.status == "partial"
    assert receipt.reason == "No pop-in for Berlin."
    # The same sentence without quotes only guesses the word, so it can never say "missing".
    guessed = check_requirement(
        _req("c11", "timing", "show the flag when voiceover says Berlin"), facts
    )
    assert guessed.status == "met"
    assert guessed.reason is None


# ------------------------------------------------------------------ sound mix


@pytest.mark.parametrize(
    "text",
    [
        "keep crowd noise quiet under the voiceover",
        "duck the background noise under my narration",
        "crowd sound low beneath my voice",
        "keep the original audio quiet under my voice",
        "keep the footage sound soft under my words",
        "turn the background noise down so my voice is clear",
        "kalabalık sesi sesimin altında kısık kalsın",
        "arka plan gürültüsü sesimin altında kalsın",
    ],
)
def test_keep_the_footage_sound_quiet_asks_are_recognised(text: str) -> None:
    from app.kria.brief_checks import _wants_bed_muted, _wants_bed_under_voice  # noqa: PLC2701

    req = _req("m1", "audio", text)
    assert _wants_bed_under_voice(req)
    assert not _wants_bed_muted(req)


@pytest.mark.parametrize(
    "text",
    [
        "cut long pauses and the restart of the kilometer thirty sentence",
        "no captions",
        "keep my voice clear",
        "clean captions",
        "keep the background music low",
        "remove the noise",
        "mute the crowd noise",
        "Arka planda hafif müzik olsun",
        "keep the background music soft under my voice",
        "turn the crowd noise up",
        "make the crowd noise louder under my voice",
        "keep the crowd noise not too quiet",
        "kalabalık sesini yükselt",
        "video sound calm and soft",
        "remove the background sound",
    ],
)
def test_other_asks_are_not_keep_it_quiet_asks(text: str) -> None:
    from app.kria.brief_checks import _wants_bed_under_voice  # noqa: PLC2701

    assert not _wants_bed_under_voice(_req("m2", "audio", text))


@pytest.mark.parametrize(
    "text",
    [
        "mute the crowd noise",
        "remove the original audio",
        "no crowd noise please",
        "strip the original audio",
        "kalabalık sesini kapat",
        "kalabalik sesini kapat",
    ],
)
def test_mute_the_footage_sound_asks_are_recognised(text: str) -> None:
    from app.kria.brief_checks import _wants_bed_muted  # noqa: PLC2701

    assert _wants_bed_muted(_req("m3", "audio", text))


def test_the_bed_is_met_at_draft_time_on_a_voiceover_draft() -> None:
    receipt = check_requirement(R3, _draft_facts())
    assert receipt.status == "met"
    assert receipt.reason == "Your footage sound plays under your voice"


def test_the_bed_percentage_is_read_off_the_render() -> None:
    receipt = check_requirement(R3, plan_facts_from_phone_variant(_variant()))
    assert receipt.status == "met"
    assert receipt.reason == "Your footage sound sits under your voice at about 25% volume"


def test_a_full_volume_bed_is_not_under_the_voice() -> None:
    facts = plan_facts_from_phone_variant(_variant(voiceover_bed_level=1.0))
    receipt = check_requirement(R3, facts)
    assert receipt.status == "partial"
    assert receipt.reason == "The footage sound plays at full volume alongside your voice"


def test_a_mute_ask_is_partly_met_and_never_asks_the_creator_to_choose() -> None:
    mute = _req("m4", "audio", "mute the crowd noise")

    at_render = check_requirement(mute, plan_facts_from_phone_variant(_variant()))
    at_draft = check_requirement(mute, _draft_facts())

    assert at_render.status == "partial"
    assert at_render.reason == (
        "The footage sound still plays softly under your voice (about 25%); "
        "lower it in the editor's mix"
    )
    assert at_draft.status == "partial"
    assert at_draft.reason == (
        "The footage sound still plays softly under your voice; lower it in the editor's mix"
    )
    for receipt in (at_render, at_draft):
        assert is_format_limit(receipt.reason)
        assert is_judged(mute, receipt)
        assert not needs_creator_choice(receipt.model_copy(update={"verification": "checked"}))


def test_a_mute_ask_is_met_when_the_bed_is_off() -> None:
    mute = _req("m4", "audio", "mute the crowd noise")
    receipt = check_requirement(
        mute, plan_facts_from_phone_variant(_variant(voiceover_bed_level=0))
    )
    assert receipt.status == "met"
    assert receipt.reason is None


def test_a_draft_that_is_not_a_voiceover_leaves_the_mix_unchecked() -> None:
    facts = plan_facts_from_strategy({"edit_format": "montage", "audio_strategy": "original_audio"})
    receipt = check_requirement(R3, facts)

    assert receipt.status == "partial"
    assert receipt.reason == "I can't check the sound mix on this draft yet."
    assert not is_judged(R3, receipt)
    [bound] = build_receipts([R3], facts, include_unchecked=True)
    assert bound.verification == "unchecked"
    assert [r.requirement_id for r in build_receipts([R3], facts)] == []


def test_a_draft_with_no_audio_strategy_leaves_the_mix_unchecked() -> None:
    receipt = check_requirement(R3, PlanFacts())
    assert receipt.reason == "I can't check the sound mix on this draft yet."
    assert not is_judged(R3, receipt)


def test_the_cleanup_ask_is_not_a_mix_ask() -> None:
    from app.kria.brief_checks import _wants_bed_muted, _wants_bed_under_voice  # noqa: PLC2701

    assert not _wants_bed_under_voice(R2)
    assert not _wants_bed_muted(R2)


# ------------------------------------------------------------------ Turkish


def test_the_new_receipts_speak_turkish_in_a_turkish_chat() -> None:
    facts = plan_facts_from_phone_variant(_variant())
    muted = _req("m4", "audio", "mute the crowd noise")
    missed = plan_facts_from_phone_variant(
        _variant(
            phone_beat_receipt={
                **_PHONE_RECEIPT,
                "placed": [_PHONE_RECEIPT["placed"][1]],
                "unplaced": [{"beat_id": "medal", "trigger": "the medal", "reason": "never_heard"}],
            }
        )
    )
    with reply_language_for("tr"):
        pop = check_requirement(R5, facts)
        bed = check_requirement(R3, facts)
        draft_bed = check_requirement(R3, _draft_facts())
        mute = check_requirement(muted, facts)
        unheard = check_requirement(R4, missed)
        no_mix = check_requirement(R3, PlanFacts())
        reply = reply_from_receipts(
            _brief(R3, R5),
            build_receipts([R3, R5], facts, include_unchecked=True),
        )

    assert pop.reason == '"four hours and twelve minutes" 23.3 sn\'de çıkıyor'
    assert bed.reason == "Çekim sesi, sesinin altında yaklaşık %25 seviyesinde"
    assert draft_bed.reason == "Çekim sesi sesinin altında çalıyor"
    assert mute.reason is not None
    assert mute.reason.startswith("Çekim sesi hâlâ çalıyor")
    assert is_format_limit(mute.reason)
    assert unheard.reason == "Sesinde the medal sözünü duymadım."
    assert no_mix.reason == "Bu taslakta ses karışımını henüz kontrol edemiyorum."
    assert not is_judged(R3, no_mix)
    assert "Couldn't verify" not in reply and "Doğrulayamadım" not in reply


# ------------------------------------------------------------------ compatibility


def test_a_beat_fact_still_constructs_without_a_time() -> None:
    beat = BeatFact(trigger="x")
    assert beat.at_s is None
    assert beat.visual_id is None and beat.sound is None


def test_draft_beats_carry_no_time_and_keep_their_old_shape() -> None:
    facts = _draft_facts()
    assert all(b.at_s is None for b in facts.reaction_beats or ())
    assert facts.unheard_beat_triggers == ()
    assert facts.voiceover_bed_level is None


def test_a_rendered_facts_object_with_text_lanes_can_be_judged_for_beats() -> None:
    # `editor=True` used to refuse every beat ask; a rendered variant keeps it for its text.
    facts = plan_facts_from_phone_variant(_variant(text_elements=[{"id": "t", "text": "Hi"}]))
    assert facts.editor is True
    assert check_requirement(R4, facts).status == "met"


def test_a_real_editor_edit_still_cannot_judge_a_style_pop_in_ask() -> None:
    req = _req("e1", "style", "show the photo when I say pasta")
    receipt = check_requirement(req, PlanFacts(editor=True))
    assert receipt.status == "partial"
    assert receipt.reason == "I can't verify this one automatically yet."  # the old route
    assert not is_judged(req, receipt)


# ---------------------------------------------------- timing asks that are cuts, not pop-ins


@pytest.mark.parametrize(
    "text",
    [
        "when I say go, cut to the next clip",
        "jump to the finish line footage when I say the medal",
        "switch to the crowd shot when the voiceover mentions the crowd",
        "madalya dediğimde sonraki klibe geç",
        "madalya dediğimde sonraki videoya geç",
        "madalya dedigimde sonraki videoya gec",
    ],
)
def test_a_timing_ask_about_a_cut_is_not_a_pop_in_ask(text: str) -> None:
    req = _req("cut1", "timing", text)
    assert not _wants_beats(req)
    receipt = check_requirement(req, _draft_facts())
    assert receipt.reason == "I can't verify this timing automatically."
    assert not is_judged(req, receipt)


@pytest.mark.parametrize(
    "text",
    [
        "when I say the medal show my video in the corner",
        "show the medal clip card when the voiceover says the medal",
        "play a ding sound when I say four hours",
    ],
)
def test_a_timing_ask_that_shows_a_card_or_sound_on_a_cue_is_a_pop_in_ask(text: str) -> None:
    assert _wants_beats(_req("cue3", "timing", text))


def test_a_timing_cue_with_nothing_to_show_stays_a_timing_ask() -> None:
    req = _req("cue4", "timing", "slow down when I say the medal")
    assert not _wants_beats(req)
    receipt = check_requirement(req, _draft_facts())
    assert not is_judged(req, receipt)


# ------------------------------------------- cue sentences that are NOT pop-in asks (rollback)

_NOT_POP_INS: list[tuple[str, str]] = [
    ("timing", "Cut the video when I say goodbye"),
    ("timing", "Slow down the video when I say wait"),
    ("timing", "Add a pause when I say wait"),
    ("timing", "End the clip when I say that's it"),
    ("timing", "Speed up the clips whenever I mention the hills"),
    ("timing", "Make the text pop up quickly in the first second"),
    ("timing", "madalya dedigimde sonraki videoya gec"),
    ("timing", "madalya dediğimde sonraki klibe geç"),
    ("audio", "Every time I say um, cut it out"),
    ("audio", "Whenever I say um or uh, remove it"),
    ("audio", "Once I get to the end, fade out the music"),
    ("style", "Cut to the next clip every time I say next"),
    ("select", "Skip the clips whenever I mention the weather"),
]


def _draft_shapes() -> dict[str, PlanFacts]:
    """The three draft fact shapes a pop-in checker can see, on a Voiceover draft."""
    base = {"edit_format": "narrated_planned", "audio_strategy": "voiceover"}
    return {
        "capability unavailable": PlanFacts(
            **base, reaction_beats_available=False, reaction_beats=()
        ),
        "available, no beats": PlanFacts(**base, reaction_beats_available=True, reaction_beats=()),
        "available, with beats": _draft_facts(speech_cleanup_offered=True),
    }


@pytest.mark.parametrize(("kind", "text"), _NOT_POP_INS)
def test_cut_and_cleanup_sentences_are_never_pop_in_asks_and_never_roll_a_draft_back(
    kind: str, text: str
) -> None:
    req = _req("n1", kind, text)
    assert not _wants_beats(req)
    for name, facts in _draft_shapes().items():
        [receipt] = build_receipts([req], facts, include_unchecked=True)
        assert not needs_creator_choice(receipt), (name, receipt.reason)
        assert "pop-in" not in (receipt.reason or ""), (name, receipt.reason)


@pytest.mark.parametrize(
    "text",
    [
        "Every time I say um, cut it out",
        "Whenever I say um or uh, remove it",
        "every time I say uh remove it",
    ],
)
def test_whenever_and_every_time_with_cleanup_wording_stay_cleanup_asks(text: str) -> None:
    req = _req("n2", "audio", text)
    assert not _wants_beats(req)
    assert _wants_cleanup(req)
    receipt = check_requirement(req, _draft_facts(speech_cleanup_offered=True))
    assert receipt.reason == "Choose Clean up speech when you approve and the long pauses are cut"


def test_a_pop_in_ask_on_a_draft_that_cannot_do_pop_ins_still_says_so() -> None:
    receipt = check_requirement(R4, _draft_shapes()["capability unavailable"])
    assert receipt.status == "not_possible"
    assert "aren't available" in (receipt.reason or "")


@pytest.mark.parametrize(
    "text",
    [
        "pop up a sticker when I say pause, cut the rest",
        "show the photo when I say stop",
        "show the flag whenever I say start",
    ],
)
def test_an_explicit_picture_ask_stays_a_pop_in_ask_despite_a_cut_word(text: str) -> None:
    assert _wants_beats(_req("n3", "style", text))


def test_a_timing_ask_needs_a_spoken_cue_when_no_word_is_named() -> None:
    # A style ask keeps the original rule: the sticker / pop-up word alone is enough.
    assert _wants_beats(_req("n4", "style", "use my stickers and pop them up"))
    assert not _wants_beats(_req("n5", "timing", "show the photo for two seconds"))
    # ... but a named word needs no cue.
    assert _wants_beats(_req("n6", "style", "stickers", triggers=["pasta"]))
    assert _wants_beats(_req("n7", "style", 'put a sticker up at "pasta"'))


# ------------------------------------------------------ evidence edge cases


def test_a_sound_only_placement_is_not_a_photo() -> None:
    receipt_data = {
        **_PHONE_RECEIPT,
        "placed": [
            {
                "at_s": 25.4,
                "end_s": 26.0,
                "beat_id": "medal",
                "trigger": "the medal",
                "sound_label": "ding",
            },
            _PHONE_RECEIPT["placed"][1],
        ],
    }
    facts = plan_facts_from_phone_variant(_variant(phone_beat_receipt=receipt_data))

    receipt = check_requirement(R4, facts)

    # The watch photo elsewhere does not make the medal ask's photo exist.
    assert receipt.status == "partial"
    assert receipt.reason == "None of the pop-ins shows a photo or sticker."
    assert check_requirement(R5, facts).status == "met"


def test_a_sound_ask_is_judged_on_its_own_placements() -> None:
    receipt_data = {
        **_PHONE_RECEIPT,
        "placed": [
            {**_PHONE_RECEIPT["placed"][0], "sound_label": "ding"},
            _PHONE_RECEIPT["placed"][1],
        ],
    }
    facts = plan_facts_from_phone_variant(_variant(phone_beat_receipt=receipt_data))
    ask = _req("s1", "audio", "play a ding when voiceover says four hours and twelve minutes")
    assert check_requirement(ask, facts).reason == "None of the pop-ins plays a sound."


def test_a_failed_matcher_says_nothing_about_the_pop_ins() -> None:
    facts = plan_facts_from_phone_variant(
        _variant(phone_beat_receipt={"matcher": "failed", "placed": [], "unplaced": []})
    )
    assert facts.reaction_beats is None
    assert facts.reaction_beats_available is None
    receipt = check_requirement(R4, facts)
    assert not is_judged(R4, receipt)
    assert receipt.reason == "I can't check the pop-ins on this draft yet."


def test_a_talking_render_quotes_no_times_because_its_receipt_is_on_the_source_take() -> None:
    facts = plan_facts_from_phone_variant(_variant(resolved_archetype="subtitled"))

    assert all(b.at_s is None for b in facts.reaction_beats or ())
    assert [b.trigger for b in facts.reaction_beats or ()] == [
        "the medal",
        "four hours and twelve minutes",
    ]
    receipt = check_requirement(R4, facts)
    assert receipt.status == "met"
    assert receipt.reason is None


def test_an_unnamed_ask_is_not_blamed_for_another_beats_unheard_word() -> None:
    receipt_data = {
        **_PHONE_RECEIPT,
        "placed": [_PHONE_RECEIPT["placed"][1]],
        "unplaced": [{"beat_id": "medal", "trigger": "the medal", "reason": "never_heard"}],
    }
    facts = plan_facts_from_phone_variant(_variant(phone_beat_receipt=receipt_data))
    ask = _req("u1", "style", "pop up my photos at the exact words")

    receipt = check_requirement(ask, facts)

    assert receipt.status == "met"
    assert "never heard" not in (receipt.reason or "")


def test_a_cue_name_matching_no_beat_judges_the_pop_ins_as_a_whole() -> None:
    ask = _req("u2", "timing", "show the flag when voiceover says Berlin")
    facts = plan_facts_from_phone_variant(_variant())
    receipt = check_requirement(ask, facts)
    assert receipt.status == "met"
    assert receipt.reason is None  # no times: they belong to other words


def test_more_than_six_placements_read_with_one_and() -> None:
    beats = tuple(BeatFact(trigger=f"w{i}", visual_id=f"v{i}", at_s=float(i)) for i in range(8))
    facts = PlanFacts(reaction_beats_available=True, reaction_beats=beats)
    receipt = check_requirement(_req("p1", "style", "pop up my photos at the exact words"), facts)
    assert receipt.reason == (
        'Pop-ins on "w0" at 0.0 s, "w1" at 1.0 s, "w2" at 2.0 s, "w3" at 3.0 s, '
        '"w4" at 4.0 s, "w5" at 5.0 s and 2 more'
    )


# --------------------------------------------------------------- mix thresholds, ASCII Turkish


def test_a_bed_between_half_and_full_volume_is_not_far_enough_under_the_voice() -> None:
    loud = check_requirement(R3, plan_facts_from_phone_variant(_variant(voiceover_bed_level=0.6)))
    assert loud.status == "partial"
    assert (
        loud.reason == "The footage sound plays at about 60% alongside your voice, not far under it"
    )
    quiet = check_requirement(R3, plan_facts_from_phone_variant(_variant(voiceover_bed_level=0.49)))
    assert quiet.status == "met"


@pytest.mark.parametrize(
    "text",
    [
        "arka plan gurultusu sesimin altinda kalsin",
        "cekim sesi kisik kalsin",
        "kalabalik sesi seslendirmemin altinda kisik olsun",
    ],
)
def test_ascii_turkish_keep_it_quiet_asks_are_recognised(text: str) -> None:
    from app.kria.brief_checks import _wants_bed_under_voice  # noqa: PLC2701

    assert _wants_bed_under_voice(_req("a1", "audio", text))


def test_ascii_turkish_sticker_cue_and_cut_guard() -> None:
    assert _wants_beats(_req("a2", "style", "madalya dedigimde cikartma goster"))
    assert _wants_beats(_req("a3", "style", "madalya dediğimde çıkartma göster"))


# ------------------------------------- the pre-KRI-537 Talking phrasings still count


@pytest.mark.parametrize(
    "kind, text",
    [
        ("style", "add stickers for each food"),
        ("style", "pop up the pasta sticker"),
        ("select", "pop-ins on the exact words"),
        ("audio", "stamp a sticker on every dish"),
    ],
)
def test_an_explicit_sticker_or_pop_in_word_is_still_a_pop_in_ask_without_a_spoken_cue(
    kind: str, text: str
) -> None:
    req = _req("old1", kind, text)
    assert _wants_beats(req)
    facts = PlanFacts(
        edit_format="subtitled",
        reaction_beats_available=True,
        reaction_beats=(BeatFact("pasta", "asset-pasta.png"),),
    )
    assert check_requirement(req, facts).status == "met"


def test_a_timing_ask_with_a_pop_in_word_but_no_spoken_cue_stays_a_timing_ask() -> None:
    req = _req("old2", "timing", "make the text pop up quickly in the first second")
    assert not _wants_beats(req)
    receipt = check_requirement(req, _draft_facts())
    assert not is_judged(req, receipt)


# --------------------------------------- KRI-533 narrated deferral leaves pop-in asks alone


def test_a_pop_in_timing_ask_is_not_clip_timing_and_is_judged_at_draft(monkeypatch) -> None:
    from app.kria import brief_checks

    assert not brief_checks._wants_clip_timing(R4)
    assert brief_checks._wants_clip_timing(
        _req("ct1", "timing", "show the balloons while talking about the balloons")
    )
    monkeypatch.setattr(brief_checks, "defers_to_narrated_render", lambda **_kw: True)
    kept = brief_checks.requirements_to_check_at_draft(
        [R4, _req("ct1", "timing", "show the balloons while talking about the balloons")],
        creator_id="c",
        strategy={"edit_format": "narrated_planned", "audio_strategy": "voiceover"},
        item_edit_format="narrated_planned",
        clip_paths=["analysis-proxy-a.mp4"],
    )
    assert [r.id for r in kept] == ["r4"]

"""KRI-127: the on-screen text fence for open-vocabulary clip labels.
KRI-129 extends the SAME fence to intent-level captions (one on-screen
phrase for a whole chapter, rather than a per-clip tag).

Replaces the closed sport allowlist. A label/caption renders only when it is
the creator's own words, a span of what the vision model wrote about the clip
(a label: THAT clip; a caption: the UNION of every member clip, confidence >=
0.8), or a vision-verified answer (>= 0.8).
"""

import pytest

from app.schemas.clip_intents import (
    CAPTION_MAX_CHARS,
    CAPTION_MAX_WORDS,
    LABEL_MIN_CONFIDENCE,
    ClipIntent,
    ResolvedClipIntent,
    clean_caption_text,
    clean_label_text,
    ground_caption,
    ground_label,
)
from app.schemas.clip_understanding import ClipSpeech, ClipUnderstanding

REQUEST = 'Group by sport and add each sport name. Group the pub videos and say "post match pub".'


def _record(**overrides) -> ClipUnderstanding:
    base = {
        "subject": "people playing volleyball",
        "summary": "Friends rally on a sand court.",
        "setting": "outdoor beach volleyball court",
        "activity": "playing beach volleyball",
    }
    base.update(overrides)
    return ClipUnderstanding(**base)


def _ground(value, confidence=0.95, record=None, **kwargs):
    return ground_label(
        media_id="m1",
        value=value,
        confidence=confidence,
        creator_request=REQUEST,
        record=record or _record(),
        **kwargs,
    )


def test_record_span_label_is_grounded_above_the_threshold():
    label = _ground("Volleyball")

    assert label is not None
    assert label.text == "Volleyball"
    assert label.grounding == "record_span"


def test_record_span_label_below_the_threshold_is_omitted():
    assert _ground("Volleyball", confidence=LABEL_MIN_CONFIDENCE - 0.01) is None


def test_made_up_value_is_rejected_even_at_full_confidence():
    # The old allowlist test's adversarial case: confident prose with no grounding.
    assert _ground("Quidditch", confidence=0.99) is None
    # A plausible synonym the vision model never wrote is not grounded either.
    assert _ground("Football", confidence=0.99, record=_record(activity="playing soccer")) is None


def test_open_vocabulary_needs_no_code_change():
    dish = _record(
        subject="plate of food",
        summary="A bowl of ramen with egg.",
        setting="restaurant table",
        activity="eating ramen",
    )
    city = _record(
        subject="city skyline",
        summary="Lisbon rooftops at dusk.",
        setting="Lisbon",
        activity="panning across rooftops",
    )

    assert _ground("Ramen", record=dish).grounding == "record_span"
    assert _ground("Lisbon", record=city).grounding == "record_span"


def test_creator_words_are_grounded_without_any_clip_evidence():
    label = _ground("post match pub", confidence=0.0, record=ClipUnderstanding())

    assert label is not None
    assert label.grounding == "creator_text"
    assert label.text == "post match pub"


def test_creator_words_must_match_whole_words_not_letters_across_word_boundaries():
    request = "add the name of each sport, say it is great, then the pub"
    nothing = ClipUnderstanding()

    for run_on in ("Theme", "Sayit", "Tit", "Hes", "Ache"):  # letters spanning two words
        assert (
            ground_label(
                media_id="m1", value=run_on, confidence=1.0, creator_request=request, record=nothing
            )
            is None
        ), run_on
    for real in ("sport", "the pub", "Say It"):
        label = ground_label(
            media_id="m1", value=real, confidence=0.0, creator_request=request, record=nothing
        )
        assert label is not None and label.grounding == "creator_text", real


def test_every_word_of_a_label_must_be_grounded_including_short_ones():
    record = _record(subject="", summary="", setting="", activity="playing soccer")

    assert _ground("Soccer", record=record) is not None
    assert _ground("X Soccer", record=record) is None
    assert _ground("5 Soccer", record=record) is None


def test_vision_verified_answer_grounds_a_value_the_record_lacks():
    vague = _record(subject="people playing field sport", summary="", setting="", activity="")

    assert _ground("Soccer", record=vague) is None
    label = _ground("Soccer", record=vague, vision_answer="soccer", vision_confidence=0.9)
    assert label is not None
    assert label.grounding == "vision_verified"
    # A low-confidence or mismatching vision answer grounds nothing.
    assert _ground("Soccer", record=vague, vision_answer="soccer", vision_confidence=0.5) is None
    assert _ground("Rugby", record=vague, vision_answer="soccer", vision_confidence=0.95) is None


def test_spoken_transcript_never_grounds_a_label():
    spoken = ClipUnderstanding(
        subject="man on grass", speech=ClipSpeech(has_speech=True, transcript="we love tennis")
    )

    assert _ground("Tennis", confidence=0.99, record=spoken) is None


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "a very long label that goes on",
        "four words are too many",
        "http://evil.example",
        "Soccer!!!",
        "<b>Soccer</b>",
        "12345",
        None,
        42,
    ],
)
def test_unsafe_or_oversized_copy_can_never_be_a_label(value):
    assert clean_label_text(value) is None
    assert _ground(value, confidence=1.0) is None


def test_accents_and_case_do_not_block_grounding():
    record = _record(activity="jugando al fútbol", subject="", summary="", setting="")

    label = _ground("Futbol", record=record)

    assert label is not None
    assert label.text == "Futbol"


def test_intent_models_are_open_vocabulary():
    intent = ClipIntent(intent_id="i1", op="label", attribute="  the dish on each   food clip ")
    assert intent.attribute == "the dish on each food clip"

    resolved = ResolvedClipIntent(
        intent_id="i2",
        op="group",
        attribute="pub videos",
        creator_text="post match pub",
        assignments=[{"media_id": "m30", "confidence": 0.9, "evidence": "cafe interior"}],
    )
    assert resolved.media_ids() == ["m30"]
    assert resolved.status == "resolved"


# ── KRI-129: intent-level captions (one phrase for a whole chapter) ─────────


def _caption_ground(value, confidence=0.95, records=None, **kwargs):
    return ground_caption(
        value=value,
        confidence=confidence,
        creator_request=REQUEST,
        records=records if records is not None else [_record()],
        **kwargs,
    )


def test_caption_creator_text_grounded_without_any_clip_evidence():
    caption = _caption_ground("post match pub", confidence=0.0, records=[ClipUnderstanding()])

    assert caption is not None
    assert caption.grounding == "creator_text"
    assert caption.text == "post match pub"


def test_caption_record_span_across_the_union_of_two_clips():
    """Neither clip alone contains every word of the phrase, only the union."""
    request = "add a caption about the weather on the park clips"
    clip_a = _record(subject="rainy park bench", summary="", setting="", activity="")
    clip_b = _record(subject="", summary="", setting="", activity="a windy afternoon walk")

    assert (
        ground_caption(
            value="rainy windy", confidence=0.9, creator_request=request, records=[clip_a]
        )
        is None
    )
    assert (
        ground_caption(
            value="rainy windy", confidence=0.9, creator_request=request, records=[clip_b]
        )
        is None
    )
    caption = ground_caption(
        value="rainy windy", confidence=0.9, creator_request=request, records=[clip_a, clip_b]
    )
    assert caption is not None
    assert caption.grounding == "record_span"
    assert caption.text == "rainy windy"


def test_caption_vision_verified_answer_grounds_a_phrase_the_records_lack():
    vague = _record(subject="people at a park", summary="", setting="", activity="")

    assert (
        ground_caption(
            value="cold and rainy", confidence=0.9, creator_request=REQUEST, records=[vague]
        )
        is None
    )
    caption = ground_caption(
        value="cold and rainy",
        confidence=0.0,
        creator_request=REQUEST,
        records=[vague],
        vision_answer="cold and rainy",
        vision_confidence=0.9,
    )
    assert caption is not None
    assert caption.grounding == "vision_verified"
    # A low-confidence or mismatching vision answer grounds nothing.
    assert (
        ground_caption(
            value="cold and rainy",
            confidence=0.0,
            creator_request=REQUEST,
            records=[vague],
            vision_answer="cold and rainy",
            vision_confidence=0.5,
        )
        is None
    )
    assert (
        ground_caption(
            value="hot and sunny",
            confidence=0.0,
            creator_request=REQUEST,
            records=[vague],
            vision_answer="cold and rainy",
            vision_confidence=0.95,
        )
        is None
    )


def test_caption_made_up_phrase_is_rejected_even_at_full_confidence():
    assert _caption_ground("a magical festival of lights", confidence=0.99) is None


def test_spoken_transcript_never_grounds_a_caption():
    spoken = ClipUnderstanding(
        subject="man on grass", speech=ClipSpeech(has_speech=True, transcript="we love tennis")
    )

    assert (
        ground_caption(
            value="tennis match today", confidence=0.99, creator_request=REQUEST, records=[spoken]
        )
        is None
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "this phrase has way more than ten words in it so it should be rejected",
        "<script>alert(1)</script>",
        None,
        42,
        "12345",
    ],
)
def test_unsafe_or_oversized_copy_can_never_be_a_caption(value):
    assert clean_caption_text(value) is None
    assert _caption_ground(value, confidence=1.0) is None


def test_caption_allows_ordinary_punctuation_unlike_a_label():
    text = clean_caption_text("what a day, huh?!")
    assert text == "what a day, huh?!"


def test_caption_word_and_char_limits_match_constants():
    assert CAPTION_MAX_WORDS == 10
    assert CAPTION_MAX_CHARS == 60
    ten_words = " ".join(["word"] * 10)
    eleven_words = " ".join(["word"] * 11)
    assert clean_caption_text(ten_words) == ten_words
    assert clean_caption_text(eleven_words) is None


def test_caption_intent_model_accepts_op_and_caption_attribute():
    intent = ClipIntent(
        intent_id="i3",
        op="caption",
        attribute="the park clips",
        caption_attribute="  the weather  ",
    )
    assert intent.op == "caption"
    assert intent.caption_attribute == "the weather"

    resolved = ResolvedClipIntent(
        intent_id="i4",
        op="caption",
        attribute="the food clips",
        creator_text="post match feast",
        caption_text="post match feast",
        caption_grounding="creator_text",
        assignments=[{"media_id": "m1", "confidence": 0.9, "evidence": "food on table"}],
    )
    assert resolved.media_ids() == ["m1"]
    assert resolved.assignments[0].value is None  # membership only, like group/order/include
    assert resolved.caption_text == "post match feast"
    assert resolved.caption_grounding == "creator_text"


def test_unused_intent_fields_never_appear_in_stored_strategy_or_brief():
    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.schemas.edit_proposal import ProposalBrief

    brief = ProposalBrief().model_dump(mode="json")
    assert "clip_intents" not in brief

    fields = set(CreativeStrategy.model_fields)
    assert {"clip_intents", "resolved_clip_intents"} <= fields
    required = {n: "x" for n, f in CreativeStrategy.model_fields.items() if f.is_required()}
    if not required:
        dumped = CreativeStrategy().model_dump(mode="json")
        assert "clip_intents" not in dumped
        assert "resolved_clip_intents" not in dumped

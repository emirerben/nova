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
    CREATOR_CAPTION_MAX_CHARS,
    LABEL_MIN_CONFIDENCE,
    ClipIntent,
    ResolvedClipIntent,
    clean_caption_text,
    clean_creator_caption_text,
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


def test_kri422_long_model_intent_id_is_shortened_deterministically_not_rejected():
    """The id is a model-minted handle: its length never invalidates an instruction.

    In-bound ids (every persisted intent) re-validate byte-identically.
    """
    from app.agents._schemas.creator_agent import CreativeStrategy

    long_id = "caption_guy_glasses_grey_tshirt_skating_video"  # 45 chars
    kept = ClipIntent(
        intent_id="caption_bakery_photo", op="caption", attribute="a", creator_text="b"
    )
    assert kept.intent_id == "caption_bakery_photo"
    exact = "x" * 40
    assert ClipIntent(intent_id=exact, op="label", attribute="a").intent_id == exact

    first = ClipIntent(intent_id=long_id, op="caption", attribute="a", creator_text="b")
    again = ResolvedClipIntent(intent_id=long_id, op="caption", attribute="a", creator_text="b")
    assert len(first.intent_id) == 40
    assert first.intent_id == again.intent_id
    assert first.intent_id.startswith(long_id[:31])
    other = ClipIntent(intent_id=long_id + "_2", op="caption", attribute="a", creator_text="b")
    assert other.intent_id != first.intent_id

    # The Main Creator's strategy hints carry the same model-minted ids.
    strategy = CreativeStrategy.model_validate(
        {
            "clip_intents": [
                {"intent_id": long_id, "op": "caption", "attribute": "a", "creator_text": "b"}
            ]
        }
    )
    assert strategy.clip_intents[0].intent_id == first.intent_id

    with pytest.raises(ValueError):
        ClipIntent(intent_id="", op="label", attribute="a")


# ── Creator caption copy (2026-10-04 La Mercè chat) ──────────────────────────
# The creator's own word-for-word chapter line is not a model-authored phrase:
# the 60-char / 10-word / narrow-charset shape bounds only what a MODEL may write.

_MERCE_LINE = "It's La Mercè, Barcelona's biggest festival. Every September."
_GAUDI_LINE = (
    "#1 Antoni Gaudí gave Barcelona its skyline, and he’s buried inside his unfinished church…"
)
_CHAPTER_REQUEST = (
    "Show each chapter line word for word. Chapter 5 · fireworks · 4 seconds · "
    f"{_MERCE_LINE} Chapter 6 · Gaudí · 5 s · {_GAUDI_LINE}"
)


@pytest.mark.parametrize("line", [_MERCE_LINE, _GAUDI_LINE])
def test_creator_caption_copy_is_grounded_verbatim_past_the_phrase_bounds(line):
    assert clean_caption_text(line) is None  # the authored-phrase shape still refuses it
    caption = ground_caption(
        value=line,
        confidence=1.0,
        creator_request=_CHAPTER_REQUEST,
        records=[ClipUnderstanding()],
        creator_copy=True,
    )
    assert caption is not None
    assert caption.grounding == "creator_text"
    assert caption.text == line


def test_creator_caption_copy_must_still_be_the_creators_words():
    invented = "A magical night of fireworks over the old harbour of Barcelona."
    assert (
        ground_caption(
            value=invented,
            confidence=1.0,
            creator_request=_CHAPTER_REQUEST,
            records=[ClipUnderstanding()],
            creator_copy=True,
        )
        is None
    )


@pytest.mark.parametrize("value", ["", "   ", "…", None, 42, "x" * 201, "bad\x01copy here"])
def test_creator_caption_copy_rejects_empty_oversized_or_control_text(value):
    request = f"{_CHAPTER_REQUEST} {value}" if isinstance(value, str) else _CHAPTER_REQUEST
    assert clean_creator_caption_text(value) is None
    assert (
        ground_caption(
            value=value,
            confidence=1.0,
            creator_request=request,
            records=[ClipUnderstanding()],
            creator_copy=True,
        )
        is None
    )


def test_authored_captions_keep_the_short_phrase_bounds():
    """Without creator_copy (a resolver-authored phrase) the old fence is unchanged."""
    assert (
        ground_caption(
            value=_MERCE_LINE,
            confidence=1.0,
            creator_request=_CHAPTER_REQUEST,
            records=[ClipUnderstanding()],
        )
        is None
    )


def test_intent_models_hold_long_creator_caption_copy_untruncated():
    assert CREATOR_CAPTION_MAX_CHARS == 200
    intent = ClipIntent(
        intent_id="c5", op="caption", attribute="Chapter 5", creator_text=_GAUDI_LINE
    )
    assert intent.creator_text == _GAUDI_LINE
    resolved = ResolvedClipIntent(
        **intent.model_dump(), caption_text=_GAUDI_LINE, caption_grounding="creator_text"
    )
    assert resolved.caption_text == _GAUDI_LINE

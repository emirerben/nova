"""KRI-826: the creator's own label words print exactly as typed (Don Julio M1, Y06).

`Put "chimichurri + salsa criolla" on the sauce bowls` was refused ("a label can't show
+"), and "chimichurri & salsa criolla" was refused as too long. The creator's request is
the label on the sauce bowls, with the "+", so that is the only passing outcome here.

The planner and resolver are the REAL parsing code over recorded model output (the same
harness as KRI-804); the placement is checked by the real render contract and the real
phone-recipe verifier. No paid model runs.

Ways this can fail, written down before the code:

 1. "chimichurri + salsa criolla" is refused for its "+" instead of printing on the bowls.
 2. "chimichurri & salsa criolla" asks "which clip" or says too long.
 3. The model turns "+" into "and", adds "!" or "🔥", or recases the words, and THAT
    prints: the printed text must be the creator's own span; an invented symbol grounds
    nothing.
 4. Model, vision or record copy gets the creator's freedom ("+", a 4th word).
 5. The right words on the wrong clip, or a rendered text missing the "+", passes the
    verifier (wrong-source and stripped-text controls).
 6. A line break, "’", an emoji (with its ZWJ / variation selector) or Turkish letters
    are lost on the way to the stored label, or the stored row re-dumps differently.
 7. Words that truly can't fit the label bar are cut short instead of an honest reply.
 8. Persisted labels and intents written before this change no longer validate, or
    re-dump differently.
"""

from __future__ import annotations

import pytest

from app.schemas.clip_intents import (
    ClipAssignment,
    ClipIntent,
    ClipUnderstanding,
    GroundedLabel,
    ResolvedClipIntent,
    clean_creator_caption_text,
    clean_creator_label_text,
    creator_label_problems,
    ground_caption,
    ground_label,
)
from app.services.creator_render_contract import (
    CreatorRenderContractError,
    build_render_contract,
    verify_phone_recipe,
)
from tests.kria.test_kri804_label_text_reply import (
    AMP,
    BOWLS,
    LONG,
    PLUS,
    POINTS,
    WAITER,
    _brief,
    _group_recipe,
    _plan,
    _prompt,
    _row,
)

RECORD = ClipUnderstanding(subject="three wooden sauce bowls close up with bread")
TURKISH = "Kadıköy’de çay ☕️ ve simit 👨‍👩‍👧"


def _label(value: str, request: str, **kwargs) -> GroundedLabel | None:
    return ground_label(
        media_id=BOWLS,
        value=value,
        confidence=1.0,
        creator_request=request,
        record=RECORD,
        **kwargs,
    )


# 1, 2, 5: the whole planning turn, then the render contract and the phone verifier ------


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [PLUS, AMP, "Julio’s 🔥 asado", TURKISH])
async def test_the_creators_words_print_on_the_sauce_bowls_as_typed(monkeypatch, text):
    models, planned = await _plan(monkeypatch, [_row("label", text)], messages=[_prompt(text)])

    resolution = planned.resolution
    assert not resolution.needs_creator, resolution.question
    assert resolution.error_code is None
    assert models.resolved == [("text_bowls", "label", text)]
    (label,) = resolution.intents
    assert [(a.media_id, a.value, a.grounding) for a in label.assignments] == [
        (BOWLS, text, "creator_text")
    ]
    # Byte-identical, including the "+", the "’", the emoji and its joiners.
    assert label.assignments[0].value.encode() == text.encode()

    contract = build_render_contract(
        {"resolved_clip_intents": [label.model_dump(mode="json")]},
        generation_id="g",
        brief=_brief(text),
    )
    assert contract is not None and not contract.unresolved
    assert [(t.role, t.text, t.media_id) for t in contract.exact_texts] == [("clip", text, BOWLS)]
    assert verify_phone_recipe(contract, _group_recipe([BOWLS, POINTS, WAITER], [(0.1, 0.9)], text))


@pytest.mark.parametrize(
    ("media_ids", "printed"),
    [
        ([WAITER, BOWLS, POINTS], PLUS),  # the right words on the wrong clip
        ([BOWLS, POINTS, WAITER], "chimichurri salsa criolla"),  # the "+" stripped
        ([BOWLS, POINTS, WAITER], "chimichurri and salsa criolla"),  # "+" rewritten
    ],
    ids=["wrong-source", "stripped-plus", "rewritten-plus"],
)
def test_wrong_clip_or_altered_words_fail_the_verifier(media_ids, printed) -> None:
    label = ResolvedClipIntent(
        intent_id="text_bowls",
        op="label",
        attribute="the sauce bowls",
        creator_text=PLUS,
        assignments=[ClipAssignment(media_id=BOWLS, value=PLUS, grounding="creator_text")],
    )
    contract = build_render_contract(
        {"resolved_clip_intents": [label.model_dump(mode="json")]},
        generation_id="g",
        brief=_brief(PLUS),
    )
    assert contract is not None and not contract.unresolved
    with pytest.raises(CreatorRenderContractError, match="text"):
        verify_phone_recipe(contract, _group_recipe(media_ids, [(0.1, 0.9)], printed))


# 3: what prints is the creator's span, never the model's copy -------------------------


@pytest.mark.parametrize(
    ("model_value", "printed"),
    [
        (PLUS, PLUS),
        ("Chimichurri + Salsa Criolla", PLUS),  # the model's casing never prints
        ("chimichurri salsa criolla", PLUS),  # a dropped "+" is the creator's: it prints
        ('"chimichurri + salsa criolla"', PLUS),  # the chat's quotes are not the label
        ("chimichurri and salsa criolla", None),  # "+" -> "and": a different word list
        ("chimichurri + salsa criolla!", None),  # an invented "!"
        ("chimichurri + salsa criolla 🔥", None),  # an invented emoji
    ],
)
def test_only_the_creators_own_characters_print(model_value, printed) -> None:
    label = _label(model_value, _prompt(PLUS))
    assert (label.text if label else None) == printed
    if label is not None:
        assert label.grounding == "creator_text"


def test_a_symbol_next_to_the_words_prints_only_where_the_creator_wrote_it() -> None:
    request = 'Label the grill clip "asado 🔥" and the sauce bowls "Julio’s chimichurri"'
    assert _label("asado 🔥", request).text == "asado 🔥"
    assert _label("Julio's chimichurri", request).text == "Julio’s chimichurri"
    assert _label("🔥 Julio’s chimichurri", request) is None


# 4: the taste rules stay for every other source --------------------------------------


def test_model_and_vision_copy_keep_the_label_charset_and_the_short_tag() -> None:
    for value in ("salsa + bread", "three sauce bowls today"):
        assert _label(value, "label each dish", vision_answer=value, vision_confidence=1.0) is None
    record_line = _label("sauce bowls", "label each dish")
    assert record_line is not None and record_line.grounding == "record_span"


# 6: line breaks, apostrophes, emoji and Turkish survive storage ------------------------


def test_a_typed_line_break_survives_the_label_and_the_caption() -> None:
    request = 'Put "chimichurri +\nsalsa criolla" on the sauce bowls'
    label = _label("chimichurri + salsa criolla", request)
    assert label is not None and label.text == "chimichurri +\nsalsa criolla"
    assert GroundedLabel.model_validate_json(label.model_dump_json()) == label
    caption = ground_caption(
        value="chimichurri + salsa criolla",
        confidence=1.0,
        creator_request=request,
        records=[RECORD],
        creator_copy=True,
    )
    assert caption is not None and caption.text == "chimichurri +\nsalsa criolla"
    intent = ClipIntent(
        intent_id="i", op="caption", attribute="bowls", creator_text="one\n\n\n two  "
    )
    assert intent.creator_text == "one\n\ntwo"
    assert ClipIntent.model_validate_json(intent.model_dump_json()) == intent


def test_turkish_letters_and_emoji_sequences_are_kept_byte_for_byte() -> None:
    assert clean_creator_label_text(TURKISH) == TURKISH
    assert clean_creator_caption_text(TURKISH) == TURKISH
    # Invisible characters that never show are dropped; the ones emoji need stay.
    assert clean_creator_label_text("Kadıköy​’de") == "Kadıköy’de"
    stored = ClipAssignment(media_id=BOWLS, value=TURKISH, grounding="creator_text")
    assert ClipAssignment.model_validate_json(stored.model_dump_json()).value == TURKISH


# 7: an honest limit, never a cut ---------------------------------------------------------


def test_words_past_the_label_bar_are_refused_whole_never_cut() -> None:
    assert creator_label_problems(LONG) == ("too_long",)
    assert clean_creator_label_text(LONG) is None
    assert _label(LONG, _prompt(LONG)) is None


# 8: rows stored before this change re-validate and re-dump byte-identically ------------


@pytest.mark.parametrize(
    "row",
    [
        {
            "media_id": "m1",
            "text": "Parque de la Costa",
            "grounding": "creator_text",
            "confidence": 1.0,
            "intent_id": "i1",
        },
        {
            "media_id": "m1",
            "text": "Volleyball",
            "grounding": "record_span",
            "confidence": 0.9,
            "intent_id": "",
        },
    ],
)
def test_stored_grounded_labels_redump_byte_identically(row) -> None:
    assert GroundedLabel.model_validate(row).model_dump(mode="json") == row


def test_stored_intents_redump_byte_identically() -> None:
    row = {
        "intent_id": "text_bowls",
        "op": "label",
        "attribute": "the sauce bowls",
        "creator_text": "chimichurri & salsa criolla",
        "caption_attribute": None,
        "position": None,
        "status": "resolved",
        "assignments": [
            {
                "media_id": BOWLS,
                "value": "chimichurri & salsa criolla",
                "evidence": "bowls",
                "confidence": 0.95,
                "grounding": "creator_text",
            }
        ],
        "question": None,
        "caption_text": None,
        "caption_grounding": None,
    }
    assert ResolvedClipIntent.model_validate(row).model_dump(mode="json") == row

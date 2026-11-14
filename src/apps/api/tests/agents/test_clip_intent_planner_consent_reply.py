"""A one-word "yes" keeps the caption the creator already asked for (Tadic, thread aa36d140).

Prod 2026-11-14: one clip, "add the lyrics as text to this video", then the pasted lyrics, then
"Yap" ("do it"). The re-plan's caption intent quoted the earlier messages, the fence rejected it
(`planner_rejected_all`, `source_quote_not_creator_text`), and the creator read "I understood 0
of your clip instructions. I couldn't safely verify "yazı: this video"". The words below are
invented stand-ins of the same shape (an ASCII-typed Turkish request, a pasted verse).

Failure modes, written before the fix:
1. The model restores diacritics the creator typed without ("Yildizlar" -> "Yıldızlar") in a
   quote from an earlier message: kept, with the quote as the creator typed it.
2. The same respelling in ``creator_text``: kept, and printed as the creator typed it.
3. A quote joining the request and the pasted verse across the message seam, with a ":" the
   model added: kept, with the verse (which holds the text) as the quote.
4. A quote that appears in no creator message (the brief's wording, an invented sentence):
   still dropped as `source_quote_not_creator_text`.
5. A quote that changes a word, not only letters' accents or case: still dropped.
6. The question for a dropped instruction never shows the internal "op: attribute" form
   ("yazı: this video", "caption: ..."), in Turkish or English.
7. On-screen text whose capitals changed is not the creator's text (KRI-950): still asked,
   even though a quote with changed capitals is accepted as provenance.

Found in review, written before the second fix:
8. The quote is the original request (as typed, or respelled) and the on-screen text is the
   verse pasted in its own message, so never inside the quote: kept.
9. Both repairs at once, a ":" at the seam and a respelled verse: kept.
10. On-screen text that is only a phrase inside another message, not lines the creator
    pasted on their own, outside the quote: still asked (`creator_text_not_source_backed`).
"""

from __future__ import annotations

import json

import pytest

from app.agents.clip_intent_planner import (
    ClipIntentPlannerAgent,
    ClipIntentPlannerInput,
    ClipIntentSchemaError,
    salvage_question,
)

_REQUEST = 'Find the words to the anthem "Yildizlar Bizimle" and add it as text to this video'
_VERSE = "Gökyüzü bizim olsun Yollar uzasa da Kalbimizde saklı Yildizlar seninle"
_RESPELLED_VERSE = "Gökyüzü bizim olsun Yollar uzasa da Kalbimizde saklı Yıldızlar seninle"


def _consent_input() -> ClipIntentPlannerInput:
    # What the runtime sends on "Yap": every creator message in order, then the reply itself.
    return ClipIntentPlannerInput(
        creator_request="\n".join([_REQUEST, _VERSE, "Yap"]),
        latest_user_message="Yap",
        generated_brief="Gönderilen sözleri videoya yazı olarak ekle.",
        reply_language="tr",
    )


def _caption(source_quote: str, creator_text: str = _VERSE) -> str:
    return json.dumps(
        {
            "intents": [
                {
                    "intent_id": "verse",
                    "op": "caption",
                    "attribute": "this video",
                    "creator_text": creator_text,
                    "caption_attribute": None,
                    "source_quote": source_quote,
                }
            ],
            "question": None,
        },
        ensure_ascii=False,
    )


def _parse(raw: str):  # noqa: ANN202
    return ClipIntentPlannerAgent(None).parse(raw, _consent_input())  # type: ignore[arg-type]


def test_a_respelled_quote_from_an_earlier_message_keeps_the_caption() -> None:
    out = _parse(_caption(_RESPELLED_VERSE))

    [intent] = out.intents
    assert intent.source_quote == _VERSE
    assert intent.creator_text == _VERSE
    assert out.salvage_question is None


def test_respelled_on_screen_text_prints_as_the_creator_typed_it() -> None:
    out = _parse(_caption(_RESPELLED_VERSE, creator_text=_RESPELLED_VERSE))

    [intent] = out.intents
    assert intent.creator_text == _VERSE
    assert intent.source_quote == _VERSE


def test_on_screen_text_in_other_capitals_is_still_asked() -> None:
    with pytest.raises(ClipIntentSchemaError) as raised:
        _parse(_caption(_VERSE.upper(), creator_text=_VERSE.upper()))

    assert raised.value.drop_classes == ["creator_text_not_source_backed"]


def test_a_quote_across_the_message_seam_keeps_the_caption() -> None:
    out = _parse(_caption(f"add it as text to this video: {_VERSE}"))

    [intent] = out.intents
    assert intent.source_quote == _VERSE
    assert intent.creator_text == _VERSE


@pytest.mark.parametrize(
    "quote",
    [
        "Gönderilen sözleri videoya yazı olarak ekle.",  # the generated brief's wording
        "Put the anthem words over the whole clip",  # in no message at all
        "Gökyüzü bizim olsun Yollar uzasa bile",  # a changed word, not a changed accent
    ],
)
def test_a_quote_no_creator_message_holds_is_still_dropped(quote: str) -> None:
    with pytest.raises(ClipIntentSchemaError) as raised:
        _parse(_caption(quote))

    assert raised.value.drop_classes == ["source_quote_not_creator_text"]
    # The question the runtime builds from the drop (`plan_and_resolve_clip_intents`).
    question = salvage_question(0, raised.value.dropped, language="tr")
    assert "yazı:" not in question
    assert ": this video" not in question
    assert "Gökyüzü bizim olsun" in question


def test_the_question_never_names_an_instruction_by_its_operation() -> None:
    raw = json.dumps(
        {
            "intents": [
                {
                    "intent_id": "keep",
                    "op": "include",
                    "attribute": "this video",
                    "source_quote": "add it as text to this video",
                },
                {
                    "intent_id": "made-up",
                    "op": "label",
                    "attribute": "the anthem",
                    "source_quote": "Label the anthem",
                },
            ],
            "question": None,
        }
    )
    for language in ("tr", "en"):
        out = ClipIntentPlannerAgent(None).parse(  # type: ignore[arg-type]
            raw, _consent_input().model_copy(update={"reply_language": language})
        )
        assert out.salvage_question is not None
        assert '"the anthem"' in out.salvage_question
        assert "label:" not in out.salvage_question
        assert "etiket:" not in out.salvage_question


@pytest.mark.parametrize(
    "quote",
    [_REQUEST, _REQUEST.replace("Yildizlar", "Yıldızlar")],
    ids=["as-typed", "respelled"],
)
def test_the_request_as_quote_keeps_the_verse_pasted_after_it(quote: str) -> None:
    out = _parse(_caption(quote))

    [intent] = out.intents
    assert intent.source_quote == _REQUEST
    assert intent.creator_text == _VERSE


def test_a_seam_quote_with_a_respelled_verse_keeps_the_caption() -> None:
    out = _parse(_caption(f"add it as text to this video: {_RESPELLED_VERSE}", _RESPELLED_VERSE))

    [intent] = out.intents
    assert intent.source_quote == _VERSE
    assert intent.creator_text == _VERSE


def test_a_phrase_from_another_message_is_still_asked() -> None:
    with pytest.raises(ClipIntentSchemaError) as raised:
        _parse(_caption(_VERSE, creator_text="add it as text"))

    assert raised.value.drop_classes == ["creator_text_not_source_backed"]

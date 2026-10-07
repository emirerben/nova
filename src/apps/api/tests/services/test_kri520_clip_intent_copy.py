"""KRI-520: clip-intent questions reach a Turkish creator in Turkish.

Two layers speak to the creator when a clip request cannot be settled: the server's own
templates (`clip_intent_resolution`, `clip_intent_planning`, the planner's salvage question)
and the two agents that author a `question` (clip-intent planner, clip request resolver).
English copy must stay byte-identical; the model-facing vision questions stay English on
purpose (their answers are parsed as yes/no/"unknown").
"""

from __future__ import annotations

import json

import pytest

from app.agents._runtime import RunContext
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents.clip_intent_planner import (
    ClipIntentPlannerAgent,
    ClipIntentPlannerInput,
    _preview,
    _repair_placeholder,
    _repair_position,
    salvage_question,
)
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverInput,
    ResolverClipIn,
    ResolverIntentIn,
)
from app.kria.reply_language import prompt_language_line, reply_language_for
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import ClipIntent, GroundedLabel, ground_label
from app.services.clip_intent_planning import plan_and_resolve_clip_intents
from app.services.clip_intent_resolution import (
    _build_question,
    _build_resolver_input,
    _caption_authoring_question,
    _caption_question,
    _fallback_question,
    _generic_question,
    _IntentWork,
    _membership_question,
    _position_phrase,
)

TR = "Creator chat language: Turkish (tr)."


def _intent(intent_id: str = "i1", op: str = "label", attribute: str = "sport") -> ClipIntent:
    return ClipIntent(intent_id=intent_id, op=op, attribute=attribute)  # type: ignore[arg-type]


def _work(intent: ClipIntent, *failed: str, **kwargs) -> _IntentWork:
    return _IntentWork(intent=intent, failed_media_ids=set(failed), **kwargs)


# ── server templates in clip_intent_resolution ──────────────────────────────────────


def test_position_phrase_in_both_languages() -> None:
    assert _position_phrase([3]) == "clip 3"
    assert _position_phrase([1, 2, 3]) == "clips 1, 2 and 3"
    assert _position_phrase(list(range(1, 9))) == "clips 1, 2, 3, 4, 5 and 3 more"
    with reply_language_for("tr"):
        assert _position_phrase([3]) == "klip 3"
        assert _position_phrase([1, 2, 3]) == "klip 1, 2 ve 3"
        assert _position_phrase(list(range(1, 9))) == "klip 1, 2, 3, 4, 5 ve 3 klip daha"


def test_build_question_english_is_unchanged() -> None:
    sport = _intent(attribute="sport")
    works = [_work(sport, "a", "b"), _work(_intent("i2", "group", "pub clips"))]
    position = {"a": 1, "b": 4}

    assert _build_question(works, position) == (
        "I couldn't tell the sport for clips 1 and 4; "
        'I couldn\'t find any clips for "pub clips". Could you clarify?'
    )
    assert _build_question([], position) == _generic_question()
    assert _generic_question() == (
        "I couldn't match that request to your clips automatically — can you "
        "tell me which clips you mean?"
    )


def test_build_question_in_turkish() -> None:
    sport = _intent(attribute="sport")
    works = [_work(sport, "a", "b"), _work(_intent("i2", "group", "pub clips"))]
    position = {"a": 1, "b": 4}

    with reply_language_for("tr"):
        question = _build_question(works, position)

    assert question == (
        'Anlayamadım: sport (klip 1 ve 4); Şunun için klip bulamadım: "pub clips". '
        "Biraz açıklar mısın?"
    )
    # The creator's own words (the attribute) stay as written; no English template is left.
    assert "couldn't" not in question and "clip 1" not in question


def test_build_question_placeholder_and_model_question_in_turkish() -> None:
    placeholder = ClipIntent(
        intent_id="p", op="label", attribute="shots of people", placeholder=True
    )
    asked = _work(_intent("q", "group", "the good ones"), intent_question="Hangi klipler iyi?")
    with reply_language_for("tr"):
        question = _build_question([_work(placeholder, "a"), _work(placeholder), asked], {"a": 2})

    assert question.startswith('Şunların "shots of people" olup olmadığını anlayamadım: klip 2;')
    assert 'İsim yer tutucusunu koyacak "shots of people" klibi bulamadım' in question
    assert "Hangi klipler iyi?" in question  # the resolver's own (already Turkish) question
    assert question.endswith(". Biraz açıklar mısın?")


def test_build_question_too_long_falls_back_in_the_chat_language() -> None:
    long_works = [_work(_intent(f"i{n}", "group", f"{n}" + "x" * 60)) for n in range(8)]

    assert _build_question(long_works, {}) == (
        "I couldn't resolve all requested clip matches. Could you clarify?"
    )
    with reply_language_for("tr"):
        assert _build_question(long_works, {}) == (
            "İstediğin klip eşleşmelerinin hepsini çözemedim. Biraz açıklar mısın?"
        )


def test_caption_question_in_both_languages() -> None:
    caption = _intent(op="caption", attribute="pub clips")
    assert _caption_question(caption) == "What should the caption on the pub clips say?"
    with reply_language_for("tr"):
        assert _caption_question(caption) == '"pub clips" kliplerindeki yazı ne olsun?'


def test_vision_questions_stay_english_in_a_turkish_chat() -> None:
    """Their answers are parsed as yes/no/"unknown" and printed as labels; never translate."""
    intent = _intent(attribute="spor")
    with reply_language_for("tr"):
        assert _fallback_question(intent) == "What is the spor?"
        assert _membership_question("spor") == (
            'Does this clip match this description: "spor"? Answer only "yes" or "no".'
        )
        assert _caption_authoring_question(intent).startswith("In a short phrase")


def test_resolver_input_carries_the_chat_language() -> None:
    clips = []
    request = "Label each sport."

    plain, _, _ = _build_resolver_input([_intent()], request, clips)
    assert plain.reply_language is None
    assert "reply_language" not in plain.model_dump()

    with reply_language_for("tr"):
        turkish, _, _ = _build_resolver_input([_intent()], request, clips)
    assert turkish.reply_language == "tr"
    with reply_language_for("en"):
        english, _, _ = _build_resolver_input([_intent()], request, clips)
    assert english.reply_language == "en"


# ── clip_intent_planning ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_over_long_request_is_answered_in_the_chat_language() -> None:
    too_long = "x" * (CREATOR_REQUEST_MAX_CHARS + 1)

    async def ask() -> str | None:
        planned = await plan_and_resolve_clip_intents(
            creator_request=too_long,
            latest_user_message=None,
            candidate_intents=None,
            clips=[],
            run_context=RunContext(),
        )
        return planned.resolution.question

    assert await ask() == (
        "Please restate the complete clip instructions in a shorter message "
        "so I can preserve all of them."
    )
    with reply_language_for("tr"):
        turkish = await ask()
    assert turkish is not None
    assert turkish.startswith("Klip talimatlarının hepsini daha kısa bir mesajda")


# ── planner agent ───────────────────────────────────────────────────────────────────


def test_salvage_question_english_is_unchanged() -> None:
    assert salvage_question(2, ["label: sport", "group: pub"], 0) == (
        'I understood 2 of your clip instructions. I couldn\'t safely verify "label: sport"; '
        '"group: pub". Please restate just those so I can add them.'
    )
    assert salvage_question(16, [], 3) == (
        "I understood 16 of your clip instructions. I can apply at most 16 clip "
        "instructions at a time, so 3 more weren't included. Please restate just those so "
        "I can add them."
    )


def test_salvage_question_in_turkish() -> None:
    labels = ["label: sport", "group: pub", "order: park", "caption: bar"]
    text = salvage_question(2, labels, 3, language="tr")

    assert text == (
        "Klip talimatlarından 2 tanesini anladım. Şunları güvenle doğrulayamadım: "
        '"label: sport"; "group: pub"; "order: park" (ve 1 tane daha); '
        "Bir seferde en fazla 16 klip talimatı uygulayabiliyorum, bu yüzden 3 talimat daha "
        "eklenmedi. Sadece bunları yeniden yazar mısın, ekleyeyim."
    )
    # The turn's bound language is used when the input carries none.
    with reply_language_for("tr"):
        assert salvage_question(1, []) == (
            "Klip talimatlarından 1 tanesini anladım. Sadece bunları yeniden yazar mısın, "
            "ekleyeyim."
        )


def test_preview_names_the_operation_in_turkish() -> None:
    raw = {"op": "caption", "attribute": "pub clips"}
    assert _preview(raw) == "caption: pub clips"
    assert _preview(raw, "tr") == "yazı: pub clips"
    assert _preview("nope", "tr") == "bir talimat"
    assert _preview("nope") == "an instruction"


def test_planner_parse_asks_the_salvage_question_in_the_input_language() -> None:
    request = "Etiket olarak spor ekle ve pub klipleri grupla."
    raw = json.dumps(
        {
            "intents": [
                {
                    "intent_id": "sport",
                    "op": "label",
                    "attribute": "spor",
                    "source_quote": "spor ekle",
                },
                {
                    "intent_id": "pub",
                    "op": "include",
                    "attribute": "pub klipleri",
                    "source_quote": "this is not in the request",
                },
            ],
            "question": None,
        }
    )
    agent = ClipIntentPlannerAgent(None)  # type: ignore[arg-type]

    turkish = agent.parse(raw, ClipIntentPlannerInput(creator_request=request, reply_language="tr"))
    english = agent.parse(raw, ClipIntentPlannerInput(creator_request=request))

    assert turkish.salvage_question is not None
    assert turkish.salvage_question.startswith("Klip talimatlarından 1 tanesini anladım.")
    assert '"dahil etme: pub klipleri"' in turkish.salvage_question
    assert english.salvage_question is not None
    assert english.salvage_question.startswith("I understood 1 of your clip instructions.")
    assert '"include: pub klipleri"' in english.salvage_question


def test_planner_reads_turkish_placeholder_requests_and_position_prose() -> None:
    data = {
        "op": "caption",
        "attribute": "kişilerin olduğu çekimler",
        "source_quote": "Kişilerin çekimlerine isimleri için yer tutucu ekle",
        "creator_text": "NAME",
    }
    sources = ("kişilerin çekimlerine isimleri için yer tutucu ekle",)
    _repair_placeholder(data, ("Kişilerin çekimlerine isimleri için yer tutucu ekle",) + sources)

    assert data["placeholder"] is True and data["op"] == "label"
    assert data["creator_text"] is None

    for prose, expected in [
        ("en başta", "first"),
        ("videonun başında", "first"),
        ("ilk sırada", "first"),
        ("sonunda", "last"),
        ("en sona", "last"),
        ("bu bölüm sırasıyla", None),
        ("sonra", None),
    ]:
        order = {"op": "order", "position": prose}
        _repair_position(order)
        assert order["position"] == expected, prose


def test_planner_prompt_gets_the_language_line_only_for_turkish() -> None:
    agent = ClipIntentPlannerAgent(None)  # type: ignore[arg-type]
    base = ClipIntentPlannerInput(creator_request="Etiket ekle: sporlar.")
    english = agent.render_prompt(base)

    assert agent.render_prompt(base.model_copy(update={"reply_language": "en"})) == english
    assert agent.render_prompt(base.model_copy(update={"reply_language": "de"})) == english
    assert TR not in english
    turkish = agent.render_prompt(base.model_copy(update={"reply_language": "tr"}))
    assert turkish.startswith(english.rstrip("\n"))
    assert turkish.rstrip("\n").endswith("stay in the creator's own words.")
    assert prompt_language_line("tr") in turkish
    assert "source_quote` stays an exact copy" in turkish
    assert "reply_language" not in base.model_dump()


# ── resolver agent ──────────────────────────────────────────────────────────────────


def _resolver_input(**kwargs) -> ClipRequestResolverInput:
    return ClipRequestResolverInput(
        creator_request="Her sporu etiketle.",
        intents=[ResolverIntentIn(intent_id="i1", op="label", attribute="spor")],
        clips=[ResolverClipIn(alias="m001", record={"subject": "football"})],
        **kwargs,
    )


def test_resolver_prompt_gets_the_language_line_only_for_turkish() -> None:
    agent = ClipRequestResolverAgent(None)  # type: ignore[arg-type]
    english = agent.render_prompt(_resolver_input())

    assert agent.render_prompt(_resolver_input(reply_language="en")) == english
    assert TR not in english
    assert english == load_prompt(
        "clip_request_resolver",
        creator_request="Her sporu etiketle.",
        intent_count="1",
        clip_count="1",
        intent_lines=english.split("## Intents (1)")[1]
        .split("## Creator's explicit selections")[0]
        .split("\n\n", 2)[2]
        .strip(),
        clip_lines='- m001 (video): {"subject": "football"}',
        valid_aliases="m001",
        valid_intent_ids="i1",
    )
    turkish = agent.render_prompt(_resolver_input(reply_language="tr"))
    assert turkish.startswith(english.rstrip("\n"))
    assert prompt_language_line("tr") in turkish
    # the vision question stays English and the clips' own words stay as they are
    assert "Keep every `needs_vision` question in English" in turkish
    assert "reply_language" not in _resolver_input().model_dump()


def test_prompt_versions_moved_with_the_new_prompt_tail() -> None:
    assert ClipIntentPlannerAgent.spec.prompt_version == "2026-10-08.1"
    assert ClipRequestResolverAgent.spec.prompt_version == "2026-10-08.1"


# ── grounding folds Turkish case ────────────────────────────────────────────────────


def test_label_grounding_ignores_dotless_i_case() -> None:
    label = ground_label(
        media_id="m1",
        value="Kırmızı Kapı",
        confidence=1.0,
        creator_request="Kapıya KIRMIZI KAPI yaz",
        record=None,  # type: ignore[arg-type]
        intent_id="i1",
    )

    assert isinstance(label, GroundedLabel)
    assert label.text == "Kırmızı Kapı" and label.grounding == "creator_text"

"""KRI-804: a creator's label that can't print dead-ended the chat (Don Julio M1, 2026-10-21).

Chat 1 + 2 (`Put "chimichurri + salsa criolla" on the sauce bowls`): "The label ... has
characters a label can't show ... What shorter label should I use instead?" The reply
named only the "+"; the creator's fix "chimichurri & salsa criolla" was still refused
(27 > LABEL_MAX_CHARS 24), and nothing on screen offered an answer to tap.

Chat 3 (thread 3E27DEF8, turn 87ecce2f), same ask with "&": the inventory planner did not
carry the text as a label, ``with_brief_labels`` skipped it as unprintable, and the
render-contract backstop answered 'I couldn't tell which clip "chimichurri & salsa
criolla" goes on' as a `recovery` turn: no picker, no options, a wrong reason.

Renderer evidence (`tests/fixtures/overlay_verify/kri804_montage_clip_label_width.json`):
the montage label bar draws 27 characters on one line with room to spare on cloud Skia
and the phone; both wrap longer text and never clip it. 24 was never the renderer's bound.

Ways this can fail, written down before these tests:

 1. A label with several problems names only the first, so the creator's fix is refused
    again ("+" fixed, still too long).
 2. The reply is English in a Turkish chat.
 3. The reply offers no way forward ("What shorter label?"), so the creator guesses.
 4. The suggested shorter label itself can't print, or uses words the creator never
    wrote (system text on screen).
 5. The reply is a question with nothing to tap: no `reply_options`, or a turn the phone
    shows no chips on (anything but `turn_value="question"`).
 6. A dictated shot text the planner left out is skipped, and the backstop says "which
    clip" as a recovery dead end (chat 3).
 7. A described-shot caption carrying the creator's words is not accepted as the text's
    placement, so the backstop says "which clip" for text that is placed.
 8. A caption over two neighbouring clips is pinned per clip, but the montage draws ONE
    title across them, so the render is refused as "text in the wrong place".
 9. The creator's "keep it exactly as written" answer doesn't stick: the next turn's
    planner writes a label again and the same question loops.
10. That answer converts a different label in the same chat.
11. The longer creator bound lets model or vision copy through (it must stay at 24).
12. A placeholder or a model-written label gets the new question.
13. A printable text nobody placed stops saying "which clip" (that reply is still right).
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.clip_intent_planner import ClipIntentPlannerAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import (
    CREATOR_LABEL_MAX_CHARS,
    LABEL_MAX_CHARS,
    ClipUnderstanding,
    clean_creator_label_text,
    creator_label_problems,
    ground_label,
    shorter_creator_label,
)
from app.services import clip_intent_planning as planning
from app.services.clip_intent_planning import BriefLabel, plan_and_resolve_clip_intents
from app.services.clip_intent_resolution import IntentClip
from app.services.creator_label_reply import label_text_reply
from app.services.creator_render_contract import build_render_contract
from app.tasks import kria_runtime

PLUS = "chimichurri + salsa criolla"  # chat 1 + 2: 27 characters with a "+"
AMP = "chimichurri & salsa criolla"  # chat 3: 27 characters
LONG_PLUS = "chimichurri + salsa criolla picante"  # both problems at once
SHOT = "the sauce bowls"
KEEP_PLUS = f'Keep "{PLUS}" exactly as written'
USE_AMP = f'Use "{AMP}" instead'

# Chat 3's upload ids: M1_08 (the three bowls close up) and M1_03 (Lucas points into them).
BOWLS = "analysis-proxy-ios-051F8034-D6F7-493A-B2D4-E1517DCD4F2E.mp4"
POINTS = "analysis-proxy-ios-ED06C64A-1742-415D-922C-33B819BD1792.mp4"
WAITER = "analysis-proxy-ios-23BD8C69-AD91-41CC-978E-2B749086CD93.mp4"


def _prompt(text: str) -> str:
    return (
        "Montage of our lunch at Don Julio in Buenos Aires. Open on the waiter smiling at "
        f'the camera. Put "{text}" on the sauce bowls. Make it 15 seconds.'
    )


def _brief(text: str) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r2", kind="text", scope="per_clip", literal=text, description=SHOT)
        ],
    )


def _clips() -> list[IntentClip]:
    return [
        IntentClip(media_id=m, kind="video", analysis={"subject": s}, gcs_path=f"u/{m}")
        for m, s in (
            (WAITER, "waiter smiling at the camera holding menus"),
            (BOWLS, "three wooden sauce bowls close up with bread"),
            (POINTS, "waiter pointing a spoon into the sauce bowls"),
        )
    ]


def _row(op: str, text: str, intent_id: str = "text_bowls") -> dict[str, Any]:
    return {
        "intent_id": intent_id,
        "op": op,
        "attribute": SHOT,
        "creator_text": text,
        "source_quote": f'Put "{text}" on the sauce bowls',
    }


class _Models:
    """The REAL inventory parser and resolver code over recorded model output.

    The resolver matches the sauce-bowl close-up only (one clip, high confidence), so
    whatever reaches it settles without a picker.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, planner_rows: list[dict[str, Any]]):
        self.resolved: list[tuple[str, str, str | None]] = []
        outer = self

        def _plan(self, input, *, ctx=None):  # noqa: A002, ANN001
            return self.parse(json.dumps({"intents": planner_rows, "question": None}), input)

        def _resolve(self, input, *, ctx=None):  # noqa: A002, ANN001
            bowls = next(c.alias for c in input.clips if "close up" in json.dumps(c.record))
            out = []
            for intent in input.intents:
                outer.resolved.append((intent.intent_id, intent.op, intent.creator_text))
                value = intent.creator_text if intent.op == "label" else None
                out.append(
                    ResolverIntentOut(
                        intent_id=intent.intent_id,
                        assignments=[
                            ResolverAssignment(
                                media=bowls, value=value, evidence="bowls", confidence=0.95
                            )
                        ],
                    )
                )
            return ClipRequestResolverOutput(intents=out)

        monkeypatch.setattr(planning, "default_client", MagicMock())
        monkeypatch.setattr(ClipIntentPlannerAgent, "run", _plan)
        monkeypatch.setattr(ClipRequestResolverAgent, "run", _resolve)


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)


async def _plan(monkeypatch, rows, *, messages: list[str], brief_labels=()):  # noqa: ANN001
    models = _Models(monkeypatch, rows)
    planned = await plan_and_resolve_clip_intents(
        creator_request="\n".join(messages),
        latest_user_message=messages[-1],
        candidate_intents=None,
        clips=_clips(),
        run_context=MagicMock(),
        brief_labels=list(brief_labels),
    )
    return models, planned


# 1-4: what the reply says -------------------------------------------------------------------


def test_every_reason_is_named_with_the_limit_and_a_fitting_suggestion() -> None:
    assert creator_label_problems(LONG_PLUS) == ("characters", "too_long")
    reply = label_text_reply([LONG_PLUS])
    assert reply is not None
    assert f'"{LONG_PLUS}" can\'t be a clip label' in reply.question
    assert 'can\'t show "+"' in reply.question
    assert f"it's {len(LONG_PLUS)} characters (a label holds up to {CREATOR_LABEL_MAX_CHARS})" in (
        reply.question
    )
    # Never "which ...": this is a question about the words, not about clips.
    assert "which" not in reply.question.lower()
    keep, use = reply.options
    assert keep == f'Keep "{LONG_PLUS}" exactly as written'
    suggestion = use.removeprefix('Use "').removesuffix('" instead')
    assert suggestion == "chimichurri & salsa criolla"
    # The suggestion prints, and is made only of the creator's own words.
    assert clean_creator_label_text(suggestion) == suggestion
    assert set(suggestion.split()) - {"&"} <= set(LONG_PLUS.split())


def test_the_reply_follows_the_chat_language() -> None:
    with reply_language_for("tr"):
        reply = label_text_reply([LONG_PLUS])
    assert reply is not None
    assert "klip etiketi olamaz" in reply.question
    assert 'etiket "+" gösteremez' in reply.question
    assert f"{len(LONG_PLUS)} karakter (etiket en fazla {CREATOR_LABEL_MAX_CHARS}" in reply.question
    assert reply.options[0] == f'"{LONG_PLUS}" yazdığım gibi kalsın'
    assert "Can't" not in reply.question and "label holds" not in reply.question


def test_a_suggestion_only_drops_a_whole_word_and_must_print() -> None:
    assert shorter_creator_label(PLUS) == AMP
    assert shorter_creator_label(AMP) is None  # nothing to fix
    # Cutting two words would break a name apart: no suggestion, never "... Costa Aires".
    assert shorter_creator_label("Parque de la Costa Lunapark Buenos Aires") is None
    assert shorter_creator_label("Oblea de la casa 🇦🇷") == "Oblea de la casa"


# 11: the creator bound, measured, is for the creator's words only ------------------------


def test_the_creator_bound_is_the_measured_one_and_model_copy_keeps_24() -> None:
    assert (LABEL_MAX_CHARS, CREATOR_LABEL_MAX_CHARS) == (24, 28)
    assert creator_label_problems(AMP) == ()
    assert creator_label_problems(PLUS) == ("characters",)
    record = ClipUnderstanding(subject="chimichurri and salsa criolla sauce bowls")
    grounded = {
        source: ground_label(
            media_id="m1",
            value=AMP,
            confidence=1.0,
            creator_request=request,
            record=record,
            vision_answer=AMP,
            vision_confidence=1.0,
        )
        for source, request in (("creator", _prompt(AMP)), ("vision", "label the sauces"))
    }
    assert grounded["creator"] is not None and grounded["creator"].text == AMP
    assert grounded["vision"] is None


# 5, 9, 10, 12: the planning turn -----------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_1_a_label_with_a_plus_is_one_question_with_answers_to_tap(monkeypatch):
    models, planned = await _plan(monkeypatch, [_row("label", PLUS)], messages=[_prompt(PLUS)])

    assert models.resolved == []  # never the clip picker for a text problem
    resolution = planned.resolution
    assert resolution.status == "needs_creator"
    assert resolution.error_code == "label_text_unusable"
    assert resolution.clip_question is None
    assert resolution.reply_options == (KEEP_PLUS, USE_AMP)
    assert 'can\'t show "+"' in resolution.question


@pytest.mark.asyncio
async def test_keep_as_written_prints_the_words_as_the_shot_caption_on_every_later_turn(
    monkeypatch,
):
    for messages in (
        [_prompt(PLUS), KEEP_PLUS],
        # A later turn about something else: the planner writes the label again.
        [_prompt(PLUS), KEEP_PLUS, "Make it 12 seconds instead."],
    ):
        models, planned = await _plan(monkeypatch, [_row("label", PLUS)], messages=messages)

        assert planned.resolution.error_code != "label_text_unusable"
        assert models.resolved == [("text_bowls", "caption", PLUS)]
        (caption,) = planned.resolution.intents
        assert (caption.op, caption.caption_text, caption.caption_grounding) == (
            "caption",
            PLUS,
            "creator_text",
        )
        assert [a.media_id for a in caption.assignments] == [BOWLS]


@pytest.mark.asyncio
async def test_keep_as_written_converts_only_the_label_it_names(monkeypatch):
    other = "Lunch + friends at Don Julio"
    waiter_ask = f'Put "{other}" on the waiter clip'
    rows = [
        _row("label", PLUS),
        {**_row("label", other, intent_id="text_waiter"), "source_quote": waiter_ask},
    ]
    messages = [f"{_prompt(PLUS)} {waiter_ask}.", KEEP_PLUS]
    _models, planned = await _plan(monkeypatch, rows, messages=messages)

    # The other unprintable label is still asked about, by itself.
    assert planned.resolution.error_code == "label_text_unusable"
    assert other in planned.resolution.question
    assert PLUS not in planned.resolution.question
    assert [(i.op, i.creator_text) for i in planned.requested_intents] == [
        ("caption", PLUS),
        ("label", other),
    ]


@pytest.mark.asyncio
async def test_a_placeholder_or_a_model_written_label_is_never_asked_about(monkeypatch):
    rows = [
        {
            "intent_id": "names",
            "op": "label",
            "attribute": "each person",
            "placeholder": True,
            "source_quote": "add a text placeholder",
        },
        {
            "intent_id": "dish",
            "op": "label",
            "attribute": "dish",
            "source_quote": "label each dish",
        },
    ]
    message = "Label each dish, and add a text placeholder for each person."
    _models, planned = await _plan(monkeypatch, rows, messages=[message])
    assert planned.resolution.error_code != "label_text_unusable"
    assert not planned.resolution.reply_options


# 6: chat 3, the planner left the text out --------------------------------------------------


@pytest.mark.asyncio
async def test_chat_3_the_dictated_text_the_planner_left_out_is_placed(monkeypatch):
    models, planned = await _plan(
        monkeypatch, [], messages=[_prompt(AMP)], brief_labels=[BriefLabel(AMP, SHOT)]
    )

    assert not planned.resolution.needs_creator
    assert models.resolved == [("label-chimichurri-salsa-criolla", "label", AMP)]
    (label,) = planned.resolution.intents
    assert [(a.media_id, a.value) for a in label.assignments] == [(BOWLS, AMP)]
    contract = build_render_contract(
        {"resolved_clip_intents": [label.model_dump(mode="json")]},
        generation_id="g",
        brief=_brief(AMP),
    )
    assert contract is not None and not contract.unresolved
    assert [(t.role, t.text, t.media_id) for t in contract.exact_texts] == [("clip", AMP, BOWLS)]


@pytest.mark.asyncio
async def test_a_dictated_text_no_label_can_print_goes_as_the_shot_caption(monkeypatch):
    models, planned = await _plan(
        monkeypatch, [], messages=[_prompt(PLUS)], brief_labels=[BriefLabel(PLUS, SHOT)]
    )

    assert not planned.resolution.needs_creator
    assert [(op, text) for _id, op, text in models.resolved] == [("caption", PLUS)]
    (caption,) = planned.resolution.intents
    contract = build_render_contract(
        {"resolved_clip_intents": [caption.model_dump(mode="json")]},
        generation_id="g",
        brief=_brief(PLUS),
    )
    assert contract is not None and not contract.unresolved
    assert [(t.role, t.text, t.media_id) for t in contract.exact_texts] == [("clip", PLUS, BOWLS)]


# 7, 8, 13: the render contract backstop -----------------------------------------------------


def _caption(text: str, *media_ids: str) -> dict[str, Any]:
    return {
        "intent_id": "caption_bowls",
        "op": "caption",
        "attribute": SHOT,
        "creator_text": text,
        "caption_text": text,
        "caption_grounding": "creator_text",
        "status": "resolved",
        "assignments": [{"media_id": m, "confidence": 0.95} for m in media_ids],
    }


def test_a_caption_on_two_neighbouring_clips_needs_the_words_on_screen_not_per_clip() -> None:
    contract = build_render_contract(
        {"resolved_clip_intents": [_caption(PLUS, BOWLS, POINTS)]},
        generation_id="g",
        brief=_brief(PLUS),
    )
    assert contract is not None and not contract.unresolved
    assert [(t.role, t.text, t.media_id) for t in contract.exact_texts] == [("any", PLUS, None)]


def test_a_caption_the_resolver_wrote_is_never_the_creators_placement() -> None:
    authored = {**_caption(PLUS, BOWLS), "creator_text": None, "caption_grounding": "record_span"}
    contract = build_render_contract(
        {"resolved_clip_intents": [authored]}, generation_id="g", brief=_brief(PLUS)
    )
    assert contract is not None and contract.unresolved


def test_unplaced_words_a_label_cant_print_are_a_text_question_not_which_clip() -> None:
    for language in ("en", "tr"):
        with reply_language_for(language):
            contract = build_render_contract(
                {"resolved_clip_intents": []}, generation_id="g", brief=_brief(LONG_PLUS)
            )
            expected = label_text_reply([LONG_PLUS])
        assert contract is not None and expected is not None
        assert list(contract.unresolved) == [expected.question]
        assert "which clip" not in contract.unresolved[0]
        assert "hangi klibe" not in contract.unresolved[0]


def test_printable_words_nobody_placed_still_ask_which_clip() -> None:
    contract = build_render_contract(
        {"resolved_clip_intents": []}, generation_id="g", brief=_brief(AMP)
    )
    assert contract is not None
    assert list(contract.unresolved) == [
        f"I couldn't tell which clip \"{AMP}\" goes on, so I can't confirm that text."
    ]


# 5: the draft backstop turn is a question with answers, never a recovery -------------------


def test_the_draft_backstop_asks_about_the_words_with_answers_to_tap() -> None:
    brief = _brief(LONG_PLUS)
    blocked = kria_runtime._unresolved_choice_plan(
        {"resolved_clip_intents": []}, brief, None, contract_brief=brief, has_draft=True
    )
    assert blocked is not None
    plan, reason = blocked
    assert reason == "label_text_unusable"
    assert plan.turn_value == "question"
    expected = label_text_reply([LONG_PLUS])
    assert expected is not None
    assert plan.reply_options == list(expected.options)
    assert plan.response.startswith(expected.question)
    assert "which clip" not in plan.response


# 5: the whole planner turn, as the phone receives it ----------------------------------------

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri593_tigre_voiceover_turn.json"
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())
MANIFEST = CASE["main_creator_input"]["capability_manifest"]
KITCHEN_ASK = f'Put "{PLUS}" on the kitchen clip.'


class _Db:
    async def execute(self, _statement: Any) -> Any:
        return MagicMock(scalar_one_or_none=lambda: None)

    async def rollback(self) -> None:
        return None


async def test_the_planner_turn_carries_the_answers_the_phone_shows_as_chips(
    prod_profile, monkeypatch
) -> None:
    data = copy.deepcopy(CASE["main_creator_input"])
    data["user_message"] = KITCHEN_ASK
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": KITCHEN_ASK}]
    agent_input = MainCreatorInput.model_validate(data)
    strategy = {**copy.deepcopy(CASE["model_strategy"]), "clip_intents": [], "opening_title": None}
    raw = {
        "action": {"kind": "propose_strategy", "strategy": strategy, "summary": "Montage."},
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    row = {**_row("label", PLUS), "attribute": "the kitchen clip", "source_quote": KITCHEN_ASK}
    _Models(monkeypatch, [row])
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    clips = [
        IntentClip(
            media_id=m["media_id"], kind=m["kind"], analysis={"subject": s}, gcs_path=m["media_id"]
        )
        for m in MANIFEST["media"]
        for s in [CASE["clip_subjects"][m["media_id"]]]
    ]

    planned = await planner._plan_from_creator_output(
        _Db(),
        thread_id=uuid.uuid4(),
        item_id=uuid.UUID(MANIFEST["item_id"]),
        creator_id=uuid.uuid4(),
        user_message=KITCHEN_ASK,
        manifest=agent_input.capability_manifest,
        inputs=planner._CreatorInputs(
            agent_input=agent_input,
            intent_clips=clips,
            creator_request=None,
            raw_creator_request=KITCHEN_ASK,
        ),
        output=output,
        brief_request=CASE["main_creator_input"]["creator_request"],
        effective_brief=None,
    )

    plan = planned.plan
    assert (plan.mode, plan.turn_value) == ("respond", "question")
    assert plan.clip_question is None
    assert plan.reply_options == [KEEP_PLUS, USE_AMP]
    assert 'can\'t show "+"' in (plan.response or "")

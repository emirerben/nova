"""KRI-962 follow-up: a Voiceover shot two clips match equally is the creator's to pick.

Don Julio walk-in N1 (thread 99627FBC, job bdb0c798, 2026-10-26): "the DON JULIO sign while I
say 'Don Julio'" matched the window-sign clip AND Atakan at the "PARRILLA DON JULIO" plaque,
both at 0.9. Nothing asked. The alignment agent opened the video on Atakan (at "Don Julio"
the screen showed a whip-pan and his face, no sign) and put the real sign on the unrelated
last line. Both clips really do show a Don Julio sign, so no rule can know which one the
creator meant: the planner asks with the clip picker, both clips pre-ticked, before the draft.

Ways this can go wrong (written before the code; each has a case below):

a. The N1 tie is still decided silently (the bug).
b. Too chatty: a shot whose other equal matches another ask needs asks anyway. N1's
   "the grill" matched three clips at 0.9, but one is the only flowers clip and one the
   only butcher clip: one clip is left, nothing to ask.
c. A clear winner asks (Troy "excavated layers": one clip at 1.0, three at 0.8).
d. An order, group or whole-set ask asks (the museum: "the museum" is an order ask; "all
   five footage clips" names every clip).
e. Another format asks: a Montage plays every matched clip, there is no one moment.
f. A photo counts toward a tie (a Visuals photo is a card, not the shot; N1's ribeye photo
   matched the sign at 0.9 too).
g. The tap does not stick: the next turn asks again, or the chosen clip is not the shot's.
h. "Make this simpler version" after the tap asks again.
i. "None of these" or keeping both ticked asks again.
j. The tie and another question (the resolver's own picker, a contested skip) come in two
   turns instead of one card.
k. A Turkish chat gets English copy.
l. A clip the creator left out ("don't use ...") is counted, offered or pre-ticked.
m. A shot the creator already answered (a tap, a typed file name) is asked again.
n. The flags: the picker flag off or the tie flag off asks nothing (byte-identical draft).
o. A tie-check error costs the turn: it must fail open (no question, the draft as before).
p. Of the 6 prod Voiceover jobs with an alignment record, any job but N1 would be asked.

Replayed from the prod records (`fixtures/kri962_voiceover_tie_jobs.json`). Only the model
calls are stand-ins (clip-intent planner, resolver); the parse, the resolver, the picker
builder and the runtime-v2 planner are the real ones. No network, no paid model call.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.clip_intent_planner import ClipIntentPlannerAgent
from app.agents.clip_question import ClipQuestionAgent
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
from app.config import settings
from app.kria import planner
from app.kria.clip_ties import tie_question, voiceover_ties
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.schemas.clip_intents import (
    CREATOR_SELECTED_EVIDENCE,
    FILE_NAME_EVIDENCE,
    ClipAssignment,
    ResolvedClipIntent,
)
from app.services import clip_intent_planning as planning
from app.services.clip_intent_resolution import IntentClip

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE: dict[str, Any] = json.loads((FIXTURES / "kri962_voiceover_tie_jobs.json").read_text())
N1: dict[str, Any] = CASE["n1"]
# A real phone Voiceover turn (narrated_planned + a recorded voiceover, runtime v2); its
# 7 videos + 1 photo are swapped for N1's 7 videos + 1 photo.
TIGRE: dict[str, Any] = json.loads((FIXTURES / "kri593_tigre_voiceover_turn.json").read_text())

IDS = {
    c["media_id"].split("-")[3] if "proxy" in c["media_id"] else "photo": c["media_id"]
    for c in N1["clips"]
}
ATAKAN = IDS["6163F36C"]  # Atakan at the PARRILLA DON JULIO plaque
WINDOW = IDS["B39B8BA0"]  # the DON JULIO window sign
FLOWERS = IDS["23443478"]
GRILL = IDS["BC642645"]
BUTCHER = IDS["02B705F5"]
PHOTO = IDS["photo"]
ALL = [c["media_id"] for c in N1["clips"]]
REQUEST = N1["creator_request"]
SIGN = "include_don_julio_sign"

# The clip-intent planner's inventory for N1, quoted from the creator's message.
N1_INTENTS = [
    {
        "intent_id": SIGN,
        "op": "include",
        "attribute": "the DON JULIO sign",
        "source_quote": 'the DON JULIO sign while I say "Don Julio"',
    },
    {
        "intent_id": "include_queue",
        "op": "include",
        "attribute": "the queue",
        "source_quote": 'the queue on "the queue"',
    },
    {
        "intent_id": "include_butcher",
        "op": "include",
        "attribute": "the butcher",
        "source_quote": 'the butcher on "butcher"',
    },
    {
        "intent_id": "include_grill",
        "op": "include",
        "attribute": "the grill",
        "source_quote": 'the grill on "grill"',
    },
    {
        "intent_id": "include_flowers",
        "op": "include",
        "attribute": "the flowers",
        "source_quote": 'the flowers on "ceiling"',
    },
    {
        "intent_id": "include_plaques",
        "op": "include",
        "attribute": "the plaques",
        "source_quote": 'the plaques on "Michelin"',
    },
]
RIBEYE = {
    "intent_id": "include_ribeye_photo",
    "op": "include",
    "attribute": "the ribeye photo",
    "source_quote": 'Show the ribeye photo as a small corner card while I say "ojo de bife"',
}
ATAKAN_LINE = {
    "intent_id": "include_atakan_super_excited",
    "op": "include",
    "attribute": "Atakan saying Super excited",
    "source_quote": 'play Atakan\'s "Super excited!" with its own sound',
}
# What the resolver matched in prod (the approved draft's resolved intents).
PROD_MATCHES = {
    i["intent_id"]: [(a["media_id"], a["confidence"], a["evidence"]) for a in i["assignments"]]
    for i in N1["resolved_clip_intents"]
}
TIE_EN = "Two of your clips show"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_voiceover_tie_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_understanding_enabled", False)


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=c["media_id"],
            kind=c["kind"],
            analysis={"understanding": c["record"]},
            gcs_path=f"users/u/clips/{c['media_id']}",
        )
        for c in N1["clips"]
    ]


def _replay_models(
    monkeypatch: pytest.MonkeyPatch,
    raw_intents: list[dict[str, Any]],
    matches: dict[str, list[tuple[str, float, str]]],
    calls: list[str] | None = None,
) -> None:
    """The REAL clip-intent parse and resolver, over the models' recorded answers."""
    alias = {c["media_id"]: f"m{i + 1:03d}" for i, c in enumerate(N1["clips"])}

    def _plan(self, input, *, ctx=None):  # noqa: A002, ANN001
        if calls is not None:
            calls.append("planner")
        return self.parse(json.dumps({"intents": raw_intents, "question": None}), input)

    def _resolve(self, input, *, ctx=None):  # noqa: A002, ANN001
        if calls is not None:
            calls.append("resolver")
        return ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id=intent.intent_id,
                    assignments=[
                        ResolverAssignment(media=alias[m], confidence=c, evidence=e)
                        for m, c, e in matches.get(intent.intent_id, [])
                    ],
                )
                for intent in input.intents
            ]
        )

    def _no_vision(self, input, *, ctx=None):  # noqa: A002, ANN001
        raise AssertionError("a confident match never pays for a vision check")

    monkeypatch.setattr(planning, "default_client", MagicMock())
    monkeypatch.setattr(ClipIntentPlannerAgent, "run", _plan)
    monkeypatch.setattr(ClipRequestResolverAgent, "run", _resolve)
    monkeypatch.setattr(ClipQuestionAgent, "run", _no_vision)


def _agent_input(message: str, language: str) -> MainCreatorInput:
    data = copy.deepcopy(TIGRE["main_creator_input"])
    data["user_message"] = message
    data["creator_request"] = REQUEST
    data["reply_language"] = language
    data["conversation"] = [data["conversation"][0], {"role": "user", "content": message}]
    data["capability_manifest"]["media"] = [
        {"kind": c["kind"], "label": None, "media_id": c["media_id"], "duration_s": c["duration_s"]}
        for c in N1["clips"]
    ]
    return MainCreatorInput.model_validate(data)


async def _turn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raw_intents: list[dict[str, Any]] | None = None,
    matches: dict[str, list[tuple[str, float, str]]] | None = None,
    events: list | None = None,
    message: str = REQUEST,
    language: str = "en",
    strategy_update: dict[str, Any] | None = None,
    calls: list[str] | None = None,
) -> Any:
    """One N1 planning turn through the REAL runtime-v2 `_plan_from_creator_output`."""
    raw_intents = N1_INTENTS if raw_intents is None else raw_intents
    agent_input = _agent_input(message, language)
    strategy = {
        **copy.deepcopy(N1["model_strategy"]),
        "clip_intents": [{k: v for k, v in i.items() if k != "source_quote"} for i in raw_intents],
        **(strategy_update or {}),
    }
    raw = {
        "action": {
            "kind": "propose_strategy",
            "strategy": strategy,
            "summary": "Here's your Don Julio walkthrough, following your recording.",
        },
        "brief_updates": [],
    }
    output = MainCreatorAgent(model_client=None).parse(json.dumps(raw), agent_input)
    _replay_models(monkeypatch, raw_intents, PROD_MATCHES if matches is None else matches, calls)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=events or []))
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())
    monkeypatch.setattr(planner, "_earlier_exclusions", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    manifest = agent_input.capability_manifest
    token = bind_reply_language(language)
    try:
        return await planner._plan_from_creator_output(
            SimpleNamespace(),
            thread_id=uuid.uuid4(),
            item_id=uuid.UUID(manifest.item_id),
            creator_id=uuid.uuid4(),
            user_message=message,
            manifest=manifest,
            inputs=planner._CreatorInputs(
                agent_input=agent_input,
                intent_clips=_clips(),
                creator_request=None,
                raw_creator_request=REQUEST,
            ),
            output=output,
        )
    finally:
        release_reply_language(token)


def _answered(planned: Any, *answers: dict[str, Any]) -> list:
    question = planned.plan.clip_question
    categories = question["categories"]
    assert len(categories) == len(answers)
    return [
        ("assistant", {"clip_question": question}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question["question_id"],
                    "answers": [
                        {"key": category["key"], **answer}
                        for category, answer in zip(categories, answers, strict=True)
                    ],
                }
            },
        ),
    ]


def _resolved(planned: Any) -> dict[str, list[str]]:
    strategy = planned.plan.intents[0].arguments["strategy"]
    return {
        i["intent_id"]: [a["media_id"] for a in i["assignments"]]
        for i in strategy.get("resolved_clip_intents") or []
    }


# -- the N1 journey (a, b, f, g, h, i) -------------------------------------------------------


async def test_n1_asks_which_clip_shows_the_don_julio_sign(prod_profile, monkeypatch) -> None:
    calls: list[str] = []
    planned = await _turn(monkeypatch, calls=calls)

    assert (planned.plan.mode, planned.plan.turn_value) == ("respond", "question")
    assert planned.plan.intents == []  # no draft until the creator picks
    [category] = planned.plan.clip_question["categories"]  # b: the grill is not asked
    assert (category["op"], category["intent_id"]) == ("include", SIGN)
    assert category["suggested_media_ids"] == [ATAKAN, WINDOW]  # f: the photo is not
    assert category["candidate_media_ids"] == ALL
    assert TIE_EN in planned.plan.response and "the DON JULIO sign" in planned.plan.response
    assert planned.plan.diagnostics["stage"] == "tie"
    assert calls == ["planner", "resolver"]


async def test_tapping_the_window_sign_makes_it_the_shot_and_nothing_is_asked_again(
    prod_profile, monkeypatch
) -> None:
    asked = await _turn(monkeypatch)
    events = _answered(asked, {"media_ids": [WINDOW]})
    planned = await _turn(monkeypatch, events=events)

    assert planned.plan.mode == "act", planned.plan.response
    assert planned.plan.clip_question is None
    resolved = _resolved(planned)
    assert resolved[SIGN] == [WINDOW]
    # Every other shot keeps exactly what the resolver matched in prod.
    for intent_id, matched in PROD_MATCHES.items():
        if intent_id != SIGN:
            assert resolved[intent_id] == [m for m, _c, _e in matched]


async def test_simpler_version_after_the_tap_does_not_ask_again(prod_profile, monkeypatch) -> None:
    # h: prod N1 went on with "Make this simpler version"; the tap must still hold.
    asked = await _turn(monkeypatch)
    events = _answered(asked, {"media_ids": [WINDOW]})
    events += [("user", {"text": "Make this simpler version"})]
    planned = await _turn(monkeypatch, events=events, message="Make this simpler version")

    assert planned.plan.mode == "act", planned.plan.response
    assert _resolved(planned)[SIGN] == [WINDOW]


@pytest.mark.parametrize(
    "answer",
    [{"media_ids": [ATAKAN, WINDOW]}, {"none": True}],
    ids=["kept-both", "none-of-these"],
)
async def test_any_answer_settles_the_shot_for_good(prod_profile, monkeypatch, answer) -> None:
    # i
    asked = await _turn(monkeypatch)
    planned = await _turn(monkeypatch, events=_answered(asked, answer))

    assert planned.plan.mode == "act", planned.plan.response
    assert planned.plan.clip_question is None


async def test_when_another_ask_uses_atakans_clip_the_sign_is_not_asked(
    prod_profile, monkeypatch
) -> None:
    # b: the full N1 message also asked for Atakan's own line; his clip is spoken for.
    matches = {**PROD_MATCHES, ATAKAN_LINE["intent_id"]: [(ATAKAN, 0.9, "says Super excited")]}
    planned = await _turn(monkeypatch, raw_intents=[*N1_INTENTS, ATAKAN_LINE], matches=matches)

    assert planned.plan.mode == "act", planned.plan.response


async def test_the_tie_rides_in_the_ribeye_picker_one_card(prod_profile, monkeypatch) -> None:
    # j: the first N1 turn asked "which of your clips show 'the ribeye photo'" alone.
    planned = await _turn(monkeypatch, raw_intents=[*N1_INTENTS, RIBEYE])

    assert (planned.plan.mode, planned.plan.turn_value) == ("respond", "question")
    categories = planned.plan.clip_question["categories"]
    assert [c["intent_id"] for c in categories] == ["include_ribeye_photo", SIGN]
    assert categories[1]["suggested_media_ids"] == [ATAKAN, WINDOW]
    assert "the ribeye photo" in planned.plan.response and TIE_EN in planned.plan.response

    events = _answered(planned, {"media_ids": [PHOTO]}, {"media_ids": [WINDOW]})
    again = await _turn(monkeypatch, raw_intents=[*N1_INTENTS, RIBEYE], events=events)
    assert again.plan.mode == "act", again.plan.response
    assert _resolved(again)[SIGN] == [WINDOW]


async def test_a_turkish_chat_is_asked_in_turkish(prod_profile, monkeypatch) -> None:
    # k
    planned = await _turn(monkeypatch, language="tr")

    assert planned.plan.clip_question is not None
    assert "iki klibinde" in planned.plan.response
    assert TIE_EN not in planned.plan.response


# -- other formats, flags, failure (e, n, o) --------------------------------------------------


async def test_a_montage_never_asks(prod_profile, monkeypatch) -> None:
    # e: every matched clip plays in a montage; there is no single moment to fill.
    planned = await _turn(
        monkeypatch,
        strategy_update={
            "edit_format": "montage",
            "audio_strategy": "original_audio",
            "reaction_beats": [],
            "opening_title": None,
        },
    )
    assert planned.plan.mode == "act", planned.plan.response


@pytest.mark.parametrize(
    "flag", ["kria_voiceover_tie_questions_enabled", "kria_clip_selection_questions_enabled"]
)
async def test_either_flag_off_asks_nothing(prod_profile, monkeypatch, flag: str) -> None:
    # n
    monkeypatch.setattr(settings, flag, False)
    planned = await _turn(monkeypatch)

    assert planned.plan.mode == "act", planned.plan.response
    assert _resolved(planned)[SIGN] == [m for m, _c, _e in PROD_MATCHES[SIGN]]


async def test_a_tie_check_error_never_costs_the_turn(prod_profile, monkeypatch) -> None:
    # o
    def _boom(*_a, **_k):
        raise RuntimeError("tie check broke")

    monkeypatch.setattr(planner, "voiceover_ties", _boom)
    planned = await _turn(monkeypatch)

    assert planned.plan.mode == "act", planned.plan.response


# -- the rule over recorded intents (b, c, d, f, l, m, p) -------------------------------------


def _intent(intent_id: str, op: str, *matches: tuple[str, float], **kw: Any) -> ResolvedClipIntent:
    evidence = kw.pop("evidence", "")
    return ResolvedClipIntent(
        intent_id=intent_id,
        op=op,
        attribute=kw.pop("attribute", intent_id),
        status="resolved",
        assignments=[
            ClipAssignment(media_id=m, confidence=c, evidence=evidence) for m, c in matches
        ],
        **kw,
    )


def _job_inputs(job: dict[str, Any]) -> tuple[list[ResolvedClipIntent], list[IntentClip]]:
    intents = [ResolvedClipIntent.model_validate(i) for i in job["resolved_clip_intents"]]
    clips = [
        IntentClip(media_id=m, kind=kind, analysis=None) for m, kind in job["media_kinds"].items()
    ]
    return intents, clips


def test_of_the_six_prod_voiceover_jobs_only_n1_is_asked() -> None:
    # p (and c: Troy b9e4c530, d: the museum e573c56a)
    assert len(CASE["jobs"]) == 6
    asked = {}
    for job in CASE["jobs"]:
        assert (job["edit_format"], job["audio_strategy"]) == ("narrated_planned", "voiceover")
        intents, clips = _job_inputs(job)
        ties = voiceover_ties(intents, clips)
        if ties:
            asked[job["job_id"]] = [(t.intent.intent_id, set(t.media_ids)) for t in ties]
    assert asked == {"bdb0c798-c2e6-4f58-8a31-e50f9acdfdd7": [(SIGN, {ATAKAN, WINDOW})]}


def test_the_n1_tie_is_in_clip_order_without_the_photo() -> None:
    # b, f
    intents = [ResolvedClipIntent.model_validate(i) for i in N1["resolved_clip_intents"]]
    [tie] = voiceover_ties(intents, _clips())
    assert tie.media_ids == (ATAKAN, WINDOW)


def test_a_clip_the_creator_left_out_is_never_counted_or_offered() -> None:
    # l
    third = FLOWERS
    sign = _intent(SIGN, "include", (ATAKAN, 0.9), (WINDOW, 0.9), (third, 0.9))
    skip_atakan = _intent("exclude_atakan", "exclude", (ATAKAN, 0.9))
    [tie] = voiceover_ties([sign, skip_atakan], _clips())
    assert tie.media_ids == (third, WINDOW)
    _text, question = tie_question([tie], _clips(), excluded={ATAKAN})
    [category] = question["categories"]
    assert ATAKAN not in category["candidate_media_ids"]
    assert category["suggested_media_ids"] == [third, WINDOW]

    sign_two = _intent(SIGN, "include", (ATAKAN, 0.9), (WINDOW, 0.9))
    assert voiceover_ties([sign_two, skip_atakan], _clips()) == []


@pytest.mark.parametrize("evidence", [CREATOR_SELECTED_EVIDENCE, FILE_NAME_EVIDENCE])
def test_a_shot_the_creator_already_answered_is_never_asked(evidence: str) -> None:
    # m
    sign = _intent(SIGN, "include", (ATAKAN, 1.0), (WINDOW, 1.0), evidence=evidence)
    assert voiceover_ties([sign], _clips()) == []


@pytest.mark.parametrize("op", ["order", "group", "caption"])
def test_only_a_named_include_is_asked(op: str) -> None:
    # d
    extra = {"position": "first"} if op == "order" else {}
    shot = _intent("the_shot", op, (ATAKAN, 0.9), (WINDOW, 0.9), **extra)
    assert voiceover_ties([shot], _clips()) == []


def test_a_clear_winner_is_never_asked() -> None:
    # c
    sign = _intent(SIGN, "include", (WINDOW, 1.0), (ATAKAN, 0.9))
    assert voiceover_ties([sign], _clips()) == []


def test_two_shots_can_share_one_card_and_the_copy_names_both() -> None:
    sign = _intent(SIGN, "include", (ATAKAN, 0.9), (WINDOW, 0.9), attribute="the DON JULIO sign")
    grill = _intent("include_grill", "include", (GRILL, 0.9), (BUTCHER, 0.9), attribute="the grill")
    ties = voiceover_ties([sign, grill], _clips())
    text, question = tie_question(ties, _clips())
    assert [c["intent_id"] for c in question["categories"]] == [SIGN, "include_grill"]
    assert '"the DON JULIO sign"' in text and '"the grill"' in text

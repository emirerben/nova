"""KRI-1025: "None of these" on the clip picker is an answer, never a dead end.

Plaza de Mayo M1, thread A228E85B (2026-10-28): "End on the Obelisco" (never filmed). Kria
asked with the clip picker which clips show "the Obelisco"; the creator answered None
(`clip_selection.none_keys: ["order:the-obelisco"]`). The clip planner dropped the pin
(`apply_creator_selections`), but brief r1 still said `last_clip: "the Obelisco"`, so the
draft backstop's render contract refused with no draft, no question and no next step:
"I couldn't tell which clip "the Obelisco" means, so I can't confirm the order." Only a
typed re-explanation ("There is no obelisco in the clips. End on the cathedral") moved on.

Ways this can fail, written before the code (each is pinned below):

 F1 the incident: a last-clip pin answered None ends the turn on `_pin_not_found`.
 F2 the same for a first-clip pin ("start with ...").
 F3 a pin-only order ("end on X", no basis) whose only pin was answered None turns into an
    "ordering rule I can't verify" (a second dead end) instead of going away.
 F4 two pins, one answered None: the other pin must stay pinned and verified.
 F5 the creator later typed a new ending: the old None must never void the new anchor
    (no "none of your clips show the cathedral").
 F6 another resolved pin already holds that spot this turn: nothing is voided.
 F7 a newer picker answer that taps clips for the same words wins over the old None.
 F8 wording drift (case, "the", punctuation) between the brief and the picker row still
    matches; a different landmark never does.
 F9 creator text whose described clip was answered None ("Casa Rosada" on "the pink
    building") ends on `text_clip_not_found`, or is pinned and refused after the render.
 F10 the review row must not claim "ending on the Obelisco" as done.
 F11 a Turkish chat hears all of it in Turkish.
 F12 approval and dispatch read the same answer: the recorded choice answer and the pinned
     brief copy agree, so the dispatch-time contract never refuses what the draft offered.
 F13 no None answer: byte-identical (no answers recorded, brief untouched).
 F14 a pin the creator never answered still names the clip, now with a way forward.
 F15 a skipped picker is not reported as "none of your clips show it".
 F16 applying the answers twice changes nothing more.

Everything runs the real clip-selection fold, the real choice gate
(`planner._gate_unresolved_choices`), the real draft backstop
(`kria_runtime._unresolved_choice_plan`) and the real render contract over this run's
recorded events, brief and strategy (`kri1025_plaza_m1_turn2.json` beside this file); only
the database reads are replaced by fixed values. No model is called.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.kria import planner
from app.kria.brief import CreativeBrief
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import ClipIntent, ResolvedClipIntent
from app.services.choice_questions import answered_brief, tag_event
from app.services.clip_intent_planning import apply_creator_selections
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_none_answers import none_answer_choices
from app.services.clip_selection import fold_clip_selections
from app.services.creator_render_contract import build_render_contract
from app.tasks.kria_runtime import _unresolved_choice_plan
from tests.kria.test_choice_conflict_gate import _asked, _gate, _picked

FIXTURE = Path(__file__).with_name("kri1025_plaza_m1_turn2.json")
CASE: dict[str, Any] = json.loads(FIXTURE.read_text())

P = "analysis-proxy-ios-"
BUS = P + "0E376590-356E-4229-97EB-59D03ADBA72D.mp4"
J1 = P + "2B3DABCB-AEE9-4BF1-87C9-DF2CE6D1A521.mp4"
CATHEDRAL = P + "A61E17CA-C7A7-4363-B7A6-6BF129E93A95.mp4"
J2 = P + "9ECFFEBC-3E41-4F34-B52B-C83A1E4D7FFE.mp4"
CASA = P + "AB2850CF-EE2D-40F8-A1D6-6874E13740BC.mp4"
PALMS = P + "4869C2B5-F7C3-4304-9BA2-D713372FBD21.mp4"
RECOLETA = P + "71B14ED8-DDAF-42F5-9803-9285030AD2DD.mp4"
DEAD_END = "I couldn't tell which clip \"the Obelisco\" means, so I can't confirm the order."
# Turn 2's Main Creator summary was not recorded; any model summary is kept as is.
SUMMARY = "A 20-second montage of your afternoon at Plaza de Mayo, in the order you filmed it."


@pytest.fixture(autouse=True)
def _flags(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    # CLIP_INTENTS_ENABLED is on in prod (CLAUDE.md); the picker and choice gates default on.
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)


def _events(events: list[dict[str, Any]] | None = None) -> list[tuple[str, dict[str, Any]]]:
    """The thread as the gate loads it: ``(role, tagged payload)`` in sequence order."""
    return [
        tag_event(e["actor"], e["payload"], e["kind"], e["content"])
        for e in (CASE["events"] if events is None else events)
    ]


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "media_id": m["media_id"],
            "kind": "video",
            "duration_s": m["duration_s"],
            "capture": {"capture_time": m["capture_time"]},
        }
        for m in CASE["media"]
    ]


def _brief(**r1: Any) -> CreativeBrief:
    data = copy.deepcopy(CASE["brief_v1"])
    if r1:
        data["requirements"][0].update(r1)
    return CreativeBrief.model_validate(data)


def _turn2(*, keep: tuple[str, ...] = (), summary: str = SUMMARY) -> PlannedKriaTurn:
    """Turn 2's draft as the planner hands it to the gate.

    The approved turn-3 strategy (`strategy_t3.json`) minus the ending the creator only typed
    on turn 3 (`order_end_cathedral`); ``keep`` keeps it.
    """
    raw = copy.deepcopy(CASE["strategy_turn3"])
    later = {"order_end_cathedral"} - set(keep)
    resolved = [i for i in raw.pop("resolved_clip_intents") if i["intent_id"] not in later]
    requested = [i for i in raw.pop("clip_intents") if i["intent_id"] not in later]
    plan = planner.adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy.model_validate(raw),
            summary=summary,
        ),
        server_clip_intents=[ClipIntent.model_validate(i) for i in requested],
        server_resolved_clip_intents=[ResolvedClipIntent.model_validate(i) for i in resolved],
    )
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)


def _strategy(result: PlannedKriaTurn) -> dict[str, Any]:
    assert result.plan.mode == "act", result.plan.response
    return result.plan.intents[0].arguments["strategy"]


def _summary(result: PlannedKriaTurn) -> str:
    return str(result.plan.intents[0].arguments["summary"])


def _backstop(strategy: dict[str, Any], brief: CreativeBrief, events=None):  # noqa: ANN001, ANN202
    """The runtime's draft backstop exactly as `_complete_turn_sync` calls it."""
    pinned = answered_brief(brief, strategy)
    return _unresolved_choice_plan(
        strategy,
        pinned,
        {"clip_assignments": _rows()},
        contract_brief=pinned,
        events=_events() if events is None else events,
        has_draft=False,
    )


def _contract(strategy: dict[str, Any], brief: CreativeBrief):  # noqa: ANN202
    return build_render_contract(
        strategy,
        generation_id="g",
        brief=answered_brief(brief, strategy),
        media_snapshot={"clip_assignments": _rows()},
    )


# ── the server-side trace of the None answer (unchanged by this fix) ──────────


def test_the_none_answer_drops_the_obelisco_pin_and_settles_the_tapped_skips() -> None:
    """`fold_clip_selections` + `apply_creator_selections` over the recorded events: the
    planner side already honours the None (it drops the pin); the brief did not."""
    inventory = [  # turn 1's checked inventory (KRI-950), reused for the tap on turn 2
        ClipIntent(intent_id="order_filmed_pm", op="order", attribute="our afternoon"),
        ClipIntent(
            intent_id="exclude_short_clips", op="exclude", attribute="anything too short to use"
        ),
        ClipIntent(
            intent_id="label_casarosada",
            op="label",
            attribute="the pink building",
            creator_text="Casa Rosada",
        ),
        ClipIntent(
            intent_id="label_catedral", op="label", attribute="the columns", creator_text="Catedral"
        ),
        ClipIntent(
            intent_id="order_last_obelisco", op="order", position="last", attribute="the Obelisco"
        ),
    ]
    clips = [IntentClip(media_id=m["media_id"], kind="video", analysis={}) for m in CASE["media"]]
    settled, remaining, dropped = apply_creator_selections(
        inventory,
        clips=clips,
        selections=fold_clip_selections(_events()),
        creator_request=CASE["events"][0]["content"],
    )
    assert dropped == {"order_last_obelisco"}
    assert [(s.intent_id, [a.media_id for a in s.assignments]) for s in settled] == [
        ("exclude_short_clips", [J1, J2])
    ]
    assert [i.intent_id for i in remaining] == [
        "order_filmed_pm",
        "label_casarosada",
        "label_catedral",
    ]
    assert CASE["turn2_reply"] == DEAD_END  # what the creator saw


# ── F1 / F10 / F12: the incident, end to end ─────────────────────────────────


@pytest.mark.asyncio
async def test_f1_the_none_answer_is_a_draft_that_says_so_never_the_dead_end(monkeypatch) -> None:
    result = await _gate(monkeypatch, _turn2(), rows=_rows(), brief=_brief(), events=_events())
    strategy = _strategy(result)

    blocked = _backstop(strategy, _brief())
    assert blocked is None, blocked[0].response  # main: the recovery `DEAD_END`

    summary = _summary(result)
    assert summary.startswith(SUMMARY)
    assert (
        'None of your clips show "the Obelisco", so the video ends on the last clip you filmed. '
        "Tell me which clip to end on if you'd like a different one."
    ) in summary
    assert "the Obelisco" in " ".join(result.policy_notices)
    # F12: the decision is a server-owned answer, so approval pins it and dispatch reads it.
    (answer,) = [a for a in strategy["choice_answers"] if a["kind"] == "pin_absent"]
    assert (answer["requirement_ids"], answer["option"], answer["value"]) == (
        ["r1"],
        "last",
        "the Obelisco",
    )
    stored = CreativeStrategy.model_validate(strategy).model_dump(mode="json")
    assert stored["choice_answers"] == strategy["choice_answers"]  # the draft keeps it as is
    contract = _contract(strategy, _brief())
    assert contract is not None and not contract.unresolved
    assert contract.order_basis == "capture_time"
    assert list(contract.order_ids) == [PALMS, CASA, BUS, RECOLETA, CATHEDRAL]
    # F10: the pinned brief copy no longer promises the Obelisco anywhere.
    r1 = answered_brief(_brief(), strategy).requirements[0]
    assert r1.facts == {"key": "capture_time"}
    assert r1.description == "in the order you filmed them"
    assert "Obelisco" not in r1.text()


@pytest.mark.asyncio
async def test_f16_applying_the_answers_twice_changes_nothing_more(monkeypatch) -> None:
    strategy = _strategy(
        await _gate(monkeypatch, _turn2(), rows=_rows(), brief=_brief(), events=_events())
    )
    once = answered_brief(_brief(), strategy)
    assert answered_brief(once, strategy) == once


# ── F5 / F6 / F7 / F8: only the words the creator said None to ───────────────


@pytest.mark.asyncio
async def test_f5_a_typed_new_ending_is_never_voided_by_the_old_none(monkeypatch) -> None:
    brief = _brief(
        facts={"key": "capture_time", "last_clip": "the cathedral"},
        description="in the order we filmed it, ending on the cathedral",
    )
    result = await _gate(
        monkeypatch,
        _turn2(keep=("order_end_cathedral",)),
        rows=_rows(),
        brief=brief,
        events=_events(),
    )
    strategy = _strategy(result)
    assert not strategy.get("choice_answers")
    assert "None of your clips" not in _summary(result)
    assert answered_brief(brief, strategy) == brief


@pytest.mark.asyncio
async def test_f5b_the_new_ending_is_not_voided_even_before_the_planner_places_it(
    monkeypatch,
) -> None:
    # The brief already says "the cathedral" and the planner has not placed it: a planner
    # miss, which the contract names as before -- never "none of your clips show it".
    brief = _brief(
        facts={"key": "capture_time", "last_clip": "the cathedral"},
        description="in the order we filmed it, ending on the cathedral",
    )
    strategy = _strategy(
        await _gate(monkeypatch, _turn2(), rows=_rows(), brief=brief, events=_events())
    )
    assert not strategy.get("choice_answers")


@pytest.mark.asyncio
async def test_f6_a_spot_another_pin_already_holds_is_not_voided(monkeypatch) -> None:
    # The "last" spot is placed (e.g. by a typed file name) while the brief still says
    # "the Obelisco": the contract seats the placed pin; nothing to void or disclose.
    strategy = _strategy(
        await _gate(
            monkeypatch,
            _turn2(keep=("order_end_cathedral",)),
            rows=_rows(),
            brief=_brief(),
            events=_events(),
        )
    )
    assert not strategy.get("choice_answers")
    assert _backstop(strategy, _brief()) is None


def _tapped_later(media_ids: list[str]) -> list[dict[str, Any]]:
    """The same picker asked again, this time answered with clips."""
    question = copy.deepcopy(CASE["events"][1])
    question["sequence"] = 16
    question["payload"]["clip_question"]["question_id"] = "later-question"
    answer = copy.deepcopy(CASE["events"][2])
    answer["sequence"] = 17
    answer["payload"]["clip_selection"] = {
        "question_id": "later-question",
        "answers": [{"key": "order:the-obelisco", "media_ids": media_ids}],
        "none_keys": [],
        "skipped": False,
    }
    return [*CASE["events"], question, answer]


@pytest.mark.asyncio
async def test_f7_a_newer_tap_for_the_same_words_wins(monkeypatch) -> None:
    strategy = _strategy(
        await _gate(
            monkeypatch,
            _turn2(),
            rows=_rows(),
            brief=_brief(),
            events=_events(_tapped_later([CATHEDRAL])),
        )
    )
    assert not [a for a in strategy.get("choice_answers") or [] if a["kind"] == "pin_absent"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("words", "voided"),
    [
        ("the Obelisco", True),
        ("Obelisco", True),
        ("The obelisco!", True),
        ("the Obelisk", False),
        ("the Cabildo", False),
    ],
)
async def test_f8_only_the_same_words_are_voided(monkeypatch, words: str, voided: bool) -> None:
    brief = _brief(facts={"key": "capture_time", "last_clip": words})
    strategy = _strategy(
        await _gate(monkeypatch, _turn2(), rows=_rows(), brief=brief, events=_events())
    )
    answers = [a for a in strategy.get("choice_answers") or [] if a["kind"] == "pin_absent"]
    assert bool(answers) is voided


# ── F2 / F3 / F4: first pins, pin-only orders, two pins ──────────────────────


def _picker(op: str, label: str, *, key: str | None = None, **extra: Any) -> list[dict]:
    """A one-row clip picker answered None, in the recorded payload shape."""
    key = key or f"{op}:{'-'.join(label.casefold().split())}"
    question = copy.deepcopy(CASE["events"][1])
    question["payload"]["clip_question"]["categories"] = [
        {
            "op": op,
            "key": key,
            "label": label,
            **extra,
            "candidate_media_ids": [m["media_id"] for m in CASE["media"]],
            "suggested_media_ids": [],
        }
    ]
    answer = copy.deepcopy(CASE["events"][2])
    answer["payload"]["clip_selection"] = {
        "question_id": question["payload"]["clip_question"]["question_id"],
        "answers": [],
        "none_keys": [key],
        "skipped": False,
    }
    return [CASE["events"][0], question, answer]


@pytest.mark.asyncio
async def test_f2_a_first_clip_pin_answered_none_starts_on_the_first_clip_filmed(
    monkeypatch,
) -> None:
    brief = _brief(
        facts={"key": "capture_time", "first_clip": "the tango dancers"},
        description="in the order we filmed it, starting with the tango dancers",
    )
    events = _events(_picker("order", "the tango dancers"))
    result = await _gate(monkeypatch, _turn2(), rows=_rows(), brief=brief, events=events)
    strategy = _strategy(result)
    assert _backstop(strategy, brief, events) is None
    assert (
        'None of your clips show "the tango dancers", so the video starts with the first clip '
        "you filmed. Tell me which clip to start with if you'd like a different one."
    ) in _summary(result)
    r1 = answered_brief(brief, strategy).requirements[0]
    assert (r1.facts, r1.description) == ({"key": "capture_time"}, "in the order you filmed them")


@pytest.mark.asyncio
async def test_f3_a_pin_only_order_answered_none_goes_away_instead_of_a_second_dead_end(
    monkeypatch,
) -> None:
    brief = _brief(facts={"last_clip": "the Obelisco"}, description="End on the Obelisco")
    result = await _gate(monkeypatch, _turn2(), rows=_rows(), brief=brief, events=_events())
    strategy = _strategy(result)
    blocked = _backstop(strategy, brief)
    assert blocked is None, blocked[0].response  # never "I can't verify this ordering rule"
    assert answered_brief(brief, strategy).requirements[0].status == "superseded"
    assert (
        'None of your clips show "the Obelisco", so I didn\'t pick a clip to end on. '
        "Tell me which clip to end on if you have one in mind."
    ) in _summary(result)


@pytest.mark.asyncio
async def test_f4_two_pins_one_answered_none_keeps_the_other_pinned(monkeypatch) -> None:
    brief = _brief(
        facts={"key": "capture_time", "first_clip": "the palm trees", "last_clip": "the Obelisco"},
        description="in the order we filmed it, from the palm trees to the Obelisco",
    )
    first_pin = {
        "op": "order",
        "status": "resolved",
        "position": "first",
        "attribute": "the palm trees",
        "intent_id": "order_first_palms",
        "assignments": [{"media_id": PALMS, "evidence": "palms", "confidence": 0.9}],
    }
    case = copy.deepcopy(CASE["strategy_turn3"])
    case["resolved_clip_intents"].append(first_pin)
    case["clip_intents"].append({k: first_pin[k] for k in ("op", "position", "attribute")})
    case["clip_intents"][-1]["intent_id"] = "order_first_palms"
    monkeypatch.setitem(CASE, "strategy_turn3", case)
    strategy = _strategy(
        await _gate(monkeypatch, _turn2(), rows=_rows(), brief=brief, events=_events())
    )
    r1 = answered_brief(brief, strategy).requirements[0]
    assert r1.facts == {"key": "capture_time", "first_clip": "the palm trees"}
    assert r1.description == "in the order you filmed them, starting with the palm trees"
    contract = _contract(strategy, brief)
    assert not contract.unresolved and contract.order_ids[0] == PALMS


# ── F9: creator text whose described clip was answered None ──────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["per_clip", "global"])
async def test_f9_a_text_whose_clip_was_answered_none_is_left_out_and_said(
    monkeypatch, scope: str
) -> None:
    brief = _brief()
    data = brief.model_dump(mode="json")
    data["requirements"][5].update({"scope": scope, "description": "the pink building"})
    brief = CreativeBrief.model_validate(data)
    assert brief.requirements[5].literal == "Casa Rosada"
    events = _events(
        _picker(
            "label",
            "the pink building",
            intent_id="label_casarosada",
            creator_text="Casa Rosada",
        )
    )
    case = copy.deepcopy(CASE["strategy_turn3"])
    case["resolved_clip_intents"] = [
        i for i in case["resolved_clip_intents"] if i["intent_id"] != "label_casa_rosada"
    ]
    monkeypatch.setitem(CASE, "strategy_turn3", case)
    result = await _gate(monkeypatch, _turn2(), rows=_rows(), brief=brief, events=events)
    strategy = _strategy(result)
    pinned = answered_brief(brief, strategy)
    assert pinned.requirements[5].status == "superseded"
    contract = _contract(strategy, brief)
    # main: per_clip -> "I couldn't tell which clip "Casa Rosada" goes on"; global -> the text
    # stays pinned on an edit that prints it nowhere (a refusal after the render).
    assert contract is not None
    assert "Casa Rosada" not in {t.text for t in contract.exact_texts}
    assert not [u for u in contract.unresolved if "Casa Rosada" in u]
    assert (
        'None of your clips show "the pink building", so I left out "Casa Rosada". '
        "Tell me which clip it goes on if you still want it."
    ) in _summary(result)


# ── F11: a Turkish chat ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f11_a_turkish_chat_hears_it_in_turkish(monkeypatch) -> None:
    with reply_language_for("tr"):
        result = await _gate(monkeypatch, _turn2(), rows=_rows(), brief=_brief(), events=_events())
        strategy = _strategy(result)
        r1 = answered_brief(_brief(), strategy).requirements[0]
    assert (
        'Kliplerinin hiçbirinde "the Obelisco" yok, o yüzden video çektiğin son klipte bitiyor. '
        "Başka bir klipte bitmesini istersen hangisi olduğunu söyle."
    ) in _summary(result)
    assert r1.description == "çektiğin sırayla"


@pytest.mark.asyncio
async def test_f17_upload_order_chosen_too_ends_on_the_last_clip_added(monkeypatch) -> None:
    # No filming times reached the server: the order question is asked first, as before.
    rows = [{k: v for k, v in row.items() if k != "capture"} for row in _rows()]
    asked = _asked(await _gate(monkeypatch, _turn2(), rows=rows, brief=_brief(), events=_events()))
    assert asked[1]["choice_question"]["kind"] == "order_basis"
    events = [*_events(), asked, _picked(asked[1]["choice_question"], "attachment_order")]
    result = await _gate(monkeypatch, _turn2(), rows=rows, brief=_brief(), events=events)
    strategy = _strategy(result)
    assert (
        'None of your clips show "the Obelisco", so the video ends on the last clip you added.'
    ) in _summary(result)
    assert "last clip you filmed" not in _summary(result)
    r1 = answered_brief(_brief(), strategy).requirements[0]
    assert (r1.facts, r1.description) == (
        {"key": "attachment"},
        "in the order the clips were added",
    )
    contract = build_render_contract(
        strategy,
        generation_id="g",
        brief=answered_brief(_brief(), strategy),
        media_snapshot={"clip_assignments": rows},
    )
    assert not contract.unresolved and contract.order_basis == "attachment_order"


# ── F13 / F14 / F15 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_f13_no_none_answer_is_byte_identical(monkeypatch) -> None:
    events = _events(CASE["events"][:1])  # the prompt only: nothing was answered
    result = await _gate(monkeypatch, _turn2(), rows=_rows(), brief=_brief(), events=events)
    strategy = _strategy(result)
    assert not strategy.get("choice_answers")
    assert _summary(result) == SUMMARY
    assert answered_brief(_brief(), strategy) == _brief()


@pytest.mark.asyncio
async def test_f14_a_pin_never_answered_is_named_with_a_way_forward(monkeypatch) -> None:
    events = _events(CASE["events"][:1])
    strategy = _strategy(
        await _gate(monkeypatch, _turn2(), rows=_rows(), brief=_brief(), events=events)
    )
    blocked = _backstop(strategy, _brief(), events)
    assert blocked is not None
    plan, reason = blocked
    assert reason == "unresolved_requirement"
    assert plan.response == f"{DEAD_END} Tell me which clip you mean, and I'll make a new draft."


@pytest.mark.asyncio
async def test_f15_a_skipped_picker_is_not_told_as_none(monkeypatch) -> None:
    events = copy.deepcopy(CASE["events"])
    events[2]["payload"]["clip_selection"] = {
        **events[2]["payload"]["clip_selection"],
        "answers": [],
        "none_keys": [],
        "skipped": True,
    }
    result = await _gate(
        monkeypatch, _turn2(), rows=_rows(), brief=_brief(), events=_events(events)
    )
    summary = _summary(result)
    assert 'You didn\'t pick a clip for "the Obelisco", so the video ends on the last clip' in (
        summary
    )
    assert "None of your clips" not in summary
    assert _backstop(_strategy(result), _brief(), _events(events)) is None


def _text_none_case() -> tuple[CreativeBrief, list[tuple[str, dict[str, Any]]]]:
    req = (
        _brief()
        .requirements[5]
        .model_copy(update={"scope": "per_clip", "description": "the pink building"})
    )
    events = _events(
        _picker("label", "the pink building", creator_text=req.literal, intent_id="label_casa")
    )
    return CreativeBrief(requirements=[req]), events


def test_none_for_one_shot_preserves_identical_text_on_another_shot() -> None:
    brief, events = _text_none_case()
    other = brief.requirements[0].model_copy(update={"id": "r10", "description": "the cathedral"})
    brief = brief.model_copy(update={"requirements": [*brief.requirements, other]})
    answers = none_answer_choices(brief, {}, events)
    pinned = answered_brief(brief, {"choice_answers": list(answers.answers)})
    assert pinned.requirements[0].status == "superseded"
    assert pinned.requirements[1] == other


@pytest.mark.parametrize("shot", ["the cathedral", "the pink building"])
def test_old_none_does_not_cancel_a_later_text_request(shot: str) -> None:
    brief, events = _text_none_case()
    changed = brief.requirements[0].model_copy(
        update={"description": shot, "source_turn_id": "later-text-request"}
    )
    brief = brief.model_copy(update={"requirements": [changed]})
    events.append(("user", {"turn_id": "later-text-request"}))
    assert not none_answer_choices(brief, {}, events).answers


@pytest.mark.parametrize(
    "update",
    [
        {"literal": "New title"},
        {"description": "the cathedral"},
        {"scope": "global"},
        {"facts": {"placement": "bottom"}},
        {"source_turn_id": "later-text-request"},
    ],
)
def test_stored_text_absence_cannot_cancel_a_rewritten_requirement(update: dict) -> None:
    brief, events = _text_none_case()
    answers = none_answer_choices(brief, {}, events)
    assert len(answers.answers) == 1
    strategy = {"choice_answers": list(answers.answers)}
    assert answered_brief(brief, strategy).requirements[0].status == "superseded"
    changed = brief.model_copy(
        update={"requirements": [brief.requirements[0].model_copy(update=update)]}
    )
    assert answered_brief(changed, strategy) == changed


@pytest.mark.parametrize("source_turn_id", [None, "unobserved-request"])
def test_text_none_requires_observed_request_provenance(source_turn_id: str | None) -> None:
    brief, events = _text_none_case()
    brief = brief.model_copy(
        update={
            "requirements": [
                brief.requirements[0].model_copy(update={"source_turn_id": source_turn_id})
            ]
        }
    )
    assert not none_answer_choices(brief, {}, events).answers


@pytest.mark.parametrize("digest", [None, "legacy-unbound-digest"])
def test_unbound_stored_text_absence_keeps_the_requirement(digest: str | None) -> None:
    brief, events = _text_none_case()
    answer = {**none_answer_choices(brief, {}, events).answers[0], "input_digest": digest}
    assert answered_brief(brief, {"choice_answers": [answer]}) == brief


def test_newer_text_picker_tap_wins_over_none_for_the_same_shot() -> None:
    brief, events = _text_none_case()
    later = copy.deepcopy(events[-1][1])
    selection = later["clip_selection"]
    selection["answers"] = [{"key": selection["none_keys"][0], "media_ids": [CASA]}]
    selection["none_keys"] = []
    events.append(("user", later))
    assert not none_answer_choices(brief, {}, events).answers


def test_bound_text_absence_keeps_turkish_notice_and_is_idempotent() -> None:
    brief, events = _text_none_case()
    with reply_language_for("tr"):
        answers = none_answer_choices(brief, {}, events)
        strategy = {"choice_answers": list(answers.answers)}
        pinned = answered_brief(brief, strategy)
        assert answered_brief(pinned, strategy) == pinned
    assert pinned.requirements[0].status == "superseded"
    assert answers.notices == (
        'Kliplerinin hiçbirinde "the pink building" yok, o yüzden "Casa Rosada" yazısını '
        "koymadım. Yine de istersen hangi klibe gideceğini söyle.",
    )

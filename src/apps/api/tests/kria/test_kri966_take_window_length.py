"""KRI-966: Don Julio walk-in T1, the length card replayed from the prod thread.

Yasin's kit, 2026-10-26 (``T1_atakan_tail_take_talking/PROMPT.txt``): "Start at my question
"Parrilla Don Julio, what do you think?" and end right after "Nice!" ... About 7 seconds." One
26.9 s phone Talking take. After the start-line picker was answered, prod (``a2426885d``) asked:

    Your brief says "drop the dining room ...", but the available spoken take is 10.2 seconds.
    ... The most I can do is: Use the available 10.2 seconds.

10.2 s is the WHOLE take's timed speech (15.0 -> 24.88 s plus the composer's pad); the lines the
creator chose keep 0:15 -> 0:20. The creator tapped the only option, the Main Creator filed the
tapped label as a new requirement (r7, ``duration_s: 10.2``), and the after-render review said
"Partly: Use the available 10.2 seconds (... you asked for 10.2s)".

``tests/fixtures/kri966_donjulio_walkin_t1_length.json`` holds the thread's real inputs (its
``_source`` says where each part comes from): the approval's media snapshot and brief, the
picker / length questions and both taps, and the finished phone variant. Nothing in the chain
is replaced but the Main Creator's draft (a literal strategy: prod recorded only its hash) and
the thread's database reads:

    brief (r1: start_line / end_line / duration_s 7)
      -> the planner's question gate (``planner._gate_unresolved_choices``), turn by turn
      -> the creator's taps (the real ones where the question is unchanged)
      -> ``answered_brief`` (the copy approval pins)
      -> the runtime's approval backstop (``kria_runtime._unresolved_choice_plan``)
      -> the draft card (``estimate_take_window`` + ``build_receipts``)
      -> the after-render review (``plan_facts_from_phone_variant`` + ``build_receipts``).

Failure modes (``docs/reviews/kri-966-take-window-length.md``): F1 the lines' window is the
capacity; F2 a line not found asks no whole-take card; F3 no window is unchanged; F4 inside
the tolerance, no card; F5/F6 shorter / longer window: one honest option, no trim; F8 the
answer never returns as "you asked for N s"; F9 the answer settles (no re-ask loop); F10
Turkish. The model echo of the tapped label (r7) is dropped by the planner
(``choice_questions.without_length_answer_echo``); its runtime replay is
``test_kri966_t1_runtime_postgres.py``.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief
from app.kria.brief_checks import PlanFacts, build_receipts, plan_facts_from_phone_variant
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.opening_cut_ask import estimate_take_window
from app.kria.planner import PlannedKriaTurn
from app.kria.reply_language import reply_language_for
from app.services.choice_questions import (
    CONFLICT_LENGTH_VS_NATURAL,
    ChoiceCapability,
    answered_brief,
    collect_conflicts,
    tag_event,
)

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "kri966_donjulio_walkin_t1_length.json"
    ).read_text(encoding="utf-8")
)
SNAPSHOT = FIXTURE["media_snapshot"]
ROW = SNAPSHOT["clip_assignments"][0]
MEDIA_ID = ROW["media_id"]
TAKE_S = float(ROW["duration_s"])
EVENTS = FIXTURE["events"]
PROD_PICKER = EVENTS["6"]["payload"]["choice_question"]
PROD_LENGTH_CARD = EVENTS["8"]["payload"]["choice_question"]
TURN = FIXTURE["turn_ids"]
CREATOR_ID = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")
THREAD_ID = uuid.UUID("f0f3d5fb-a207-4f1f-8340-d1faaa2b1a04")
START_PICK = "what do you think?"
END_LINE = "Nice!"


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings this chat ran under: phone Talking pilot, brief binding, choice gate."""
    for name, value in {
        "phone_rendering_enabled": True,
        "phone_render_user_ids": [],
        "kria_runtime_v2_phone_enabled": True,
        "subtitled_archetype_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
    }.items():
        monkeypatch.setattr(settings, name, value)


# -- the recorded thread ----------------------------------------------------------------------


def brief(version: int, **r1_facts: object) -> CreativeBrief:
    """The thread's stored brief at ``version``, from the approval's v4 by source turn.

    v1 is the prompt's (r1 quotes "Parrilla Don Julio, what do you think?"); v2 is prod's turn 2,
    whose model rewrote r1's start line to the picked "what do you think?"; v3 adds the model's
    echo of the tapped length (r7); v4 drops the ATAKAN tag (r4, "simpler version").
    ``r1_facts`` overrides r1's facts (None removes a fact).
    """
    rows = []
    for raw in FIXTURE["brief_v4"]["requirements"]:
        req = BriefRequirement.model_validate(raw)
        if req.id == "r7" and version < 3:
            continue
        if req.id == "r4" and version < 4:
            req = req.model_copy(update={"status": "open"})
        if req.id == "r1":
            facts = dict(req.facts)
            if version < 2:
                facts["start_line"] = FIXTURE["v1_start_line"]
                req = req.model_copy(update={"source_turn_id": TURN["t1"]})
            for key, value in r1_facts.items():
                facts.pop(key, None) if value is None else facts.__setitem__(key, value)
            req = req.model_copy(update={"facts": facts})
        rows.append(req)
    return CreativeBrief(version=version, requirements=rows)


def strategy(**changes: object) -> dict:
    """The Main Creator's draft: a phone Talking edit of the one take (RECONSTRUCTED: prod kept
    only its hash; the card's "Balanced pacing", "Subtitled format" and the 7 s ask)."""
    return {
        "pacing": "balanced",
        "direction": "native",
        "rationale": "A talking edit of the question and Atakan's answer outside Don Julio.",
        "edit_format": "subtitled",
        "media_scope": "selected",
        "caption_style": "auto",
        "audio_strategy": "original_audio",
        "render_program": "native",
        "target_duration_s": 7,
        "selected_media_ids": [MEDIA_ID],
        "video_reuse_policy": "once",
        "optional_treatments": [],
        **changes,
    }


def planned(draft: dict) -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=KriaTurnPlan(
            mode="act",
            turn_value="action",
            intents=[
                KriaToolIntent(
                    intent_id="apply-strategy",
                    tool_name="draft.apply_strategy",
                    tool_version=1,
                    arguments={"summary": "", "strategy": copy.deepcopy(draft)},
                ),
                KriaToolIntent(
                    intent_id="request-render",
                    tool_name="render.request",
                    tool_version=1,
                    depends_on=["apply-strategy"],
                ),
            ],
        ),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        media_snapshot=copy.deepcopy(SNAPSHOT),
        brief_route="replan",
        brief_clip_ids=(MEDIA_ID,),
    )


def real(sequence: int) -> tuple:
    """A prod thread event as the planner reads it."""
    event = EVENTS[str(sequence)]
    return tag_event(event["actor"], event["payload"], event["kind"], event["content"])


class Chat:
    """The thread as the server reads it, from the real events on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, stored: CreativeBrief, *sequences: int):
        self.brief = stored
        self.events: list[tuple] = [real(sequence) for sequence in (4, *sequences)]
        self.question: dict | None = None
        monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(side_effect=self._brief))
        monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(side_effect=self._events))

    async def _brief(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return self.brief

    async def _events(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return list(self.events)

    async def turn(self, draft: dict | None = None) -> PlannedKriaTurn:
        """The question gate on the drafted strategy (one question per turn)."""
        result = await planner._gate_unresolved_choices(
            SimpleNamespace(),
            planned(draft or strategy()),
            thread_id=THREAD_ID,
            creator_id=CREATOR_ID,
        )
        self.question = result.plan.choice_question
        if self.question is not None:
            self.events.append(
                tag_event(
                    "assistant",
                    {"choice_question": self.question, "turn_value": "question"},
                    "assistant_response",
                    result.plan.response,
                )
            )
        return result

    def tap(self, key: str) -> str:
        """Tap the open question's option ``key`` (its label is the message); returns it."""
        assert self.question is not None, "no question is open"
        option = next(o for o in self.question["options"] if o["key"] == key)
        self.events.append(
            tag_event(
                "user",
                {
                    "turn_id": f"t{len(self.events)}",
                    "choice_selection": {
                        "question_id": self.question["question_id"],
                        "option_key": key,
                    },
                },
                "user_message",
                option["label"],
            )
        )
        return option["label"]


# The two ways turn 2 reaches the gate: prod's (the model rewrote r1's start line to the pick)
# and the server-owned one (the quote stays, the pick rides the creator's tap).
SHAPES = {
    "prod-rewrite": lambda: brief(2),
    "pick-on-answer": lambda: brief(1),
}


def draft_card(final: CreativeBrief, draft: dict) -> dict[str, str]:
    """The draft card's reasons, as the runtime builds them for a phone Talking draft."""
    facts = PlanFacts(
        edit_format="subtitled",
        phone_talking=True,
        take_duration_s=TAKE_S,
        take_window=estimate_take_window(final.live(), SNAPSHOT, strategy=draft),
    )
    with reply_language_for("en"):
        return {
            r.requirement_id: str(r.reason or "")
            for r in build_receipts(final.live(), facts, include_unchecked=True, strict_order=True)
        }


def review(final: CreativeBrief) -> dict[str, tuple[str, str]]:
    """The after-render review of the real finished variant (``kria_runtime`` review path)."""
    facts = plan_facts_from_phone_variant(FIXTURE["variant"])
    with reply_language_for("en"):
        return {
            r.requirement_id: (r.status, str(r.reason or ""))
            for r in build_receipts(final.live(), facts, include_unchecked=True)
        }


# -- the fixture is prod's input ----------------------------------------------------------------


async def test_the_fixture_reproduces_both_prod_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provenance and F3: turn 1 asks prod's picker (same digest), and without the lines the
    length card is still prod's whole-take card (same digest, same words)."""
    chat = Chat(monkeypatch, brief(1))
    first = await chat.turn()
    assert first.plan.choice_question["kind"] == "take_line_start"
    assert first.plan.choice_question["input_digest"] == PROD_PICKER["input_digest"]
    assert [o["label"] for o in first.plan.choice_question["options"]] == [
        o["label"] for o in PROD_PICKER["options"]
    ]

    no_window = brief(2, start_line=None, end_line=None)
    (card,) = [
        c
        for c in collect_conflicts(strategy(), no_window, SNAPSHOT, ChoiceCapability())
        if c.kind == CONFLICT_LENGTH_VS_NATURAL
    ]
    assert card.input_digest == PROD_LENGTH_CARD["input_digest"]
    assert [o.label for o in card.options] == ["Use the available 10.2 seconds"]
    assert "the available spoken take is 10.2 seconds" in card.intro


def test_prod_review_graded_the_echo_as_an_ask() -> None:
    """The after-render path reproduces prod's wrong row from the approved brief (v4)."""
    rows = review(brief(4))
    assert rows["r7"] == (
        "partial",
        "This draft is about 4.2s (plus a 1.6s outro on the finished video); you asked for 10.2s.",
    )
    assert rows["r1"][0] == "met"


# -- the journey ----------------------------------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
async def test_turn_2_asks_about_the_lines_not_the_whole_take(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """F1 / F5: the creator's own tap (prod #7), then the length card names the lines and the
    part they keep (about 5 s of 0:15 -> 0:20), with one way forward and no trim."""
    chat = Chat(monkeypatch, SHAPES[shape](), 6, 7)
    second = await chat.turn()
    question = second.plan.choice_question
    assert second.plan.mode == "respond" and question is not None, second.plan.response
    assert question["kind"] == CONFLICT_LENGTH_VS_NATURAL
    text = second.plan.response or ""
    assert "10.2" not in text and "spoken take" not in text, text
    assert f"“{START_PICK}”" in text and "“Nice" in text, text
    assert "about 5 seconds" in text and "7-second" in text, text
    assert [o["key"] for o in question["options"]] == ["accept_natural"]
    assert question["options"][0]["label"] == "Keep those 5 seconds"


@pytest.mark.parametrize("shape", SHAPES)
async def test_the_tapped_length_settles_once(monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    """F8 / F9: one tap settles the length; the draft, the approval backstop and the review never
    ask it again or grade it as the creator's own number."""
    from app.tasks.kria_runtime import _unresolved_choice_plan  # noqa: PLC0415

    stored = SHAPES[shape]()
    chat = Chat(monkeypatch, stored, 6, 7)
    await chat.turn()
    chat.tap("accept_natural")
    third = await chat.turn()
    assert third.plan.mode == "act", third.plan.response
    draft = third.plan.intents[0].arguments["strategy"]
    answers = {a["kind"]: a for a in draft["choice_answers"]}
    assert answers[CONFLICT_LENGTH_VS_NATURAL]["option"] == "accept_natural"
    assert draft["target_duration_s"] == 5

    final = answered_brief(stored, draft)
    r1 = next(req for req in final.live() if req.id == "r1")
    assert r1.facts["duration_s"] == 5
    assert (r1.facts.get("start_line_spoken") or r1.facts["start_line"]) == START_PICK
    assert r1.facts["end_line"] == END_LINE
    assert not [req for req in final.live() if (req.facts or {}).get("duration_s") == 10.2]

    events = [*chat.events]
    blocked = _unresolved_choice_plan(
        draft,
        final,
        copy.deepcopy(SNAPSHOT),
        contract_brief=final,
        events=events,
        creator_id=CREATOR_ID,
    )
    assert blocked is None or blocked[0].choice_question is None, blocked

    card = draft_card(final, draft)
    assert "you asked for" not in " ".join(card.values()), card
    assert (
        f"“{START_PICK}” (0:15)" in card["r1"]
        or f"“{FIXTURE['v1_start_line']}” (0:15)" in card["r1"]
    ), card["r1"]
    rows = review(final)
    assert not [row for row in rows.values() if "you asked for" in row[1]], rows
    assert rows["r1"][0] == "met"


async def test_a_line_not_found_asks_no_whole_take_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2: before the picker is answered, and after it is declined, no length card is built from
    the whole take's speech."""
    cap = ChoiceCapability(creator_id=CREATOR_ID)
    kinds = [c.kind for c in collect_conflicts(strategy(), brief(1), SNAPSHOT, cap)]
    assert kinds == ["take_line_start"], kinds

    chat = Chat(monkeypatch, brief(1), 6)
    chat.question = PROD_PICKER
    chat.tap("no_line")
    declined = await chat.turn()
    assert declined.plan.choice_question is None, declined.plan.response
    assert declined.plan.mode == "act"


@pytest.mark.parametrize(
    ("asked", "card"),
    [(5.4, None), (4.6, None), (7, "short"), (3, "long")],
    ids=["F4-just-over", "F4-just-under", "F5-shorter-window", "F6-longer-window"],
)
def test_the_window_is_judged_with_the_usual_tolerance(asked: float, card: str | None) -> None:
    """F4 / F5 / F6 on the real take: 10 % of the ask, one option, never a trim."""
    cap = ChoiceCapability(creator_id=CREATOR_ID)
    found = [
        c
        for c in collect_conflicts(strategy(), brief(2, duration_s=asked), SNAPSHOT, cap)
        if c.kind == CONFLICT_LENGTH_VS_NATURAL
    ]
    if card is None:
        assert found == []
        return
    (choice,) = found
    assert [o.key for o in choice.options] == ["accept_natural"]
    assert choice.options[0].recommended is (card == "short")
    assert "about 5 seconds" in choice.intro


async def test_a_turkish_chat_gets_the_window_card_in_turkish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F10: the same card in a Turkish chat names the same lines, in Turkish."""
    chat = Chat(monkeypatch, brief(2), 6, 7)
    with reply_language_for("tr"):
        second = await chat.turn()
    text = second.plan.response or ""
    assert f"“{START_PICK}”" in text and "yaklaşık 5 saniye" in text, text
    assert "Maalesef" in text and "10.2" not in text, text
    assert second.plan.choice_question["options"][0]["label"] == "O 5 saniyeyi kullan"


# -- the echo of the tapped label --------------------------------------------------------------


def _prod_echo() -> BriefUpdate:
    """The Main Creator's turn-3 update as prod stored it (r7)."""
    r7 = next(r for r in FIXTURE["brief_v4"]["requirements"] if r["id"] == "r7")
    return BriefUpdate(
        operation="add",
        kind=r7["kind"],
        scope=r7["scope"],
        description=r7["description"],
        facts=r7["facts"],
    )


def test_the_echo_of_a_tapped_length_is_not_filed() -> None:
    """F8 / F11: on prod's turn 3 (the tap, #9, answering #8) the model's r7 is dropped, and with
    it the second length that made the gate stop recording the answer."""
    from app.services.choice_questions import without_length_answer_echo  # noqa: PLC0415

    events = [real(sequence) for sequence in (4, 6, 7, 8, 9)]
    rewrite_r1 = BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        expected_version=2,
        kind="timing",
        scope="global",
        description="Keep 10.2 seconds",
        facts={"start_line": START_PICK, "end_line": END_LINE, "duration_s": 10.2},
    )
    title = BriefUpdate(operation="add", kind="text", scope="title", literal="Before the meat")
    assert without_length_answer_echo(brief(2), (_prod_echo(), rewrite_r1, title), events) == (
        title,
    )
    # A partial change (KRI-805: no kind, the target's is kept) that puts the seconds on a
    # timing row holding only the lines is the same echo.
    partial = BriefUpdate(
        operation="change", target_requirement_id="r1", facts={"duration_s": 10.2}
    )
    assert without_length_answer_echo(brief(2, duration_s=None), (partial,), events) == ()


def test_a_length_the_creator_types_or_another_answer_is_kept() -> None:
    """Wrong-content controls: the same update survives a typed message and the start-line tap."""
    from app.services.choice_questions import without_length_answer_echo  # noqa: PLC0415

    echo = _prod_echo()
    typed = [
        real(4),
        tag_event("user", {"turn_id": "t9"}, "user_message", "Make it 10.2 seconds"),
    ]
    assert without_length_answer_echo(brief(2), (echo,), typed) == (echo,)
    picker_tap = [real(sequence) for sequence in (4, 6, 7)]
    assert without_length_answer_echo(brief(2), (echo,), picker_tap) == (echo,)

"""KRI-476 (PR-C): the clarification gate asks ONE focused question for a real conflict,
follows the stored answer, and never asks (or silently decides) anything else.

Failure modes this file is written against (before the code):

* asks on a clear request (30 clips in 60 s, a default length, fewer clips than fit);
* silently flash-cuts / silently changes the plan instead of asking;
* an answer lost on a retried turn, or a re-sent prompt asking again;
* a changed media set NOT reopening the question, or an old answer silently answering a
  different (later) question;
* a question loop (an unanswerable question asked forever);
* a model-authored answer being trusted;
* the contract still rejecting the plan the creator chose.

Everything runs the real adapter, the real gate (`_gate_unresolved_choices`, the code
`plan_live_turn` calls once the approved snapshot is attached) and the real contract
builder; only the database reads are replaced by fixed values.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.planner import PlannedKriaTurn
from app.services.choice_questions import (
    MAX_ASKS_PER_QUESTION,
    answered_brief,
    ask_user_choice,
    collect_conflicts,
    match_open_choice,
    open_conflicts,
)
from app.services.creator_render_contract import build_render_contract


def _rows(count: int, *, dated: bool = True, offset: int = 0) -> list[dict]:
    rows = []
    for i in range(count):
        row: dict = {"media_id": f"c{i + offset:02d}", "kind": "video", "duration_s": 3.0}
        if dated:
            row["capture"] = {"capture_time": f"2026-09-20T09:{i % 60:02d}:00Z"}
        rows.append(row)
    return rows


def _timing(seconds: int, *, rid: str = "r1") -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="timing",
        scope="global",
        description=f"Keep {seconds} seconds",
        facts={"duration_s": seconds},
    )


def _order(*, rid: str = "r2", key: str = "capture_time") -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="order",
        scope="global",
        description="in the order I filmed them",
        facts={"key": key},
    )


def _brief(*requirements: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(requirements))


def _planned(**strategy: object) -> PlannedKriaTurn:
    seconds = strategy.pop("seconds", None)
    base: dict = {
        "direction": "guided_story",
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "pacing": "fast",
        "render_program": "guided",
        "rationale": "A montage.",
    }
    if seconds is not None:
        base |= {"target_duration_s": seconds, "target_duration_requested": True}
    strategy_model = CreativeStrategy(**{**base, **strategy})
    plan = planner.adapt_creator_action(
        ProposeStrategy(kind="propose_strategy", strategy=strategy_model, summary="A montage.")
    )
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)


async def _gate(
    monkeypatch: pytest.MonkeyPatch,
    planned: PlannedKriaTurn,
    *,
    rows: list[dict],
    brief: CreativeBrief | None = None,
    events: tuple = (),
) -> PlannedKriaTurn:
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _s, _i: True)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=list(events)))
    planned = replace(planned, media_snapshot={"clip_assignments": rows})
    return await planner._gate_unresolved_choices(
        SimpleNamespace(), planned, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
    )


def _strategy(result: PlannedKriaTurn) -> dict:
    assert result.plan.mode == "act", result.plan.response
    return result.plan.intents[0].arguments["strategy"]


def _asked(result: PlannedKriaTurn) -> tuple[str, dict]:
    assert result.plan.turn_value == "question" and result.plan.choice_question, result.plan
    return "assistant", {"choice_question": result.plan.choice_question}


def _picked(question: dict, option: str) -> tuple[str, dict]:
    return "user", {
        "choice_selection": {"question_id": question["question_id"], "option_key": option}
    }


# ── asks only when there is a real conflict ───────────────────────────────────


@pytest.mark.asyncio
async def test_thirty_clips_in_fifteen_seconds_asks_extend_or_fewer(monkeypatch) -> None:
    result = await _gate(
        monkeypatch, _planned(seconds=15), rows=_rows(30), brief=_brief(_timing(15))
    )
    assert result.plan.mode == "respond" and not result.plan.intents  # nothing can render
    question = result.plan.choice_question
    assert question["kind"] == "duration_vs_count"
    assert question["conflict"] == "duration_vs_count"
    assert question["input_digest"]
    assert [o["key"] for o in question["options"]] == ["extend", "fewer"]
    extend, fewer = question["options"]
    assert "24" in extend["label"] and extend["recommended"] is True
    assert "18 clips" in fewer["label"]
    # A client without the card still sees every choice, with the reason, in the text.
    for text in (extend["label"], fewer["label"], "30 clips", "15 seconds"):
        assert text in result.plan.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clips", "seconds", "asks"),
    [
        (30, 60, False),  # 2 s per clip
        (18, 15, False),  # 18 x 0.8 = 14.4 s fits
        (19, 15, True),  # 19 x 0.8 = 15.2 s does not
        (25, 20, False),  # exactly 20.0 s: fits, no float false positive
        (26, 20, True),
        (8, 15, False),  # an explicit length with FEWER clips than fit
    ],
)
async def test_duration_vs_count_boundaries(monkeypatch, clips, seconds, asks) -> None:
    result = await _gate(
        monkeypatch, _planned(seconds=seconds), rows=_rows(clips), brief=_brief(_timing(seconds))
    )
    assert (result.plan.mode == "respond") is asks


@pytest.mark.asyncio
async def test_default_length_never_asks(monkeypatch) -> None:
    # No explicit duration: the 24 s default is not a promise.
    result = await _gate(monkeypatch, _planned(), rows=_rows(50), brief=None)
    assert result.plan.mode == "act"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"edit_format": "subtitled"},
        {"archetype": "single_hero"},
        {"media_scope": "selected", "selected_media_ids": ["c00", "c01", "c02"]},
    ],
)
async def test_shapes_that_cannot_flash_cut_never_ask(monkeypatch, extra) -> None:
    result = await _gate(
        monkeypatch, _planned(seconds=15, **extra), rows=_rows(30), brief=_brief(_timing(15))
    )
    assert result.plan.mode == "act"


@pytest.mark.asyncio
async def test_missing_capture_dates_asks_attachment_order_or_continue(monkeypatch) -> None:
    result = await _gate(
        monkeypatch, _planned(), rows=_rows(8, dated=False), brief=_brief(_order())
    )
    question = result.plan.choice_question
    assert question["kind"] == "order_basis"
    assert [o["key"] for o in question["options"]] == ["attachment_order", "unordered"]
    # "your own sequence" is NOT offered: nothing can receive a typed sequence yet.
    assert all("own" not in o["key"] for o in question["options"])
    assert "none of your clips have a filming time" in result.plan.response


@pytest.mark.asyncio
async def test_dated_clips_ask_nothing_about_order(monkeypatch) -> None:
    result = await _gate(monkeypatch, _planned(), rows=_rows(8), brief=_brief(_order()))
    assert result.plan.mode == "act"


@pytest.mark.asyncio
async def test_non_capture_order_rule_is_not_a_question(monkeypatch) -> None:
    # A rule the contract cannot verify is a typed decline, never an option list.
    result = await _gate(
        monkeypatch, _planned(), rows=_rows(8, dated=False), brief=_brief(_order(key="route"))
    )
    assert result.plan.mode == "act"


@pytest.mark.asyncio
async def test_order_question_comes_first_one_question_per_turn(monkeypatch) -> None:
    brief = _brief(_timing(15), _order())
    result = await _gate(
        monkeypatch, _planned(seconds=15), rows=_rows(30, dated=False), brief=brief
    )
    assert result.plan.choice_question["kind"] == "order_basis"


# ── answers: persisted, digest-scoped, never re-asked ─────────────────────────


async def _asked_then_answered(monkeypatch, option: str, **kw):  # noqa: ANN001, ANN003, ANN202
    rows, brief = _rows(30), _brief(_timing(15))
    first = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief)
    asked = _asked(first)
    events = (asked, _picked(asked[1]["choice_question"], option))
    return await _gate(
        monkeypatch, _planned(seconds=15), rows=rows, brief=brief, events=events, **kw
    )


@pytest.mark.asyncio
async def test_extend_answer_is_folded_linked_and_disclosed(monkeypatch) -> None:
    result = await _asked_then_answered(monkeypatch, "extend")
    strategy = _strategy(result)
    assert strategy["target_duration_s"] == 24 and strategy["target_duration_requested"] is True
    (answer,) = strategy["choice_answers"]
    assert answer["kind"] == "duration_vs_count" and answer["option"] == "extend"
    assert answer["requirement_ids"] == ["r1"] and answer["source"] == "creator"
    summary = result.plan.intents[0].arguments["summary"]
    assert "extended it" in summary  # the change is never silent
    assert [i.tool_name for i in result.plan.intents] == ["draft.apply_strategy", "render.request"]


@pytest.mark.asyncio
async def test_fewer_answer_keeps_the_length_and_selects_evenly(monkeypatch) -> None:
    strategy = _strategy(await _asked_then_answered(monkeypatch, "fewer"))
    assert strategy["target_duration_s"] == 15
    chosen = strategy["selected_media_ids"]
    assert len(chosen) == 18 and chosen[0] == "c00" and chosen[-1] == "c29"
    assert chosen == sorted(chosen) and strategy["media_scope"] == "selected"


@pytest.mark.asyncio
async def test_answer_survives_a_retried_turn_and_a_resent_prompt(monkeypatch) -> None:
    rows, brief = _rows(30), _brief(_timing(15))
    first = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief)
    asked = _asked(first)
    events = (asked, _picked(asked[1]["choice_question"], "extend"))
    seen = []
    for _ in range(3):  # a retry and a re-sent identical prompt re-derive the same plan
        seen.append(
            _strategy(
                await _gate(
                    monkeypatch, _planned(seconds=15), rows=rows, brief=brief, events=events
                )
            )
        )
    assert seen[0] == seen[1] == seen[2]
    assert seen[0]["choice_answers"][0]["option"] == "extend"


@pytest.mark.asyncio
async def test_changed_media_set_reopens_only_that_question(monkeypatch) -> None:
    brief = _brief(_timing(15))
    first = await _gate(monkeypatch, _planned(seconds=15), rows=_rows(30), brief=brief)
    asked = _asked(first)
    events = (asked, _picked(asked[1]["choice_question"], "extend"))
    # Two more clips arrive: the old answer was about 30 clips, so it asks again.
    again = await _gate(
        monkeypatch, _planned(seconds=15), rows=_rows(32), brief=brief, events=events
    )
    reopened = again.plan.choice_question
    assert reopened["kind"] == "duration_vs_count"
    assert reopened["input_digest"] != asked[1]["choice_question"]["input_digest"]
    assert reopened["question_id"] != asked[1]["choice_question"]["question_id"]
    assert "32" in again.plan.response


@pytest.mark.asyncio
async def test_old_answer_never_answers_a_different_later_question(monkeypatch) -> None:
    # Answered: order basis. The length question is NEW and must still be asked; the
    # order answer is not reused for it, and the length answer later does not reopen order.
    rows = _rows(30, dated=False)
    brief = _brief(_timing(15), _order())
    first = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief)
    order_q = _asked(first)
    assert order_q[1]["choice_question"]["kind"] == "order_basis"
    events = (order_q, _picked(order_q[1]["choice_question"], "attachment_order"))
    second = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief, events=events)
    length_q = _asked(second)
    assert length_q[1]["choice_question"]["kind"] == "duration_vs_count"
    events += (length_q, _picked(length_q[1]["choice_question"], "extend"))
    done = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief, events=events)
    answers = {a["kind"]: a["option"] for a in _strategy(done)["choice_answers"]}
    assert answers == {"order_basis": "attachment_order", "duration_vs_count": "extend"}


@pytest.mark.asyncio
async def test_a_stale_answer_to_a_changed_length_does_not_apply(monkeypatch) -> None:
    rows = _rows(30)
    first = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=_brief(_timing(15)))
    asked = _asked(first)
    events = (asked, _picked(asked[1]["choice_question"], "extend"))
    # The creator later asks for 16 s: still a conflict, but a different question.
    result = await _gate(
        monkeypatch, _planned(seconds=16), rows=rows, brief=_brief(_timing(16)), events=events
    )
    assert (
        result.plan.choice_question["input_digest"] != asked[1]["choice_question"]["input_digest"]
    )


@pytest.mark.asyncio
async def test_model_authored_answers_are_never_trusted(monkeypatch) -> None:
    forged = {
        "conflict": "duration_vs_count",
        "kind": "duration_vs_count",
        "option": "extend",
        "input_digest": "forged",
    }
    action = ProposeStrategy.model_validate(
        {
            "kind": "propose_strategy",
            "strategy": {
                "edit_format": "montage",
                "target_duration_s": 15,
                "target_duration_requested": True,
                "choice_answers": [forged],
            },
            "summary": "x",
        }
    )
    assert action.strategy.choice_answers  # the model CAN emit it ...
    planned = PlannedKriaTurn(
        plan=planner.adapt_creator_action(action), manifest_hash="a", context_hash="b"
    )
    assert "choice_answers" not in planned.plan.intents[0].arguments["strategy"]  # ... and loses
    result = await _gate(monkeypatch, planned, rows=_rows(30), brief=_brief(_timing(15)))
    assert result.plan.choice_question["kind"] == "duration_vs_count"


# ── the loop guard ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_re_ask_then_the_recommended_option_is_applied_and_disclosed(
    monkeypatch,
) -> None:
    rows, brief = _rows(30), _brief(_timing(15))
    events: tuple = ()
    asks = 0
    for _ in range(MAX_ASKS_PER_QUESTION):  # the question and exactly ONE re-ask
        result = await _gate(
            monkeypatch, _planned(seconds=15), rows=rows, brief=brief, events=events
        )
        events += (_asked(result),)
        asks += 1
    assert asks == 2
    final = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief, events=events)
    (answer,) = _strategy(final)["choice_answers"]
    assert answer["source"] == "default" and answer["option"] == "extend"
    summary = final.plan.intents[0].arguments["summary"]
    assert "You didn't pick one" in summary
    # And it stays decided on every later turn: no third question, ever.
    later = await _gate(monkeypatch, _planned(seconds=15), rows=rows, brief=brief, events=events)
    assert later.plan.mode == "act"


# ── free text ─────────────────────────────────────────────────────────────────


def _question(options: list[dict]) -> dict:
    return {"question_id": "q1", "conflict": "duration_vs_count", "options": options}


OPTIONS = [
    {"key": "extend", "label": "Extend it to 24 seconds (recommended)", "aliases": ["extend it"]},
    {"key": "fewer", "label": "Keep 15 seconds with 18 clips", "aliases": ["fewer clips"]},
]


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("extend it", "extend"),
        ("Extend it!", "extend"),
        ("  EXTEND   IT. ", "extend"),
        ("extend it to 24 seconds", "extend"),  # the label, without the "(recommended)" tag
        ("Keep 15 seconds with 18 clips", "fewer"),
        ("fewer clips", "fewer"),
        ("2", "fewer"),
        ("option 1", "extend"),
        ("fewer", "fewer"),  # the key
        ("make it more fun", None),
        ("extend it and add music", None),  # an instruction, not an answer
        ("", None),
    ],
)
def test_free_text_matches_exactly_one_option_or_nothing(reply, expected) -> None:
    assert match_open_choice(_question(OPTIONS), reply) == expected


def test_a_reply_that_names_two_options_is_not_an_answer() -> None:
    options = [
        {"key": "a", "label": "Use it", "aliases": ["ok"]},
        {"key": "b", "label": "Skip it", "aliases": ["ok"]},
    ]
    assert match_open_choice(_question(options), "ok") is None


def test_ask_user_options_become_a_tappable_question_only_with_two_or_more() -> None:
    text, payload = ask_user_choice(
        "Which clip opens?", "needs_opening", ["The pier", "The bridge"]
    )
    assert [o["label"] for o in payload["options"]] == ["The pier", "The bridge"]
    assert "1. The pier" in text and "2. The bridge" in text
    assert ask_user_choice("Which?", "x", ["only one"]) is None
    assert ask_user_choice("Which?", "x", []) is None


# ── the contract and the pinned brief carry the answer ───────────────────────


def _answered_strategy(result: PlannedKriaTurn) -> dict:
    return _strategy(result)


@pytest.mark.asyncio
async def test_contract_carries_the_chosen_length_instead_of_conflicting(monkeypatch) -> None:
    rows, brief = _rows(30), _brief(_timing(15))
    answered = _strategy(await _asked_then_answered(monkeypatch, "extend"))
    # Without the answer the contract would see 24 s (strategy) against 15 s (brief).
    pinned = answered_brief(brief, answered)
    assert pinned.requirements[0].facts["duration_s"] == 24
    assert brief.requirements[0].facts["duration_s"] == 15  # the stored brief is untouched
    contract = build_render_contract(
        answered, generation_id="g", brief=pinned, media_snapshot={"clip_assignments": rows}
    )
    assert contract.duration_s == 24 and not contract.unresolved
    # ... and even the raw brief is superseded by the recorded answer.
    raw = build_render_contract(
        answered, generation_id="g", brief=brief, media_snapshot={"clip_assignments": rows}
    )
    assert raw.duration_s == 24


@pytest.mark.asyncio
async def test_attachment_order_answer_resolves_the_contract_and_pins_the_basis(
    monkeypatch,
) -> None:
    rows = _rows(8, dated=False)
    brief = _brief(_order())
    unanswered = build_render_contract(
        _planned().plan.intents[0].arguments["strategy"],
        generation_id="g",
        brief=brief,
        media_snapshot={"clip_assignments": rows},
    )
    assert unanswered.unresolved and unanswered.order_ids == ()  # today's behaviour

    first = await _gate(monkeypatch, _planned(), rows=rows, brief=brief)
    asked = _asked(first)
    answered = _strategy(
        await _gate(
            monkeypatch,
            _planned(),
            rows=rows,
            brief=brief,
            events=(asked, _picked(asked[1]["choice_question"], "attachment_order")),
        )
    )
    pinned = answered_brief(brief, answered)
    contract = build_render_contract(
        answered, generation_id="g", brief=pinned, media_snapshot={"clip_assignments": rows}
    )
    assert not contract.unresolved
    assert contract.order_required and contract.order_basis == "attachment_order"
    assert contract.order_ids == tuple(r["media_id"] for r in rows)


@pytest.mark.asyncio
async def test_unordered_answer_drops_the_promise_visibly(monkeypatch) -> None:
    rows = _rows(8, dated=False)
    brief = _brief(_order())
    first = await _gate(monkeypatch, _planned(), rows=rows, brief=brief)
    asked = _asked(first)
    result = await _gate(
        monkeypatch,
        _planned(),
        rows=rows,
        brief=brief,
        events=(asked, _picked(asked[1]["choice_question"], "unordered")),
    )
    strategy = _strategy(result)
    assert "not promising a filming-time order" in result.plan.intents[0].arguments["summary"]
    contract = build_render_contract(
        strategy,
        generation_id="g",
        brief=answered_brief(brief, strategy),
        media_snapshot={"clip_assignments": rows},
    )
    assert not contract.order_required and not contract.unresolved


@pytest.mark.asyncio
async def test_a_partly_dated_clip_set_asks_and_the_answer_resolves_it(monkeypatch) -> None:
    rows = _rows(6)
    del rows[2]["capture"]
    brief = _brief(_order())
    first = await _gate(monkeypatch, _planned(), rows=rows, brief=brief)
    asked = _asked(first)
    assert "1 of your 6 clips have no filming time" in first.plan.response
    strategy = _strategy(
        await _gate(
            monkeypatch,
            _planned(),
            rows=rows,
            brief=brief,
            events=(asked, _picked(asked[1]["choice_question"], "attachment_order")),
        )
    )
    contract = build_render_contract(
        strategy,
        generation_id="g",
        brief=answered_brief(brief, strategy),
        media_snapshot={"clip_assignments": rows},
    )
    assert contract.order_ids == tuple(r["media_id"] for r in rows)


def _shot_text_brief() -> CreativeBrief:
    return _brief(
        BriefRequirement(
            id="r7",
            kind="text",
            scope="per_clip",
            literal="Km 1",
            description="the first kilometre",
        )
    )


@pytest.mark.asyncio
async def test_a_shot_text_on_two_shots_asks_which_and_the_answer_places_it(monkeypatch) -> None:
    rows = _rows(4)
    planned = _planned(shot_labels=["Km 1", "Km 2", "Km 1"], render_program="guided")
    brief = _shot_text_brief()
    first = await _gate(monkeypatch, planned, rows=rows, brief=brief)
    question = first.plan.choice_question
    assert question["kind"] == "text_placement" and question["conflict"] == "text_placement:r7"
    assert [o["key"] for o in question["options"]] == ["shot_1", "shot_3"]
    strategy = _strategy(
        await _gate(
            monkeypatch,
            planned,
            rows=rows,
            brief=brief,
            events=(_asked(first), _picked(question, "shot_3")),
        )
    )
    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot={"clip_assignments": rows}
    )
    assert not contract.unresolved
    placed = [t for t in contract.exact_texts if t.text == "Km 1" and t.role == "clip"]
    assert any(t.shot_index == 2 for t in placed)


@pytest.mark.asyncio
async def test_a_shot_text_with_no_matching_label_is_not_a_question(monkeypatch) -> None:
    planned = _planned(shot_labels=["Km 2"], render_program="guided")
    result = await _gate(monkeypatch, planned, rows=_rows(4), brief=_shot_text_brief())
    assert result.plan.mode == "act"  # a planner miss, left to the backstop refusal


# ── collector contract ───────────────────────────────────────────────────────


def test_collector_is_pure_ordered_and_typed() -> None:
    strategy = CreativeStrategy(
        edit_format="montage", target_duration_s=15, target_duration_requested=True
    )
    brief = _brief(_timing(15), _order())
    found = collect_conflicts(strategy, brief, {"clip_assignments": _rows(30, dated=False)})
    assert [c.kind for c in found] == ["order_basis", "duration_vs_count"]
    for conflict in found:
        assert conflict.field_path and conflict.input_digest and len(conflict.options) >= 2
        assert conflict.requirement_ids  # linked to the brief requirements it resolves
    assert found[0].requirement_ids == ("r2",) and found[1].requirement_ids == ("r1",)
    # Deterministic: same inputs, same digests.
    again = collect_conflicts(strategy, brief, {"clip_assignments": _rows(30, dated=False)})
    assert [c.input_digest for c in again] == [c.input_digest for c in found]


def test_open_conflicts_skips_what_the_strategy_already_answered() -> None:
    strategy = CreativeStrategy(
        edit_format="montage",
        target_duration_s=24,
        target_duration_requested=True,
        choice_answers=[
            {
                "conflict": "order_basis",
                "kind": "order_basis",
                "option": "unordered",
                "input_digest": "d",
            }
        ],
    )
    brief = _brief(_order())
    rows = {"clip_assignments": _rows(8, dated=False)}
    assert collect_conflicts(strategy, brief, rows)  # still detectable ...
    assert open_conflicts(strategy, brief, rows) == []  # ... but not open


# ── the binding freezes the answers; stored bindings are untouched ───────────


def test_binding_digest_includes_answers_only_when_present() -> None:
    brief = _brief(_timing(15))
    plain = BriefBinding.create(uuid.uuid4(), brief, latest_message="x")
    assert "choice_answers" not in plain.model_dump(mode="json")  # stored shape unchanged
    answers = [{"conflict": "duration_vs_count", "option": "extend"}]
    frozen = BriefBinding.create(plain.thread_id, brief, latest_message="x", choice_answers=answers)
    assert frozen.digest != plain.digest
    assert frozen.model_dump(mode="json")["choice_answers"] == answers
    BriefBinding.model_validate(frozen.model_dump(mode="json"))  # round-trips and re-verifies
    forged = frozen.model_dump(mode="json") | {"choice_answers": []}
    with pytest.raises(ValueError, match="digest mismatch"):
        BriefBinding.model_validate(forged)


def test_fewer_answer_dispatches_exactly_the_chosen_clips_and_nothing_else_changes() -> None:
    from app.tasks.content_plan_build import _creator_selected_clip_paths

    item = SimpleNamespace(
        clip_assignments=[{"media_id": f"c{i}", "gcs_path": f"p{i}"} for i in range(5)]
    )
    paths = [f"p{i}" for i in range(5)]
    base = {
        "edit_format": "montage",
        "render_program": "guided",
        "media_scope": "selected",
        "selected_media_ids": ["c0", "c4"],
    }
    fewer = {
        "conflict": "duration_vs_count",
        "kind": "duration_vs_count",
        "option": "fewer",
        "input_digest": "d",
    }
    assert _creator_selected_clip_paths(item, paths, base | {"choice_answers": [fewer]}) == [
        "p0",
        "p4",
    ]
    # No answer, or the other answer: a guided job keeps every attached clip as before.
    assert _creator_selected_clip_paths(item, paths, base) == paths
    extend = fewer | {"option": "extend"}
    assert _creator_selected_clip_paths(item, paths, base | {"choice_answers": [extend]}) == paths


def test_ask_user_options_reach_the_plan_as_a_question_and_flag_off_is_text_only(
    monkeypatch,
) -> None:
    from app.agents._schemas.creator_agent import AskUser

    action = AskUser(
        kind="ask_user",
        question="Which clip should open the edit?",
        reason_code="needs_opening",
        options=["The pier", "The bridge"],
    )
    plan = planner.adapt_creator_action(action)
    assert plan.mode == "respond" and plan.turn_value == "question"
    assert [o["label"] for o in plan.choice_question["options"]] == ["The pier", "The bridge"]
    assert plan.choice_question["conflict"] == "ask_user:needs_opening"
    assert "1. The pier" in plan.response  # a client without the card still shows every choice

    no_options = AskUser(kind="ask_user", question="Which one?", reason_code="x")
    assert planner.adapt_creator_action(no_options).choice_question is None

    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", False)
    off = planner.adapt_creator_action(action)
    assert off.choice_question is None and off.response == "Which clip should open the edit?"

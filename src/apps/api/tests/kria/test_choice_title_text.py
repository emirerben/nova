"""KRI-470: a requested title with no words is a question BEFORE approval, never a render block.

Failure modes this file is written against (before the code):

* a hook title is requested with no words, the unified phone montage skips the draft-time
  text receipt, the creator approves, and the render then blocks (the incident);
* the question is asked when a title source exists (typed literal, `opening_title`, a
  global literal, brief facts) or when the draft is not rendered by the unified montage;
* "continue without a title" does not actually clear the render-time receipt;
* typed words are mistaken for an option, or do not become the title;
* a question loop, or the exhausted question silently letting an unrenderable plan through;
* the question and the renderer disagreeing about whether a title source exists.

The gate is the real `planner._gate_unresolved_choices`; the render side is the real
`plan_unified_montage` plus the real receipt builder, so "the receipt is green" is checked
on what the renderer would produce, not on a stubbed value.
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
from app.kria.brief_checks import build_receipts, plan_facts_from_unified_montage
from app.kria.planner import PlannedKriaTurn
from app.pipeline.unified_montage import brief_view, plan_unified_montage, title_source_exists
from app.services.choice_questions import (
    CONFLICT_TITLE_TEXT,
    MAX_ASKS_PER_QUESTION,
    answered_brief,
    collect_conflicts,
    match_open_choice,
)
from app.tasks.kria_runtime import _unresolved_choice_plan
from tests.pipeline.test_unified_montage import clip

CREATOR = uuid.uuid4()
WORDS = "Weekend away"


def _title_req(*, rid: str = "r2", literal: str | None = None, **extra) -> BriefRequirement:  # noqa: ANN003
    return BriefRequirement(
        id=rid,
        kind="text",
        scope="title",
        description="a hook animated with typewriter",
        literal=literal,
        **extra,
    )


def _brief(*requirements: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(requirements))


def _incident_brief(*extra: BriefRequirement) -> CreativeBrief:
    return _brief(
        BriefRequirement(
            id="r1",
            kind="order",
            scope="global",
            description="in the order I filmed them",
            facts={"key": "capture_time"},
        ),
        _title_req(),
        BriefRequirement(id="r3", kind="text", scope="per_clip", description="the location"),
        *extra,
    )


def _rows(count: int = 4) -> list[dict]:
    return [
        {
            "media_id": f"c{i}",
            "kind": "video",
            "duration_s": 4.0,
            "capture": {"capture_time": f"2026-01-01T09:0{i}:00Z"},
        }
        for i in range(count)
    ]


def _snapshot(*, proxies: bool = True, count: int = 4) -> dict:
    return {
        "clip_assignments": _rows(count),
        "clip_paths": [
            f"users/u/{'analysis-proxy-' if proxies else ''}c{i}.mp4" for i in range(count)
        ],
    }


def _planned(**strategy: object) -> PlannedKriaTurn:
    base: dict = {
        "direction": "guided_story",
        "edit_format": "montage",
        "audio_strategy": "licensed_music",
        "pacing": "fast",
        "render_program": "guided",
        "rationale": "A montage.",
    }
    model = CreativeStrategy(**{**base, **strategy})
    plan = planner.adapt_creator_action(
        ProposeStrategy(kind="propose_strategy", strategy=model, summary="A montage."),
    )
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)


async def _gate(
    monkeypatch: pytest.MonkeyPatch,
    planned: PlannedKriaTurn,
    *,
    brief: CreativeBrief | None,
    events: tuple = (),
    snapshot: dict | None = None,
    phone: bool = True,
) -> PlannedKriaTurn:
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=brief))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _s, _i: True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(type(planner.settings), "phone_rendering_for", lambda _s, _i: phone)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=list(events)))
    planned = replace(planned, media_snapshot=snapshot or _snapshot())
    return await planner._gate_unresolved_choices(
        SimpleNamespace(), planned, thread_id=uuid.uuid4(), creator_id=CREATOR
    )


def _asked(result: PlannedKriaTurn) -> tuple[str, dict]:
    assert result.plan.turn_value == "question" and result.plan.choice_question, result.plan
    return "assistant", {"choice_question": result.plan.choice_question}


def _picked(question: dict, option: str) -> tuple[str, dict]:
    return "user", {
        "choice_selection": {"question_id": question["question_id"], "option_key": option}
    }


def _strategy(result: PlannedKriaTurn) -> dict:
    assert result.plan.mode == "act", result.plan.response
    return result.plan.intents[0].arguments["strategy"]


def _render(brief: CreativeBrief, strategy: dict) -> tuple[dict, list[dict]]:
    """What the unified phone montage records for this brief, and its receipts."""
    plan = plan_unified_montage(
        [clip(i, minutes=i, place=f"Place {i}, Town") for i in range(4)],
        brief_view(brief),
        strategy=strategy,
        clip_intents_enabled=False,
    )
    record = plan.record()
    receipts = build_receipts(
        brief.live(), plan_facts_from_unified_montage(record), include_unchecked=True
    )
    return record, [r.model_dump(mode="json") for r in receipts]


def _blocks(receipts: list[dict]) -> bool:
    """The render-time block condition of `_run_phone_unified_montage_job`."""
    return any(r["verification"] == "checked" and r["status"] != "met" for r in receipts)


# -- the incident: asks BEFORE approval -------------------------------------------------


@pytest.mark.asyncio
async def test_incident_shape_blocks_at_render_time_without_the_question() -> None:
    """The control that makes the rest meaningful: this brief, rendered as-is, blocks."""
    _, receipts = _render(_incident_brief(), {})
    assert _blocks(receipts)
    title = next(r for r in receipts if r["requirement_id"] == "r2")
    assert title["status"] == "partial" and title["verification"] == "checked"


@pytest.mark.asyncio
async def test_a_title_with_no_words_asks_before_approval(monkeypatch) -> None:
    result = await _gate(monkeypatch, _planned(), brief=_incident_brief())
    assert result.plan.mode == "respond" and not result.plan.intents  # nothing approvable
    question = result.plan.choice_question
    assert question["kind"] == CONFLICT_TITLE_TEXT and question["input_digest"]
    assert [o["key"] for o in question["options"]] == ["no_title"]
    assert question["allow_free_text"] is True
    text = result.plan.response
    assert "don't write on-screen text for you" in text
    assert "Continue without a title" in text and "Type the words you want" in text
    # Never offers to invent the words.
    assert "write one" not in text.lower() and "for me" not in str(question["options"]).lower()


@pytest.mark.asyncio
async def test_the_same_title_with_a_local_capture_order_question_asks_that_first(
    monkeypatch,
) -> None:
    undated = _snapshot()
    undated["clip_assignments"] = [{"media_id": f"c{i}", "kind": "video"} for i in range(4)]
    result = await _gate(monkeypatch, _planned(), brief=_incident_brief(), snapshot=undated)
    assert result.plan.choice_question["kind"] == "order_basis"  # one question per turn


# -- must NOT ask ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "brief", "strategy", "kwargs"),
    [
        ("literal on the title", _brief(_title_req(literal=WORDS)), {}, {}),
        (
            "typed title next to the old one",
            _incident_brief(_title_req(rid="r4", literal=WORDS)),
            {},
            {},
        ),
        ("opening_title", _incident_brief(), {"opening_title": WORDS}, {}),
        (
            "global literal",
            _incident_brief(
                BriefRequirement(
                    id="r4", kind="text", scope="global", literal=WORDS, description="x"
                )
            ),
            {},
            {},
        ),
        (
            "facts to title from",
            _incident_brief(
                BriefRequirement(
                    id="r4",
                    kind="style",
                    scope="global",
                    description="a 5k run",
                    facts={"distance_km": 5, "activity": "run"},
                )
            ),
            {},
            {},
        ),
        ("no title requirement", _brief(), {}, {}),
        ("cloud draft (no phone proxies)", _incident_brief(), {}, {"proxies": False}),
        ("account not on phone rendering", _incident_brief(), {}, {"phone": False}),
        ("voiceover lane", _incident_brief(), {"audio_strategy": "voiceover"}, {}),
        ("subtitled format", _incident_brief(), {"edit_format": "subtitled"}, {}),
    ],
)
async def test_does_not_ask_when_a_title_source_exists_or_the_draft_judges_itself(
    monkeypatch, label, brief, strategy, kwargs
) -> None:
    snapshot = _snapshot(proxies=kwargs.get("proxies", True))
    result = await _gate(
        monkeypatch,
        _planned(**strategy),
        brief=brief,
        snapshot=snapshot,
        phone=kwargs.get("phone", True),
    )
    assert result.plan.turn_value != "question", label


def test_no_creator_means_never_asked() -> None:
    # Byte-identical to today for every caller that does not say whose draft this is.
    assert collect_conflicts({}, _incident_brief(), _snapshot()) == []


def test_the_question_and_the_renderer_read_the_same_title_source() -> None:
    """Whenever the gate sees no source, the real render has no title; with one, it does."""
    cases = [
        (_incident_brief(), {}),
        (_incident_brief(), {"opening_title": WORDS}),
        (_incident_brief(_title_req(rid="r4", literal=WORDS)), {}),
        (
            _incident_brief(
                BriefRequirement(
                    id="r4",
                    kind="style",
                    scope="global",
                    description="x",
                    facts={"activity": "run"},
                )
            ),
            {},
        ),
    ]
    for brief, strategy in cases:
        record, _ = _render(brief, strategy)
        assert title_source_exists(strategy, brief) is bool(record.get("title")), (brief, strategy)


# -- the answers ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continue_without_a_title_clears_the_render_time_receipt(monkeypatch) -> None:
    brief = _incident_brief()
    first = await _gate(monkeypatch, _planned(), brief=brief)
    asked = _asked(first)
    events = (asked, _picked(asked[1]["choice_question"], "no_title"))
    done = await _gate(monkeypatch, _planned(), brief=brief, events=events)
    strategy = _strategy(done)
    (answer,) = strategy["choice_answers"]
    assert answer["kind"] == "title_text" and answer["option"] == "no_title"
    assert answer["requirement_ids"] == ["r2"] and answer["source"] == "creator"
    assert "leaving the title off" in done.plan.intents[0].arguments["summary"]  # never silent
    pinned = answered_brief(brief, strategy)
    assert [r.id for r in pinned.live()] == ["r1", "r3"]  # r2 superseded in the pinned copy
    assert [r.id for r in brief.live()] == ["r1", "r2", "r3"]  # the thread's brief is unchanged
    _, receipts = _render(pinned, strategy)
    assert not _blocks(receipts)


@pytest.mark.asyncio
async def test_a_typed_reply_naming_the_option_is_the_answer_and_delegation_is_disclosed(
    monkeypatch,
) -> None:
    first = await _gate(monkeypatch, _planned(), brief=_incident_brief())
    question = first.plan.choice_question
    for reply in ("Continue without a title", "no title", "Option 1", "without a title!"):
        assert match_open_choice(question, reply) == "no_title", reply
    # Typed WORDS are never an option, however they are phrased.
    for reply in (f"The hook should say '{WORDS}'", WORDS, "Make it a title about my trip"):
        assert match_open_choice(question, reply) is None, reply


@pytest.mark.asyncio
async def test_typed_words_become_the_title_and_the_receipt_is_met(monkeypatch) -> None:
    brief = _incident_brief()
    first = await _gate(monkeypatch, _planned(), brief=brief)
    asked = _asked(first)
    # The reply is not an option, so no answer is recorded; the brief extractor turns the
    # typed words into a literal on the title requirement.
    typed = _brief(*[r if r.id != "r2" else _title_req(literal=WORDS) for r in brief.requirements])
    second = await _gate(monkeypatch, _planned(), brief=typed, events=(asked,))
    strategy = _strategy(second)  # no question: the plan goes through
    assert "choice_answers" not in strategy
    record, receipts = _render(typed, strategy)
    assert record["title"] == WORDS and record["title_source"] == "creator"
    assert not _blocks(receipts)
    title = next(r for r in receipts if r["requirement_id"] == "r2")
    assert title["status"] == "met"


@pytest.mark.asyncio
async def test_asked_at_most_twice_then_the_backstop_refuses_instead_of_inventing(
    monkeypatch,
) -> None:
    brief = _incident_brief()
    events: tuple = ()
    for _ in range(MAX_ASKS_PER_QUESTION):
        asked = _asked(await _gate(monkeypatch, _planned(), brief=brief, events=events))
        events += (asked, ("user", {"_content": "hmm"}))
    # The gate no longer asks (the plan passes through unchanged), nothing is invented...
    through = await _gate(monkeypatch, _planned(), brief=brief, events=events)
    assert through.plan.mode == "act" and "choice_answers" not in _strategy(through)
    # ...and the pre-approval backstop says so in words and keeps the question answerable.
    backstop = _unresolved_choice_plan(
        _strategy(through),
        brief,
        _snapshot(),
        contract_brief=brief,
        events=events,
        creator_id=CREATOR,
    )
    assert backstop is not None
    plan, reason = backstop
    assert reason == "unresolved_choice" and plan.turn_value == "recovery"
    assert "Continue without a title" in plan.response and "type the words" in plan.response
    assert "I haven't made an edit yet" in plan.response


def test_typed_words_on_a_new_requirement_also_satisfy_the_old_wordless_one() -> None:
    # The extractor may add a literal title requirement without superseding the old one.
    brief = _incident_brief(_title_req(rid="r4", literal=WORDS))
    record, receipts = _render(brief, {})
    assert record["title"] == WORDS
    assert not _blocks(receipts)
    assert {r["requirement_id"]: r["status"] for r in receipts}["r2"] == "met"


# -- review fixes: matcher breadth, delegation, copy, speech lane ------------------------------


@pytest.mark.asyncio
async def test_the_option_matcher_is_broad_but_exact(monkeypatch) -> None:
    first = await _gate(monkeypatch, _planned(), brief=_incident_brief())
    question = first.plan.choice_question
    for reply in (
        "no",
        "None",
        "nope",
        "skip",
        "Skip it",
        "skip title",
        "No title please",
        "don't add a title",
        "without title",
        "leave it off",
        "başlık olmasın",
        "Başlıksız",
    ):
        assert match_open_choice(question, reply) == "no_title", reply
    # Words, even words that START with an alias, are never an answer.
    for reply in (
        WORDS,
        "No Plans",
        "No title needed here, call it Weekend away",
        "none of us slept",
        "skip the line",
        "make it fun",
        "Başlık: Hafta sonu",
    ):
        assert match_open_choice(question, reply) is None, reply


@pytest.mark.asyncio
async def test_an_option_is_not_recommended_and_delegation_never_drops_the_title(
    monkeypatch,
) -> None:
    from app.services.choice_questions import delegated_choice

    brief = _incident_brief()
    first = await _gate(monkeypatch, _planned(), brief=brief)
    question = first.plan.choice_question
    assert all(o["recommended"] is False for o in question["options"])
    for phrase in ("you decide", "Surprise me", "up to you", "you choose"):
        assert delegated_choice(question, phrase) is None, phrase
    # A delegation is not an answer: the gate asks once more (within the cap) and explains.
    asked = _asked(first)
    again = await _gate(
        monkeypatch, _planned(), brief=brief, events=(asked, ("user", {"_content": "you decide"}))
    )
    assert again.plan.choice_question["kind"] == CONFLICT_TITLE_TEXT
    assert "don't write on-screen text for you" in again.plan.response


@pytest.mark.asyncio
async def test_the_single_option_reads_as_an_option_not_a_limit(monkeypatch) -> None:
    result = await _gate(monkeypatch, _planned(), brief=_incident_brief())
    text = result.plan.response
    assert 'or reply "Continue without a title"' in text
    assert "Type the words you want and I'll use them exactly" in text
    assert "The most I can do" not in text and "Unfortunately" not in text


@pytest.mark.asyncio
async def test_a_speech_montage_never_asks_about_a_title(monkeypatch) -> None:
    # `contract.audio_source_ids` takes `run_phone_speech_montage_job`, which has no title.
    audio = {"preserve_source_audio": True, "source_media_ids": ["c0"]}
    result = await _gate(monkeypatch, _planned(montage_audio=audio), brief=_incident_brief())
    assert result.plan.turn_value != "question"
    # A montage_audio that keeps no source audio is not the speech lane.
    quiet = {"preserve_source_audio": False, "source_media_ids": []}
    asked = await _gate(monkeypatch, _planned(montage_audio=quiet), brief=_incident_brief())
    assert asked.plan.turn_value == "question"

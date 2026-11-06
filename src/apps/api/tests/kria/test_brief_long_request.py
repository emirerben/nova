"""Item 23: a long creation prompt (17 separate asks) is planned in one go, behind a flag.

Ways this can fail (each one is pinned below or named as not pinned):

1. The flag is on but the Main Creator or brief extractor (follow-up turns) prompt still says
   "at most 16 objects", so the model keeps merging asks to stay under a bound the server no
   longer enforces.
2. The flag is off and a single byte changes: the prompt, the retry feedback or the recovery
   turn. Flag off must stay exactly today's behavior.
3. The higher count removes the runaway guard: a batch over 32 entries, or a 17..32 batch whose
   size blows the 9,000-character budget, must still be refused, and the refusal must tell
   the model how to recover (shorten, keep every ask) instead of letting it drop asks.
4. The size budget refuses a batch of 16 or fewer that is accepted today.
5. Partial acceptance: the batch is cut at the limit and its tail is dropped. It must be
   all-or-nothing.
6. Distinct asks are collapsed or reordered after parsing (planner dedupe, refiling, binding).
7. Downstream budgets: the rendered request must stay inside CREATOR_REQUEST_MAX_CHARS (12k)
   and the brief context (3 x 9k) must still hold the whole batch.
8. Main Creator output tokens: 32 entries of ~300 characters is far below the output cap; not
   pinned here (no model is called).
9. The flag flips between the prompt and the parse of one turn. The turn test pins that the
   prompt's cap and the accepted batch agree under one flag value; a real mid-turn flip is not
   simulated.

What each test pins:

a. Flag off, her prompt: the real planner, with the model answering her 17 asks, recovers with
   "creator_planning_failed" after 3 Main Creator calls, the retry prompt carried "brief_updates
   exceeds 16 entries", nothing is kept, and the whole result equals flag_off_golden.json
   (recorded on the code before the flag existed).
b. Flag on, her prompt: one Main Creator call, an act plan, the prompt says 32, all 17 entries
   kept in fixture order with the same kind/scope/literal/description, request within 12k.
c. Parse guard with the flag on: 33 entries refused whole, 17 over 9,000 characters refused
   whole, 16 over 9,000 still accepted, exactly 9,000 accepted, 32 small entries accepted in
   order, and no error text carries entry content.
d. Parse with the flag off: 17 refused with exactly "brief_updates exceeds 16 entries", 16 ok.
e. Prompt: flag off is byte-identical (section and full prompt); flag on differs only by one
   "at most 16 objects" -> "at most 32 objects", in the Main Creator prompt and in the brief
   extractor prompt (also with insert clips open); both prompt versions are bumped.
f. Flag on, follow-up path: BriefExtractorAgent.parse accepts a 17-entry batch (shared bound),
   and its prompt now says 32, so a long follow-up request is not merged to 16.
g. Size refusal recovery: the first answer (17 entries, over 9,000 characters) is refused whole
   with a message that says how to recover; the retry prompt carries it, the second (normal)
   answer keeps all 17 asks in order in exactly 2 model calls, with no recovery turn.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents.brief_extractor import (
    _NARROW_BRIEF_SECTION,
    BRIEF_EXTRACTOR_PROMPT_VERSION,
    BriefExtractorAgent,
)
from app.agents.main_creator import (
    _BRIEF_PROMPT_SECTION,
    MAIN_CREATOR_PROMPT_VERSION,
    MainCreatorAgent,
    brief_prompt_section,
    with_insert_clip_brief,
    with_long_request_cap,
)
from app.config import settings
from app.kria import brief as brief_mod
from app.kria.brief import BriefUpdate, BriefUpdateBatchError, parse_brief_updates

FLAG = "brief_long_request_enabled"
CAP_16 = "at most 16 objects"
CAP_32 = "at most 32 objects"
SCORECARD = Path(__file__).resolve().parents[5] / "scripts" / "kria" / "score_q2_long_prompt.py"
_spec = importlib.util.spec_from_file_location("score_q2_long_prompt", SCORECARD)
assert _spec is not None and _spec.loader is not None
score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score)

SECRET = "SECRET-ENTRY-CONTENT"


@pytest.fixture(scope="module")
def fx() -> dict:
    return score.load_fixture()


@pytest.fixture(scope="module")
def flag_off_turn() -> dict:
    return score.run_turn(long_request=False)


@pytest.fixture(scope="module")
def flag_on_turn() -> dict:
    return score.run_turn(long_request=True)


def _set_flag(monkeypatch: pytest.MonkeyPatch, value: bool) -> None:
    # raising=True: on code without the setting this fails loudly instead of testing nothing.
    monkeypatch.setattr(settings, FLAG, value)


@pytest.fixture
def flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    if hasattr(settings, FLAG):
        _set_flag(monkeypatch, False)


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_flag(monkeypatch, True)


def _entries(count: int, *, pad: int = 0) -> list[dict]:
    return [
        {
            "operation": "add",
            "kind": "text",
            "scope": "global",
            "literal": f"{SECRET} {index}",
            "description": f"{SECRET} ask number {index}",
            "facts": {"pad": "x" * pad} if pad else {},
        }
        for index in range(count)
    ]


# ------------------------------------------------------------------ a. flag off, her prompt


def test_flag_off_her_prompt_recovers_exactly_as_recorded(flag_off_turn: dict) -> None:
    turn = flag_off_turn
    assert turn["outcome"]["recovery_reason"] == "creator_planning_failed"
    assert turn["model_calls"] == 3
    assert turn["last_prompt_has_exceeds_16"] is True
    assert turn["first_prompt_cap"] == 16
    assert turn["brief_updates"] == []
    golden = json.loads(score.GOLDEN.read_text(encoding="utf-8"))
    assert turn == golden


# ------------------------------------------------------------------ b. flag on, her prompt


def test_flag_on_her_prompt_plans_all_17_asks_in_one_call(flag_on_turn: dict, fx: dict) -> None:
    turn = flag_on_turn
    assert turn["model_calls"] == 1
    assert turn["outcome"]["recovery_reason"] is None
    assert turn["outcome"]["mode"] == "act"
    assert turn["first_prompt_cap"] == 32
    want = json.loads(fx["main_creator_raw_text"])["brief_updates"]
    assert len(want) == len(fx["expected_asks"]) == 17
    shape = ("kind", "scope", "literal", "description")
    assert [tuple(u[k] for k in shape) for u in turn["brief_updates"]] == [
        (w["kind"], w["scope"], w["literal"], w["description"]) for w in want
    ]
    assert score.asks_kept(turn, fx) == (17, [])
    assert turn["effective_request_chars"] <= CREATOR_REQUEST_MAX_CHARS


# ------------------------------------------------------------------ c. parse guard, flag on


def test_flag_on_refuses_33_entries_whole(flag_on: None) -> None:
    with pytest.raises(BriefUpdateBatchError) as err:
        parse_brief_updates(_entries(33))
    assert str(err.value) == "brief_updates exceeds 32 entries"
    assert SECRET not in str(err.value)


def test_flag_on_refuses_17_entries_over_the_character_budget_whole(flag_on: None) -> None:
    with pytest.raises(BriefUpdateBatchError) as err:
        parse_brief_updates(_entries(17, pad=1_000))
    assert str(err.value) == (
        "brief_updates over 16 entries exceed 9000 characters: "
        "shorten descriptions and facts, keep every ask"
    )
    assert SECRET not in str(err.value)


def test_flag_on_never_refuses_what_flag_off_accepts(flag_on: None) -> None:
    big = _entries(16, pad=1_000)
    assert brief_mod.brief_batch_chars([BriefUpdate.model_validate(e) for e in big]) > 9_000
    assert len(parse_brief_updates(big)) == 16


def test_flag_on_budget_boundary_is_exactly_9000_characters(flag_on: None) -> None:
    count = 20
    entries = _entries(count, pad=1)

    def chars(items: list[dict]) -> int:
        return brief_mod.brief_batch_chars([BriefUpdate.model_validate(e) for e in items])

    extra = 9_000 - chars(entries)
    # The padding is spread over every entry's facts, each well under the 2,000-byte facts cap.
    assert 0 < extra and extra // count + 2 < 1_900
    share, rest = divmod(extra, count)
    for index, entry in enumerate(entries):
        entry["facts"] = {"pad": "x" * (1 + share + (1 if index < rest else 0))}
    assert chars(entries) == 9_000
    assert len(parse_brief_updates(entries)) == count
    entries[-1]["facts"]["pad"] += "x"
    assert chars(entries) == 9_001
    with pytest.raises(BriefUpdateBatchError, match="exceed 9000 characters"):
        parse_brief_updates(entries)


def test_flag_on_accepts_32_small_entries_in_order(flag_on: None) -> None:
    raw = _entries(32)
    parsed = parse_brief_updates(raw)
    assert [u.literal for u in parsed] == [e["literal"] for e in raw]
    assert brief_mod.brief_batch_chars(parsed) <= 9_000


def test_flag_on_limits_are_the_documented_constants(flag_on: None) -> None:
    assert brief_mod.MAX_UPDATES_PER_TURN == 16
    assert brief_mod.LONG_REQUEST_MAX_UPDATES == 32
    assert brief_mod.LONG_REQUEST_MAX_CHARS == brief_mod.MAX_BRIEF_REQUEST_CHARS == 9_000
    assert brief_mod.max_updates_per_turn() == 32


# ------------------------------------------------------------------ d. parse, flag off


def test_flag_off_refuses_17_entries_with_todays_message(flag_off: None) -> None:
    with pytest.raises(BriefUpdateBatchError) as err:
        parse_brief_updates(_entries(17))
    assert str(err.value) == "brief_updates exceeds 16 entries"
    assert SECRET not in str(err.value)


def test_flag_off_accepts_16_entries_of_any_size(flag_off: None) -> None:
    assert len(parse_brief_updates(_entries(16))) == 16
    assert len(parse_brief_updates(_entries(16, pad=1_000))) == 16
    assert brief_mod.MAX_UPDATES_PER_TURN == 16


# ------------------------------------------------------------------ e. prompt


def test_flag_off_prompt_sections_are_byte_identical(flag_off: None) -> None:
    assert brief_prompt_section(False) == _BRIEF_PROMPT_SECTION
    assert brief_prompt_section(True) == with_insert_clip_brief(_BRIEF_PROMPT_SECTION)
    assert _BRIEF_PROMPT_SECTION.count(CAP_16) == 1


def test_flag_on_changes_only_the_cap_in_the_main_creator_prompt(
    monkeypatch: pytest.MonkeyPatch, fx: dict
) -> None:
    agent = MainCreatorAgent(object())
    prompt_input = score.agent_input(fx)
    if hasattr(settings, FLAG):
        _set_flag(monkeypatch, False)
    off = agent.render_prompt(prompt_input)
    _set_flag(monkeypatch, True)
    on = agent.render_prompt(prompt_input)
    assert off.count(CAP_16) == 1 and CAP_32 not in off
    assert on == off.replace(CAP_16, CAP_32)
    assert on.count(CAP_32) == 1 and CAP_16 not in on
    assert brief_prompt_section(False) == _BRIEF_PROMPT_SECTION.replace(CAP_16, CAP_32)
    assert brief_prompt_section(True) == with_insert_clip_brief(_BRIEF_PROMPT_SECTION).replace(
        CAP_16, CAP_32
    )


@pytest.mark.parametrize("insert_open", [False, True])
def test_brief_extractor_prompt_flag_off_is_byte_identical_and_on_changes_only_the_cap(
    monkeypatch: pytest.MonkeyPatch, fx: dict, insert_open: bool
) -> None:
    agent = BriefExtractorAgent(object())
    extraction = BriefExtractionInput(
        creator_request="", user_message=fx["user_message"], insert_clips_open=insert_open
    )
    if hasattr(settings, FLAG):
        _set_flag(monkeypatch, False)
    off = agent.render_prompt(extraction)
    section = (
        with_insert_clip_brief(_NARROW_BRIEF_SECTION) if insert_open else _NARROW_BRIEF_SECTION
    )
    assert section in off
    assert off.count(CAP_16) == 1 and CAP_32 not in off
    _set_flag(monkeypatch, True)
    on = agent.render_prompt(extraction)
    assert on == off.replace(CAP_16, CAP_32)
    assert on.count(CAP_32) == 1 and CAP_16 not in on
    assert with_long_request_cap(section) in on


def test_brief_extractor_prompt_version_is_bumped() -> None:
    assert BRIEF_EXTRACTOR_PROMPT_VERSION == "2026-11-06-v68"  # v66 + item 16 (v68)


def test_main_creator_prompt_version_is_bumped() -> None:
    assert MAIN_CREATOR_PROMPT_VERSION == "2026-11-06-v136"  # v131 + 16 (v133) + 20 (v135) + 18 (v136)


# ------------------------------------------------------------------ f. follow-up path


def test_flag_on_brief_extractor_accepts_17_entries(flag_on: None, fx: dict) -> None:
    want = json.loads(fx["main_creator_raw_text"])["brief_updates"]
    assert len(want) == 17
    raw = json.dumps({"brief_updates": want}, ensure_ascii=False)
    out = BriefExtractorAgent(object()).parse(
        raw, BriefExtractionInput(creator_request="", user_message=fx["user_message"])
    )
    assert [(u.kind, u.scope, u.literal, u.description) for u in out.brief_updates] == [
        (w["kind"], w["scope"], w["literal"], w["description"]) for w in want
    ]


# ------------------------------------------------------------------ g. size refusal recovery


def test_flag_on_oversized_first_answer_is_retried_and_keeps_all_17_asks(
    monkeypatch: pytest.MonkeyPatch, fx: dict
) -> None:
    normal = json.loads(fx["main_creator_raw_text"])
    padded = json.loads(fx["main_creator_raw_text"])
    for entry in padded["brief_updates"]:
        entry["description"] = (entry["description"] + " " + "w" * 300)[:300]
        entry["facts"] = {**entry.get("facts", {}), "pad": "x" * 400}
    assert (
        brief_mod.brief_batch_chars(
            [BriefUpdate.model_validate(e) for e in padded["brief_updates"]]
        )
        > 9_000
    )
    turn = score.run_turn(
        long_request=True,
        raw_texts=[
            json.dumps(padded, ensure_ascii=False),
            json.dumps(normal, ensure_ascii=False),
        ],
    )
    assert turn["model_calls"] == 2
    assert "shorten descriptions and facts, keep every ask" in turn["prompts"][1]
    assert "shorten descriptions and facts" not in turn["prompts"][0]
    assert turn["outcome"]["recovery_reason"] is None
    assert turn["outcome"]["mode"] == "act"
    assert score.asks_kept(turn, fx) == (17, [])
    assert turn["brief_update_count"] == 17

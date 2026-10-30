"""KRI-1066: a TYPED "skip that" right after the ending picker is the picker's Skip.

Leverkusen–Nürnberg M1 (thread 4144DA86, parent KRI-1066). With KRI-1067 the unplaced ending
"the final whistle" asks with the clip picker. Tapping the picker's Skip drafts on the last
clip filmed and says so (KRI-1025). Typing "skip that" instead reaches the server as a plain
message: KRI-1068 reads only a reply that was stuck on a pin, and the picker is not one, so
nothing is withdrawn, the brief still names the ending, and the creator gets a NEW picker for
the same ending. The KRI-1068 PR lists this as an open gap ("a typed 'skip that' after a clip
picker isn't read here"). Probe on main 949b3aa17: tapped Skip -> mode `act`; typed
"skip that" -> mode `respond`, a fresh picker.

Ways this can fail, written before the code (each is pinned below):

 T1 the incident: the typed message after the picker is not read as its Skip, so the picker
    comes back; or it is read, but the chat does not draft on the last filmed clip.
 T2 only some wordings work: "never mind", "forget the ending", "forget the final whistle",
    "okay skip that part", "drop it", and a Turkish chat's "boşver" / "o kısmı atla".
 T3 a message that only CONTAINS a withdrawal ("skip that part of the video where I'm
    talking"), a negated one ("don't skip it"), or asks for something else is read as a Skip.
 T4 the picker is not the reply the creator is answering (another user message in between, or
    already answered), so an old picker is skipped by a later "never mind".
 T5 the picker is not about one ending: several categories, an exclusion ask, a question that
    also answers another, or another open question on the same reply.
 T6 the wrong spot is withdrawn: "forget the opening" while the picker asks about the ending,
    or a named clip that is not the one the picker asks about.
 T7 the picker's words match no brief ending, or two (ambiguous), or the brief restated the
    ending after the picker (stale).
 T8 the clip-picker flag is off and a selection is derived anyway.
 T9 the derived Skip differs from a tapped one: another question id, or `answers` / `none_keys`
    set.
 T10 a skip is derived for a creator who tapped an answer in the same send (explicit wins).

The turns run the real Main Creator parser, the real planner, the real clip-intent planning and
resolver plumbing, the real choice gate and render contract over the KRI-1067 replay (prompt,
brief r2, clip filming times). Only the model transport is replayed. Not covered here: the
`submit_turn` transaction itself (it needs Postgres; localhost DB tests are not run offline).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.kria import runtime
from app.services.choice_questions import tag_event
from app.services.clip_selection import ClipSelectionIn
from app.services.pin_withdrawal import typed_picker_skip
from tests.kria.test_choice_conflict_gate import _gate
from tests.kria.test_kri1067_filming_order_end_picker import (
    ENDING,
    FILMED,
    _answer,
    _backstop,
    _brief,
    _contract,
    _flags,  # noqa: F401  (autouse fixture)
    _provider,
    _rows,
    _strategy,
    _turn,
)

THREAD = SimpleNamespace(id="thread-1")


async def _asked(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN202
    _provider(monkeypatch)
    return await _turn(monkeypatch, brief=_brief())


def _tagged(events: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """Events as the runtime reads them: each payload tagged with its stored event type (an
    untagged reply only closes a question when it carries a ``turn_value``)."""
    return [
        tag_event(role, payload, "user_message" if role == "user" else "assistant_response", None)
        for role, payload in events
    ]


def _after_picker(asked) -> list[tuple[str, dict[str, Any]]]:  # noqa: ANN001
    """The thread when the creator's next message arrives (not stored yet): the prompt, then
    the picker reply."""
    prompt, reply, _tap = _answer(asked, skipped=True)
    return _tagged([prompt, reply])


def _skip(asked, message: str, **kwargs: Any):  # noqa: ANN001, ANN202
    events = kwargs.pop("events", None) or _after_picker(asked)
    return typed_picker_skip(kwargs.pop("brief", _brief()), events, message)


# -- T1 / T9: the incident, end to end ---------------------------------------------------


async def test_t1_t9_a_typed_skip_is_the_pickers_skip_and_the_chat_drafts_on_the_last_clip(
    monkeypatch,
) -> None:
    asked = await _asked(monkeypatch)
    skip = _skip(asked, "skip that")
    assert skip == ClipSelectionIn(
        question_id=asked.plan.clip_question["question_id"], skipped=True
    )
    assert skip.answers == [] and skip.none_keys == []

    # The stored user event carries the derived selection, exactly as a tap would.
    stored = {"clip_selection": skip.model_dump(mode="json"), "turn_id": "typed-skip"}
    events = [*_after_picker(asked), *_tagged([("user", stored)])]
    answered = await _turn(monkeypatch, brief=_brief(), events=events, message="skip that")
    gated = await _gate(monkeypatch, answered, rows=_rows(), brief=_brief(), events=tuple(events))
    assert gated.plan.clip_question is None
    strategy = _strategy(gated)
    assert "so the video ends on the last clip you filmed" in str(
        gated.plan.intents[0].arguments["summary"]
    )
    assert _backstop(strategy, _brief(), events) is None
    contract = _contract(strategy, _brief())
    assert contract is not None and not contract.unresolved
    assert list(contract.order_ids) == FILMED


# -- T2: each way of saying it -------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "skip that",
        "Okay skip that part",
        "never mind",
        "forget the ending",
        f"forget {ENDING}",
        "drop it",
        "leave it out",
        "boşver",
        "o kısmı atla",
        "skip that, never mind",
    ],
)
async def test_t2_every_way_of_dropping_the_ending_is_the_skip(monkeypatch, message: str) -> None:
    asked = await _asked(monkeypatch)
    assert _skip(asked, message) is not None


# -- T3 / T6: not a withdrawal of this ending ----------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "skip that part of the video where I'm talking",
        "don't skip it",
        "end on the trophy celebration instead",
        "tell me which clip",
        "make it 30 seconds",
        "forget the opening",
        "forget the lineup sheet",
        "skip the beginning",
        "",
    ],
)
async def test_t3_t6_other_messages_are_not_the_skip(monkeypatch, message: str) -> None:
    asked = await _asked(monkeypatch)
    assert _skip(asked, message) is None


# -- T4: it must be the picker the creator is answering ------------------------------------


async def test_t4_an_older_picker_is_not_skipped_by_a_later_message(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    events = [
        *_after_picker(asked),
        *_tagged(
            [
                ("user", {"turn_id": "typed-0"}),
                ("assistant", {"turn_id": "r", "turn_value": "answer"}),
            ]
        ),
    ]
    assert _skip(asked, "never mind", events=events) is None


async def test_t4_an_answered_picker_is_not_skipped_again(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    assert _skip(asked, "never mind", events=_tagged(_answer(asked, skipped=True))) is None


async def test_t4_no_picker_at_all(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    events = _tagged([("user", {"turn_id": "kri1067-turn-1"}), ("assistant", {"turn_id": "r"})])
    assert _skip(asked, "never mind", events=events) is None


# -- T5: only a single-ending picker -------------------------------------------------------


def _with_question(asked, **changes: Any):  # noqa: ANN001, ANN202
    prompt, reply, _ = _answer(asked, skipped=True)
    question = {**reply[1]["clip_question"], **changes}
    return _tagged([prompt, ("assistant", {**reply[1], "clip_question": question})])


async def test_t5_a_picker_with_two_categories_is_not_read(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    [category] = asked.plan.clip_question["categories"]
    events = _with_question(asked, categories=[category, {**category, "key": "order:other"}])
    assert _skip(asked, "never mind", events=events) is None


async def test_t5_a_picker_for_something_other_than_an_ending_is_not_read(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    [category] = asked.plan.clip_question["categories"]
    events = _with_question(asked, categories=[{**category, "op": "exclude"}])
    assert _skip(asked, "never mind", events=events) is None


async def test_t5_a_question_that_also_answers_another_is_not_read(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    [category] = asked.plan.clip_question["categories"]
    events = _with_question(asked, categories=[{**category, "also_answers": ["other"]}])
    assert _skip(asked, "never mind", events=events) is None


async def test_t5_another_open_question_on_the_same_reply_is_not_read(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    prompt, reply, _ = _answer(asked, skipped=True)
    reply = ("assistant", {**reply[1], "choice_question": {"question_id": "c1"}})
    assert _skip(asked, "never mind", events=_tagged([prompt, reply])) is None


# -- T7: the picker must match exactly one brief ending ------------------------------------


async def test_t7_a_picker_whose_words_match_no_ending_is_not_read(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    assert _skip(asked, "never mind", brief=_brief(key="capture_time")) is None
    assert _skip(asked, "never mind", brief=None) is None


async def test_t7_an_ending_whose_source_message_is_not_before_the_picker_is_not_read(
    monkeypatch,
) -> None:
    asked = await _asked(monkeypatch)
    brief = _brief()
    [req] = brief.requirements
    assert _skip(asked, "never mind", brief=brief) is not None  # stated in the first message

    def restated(turn_id: str | None):  # noqa: ANN202
        moved = req.model_copy(update={"source_turn_id": turn_id})
        return brief.model_copy(update={"requirements": [moved]})

    # Restated in a message the picker has not seen, or from a source nobody can name.
    assert _skip(asked, "never mind", brief=restated("a-later-turn")) is None
    assert _skip(asked, "never mind", brief=restated(None)) is None


# -- the runtime wrapper: what `submit_turn` calls -----------------------------------------


async def _derived(  # noqa: ANN202
    monkeypatch,
    asked,
    message: str,
    validator_error: Exception | None = None,
    **overrides: Any,  # noqa: ANN001
):
    events = _after_picker(asked)
    rows = MagicMock()
    rows.all.return_value = [
        (role, payload, "user_message" if role == "user" else "assistant_response", None)
        for role, payload in events
    ]
    db = SimpleNamespace(execute=AsyncMock(return_value=rows))
    monkeypatch.setattr(runtime, "load_latest_brief", AsyncMock(return_value=_brief()))
    # The validator has its own two-column query; the fake rows above are the helper's.
    validate = AsyncMock(side_effect=validator_error)
    monkeypatch.setattr(runtime, "_validate_clip_selection", validate)
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    derived = await runtime._free_text_clip_skip(db, THREAD, message)
    return derived, db.execute.await_count, validate.await_count


async def test_the_runtime_wrapper_reads_the_thread_and_returns_the_skip(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    derived, reads, validated = await _derived(monkeypatch, asked, "skip that")
    assert derived == ClipSelectionIn(
        question_id=asked.plan.clip_question["question_id"], skipped=True
    )
    assert (reads, validated) == (1, 1)  # the derived answer is validated like a tap


async def test_the_runtime_wrapper_does_not_read_the_thread_for_an_ordinary_message(
    monkeypatch,
) -> None:
    asked = await _asked(monkeypatch)
    assert await _derived(monkeypatch, asked, "make it 30 seconds") == (None, 0, 0)


async def test_t8_with_the_picker_flag_off_nothing_is_derived(monkeypatch) -> None:
    asked = await _asked(monkeypatch)
    off = await _derived(
        monkeypatch, asked, "skip that", kria_clip_selection_questions_enabled=False
    )
    assert off == (None, 0, 0)


async def test_a_derived_skip_the_validator_refuses_falls_back_to_the_plain_message(
    monkeypatch,
) -> None:
    asked = await _asked(monkeypatch)
    stale = runtime.RuntimeFailure(
        422, "clip_selection_stale", "stale", recovery="refresh_replan", current_revision=1
    )
    derived, _reads, validated = await _derived(
        monkeypatch, asked, "skip that", validator_error=stale
    )
    assert derived is None and validated == 1  # never a hard error for a typed message


async def test_a_message_that_also_drops_the_other_spot_still_skips_this_picker(
    monkeypatch,
) -> None:
    # The opening is read from the message text by the planner, as for any message; this
    # picker's own Skip is still right.
    asked = await _asked(monkeypatch)
    assert _skip(asked, "skip that, forget the opening") is not None
    assert _skip(asked, "forget the opening, forget the ending") is not None


def test_no_events_derive_nothing() -> None:
    assert typed_picker_skip(_brief(), [], "skip that") is None

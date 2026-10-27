"""KRI-990 journey on real Postgres: a typed reply while a timing card is pending.

Prod 4C536AFE: the source-coverage card (one option), then a typed message that was not an
option match -> the bare "Your length choice is still waiting." text, no card, and the phone
made the old card read-only. Only a strategy change ("Forget the steak photo") escaped.

Failure modes (before the code):

1. The typed non-answer gets the bare progress wall instead of a tappable card again.
2. The re-asked card is not the live question: a typed label does not become a selection.
3. "Leave those clips out" (typed) is not converted, or the draft still names the gone clip.
4. The exit reaches a draft that is not approvable (turn still "question").
5. A second and third non-answer end the question for good.

Only the Main Creator call (``planner._plan_live_turn``) is replaced; the media snapshot
fence, the length plan, the gate, free-text ingestion in ``submit_turn`` and the draft are real.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.kria import planner
from app.kria.planner import PlannedKriaTurn
from app.models import CreationThreadEvent
from tests.kria.test_choice_conflict_journey import (
    _dispose_async_engine,  # noqa: F401  (autouse fixture)
    _events,
    _flags,  # noqa: F401  (autouse fixture)
    _head_draft,
    _questions,
    _say,
)
from tests.kria.test_choice_conflict_journey import _project as _seed

PROMPT = "Make a montage of my clips"
NON_ANSWER = (
    "All my clips are still attached, nothing was removed. Use the steak photo and make "
    "the montage."
)
GONE = "clip-gone"


def _project(monkeypatch: pytest.MonkeyPatch) -> tuple[uuid.UUID, uuid.UUID]:
    """Two attached clips; the Main Creator selects them AND a clip id with no clip row."""
    user_id, thread_id, _ = _seed(monkeypatch, clips=2)

    async def _model(*_args, **_kwargs) -> PlannedKriaTurn:  # noqa: ANN002, ANN003
        plan = planner.adapt_creator_action(
            ProposeStrategy(
                kind="propose_strategy",
                strategy=CreativeStrategy(
                    direction="guided_story",
                    edit_format="montage",
                    audio_strategy="licensed_music",
                    pacing="fast",
                    render_program="guided",
                    media_scope="selected",
                    selected_media_ids=["clip-00", "clip-01", GONE],
                    rationale="Every clip, fast.",
                ),
                summary="A fast montage of your clips.",
            )
        )
        return PlannedKriaTurn(
            plan=plan,
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_route="replan",
            brief_clip_ids=("clip-00", "clip-01", GONE),
        )

    monkeypatch.setattr(planner, "_plan_live_turn", _model)
    return user_id, thread_id


def _assistant_events(thread_id) -> list[CreationThreadEvent]:  # noqa: ANN001
    return [e for e in _events(thread_id) if e.role == "assistant"]


def _selections(thread_id) -> list[dict]:  # noqa: ANN001
    return [
        e.payload["choice_selection"]
        for e in _events(thread_id)
        if e.role == "user" and e.payload and e.payload.get("choice_selection")
    ]


@pytest.mark.asyncio
async def test_a_typed_non_answer_gets_the_card_again_and_the_typed_exit_reaches_a_draft(
    monkeypatch,
) -> None:
    user_id, thread_id = _project(monkeypatch)

    # 1. The first ask: a source-coverage card with the exit, nothing drafted.
    await _say(user_id, thread_id, PROMPT)
    (first,) = _questions(thread_id)
    assert first["kind"] == "source_coverage"
    assert [o["key"] for o in first["options"]] == ["update_footage", "leave_out_sources"]
    assert _head_draft(thread_id) is None

    # 2. A typed message that is no answer: the card comes back (never the bare wall).
    await _say(user_id, thread_id, NON_ANSWER)
    asked = _questions(thread_id)
    assert len(asked) == 2 and asked[1]["kind"] == "source_coverage"
    assert asked[1]["question_id"] != first["question_id"]
    assert asked[1]["input_digest"] == first["input_digest"]
    last = _assistant_events(thread_id)[-1]
    assert last.payload["turn_value"] == "question"
    assert "Your length choice is still waiting." in (last.content or "")
    assert "Leave those clips out" in (last.content or "")
    assert _selections(thread_id) == [] and _head_draft(thread_id) is None

    # 3. Another non-answer: still a live card (the question is never dropped).
    await _say(user_id, thread_id, "hmm, why can't you just do it?")
    asked = _questions(thread_id)
    assert len(asked) == 3 and asked[2]["kind"] == "source_coverage"
    assert _head_draft(thread_id) is None

    # 4. The label typed as plain text becomes a selection and the turn reaches a draft.
    result = await _say(user_id, thread_id, "Leave those clips out")
    assert result["status"] == "awaiting_approval"
    assert _selections(thread_id) == [
        {"question_id": asked[2]["question_id"], "option_key": "leave_out_sources"}
    ]
    assert len(_questions(thread_id)) == 3  # no further question
    strategy = ((_head_draft(thread_id).snapshot_json or {}).get("strategy")) or {}
    assert strategy["selected_media_ids"] == ["clip-00", "clip-01"]
    assert strategy["media_scope"] == "selected"
    (answer,) = strategy["choice_answers"]
    assert answer["kind"] == "source_coverage" and answer["option"] == "leave_out_sources"
    reply = _assistant_events(thread_id)[-1]
    assert reply.payload.get("turn_value") != "question"
    assert "choice_question" not in (reply.payload or {})

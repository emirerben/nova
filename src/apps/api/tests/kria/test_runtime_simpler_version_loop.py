"""The "different approach, or simpler version?" question is asked once, never in a loop.

Thread 0ef4fdde (2026-10-11, KRI-610): a new creator asked a montage to "explain the
footages". No plan could put text on each clip, so the brief-binding gate rolled the
draft back and asked "Should I try a different approach, or make this simpler
version?". The creator answered "simpler version", "you decide", "make it more
basic", "skip, decide for me"... and got the same question five times in an hour,
with no draft ever made: answering relied on the planner dropping the requirement,
and it never did.

Ways this can go wrong, each pinned below:
- the second identical refusal asks again (the loop) instead of making the draft;
- the draft made instead hides the miss (no honest "Couldn't" line);
- a clip question between the two refusals makes the breaker forget the first ask;
- a NEW unmet requirement is waved through because an older one was already asked;
- the very first refusal stops asking (the gate itself must keep working).
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria.brief import BriefUpdate
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.models import CreationThread, CreationThreadEvent
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit

_SUMMARY = "a summary of the match day event"
_ASK = "Should I try a different approach, or make this simpler version?"


@pytest.fixture(autouse=True)
def _binding_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _textless_plan():
    """A plan that puts no text on any clip: every per-clip text ask is unmet."""
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="day_vlog",
                audio_strategy="licensed_music",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="All four clips, in the order of the day.",
                target_duration_s=20,
            ),
            summary="A fast montage of the match day.",
        )
    )


def _planned(plan, *updates: BriefUpdate) -> PlannedKriaTurn:
    return PlannedKriaTurn(
        plan=plan,
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_updates=tuple(updates),
        brief_route="replan",
        brief_clip_ids=("clip-1", "clip-2"),
    )


def _summary_ask() -> BriefUpdate:
    return BriefUpdate(kind="text", scope="per_clip", description=_SUMMARY)


def _revision(thread_id) -> int:  # noqa: ANN001
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _last_reply(thread_id) -> tuple[str, str]:  # noqa: ANN001
    with sync_session() as db:
        event = (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(["draft_applied", "assistant_response"]),
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )
        return event.event_type, str(event.content or "")


async def _run_turns(monkeypatch, user_id, thread_id, turns):  # noqa: ANN001, ANN202
    """Run (message, PlannedKriaTurn) pairs in order; return each turn's reply."""
    replies = []
    for message, planned in turns:

        async def _plan(*_args, _planned=planned, **_kwargs):  # noqa: ANN002, ANN003, ANN202
            return _planned

        monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _plan)
        accepted = await _submit(user_id, thread_id, message, _revision(thread_id))
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        replies.append((result["status"], *_last_reply(thread_id)))
    return replies


@pytest.mark.asyncio
async def test_answering_the_question_makes_the_simpler_draft_instead_of_asking_again(
    monkeypatch,
):
    user_id, thread_id, _session_id = _seed_runtime_project()
    first, second = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            (
                "collab all those videos and explain the footages",
                _planned(_textless_plan(), _summary_ask()),
            ),
            # The planner kept the requirement (it emitted no change/remove), as in prod.
            ("simpler version", _planned(_textless_plan())),
        ],
    )

    # The first refusal still asks: the gate itself is unchanged.
    assert first[0] == "completed"
    assert first[1] == "assistant_response"
    assert first[2].endswith(_ASK)

    # The answer is honoured: a draft is made, and the miss is reported, not hidden.
    assert second[0] == "awaiting_approval"
    assert second[1] == "draft_applied"
    assert _ASK not in second[2]
    assert second[2].startswith("Here's the simpler version.")
    assert f"Couldn't: {_SUMMARY}" in second[2]


@pytest.mark.asyncio
async def test_a_clip_question_in_between_does_not_reset_the_question(monkeypatch):
    user_id, thread_id, _session_id = _seed_runtime_project()
    clip_card = KriaTurnPlan(
        mode="respond", turn_value="question", response="Which clip is IMG_7382?"
    )
    replies = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            ("first IMG_7382 then IMG_7820", _planned(clip_card)),
            ("skip, decide for me", _planned(_textless_plan())),
        ],
    )

    assert replies[0][2].endswith(_ASK)
    assert replies[1][2] == "Which clip is IMG_7382?"
    assert replies[2][0] == "awaiting_approval"
    assert _ASK not in replies[2][2]


@pytest.mark.asyncio
async def test_a_new_unmet_requirement_is_still_asked_about(monkeypatch):
    user_id, thread_id, _session_id = _seed_runtime_project()
    first, second = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            (
                "and put the player's name on each clip",
                _planned(
                    _textless_plan(),
                    BriefUpdate(kind="text", scope="per_clip", description="the player's name"),
                ),
            ),
        ],
    )

    assert first[2].endswith(_ASK)
    # r2 was never offered as a choice, so the creator decides about it first.
    assert second[0] == "completed"
    assert second[1] == "assistant_response"
    assert second[2].endswith(_ASK)

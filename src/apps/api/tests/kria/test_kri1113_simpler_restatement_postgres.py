"""KRI-1113 (b): "Make it without these" is honoured when the model restates what it leaves out.

Prod thread 83f2dcaa, 2026-10-31 13:51:14Z: the creator tapped "Make it without these" on an
offer that named r4 at brief v7. The Main Creator restated r4 on that turn (brief v8), and the
server omission never ran: it was skipped whenever the model wrote any brief update, and the
consent was pinned to v7 while the turn now held v8. The reply was the same offer again ("Your
current draft is unchanged. ... Should I make it without these ...").

Ways this can fail, written before any fix:

 F1 the incident: a restatement of exactly the offered asks cancels the creator's consent and
    the same question comes back (the loop KRI-610 / KRI-968 closed for the no-update case).
 F2 over-waiver: the model ALSO adds a new ask on the same turn. A new instruction needs its own
    offer, so nothing may be dropped and the offer is made again.
 F3 over-waiver: the model removes an ask the offer never named. Same: ask again, keep it.

Runs the real runtime turns (submit, run_kria_turn, brief persistence, receipts, the offer and
consent) on scratch Postgres; only the planner's output is supplied, as in
``test_runtime_simpler_version_loop``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria.brief import BriefUpdate, load_latest_brief_sync
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.kria.test_runtime_simpler_version_loop import (
    _ASK,
    _SUMMARY,
    _planned,
    _run_turns,
    _summary_ask,
    _textless_plan,
)

_TAP = "Make it without these"


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


def _restated() -> BriefUpdate:
    """The model's same-turn restatement of r1, as the 13:51 turn restated r4."""
    return BriefUpdate(
        operation="change",
        target_requirement_id="r1",
        expected_version=1,
        kind="text",
        scope="per_clip",
        description=_SUMMARY,
    )


@pytest.mark.asyncio
async def test_a_restatement_of_the_offered_ask_still_makes_the_simpler_version(monkeypatch):
    """F1."""
    user_id, thread_id, _ = _seed_runtime_project()
    first, second = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            (_TAP, _planned(_textless_plan(), _restated())),
        ],
    )
    assert first[2].endswith(_ASK)
    assert (second[0], second[1]) == ("awaiting_approval", "draft_applied"), second
    assert second[2].startswith("Here's the simpler version.")
    assert f"Left out as agreed: {_SUMMARY}" in second[2]
    with sync_session() as db:
        assert not load_latest_brief_sync(db, thread_id).live()


@pytest.mark.asyncio
async def test_a_new_ask_beside_the_restatement_is_asked_about_again(monkeypatch):
    """F2."""
    user_id, thread_id, _ = _seed_runtime_project()
    _, second = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            ("explain the footages", _planned(_textless_plan(), _summary_ask())),
            (
                _TAP,
                _planned(
                    _textless_plan(),
                    _restated(),
                    BriefUpdate(kind="text", scope="per_clip", description="the player's name"),
                ),
            ),
        ],
    )
    assert (second[0], second[1]) == ("completed", "assistant_response"), second
    assert second[2].endswith(_ASK)
    with sync_session() as db:
        assert len(load_latest_brief_sync(db, thread_id).live()) == 2


@pytest.mark.asyncio
async def test_removing_an_ask_the_offer_never_named_is_asked_about_again(monkeypatch):
    """F3: the offer named r1 only; the same turn also removes r2."""
    user_id, thread_id, _ = _seed_runtime_project()
    await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            (
                "explain the footages, title it Match day",
                _planned(
                    _textless_plan(),
                    _summary_ask(),
                    BriefUpdate(kind="style", scope="global", description="all vertical"),
                ),
            )
        ],
    )
    [reply] = await _run_turns(
        monkeypatch,
        user_id,
        thread_id,
        [
            (
                _TAP,
                _planned(
                    _textless_plan(),
                    _restated(),
                    BriefUpdate(operation="remove", target_requirement_id="r2", expected_version=1),
                ),
            ),
        ],
    )
    assert reply[0:2] == ("completed", "assistant_response"), reply
    with sync_session() as db:
        assert "r1" in {req.id for req in load_latest_brief_sync(db, thread_id).live()}

"""KRI-1392: a fresh version is the creator's call, read by a model or asked for by our offer.

Written from the failure list before the code (docs/reviews/kri-1384/routing-cues.md):
* with the brief flag off the copilot reads every rendered turn and cannot rebuild, so its
  refusal offers a fresh version; replying with the quoted word must re-plan (it went back to
  the copilot, which refused again: a dead end);
* the same word with no offer before it ("undo that", then "redo") is not a re-plan;
* an unopenable draft must never become a silent re-plan, whatever the wording, and its reply
  must carry the way to a fresh version;
* with the brief flag on, an accepted offer re-plans without asking the extractor again.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import AskUser
from app.config import settings
from app.kria import planner
from app.kria.copy_catalog import translate
from tests.kria.test_creative_brief import _ASK, _ops_plan, _wire_planner

pytestmark = pytest.mark.asyncio

_REFUSAL = "That kind of edit isn't available for this draft yet."


def _wire(monkeypatch, *, brief_on: bool, last_reply: str, editor_plan=None, miss=None):  # noqa: ANN001, ANN202
    output = SimpleNamespace(action=AskUser(**_ASK), brief_updates=[])
    db, item, creator_id, copilot, runs = _wire_planner(
        monkeypatch, output=output, editor_plan=editor_plan, snapshot={"text_bars": []}
    )
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", brief_on)
    if miss is not None:

        async def unopenable(*_a, **_k):  # noqa: ANN202
            planner._editor_target_miss.set(miss)
            return None

        copilot.side_effect = unopenable

    async def execute(statement, *_a, **_k):  # noqa: ANN001, ANN202
        reply_read = str(statement).split("FROM")[0].split() == [
            "SELECT",
            "creation_thread_events.content",
        ]
        rows = [last_reply] if reply_read and last_reply else []
        return SimpleNamespace(
            all=lambda: [],
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            scalar_one_or_none=lambda: None,
        )

    db.execute = AsyncMock(side_effect=execute)
    return db, item, creator_id, copilot, runs


async def _ask(db, item, creator_id, message):  # noqa: ANN001, ANN202
    return await planner.plan_live_turn(
        db,
        thread_id=uuid.uuid4(),
        item_id=item._fields["id"],
        creator_id=creator_id,
        user_message=message,
    )


@pytest.mark.parametrize(
    ("offer", "reply"),
    [
        (planner._REDO_OFFER, "redo"),
        (planner._REDO_OFFER, "Redo."),
        (planner._REDO_OFFER_TR, "YENİDEN YAP"),
        (translate("de", planner._REDO_OFFER), "nochmal"),
    ],
)
async def test_replying_with_the_offered_word_replans_without_the_copilot(
    monkeypatch, offer, reply
) -> None:
    db, item, creator_id, copilot, runs = _wire(
        monkeypatch, brief_on=False, last_reply=_REFUSAL + offer, editor_plan=_ops_plan("x")
    )
    await _ask(db, item, creator_id, reply)
    assert len(runs) == 1
    copilot.assert_not_awaited()


async def test_the_word_without_an_offer_is_the_copilots_to_read(monkeypatch) -> None:
    db, item, creator_id, copilot, runs = _wire(
        monkeypatch,
        brief_on=False,
        last_reply="I undid my last change (Colour on the title).",
        editor_plan=_ops_plan("patch_text_style"),
    )
    result = await _ask(db, item, creator_id, "redo")
    assert result.plan.intents[0].tool_name == "draft.apply_editor_ops"
    assert runs == []
    copilot.assert_awaited_once()


@pytest.mark.parametrize(
    "message", ["start over", "make the font different", "Mach das Video komplett neu"]
)
async def test_an_unopenable_draft_never_replans_and_offers_the_way_out(
    monkeypatch, message
) -> None:
    db, item, creator_id, _copilot, runs = _wire(
        monkeypatch, brief_on=False, last_reply="", miss="no_ready_variant"
    )
    result = await _ask(db, item, creator_id, message)
    assert result.plan.turn_value == "recovery" and not result.plan.intents
    assert result.plan.response.endswith(planner._REDO_OFFER)
    assert runs == []


async def test_an_accepted_offer_replans_without_reading_the_word_again(monkeypatch) -> None:
    db, item, creator_id, copilot, runs = _wire(
        monkeypatch, brief_on=True, last_reply=_REFUSAL + planner._REDO_OFFER
    )
    extractor = AsyncMock(side_effect=AssertionError("the offer word needs no extraction"))
    monkeypatch.setattr(planner, "_call_brief_extractor", extractor)
    result = await _ask(db, item, creator_id, "redo")
    assert len(runs) == 1
    assert result.brief_route == "replan"
    copilot.assert_not_awaited()


def test_the_routing_replay_never_replans_without_the_creators_word() -> None:
    """scripts/kria/score_kri1392_routing.py over its authored corpus (EN, TR, DE, ES): on an
    unopenable draft nothing re-plans, and after the offer only the offered word does."""
    import asyncio
    import importlib.util
    from pathlib import Path

    from structlog.testing import capture_logs

    script = Path(__file__).resolve().parents[5] / "scripts/kria/score_kri1392_routing.py"
    spec = importlib.util.spec_from_file_location("score_kri1392_routing", script)
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    with capture_logs():
        rows = asyncio.run(replay.score(Path.cwd(), recorded=False))
    words = {"redo", "yeniden yap", "nochmal"}
    for row in rows.values():
        assert row["paths"]["missing"] == "recovery+offer", row
        assert (row["paths"]["offer"] == "main_creator") == (row["message"] in words), row

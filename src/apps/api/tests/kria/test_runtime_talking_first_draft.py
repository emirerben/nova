"""A Talking edit's first draft is made, not refused, when the ask names a length.

Thread 17f666cb (2026-10-06): the KRI-459 gate rolled back the very first draft of a
Talking edit and asked "Your current draft is unchanged. Should I try a different
approach, or make this simpler version?" because "keep it under 45 seconds" can
never be "met" by a format that keeps the take. The gate now ignores receipts that
only describe what the chosen format does, and names the real state when it does
fire on a thread with no draft.
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
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.models import CreationThreadEvent
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit

_CUTS = (
    "Cut out the long pauses, the part where I say 'let me start that one again', "
    "and the 'where was I' bit."
)


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


def _talking_plan(*, caption_style: str = "karaoke"):
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                edit_format="subtitled",
                audio_strategy="original_audio",
                render_program="native",
                selected_media_ids=["clip-1"],
                rationale="One take, captioned.",
                caption_style=caption_style,
                opening_title="3 sourdough mistakes",
                opening_title_duration_s=2,
                target_duration_s=45,
            ),
            summary="A Talking edit with your hook title and karaoke captions.",
        )
    )


def _sourdough_updates() -> tuple[BriefUpdate, ...]:
    return (
        BriefUpdate(kind="timing", scope="global", description=_CUTS, facts={"duration_s": 45}),
        BriefUpdate(
            kind="style",
            scope="global",
            description="Karaoke captions with the key words highlighted",
        ),
        BriefUpdate(kind="text", scope="title", literal="3 sourdough mistakes"),
    )


async def _first_turn(monkeypatch: pytest.MonkeyPatch, *, caption_style: str) -> tuple:
    user_id, thread_id, _session_id = _seed_runtime_project()

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=_talking_plan(caption_style=caption_style),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=_sourdough_updates(),
            brief_route="replan",
            brief_clip_ids=("clip-1",),
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    accepted = await _submit(user_id, thread_id, _CUTS + " Keep it under 45 seconds.", 2)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    with sync_session() as db:
        # A made draft replies on `draft_applied`; a refused one on `assistant_response`.
        response = (
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
        content = str(response.content or "") if response is not None else ""
        receipts = (response.payload or {}).get("requirement_receipts") if response else None
    return result, content, receipts or []


@pytest.mark.asyncio
async def test_the_sourdough_first_draft_is_made_with_honest_receipts(monkeypatch):
    result, content, receipts = await _first_turn(monkeypatch, caption_style="karaoke")

    assert result["status"] == "awaiting_approval"
    assert "draft is unchanged" not in content
    assert "simpler version" not in content
    assert "- Done: Karaoke captions with the key words highlighted" in content
    assert '- Done: "3 sourdough mistakes"' in content
    assert "- Partly: Cut out the long pauses" in content
    assert "can't promise 45s" in content
    by_status = {r["requirement_id"]: r["status"] for r in receipts}
    assert by_status == {"r1": "partial", "r2": "met", "r3": "met"}
    assert all(r["verification"] == "checked" for r in receipts)


@pytest.mark.asyncio
async def test_a_dropped_caption_ask_still_asks_and_says_there_is_no_draft_yet(monkeypatch):
    """Sentence captions instead of the karaoke the creator asked for is a real
    simplification: the gate keeps asking, with the true state of the thread."""
    result, content, _receipts = await _first_turn(monkeypatch, caption_style="clean")

    assert result["status"] == "completed"
    assert "- Partly: Karaoke captions with the key words highlighted" in content
    assert "I haven't started a draft yet." in content
    assert "Your current draft is unchanged" not in content
    assert content.endswith("Should I try a different approach, or make this simpler version?")

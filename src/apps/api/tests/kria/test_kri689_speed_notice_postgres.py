"""KRI-689: the draft reply and the approval card say a phone montage cannot change clip speed.

Prod thread 1390E686 (2026-10-15): the English prompt held "Speed the river shots up 2x", the
brief kept it as `r4 [style/global]`, the draft ("I've removed the music so every clip plays
with its own sound...") and the approval card never mentioned speed, and the phone rendered
every clip at 1x. This drives the real runtime-v2 draft turn (`run_kria_turn`) against Postgres
with that brief and asserts the chat reply, the approval card and the stored draft.

Failure modes pinned here:
- the notice is missing from the draft reply, or only from the approval card text;
- a Turkish chat gets the English sentence;
- a pacing-only ask ("Fast pacing") or a cloud-render project is told about the iPhone;
- the notice swallows the model's own summary or turns the draft into a question.
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
from app.models import CreationThread, CreationThreadEvent, CreatorAgentApproval, PlanItem
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit

_SUMMARY = "A fast montage of the night boat ride."
_EN = "I can't change clip speed on your iPhone yet, so the clips play at normal speed."
_TR = "iPhone'unda klip hızını henüz değiştiremiyorum, o yüzden klipler normal hızda oynuyor."
# The phone montage keeps its fast cut for this plan's guided story (a speed ask, or no story
# lines to build it from), so the card says so for every account (KRI-1097 route registry).
_HEADS_UP = (
    "Heads-up for this video:\n"
    "- The guided story direction isn't applied on this video yet: it is cut as a fast montage"
)
_HEADS_UP_TR = (
    "Bu video için not:\n"
    "- guided story yönü bu videoda henüz uygulanmıyor: hızlı bir montaj olarak kesiliyor"
)
_PROXY = "users/u/creation-threads/t/analysis-proxy-ios-88F0B87C.mp4"
_CLOUD = "users/u/creation-threads/t/original-clip.mp4"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    # No verified `variableSpeed`: the plan cannot carry the speed, so the heads-up is owed.
    monkeypatch.setattr(settings, "phone_render_verified_features", [])
    monkeypatch.setattr(settings, "phone_render_user_ids", [])


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _plan():
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="montage",
                audio_strategy="original_audio",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="The four clips in the order of the ride.",
                target_duration_s=20,
            ),
            summary=_SUMMARY,
        )
    )


async def _draft(monkeypatch, description, *, language="en", clip=_PROXY):  # noqa: ANN001, ANN202
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "edit_format": "montage", "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = [clip]
        revision = int(thread.revision)
        db.commit()
    planned = PlannedKriaTurn(
        plan=_plan(),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_updates=(BriefUpdate(kind="style", scope="global", description=description),),
        brief_route="replan",
        brief_clip_ids=("clip-1",),
    )

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return planned

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    accepted = await _submit(user_id, thread_id, description, revision)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    with sync_session() as db:
        draft = (
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
        )
        approval = db.execute(
            select(CreatorAgentApproval).where(CreatorAgentApproval.thread_id == thread_id)
        ).scalar_one_or_none()
        return (
            result["status"],
            str(draft.content) if draft else None,
            approval.consequence_summary if approval else None,
        )


@pytest.mark.asyncio
async def test_the_prod_speed_ask_is_announced_in_the_reply_and_on_the_approval_card(monkeypatch):
    status, reply, card = await _draft(monkeypatch, "Speed the river shots up 2x")

    assert status == "awaiting_approval"
    # The model's own summary stays; the notice is its own line, never a question.
    assert reply == f"{_SUMMARY}\n{_EN}\n{_HEADS_UP}"
    assert card == f"Render this draft: {_SUMMARY} {_EN} {_HEADS_UP}"


@pytest.mark.asyncio
async def test_a_turkish_chat_gets_the_turkish_sentence(monkeypatch):
    status, reply, card = await _draft(
        monkeypatch, "Nehir çekimlerini 2 kat hızlandır", language="tr"
    )

    assert status == "awaiting_approval"
    assert reply == f"{_SUMMARY}\n{_TR}\n{_HEADS_UP_TR}"
    assert card.endswith(f"{_TR} {_HEADS_UP_TR}")
    assert _EN not in reply and _EN not in card


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description", "clip", "heads_up"),
    [
        ("Fast pacing", _PROXY, _HEADS_UP),  # pacing, not clip speed
        ("Speed the river shots up 2x", _CLOUD, None),  # a cloud render is not the iPhone
    ],
)
async def test_no_notice_where_it_would_be_false(monkeypatch, description, clip, heads_up):
    status, reply, card = await _draft(monkeypatch, description, clip=clip)

    assert status == "awaiting_approval"
    assert _EN not in reply and _EN not in card
    assert reply == "\n".join(part for part in (_SUMMARY, heads_up) if part)
    assert card == " ".join(part for part in ("Render this draft:", _SUMMARY, heads_up) if part)


@pytest.mark.asyncio
async def test_with_a_verified_speed_renderer_the_draft_promises_nothing_it_cannot_judge(
    monkeypatch,
):
    """The plan will carry the speed (or its receipt will say why not): no blanket refusal."""
    monkeypatch.setattr(settings, "phone_render_verified_features", ["variableSpeed"])
    status, reply, card = await _draft(monkeypatch, "Speed the river shots up 2x")

    assert status == "awaiting_approval"
    assert _EN not in reply and _EN not in card
    assert reply == f"{_SUMMARY}\n{_HEADS_UP}"
    assert card == f"Render this draft: {_SUMMARY} {_HEADS_UP}"

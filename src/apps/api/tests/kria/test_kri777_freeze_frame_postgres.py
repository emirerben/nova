"""KRI-777: the draft reply and the approval card never promise a freeze frame.

Café kit M2 (prod 6b689783a, thread EE7B4DCA, 2026-10-21, events #8-#9): the brief held
`r3 [timing/global] {"duration_s": 1, "freeze_frame": true}` "Freeze the frame for one second on
Atakan's bite", and the phone montage draft and its approval card both said "I will cut the 6
exact moments in order, insert the photo with text, freeze the frame on the bite, and keep the
original cafe sound." with no notice. Nothing can freeze a frame. This drives the real
runtime-v2 draft turn (`run_kria_turn`) against Postgres with the stored M2 brief (r2-r7,
verbatim; the clip order is left out, see `_m2_updates`) and that summary, and asserts the
chat reply, the approval card, the draft's change list and the stored receipts.

Failure modes pinned here:
- the reply or the card (or the change list under the draft) still says the frame freezes;
- the freeze gets no "Couldn't" line before approval (the phone montage defers `timing`);
- the "Couldn't" turns the draft into the "simpler version?" question, so nothing is approved;
- a Turkish chat gets the English sentence;
- a brief with no freeze ask loses the model's summary.
"""

from __future__ import annotations

import asyncio
import uuid

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

_PROMPT = (
    "Make a montage of six moments from it, about 2 seconds each. Freeze the frame for one "
    "second on Atakan's bite. Keep the original sound, no music. About 14 seconds."
)
_PROMPT_TR = (
    "Altı andan bir montaj yap, her biri yaklaşık 2 saniye. Atakan'ın ısırığında kareyi bir "
    "saniye dondur. Orijinal ses kalsın, müzik olmasın. Yaklaşık 14 saniye."
)
_SUMMARY = (
    "I will cut the 6 exact moments in order, insert the photo with text, freeze the frame on "
    "the bite, and keep the original cafe sound."
)
_FREEZE_EN = "Freeze the frame for one second on Atakan's bite"
_FREEZE_TR = "Atakan'ın ısırığında kareyi bir saniye dondur"
_EN = "I can't freeze a frame in this edit yet, so that moment plays normally"
_TR = "Bu düzenlemede henüz bir kareyi donduramıyorum, o yüzden o an normal akıyor"
# The phone montage keeps its fast cut for this plan (the photo's own text keeps the guided story
# out) and holds clips by its own default, not the plan's unasked balanced pace; the card says
# both for every account (KRI-1097 route registry).
_HEADS_UP = (
    "Heads-up for this video:\n"
    "- The balanced pacing isn't applied on this video yet: the cuts follow a fast pace\n"
    "- The guided story direction isn't applied on this video yet: it is cut as a fast montage"
)
_HEADS_UP_TR = (
    "Bu video için not:\n"
    "- balanced tempo bu videoda henüz uygulanmıyor: kesimler hızlı tempoda\n"
    "- guided story yönü bu videoda henüz uygulanmıyor: hızlı bir montaj olarak kesiliyor"
)
_PROXY = "users/u/creation-threads/t/analysis-proxy-ios-11CF148F-2374-4097-B299-FC3926E878BD.mp4"
_PHOTO = "clip:asset-6ae61e1a-96ff-4776-8f95-54ace669db23"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
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
                pacing="balanced",
                render_program="guided",
                selected_media_ids=[],
                rationale=(
                    "I'll sequence the six exact moments you requested. The frame will freeze "
                    "on Atakan's bite, and I'll keep the cafe's original audio throughout."
                ),
                opening_title="Colombian dessert, Ecuadorian hands",
                target_duration_s=14,
            ),
            summary=_SUMMARY,
        )
    )


def _m2_updates(freeze: str | None) -> tuple[BriefUpdate, ...]:
    """The stored M2 brief r2-r7 (job_debug.json), with the freeze row swapped or left out.

    r1, the six-moment clip order, is left out: prod resolved it with the clip resolver, and
    without that its own order question would end this draft before the freeze is judged.
    """
    return (
        BriefUpdate(kind="timing", scope=_PHOTO, description="1 second", facts={"duration_s": 1}),
        *(
            (
                BriefUpdate(
                    kind="timing",
                    scope="global",
                    description=freeze,
                    facts={"duration_s": 1, "freeze_frame": True},
                ),
            )
            if freeze
            else ()
        ),
        BriefUpdate(
            kind="audio",
            scope="global",
            description="Keep the original sound, no music",
            facts={"audio_intent": "source_unchanged"},
        ),
        BriefUpdate(kind="text", scope="title", literal="Colombian dessert, Ecuadorian hands"),
        BriefUpdate(kind="text", scope=_PHOTO, literal="From Ecuador 🇪🇨"),
        BriefUpdate(
            kind="timing", scope="global", description="About 14 seconds", facts={"duration_s": 14}
        ),
    )


async def _draft(monkeypatch, *, freeze: str | None, language: str = "en"):  # noqa: ANN001, ANN202
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "edit_format": "montage", "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = [_PROXY]
        revision = int(thread.revision)
        db.commit()
    planned = PlannedKriaTurn(
        plan=_plan(),
        manifest_hash="a" * 64,
        context_hash="b" * 64,
        brief_updates=_m2_updates(freeze),
        brief_route="replan",
        brief_clip_ids=("clip-1",),
    )

    async def _planner(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return planned

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planner)
    accepted = await _submit(
        user_id, thread_id, _PROMPT_TR if language == "tr" else _PROMPT, revision
    )
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
        card = db.execute(
            select(CreatorAgentApproval.consequence_summary).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        ).scalar_one_or_none()
        payload = dict(draft.payload) if draft else {}
        return result["status"], (str(draft.content) if draft else None), card, payload


@pytest.mark.asyncio
async def test_m2_freeze_is_a_couldnt_line_and_never_reaches_the_approval_card(monkeypatch):
    status, reply, card, payload = await _draft(monkeypatch, freeze=_FREEZE_EN)

    # A limit of every edit, not a simplification: the draft goes ahead to approval.
    assert status == "awaiting_approval"
    assert reply == (
        f"Not everything you asked for made it in:\n- Couldn't: {_FREEZE_EN} ({_EN})\n{_HEADS_UP}"
    )
    assert card == f"Render this draft: {reply}"
    for said in (reply, card, *payload["changes"]):
        assert "freeze the frame on the bite" not in said, said
    receipts = {row["requirement_id"]: row for row in payload["requirement_receipts"]}
    freeze = next(row for row in receipts.values() if row["reason"] == _EN)
    assert (freeze["status"], freeze["verification"]) == ("not_possible", "checked")
    # Never judged against the video's length.
    assert all("you asked for" not in (row["reason"] or "") for row in receipts.values())


@pytest.mark.asyncio
async def test_a_turkish_chat_gets_the_turkish_couldnt(monkeypatch):
    status, reply, card, payload = await _draft(monkeypatch, freeze=_FREEZE_TR, language="tr")

    assert status == "awaiting_approval"
    assert reply == (
        f"İstediklerinin hepsi videoya giremedi:\n- Yapamadım: {_FREEZE_TR} ({_TR})\n{_HEADS_UP_TR}"
    )
    assert card.endswith(reply)
    assert _EN not in reply and _EN not in card
    assert "freeze the frame on the bite" not in card


@pytest.mark.asyncio
async def test_without_a_freeze_ask_the_models_summary_stays(monkeypatch):
    # No freeze ask in the brief: nothing the receipts judged is unmet, so the model's words stay.
    status, reply, card, _payload = await _draft(monkeypatch, freeze=None)

    assert status == "awaiting_approval"
    assert reply == f"{_SUMMARY}\n{_HEADS_UP}"
    assert card == f"Render this draft: {_SUMMARY} {_HEADS_UP}"

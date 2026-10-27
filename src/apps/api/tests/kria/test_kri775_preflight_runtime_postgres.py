"""KRI-775 (runtime half): the M2 draft the phone planner refuses never gets an approval card.

Real runtime turn (``run_kria_turn`` over scratch Postgres), the Main Creator's planned turn
replaced by prod's own approved draft for job 417eecd4 after the real server gate
(``test_kri775_merope_m2_replay.run_gate``). In prod this turn produced
``approval_requested`` ("Render this draft: I will cut the 6 exact moments in order ...")
and the render was refused ten seconds after approval.

Ways this can fail, each pinned below:
1. The draft still reaches the approval card.
2. It is held back without the reason ("about 4.7s; you asked for 14s") or without a choice.
3. Picking "this simpler version" loops back into the same hold-back (KRI-759's trap), or the
   simpler version it approves is one the phone worker still refuses. (Known gap, xfail: once
   the order, freeze and 14 s rows are dropped, the photo's clip-scoped "1 second" is the only
   timing left and `length_policy.creator_duration_s` reads it as the whole video's length, so
   a length question comes back instead of the approval card. Same reader KRI-777 noted.)
4. A buildable draft, the kill switch, or a preflight that crashes changes anything.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria.brief import BriefUpdate, CreativeBrief, apply_updates
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentApproval,
    PlanItem,
    PlanItemAsset,
)
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_kri775_merope_m2_replay import (
    FIXTURE,
    RENDERS,
    gate_media,
    prod_flags,
    run_gate,
    run_worker,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project, _submit

PROD_PHOTO_ID = FIXTURE["visual"]["id"]


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    prod_flags(monkeypatch)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _m2_render(photo_id: uuid.UUID, *, five_seconds: bool = False) -> dict:
    """Job 417eecd4's approved draft, with this test's own Visual id in every reference."""
    render = json.loads(json.dumps(RENDERS[0]).replace(PROD_PHOTO_ID, str(photo_id)))
    if five_seconds:
        kept = []
        for req in render["brief"]["requirements"]:
            if req["id"] in {"r1", "r3"}:
                continue
            if req["id"] == "r7":
                req = {**req, "description": "About 5 seconds", "facts": {"duration_s": 5}}
            kept.append(req)
        render["brief"]["requirements"] = kept
    return render


def _seed_m2(render: dict, photo_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, _session_id = _seed_runtime_project()
    media = render["media_snapshot"]
    visual = FIXTURE["visual"]
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.edit_format = "montage"
        item.clip_gcs_paths = list(media["clip_paths"])
        item.clip_assignments = copy.deepcopy(media["clip_assignments"])
        thread.state = {
            "media": [{"media_id": media["clip_assignments"][0]["media_id"]}],
            "media_count": 1,
            "edit_format": "montage",
        }
        db.add(
            PlanItemAsset(
                id=photo_id,
                plan_item_id=item.id,
                user_id=user_id,
                gcs_path=f"users/{user_id}/plan/{item.id}/pool/{photo_id}.jpg",
                gcs_generation="1",
                kind="image",
                source_filename=visual["source_filename"],
                status="ready",
                aspect=visual["aspect"],
                analysis=dict(visual["analysis"]),
            )
        )
        db.commit()
    return user_id, thread_id


def _updates(render: dict) -> tuple[BriefUpdate, ...]:
    return tuple(
        BriefUpdate(
            kind=req["kind"],
            scope=req["scope"],
            description=req.get("description"),
            literal=req.get("literal"),
            facts=dict(req.get("facts") or {}),
        )
        for req in render["brief"]["requirements"]
    )


async def _first_turn(monkeypatch: pytest.MonkeyPatch, *, five_seconds: bool = False):
    photo_id = uuid.uuid4()
    render = _m2_render(photo_id, five_seconds=five_seconds)
    user_id, thread_id = _seed_m2(render, photo_id)
    # KRI-824: the gate previews the brief this turn persists, stamped from the same creator
    # message, exactly as `plan_live_turn` does.
    stamped = apply_updates(
        None, _updates(render), source_turn_id=None, creator_text=FIXTURE["creator_messages"][0]
    )
    gated = await run_gate(
        monkeypatch,
        render,
        brief=CreativeBrief(version=len(stamped.requirements), requirements=stamped.requirements),
    )
    calls = {"n": 0}

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        from dataclasses import replace  # noqa: PLC0415

        calls["n"] += 1
        return replace(
            gated,
            # The brief is written once, by the first turn; a choice answer adds nothing.
            brief_updates=_updates(render) if calls["n"] == 1 else (),
            brief_route="replan",
            brief_clip_ids=(render["media_snapshot"]["clip_assignments"][0]["media_id"],),
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    accepted = await _submit(user_id, thread_id, FIXTURE["creator_messages"][0], 2)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    return result, accepted, user_id, thread_id, render, gated


def _latest_reply(thread_id: uuid.UUID) -> CreationThreadEvent:
    with sync_session() as db:
        return (
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


def _approvals(turn_id: str) -> list[str]:
    with sync_session() as db:
        return list(
            db.execute(
                select(CreatorAgentApproval.consequence_summary).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(turn_id)
                )
            ).scalars()
        )


@pytest.mark.asyncio
async def test_the_m2_draft_is_held_back_with_the_reason_and_a_choice(monkeypatch) -> None:
    result, accepted, _user_id, thread_id, _render, _gated = await _first_turn(monkeypatch)

    assert result["status"] == "completed", result
    assert _approvals(accepted.turn_id) == []
    reply = _latest_reply(thread_id)
    assert reply.event_type == "assistant_response"
    content = str(reply.content)
    # KRI-824: the photo's "1 second" is that photo's time now, so "About 14 seconds" is met
    # and no longer a reason; the order the phone cannot place still holds the draft back.
    assert "About 14 seconds" not in content
    assert "- Partly: Caro arriving with tray" in content
    assert "I held this first draft back." in content
    assert content.endswith("Should I make it without these, or do you want to change your request?")
    question = (reply.payload or {}).get("choice_question") or {}
    assert {option["key"] for option in question.get("options", [])} >= {"simpler_version"}
    receipts = {r["requirement_id"]: r for r in reply.payload["requirement_receipts"]}
    assert (receipts["r1"]["status"], receipts["r1"]["verification"]) == ("partial", "checked")
    assert "r7" not in receipts


async def _choose_simpler(monkeypatch):  # noqa: ANN001, ANN202
    first, _accepted, user_id, thread_id, render, gated = await _first_turn(monkeypatch)
    assert first["status"] == "completed", first
    held_back = str(_latest_reply(thread_id).content)
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    accepted = await _submit(user_id, thread_id, "Make this simpler version", revision)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    return result, accepted, thread_id, render, gated, held_back


@pytest.mark.asyncio
async def test_choosing_the_simpler_version_never_loops_into_the_same_hold_back(
    monkeypatch,
) -> None:
    _result, _accepted, thread_id, _render, _gated, held_back = await _choose_simpler(monkeypatch)
    reply = str(_latest_reply(thread_id).content)
    assert reply != held_back
    assert "About 14 seconds (This draft is about 4.7s" not in reply
    with sync_session() as db:
        latest = (
            db.execute(
                select(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == thread_id)
                .order_by(CreativeBriefVersion.version.desc())
            )
            .scalars()
            .first()
        )
    live = {row["id"] for row in latest.requirements if row.get("status") != "superseded"}
    # The agreed omissions are out of the brief: the creator's "yes" was taken. The 14 s
    # length was met (KRI-824), so it was never one of them.
    assert "r1" not in live, live
    assert "r7" in live, live


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "KRI-775 follow-up: with the 14 s row dropped, the photo's clip-scoped '1 second' is "
        "read as the whole video's length (length_policy.creator_duration_s)"
    ),
)
@pytest.mark.asyncio
async def test_choosing_the_simpler_version_drafts_one_the_phone_worker_renders(
    monkeypatch,
) -> None:
    result, accepted, thread_id, render, gated, _held_back = await _choose_simpler(monkeypatch)

    assert result["status"] == "awaiting_approval", result
    assert len(_approvals(accepted.turn_id)) == 1
    reply = _latest_reply(thread_id)
    assert reply.event_type == "draft_applied"
    assert str(reply.content).startswith("Here's the simpler version.")
    assert "Left out as agreed: About 14 seconds" in str(reply.content)
    with sync_session() as db:
        latest = (
            db.execute(
                select(CreativeBriefVersion)
                .where(CreativeBriefVersion.thread_id == thread_id)
                .order_by(CreativeBriefVersion.version.desc())
            )
            .scalars()
            .first()
        )
        brief = CreativeBrief(version=latest.version, requirements=latest.requirements)
    assert {req.id for req in brief.live()}.isdisjoint({"r1", "r3", "r7"})
    # The draft the creator now approves is one the phone worker builds.
    outcome = run_worker(monkeypatch, render, brief=brief, media=gate_media(gated))
    assert outcome.refusal is None, outcome.refusal


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["buildable", "kill_switch", "preflight_crash"])
async def test_nothing_changes_where_the_worker_would_render_or_the_check_is_off(
    monkeypatch, case: str
) -> None:
    if case == "kill_switch":
        monkeypatch.setattr(settings, "kria_phone_montage_preflight_enabled", False)
    if case == "preflight_crash":
        from app.services import phone_montage_preflight  # noqa: PLC0415

        def broken(*_a, **_k):  # noqa: ANN202
            raise RuntimeError("preflight bug")

        monkeypatch.setattr(phone_montage_preflight, "montage_blockers", broken)
    result, accepted, *_rest = await _first_turn(monkeypatch, five_seconds=case == "buildable")

    assert result["status"] == "awaiting_approval", result
    assert len(_approvals(accepted.turn_id)) == 1

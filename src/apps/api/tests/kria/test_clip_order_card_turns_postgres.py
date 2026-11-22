"""The clip-order card over the real submit path, worker and approval (Postgres).

The failure-mode list is the docstring of `test_clip_order_card.py` (FM numbers match). This module
pins the ones that need real rows: the draft and reply events a card build, an old build and the
flag-off path write (FM1, FM2, FM15), a drag that never plans (FM3), never refuses (FM4, FM5) and
is idempotent (FM6), two quick drags (FM7), "use this order" (FM9), a drag after a video exists
and an approval prepared before a drag (FM10), the request digest (FM14) and the capability.

No model is called: the planner seam (`_plan_with_live_agent`) is replaced by an authored plan
where a draft is needed, and is made to FAIL where a drag must not reach it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody, ClipOrderOpsIn, SubmitTurnBody
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval, read_delta
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.services import clip_order_card as card_mod
from app.services import clip_order_ledger as ledger
from app.services.client_protocol import (
    CLIP_ORDER_CARD_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.tasks.kria_runtime import _claim, _complete_draft_turn, run_kria_turn
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

IDS = ["m1", "m2", "m3", "m4", "m5"]
SUMMARY = "Open on the whisk and finish on the packed order."


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    yield
    clear_request_context()


def _seed(count: int = 5, *, notes: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    user_id, thread_id, _ = _seed_runtime_project()
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        item = db.get(PlanItem, thread.active_plan_item_id)
        item.clip_assignments = [
            {
                "media_id": f"m{n}",
                "kind": "video",
                **({"user_note": f"take {n}"} if notes else {}),
            }
            for n in range(1, count + 1)
        ]
        db.commit()
    return user_id, thread_id


def _revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


def _events(thread_id: uuid.UUID) -> list[CreationThreadEvent]:
    with sync_session() as db:
        rows = list(
            db.execute(
                select(CreationThreadEvent)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence)
            ).scalars()
        )
        for row in rows:
            db.expunge(row)
        return rows


def _entries(thread_id: uuid.UUID) -> tuple[ledger.LedgerEntry, ...]:
    return ledger.fold_clip_order((e.role, e.payload) for e in _events(thread_id))


def _cards(thread_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        e.payload[card_mod.CARD_KEY]
        for e in _events(thread_id)
        if isinstance(e.payload, dict) and card_mod.CARD_KEY in e.payload
    ]


async def _drag(
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    ordered: list[str],
    *,
    proposal_id: str = "",
    confirm: bool = False,
    event_id: str | None = None,
    revision: int | None = None,
):
    set_client_protocol(CLIP_ORDER_CARD_PROTOCOL)
    async with AsyncSessionLocal() as db:
        return await _submit(
            db,
            user_id,
            thread_id,
            SubmitTurnBody(
                message="Order updated",
                client_event_id=event_id or f"drag-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id) if revision is None else revision,
                clip_order_ops=ClipOrderOpsIn(
                    proposal_id=proposal_id, ordered_media_ids=ordered, confirm=confirm
                ),
            ),
        )


async def _submit(db, user_id, thread_id, body):  # noqa: ANN001, ANN202
    from app.kria.runtime import submit_turn  # noqa: PLC0415

    return await submit_turn(db, thread_id=thread_id, creator_id=user_id, body=body)


def _strategy_plan(**fields: Any):  # noqa: ANN202
    base: dict[str, Any] = {
        "direction": "guided_story",
        "edit_format": "day_vlog",
        "audio_strategy": "licensed_music",
        "pacing": "fast",
        "render_program": "guided",
        "selected_media_ids": [],
        "rationale": "Open on the whisk and end on the packed order.",
    }
    return adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(**{**base, **fields}),
            summary=SUMMARY,
        )
    )


async def _draft(
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    *,
    protocol: int | None,
    entries: tuple[dict[str, Any], ...] = (),
    **strategy: Any,
) -> str:
    """A first draft turn through the real submit and the real worker completion."""
    if protocol is None:
        clear_request_context()
    else:
        set_client_protocol(protocol)
    plan = _strategy_plan(**strategy)

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=plan, manifest_hash="a" * 64, context_hash="b" * 64, clip_order_entries=entries
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    async with AsyncSessionLocal() as db:
        accepted, _ = await _submit(
            db,
            user_id,
            thread_id,
            SubmitTurnBody(
                message="Make the matcha update feel quick but personal",
                client_event_id=f"draft-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id),
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "awaiting_approval", result
    return accepted.turn_id


def _draft_event(thread_id: uuid.UUID) -> CreationThreadEvent:
    return [e for e in _events(thread_id) if e.event_type == "draft_applied"][-1]


# ── FM1 / FM2: the draft event for a card build, an old build and the flag off ─────────────────


@pytest.mark.asyncio
async def test_fm2_a_card_build_gets_the_strip_on_the_draft_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _seed()
    try:
        await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        event = _draft_event(thread_id)
        card = event.payload[card_mod.CARD_KEY]
        assert card["shown"] is True and card["confirmed"] is False and card["editable"] is True
        assert [c["media_id"] for c in card["cards"]] == IDS
        assert [c["label"] for c in card["cards"]] == [f"take {n}" for n in range(1, 6)]
        assert {c["source"] for c in card["cards"]} == {"server_default"}
        assert event.content == SUMMARY  # no listing: the build draws the strip itself
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm1_an_old_build_is_told_the_order_as_text_and_gets_no_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _seed()
    try:
        await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL - 1)
        event = _draft_event(thread_id)
        assert card_mod.CARD_KEY not in event.payload
        assert event.content == (
            f"{SUMMARY}\n\nYour clips play in this order:\n"
            + "\n".join(f"{n}. take {n}" for n in range(1, 6))
        )
        assert "analysis-proxy" not in event.content and "m1" not in event.content
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["flag_off", "no_build", "kill_switch_text_only"])
async def test_fm1_the_draft_event_is_unchanged_when_the_order_is_not_offered(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Flag off, or a caller that declares no build (the web): no key, no byte of prose. The kill
    switch keeps the text listing for declared builds but never the card."""
    user_id, thread_id = _seed()
    protocol: int | None = CLIP_ORDER_CARD_PROTOCOL
    if case == "flag_off":
        monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    elif case == "no_build":
        protocol = None
    else:
        monkeypatch.setattr(settings, "clip_order_card_min_client_protocol", 999)
    try:
        await _draft(user_id, thread_id, monkeypatch, protocol=protocol)
        event = _draft_event(thread_id)
        assert card_mod.CARD_KEY not in event.payload
        if case == "kill_switch_text_only":
            assert event.content.startswith(f"{SUMMARY}\n\nYour clips play in this order:")
        else:
            assert event.content == SUMMARY
            assert ledger.LEDGER_KEY not in event.payload
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm2_the_draft_of_an_order_that_is_not_the_upload_order_shows_no_strip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _seed()
    try:
        await _draft(
            user_id,
            thread_id,
            monkeypatch,
            protocol=CLIP_ORDER_CARD_PROTOCOL,
            audio_strategy="voiceover",
        )
        event = _draft_event(thread_id)
        assert card_mod.CARD_KEY not in event.payload and event.content == SUMMARY
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm2_one_clip_shows_no_strip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    one_user, one_thread = _seed(1)
    try:
        await _draft(one_user, one_thread, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        assert card_mod.CARD_KEY not in _draft_event(one_thread).payload
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm15_the_card_never_reaches_the_draft_the_policy_or_a_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card is a projection on the event: the stored draft, the approval and the thread carry
    no trace of it, so the re-check has nothing to rewrite and asks no "simpler version?"."""
    user_id, thread_id = _seed()
    try:
        await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        with sync_session() as db:
            drafts = list(
                db.execute(
                    select(CreatorEditDraft.snapshot_json).where(
                        CreatorEditDraft.thread_id == thread_id
                    )
                ).scalars()
            )
        assert drafts and all(card_mod.CARD_KEY not in json.dumps(d) for d in drafts)
        events = _events(thread_id)
        assert not any((e.payload or {}).get("choice_question") for e in events)
        assert [e.event_type for e in events].count("draft_applied") == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol_offset", [0, -1])
async def test_fm2_a_reply_that_changes_the_order_carries_the_card_or_the_listing(
    monkeypatch: pytest.MonkeyPatch, protocol_offset: int
) -> None:
    """A chat reorder's reply event: the card for a build that draws it, the text for an old one."""
    user_id, thread_id = _seed()
    entry = ledger.LedgerEntry("creator_text", tuple(IDS), ("m3", "m1", "m2", "m4", "m5"))

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=KriaTurnPlan(mode="respond", turn_value="question", response="Moved it."),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            clip_order_entries=(entry.to_payload(),),
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    set_client_protocol(CLIP_ORDER_CARD_PROTOCOL + protocol_offset)
    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await _submit(
                db,
                user_id,
                thread_id,
                SubmitTurnBody(
                    message="Put the third clip first",
                    client_event_id=f"reply-{uuid.uuid4().hex}",
                    expected_thread_revision=_revision(thread_id),
                ),
            )
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert result["status"] == "completed", result
        reply = _events(thread_id)[-1]
        assert reply.payload[ledger.LEDGER_KEY][0]["actor"] == "creator_text"
        if protocol_offset == 0:
            card = reply.payload[card_mod.CARD_KEY]
            assert [c["media_id"] for c in card["cards"]][:3] == ["m3", "m1", "m2"]
            assert card["editable"] is False  # a chat reply is not the card's drag
            assert reply.content == "Moved it."
        else:
            assert card_mod.CARD_KEY not in reply.payload
            assert reply.content.startswith(
                "Moved it.\n\nYour clips play in this order:\n1. take 3"
            )
    finally:
        await async_engine.dispose()


# ── FM3 / FM4 / FM5 / FM6 / FM7 / FM9: the drag ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fm3_a_drag_makes_no_model_call_adds_no_prose_and_never_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _seed()

    async def _must_not_plan(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a drag reached the planner")

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _must_not_plan)
    monkeypatch.setattr("app.kria.planner.plan_live_turn", _must_not_plan)
    monkeypatch.setattr(
        run_kria_turn, "apply_async", lambda *a, **k: pytest.fail("a drag published a worker")
    )
    try:
        # a turn is running (pending): a drag does not queue behind it
        async with AsyncSessionLocal() as db:
            busy, _ = await _submit(
                db,
                user_id,
                thread_id,
                SubmitTurnBody(
                    message="Make it quick",
                    client_event_id=f"busy-{uuid.uuid4().hex}",
                    expected_thread_revision=_revision(thread_id),
                ),
            )
        assert busy.status == "pending"
        accepted, should_publish = await _drag(user_id, thread_id, ["m3", "m1", "m2", "m4", "m5"])
        assert accepted.status == "completed" and should_publish is False
        events = _events(thread_id)
        user, card_event = events[-2], events[-1]
        assert user.role == "user" and user.payload["inert"] is True
        assert user.content is None  # the placeholder is never stored: no transcript reader sees it
        assert user.payload[card_mod.OPS_KEY]["ordered_media_ids"] == ["m3", "m1", "m2", "m4", "m5"]
        assert card_event.role == "assistant" and card_event.event_type == "clip_order_card"
        assert card_event.content is None  # the card IS the answer: no chat message
        assert card_event.payload[ledger.LEDGER_KEY][0]["actor"] == "creator_drag"
        card = card_event.payload[card_mod.CARD_KEY]
        assert [c["media_id"] for c in card["cards"]] == ["m3", "m1", "m2", "m4", "m5"]
        assert [c["media_id"] for c in card["cards"] if c["moved"]] == []  # no card seen before
        # the busy turn is untouched and the drag is a finished turn of its own
        with sync_session() as db:
            statuses = {
                t.status
                for t in db.execute(
                    select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread_id)
                ).scalars()
            }
        assert statuses == {"pending", "completed"}
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm4_a_stale_proposal_and_a_stale_revision_rebase_and_never_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _seed()
    try:
        await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        shown = _cards(thread_id)[-1]
        await _drag(user_id, thread_id, ["m2", "m1", "m3", "m4", "m5"], proposal_id="old-offer")
        card = _cards(thread_id)[-1]
        assert card["rebased"] is True and card["proposal_id"] != shown["proposal_id"]
        # a drag written against the first card's order, sent late with the first revision
        await _drag(
            user_id,
            thread_id,
            ["m5", "m4", "m3", "m2", "m1"],
            proposal_id=shown["proposal_id"],
            revision=0,
        )
        assert [c["media_id"] for c in _cards(thread_id)[-1]["cards"]] == [
            "m5",
            "m4",
            "m3",
            "m2",
            "m1",
        ]
        assert [e.after_ids[0] for e in _entries(thread_id)] == ["m2", "m5"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "expected", "writes_entry"),
    [
        (["m3", "m1"], ["m3", "m2", "m1", "m4", "m5"], True),  # missing ids: named clips swap slots
        (["m5", "m4", "m3", "m2", "m1", "m9"], ["m5", "m4", "m3", "m2", "m1"], True),  # extra id
        (["m2", "m2", "m1", "m3", "m4", "m5"], ["m2", "m1", "m3", "m4", "m5"], True),  # duplicate
        ([], IDS, False),  # empty list: the current card, nothing written
        (["x", "y"], IDS, False),  # unknown only: the same
    ],
)
async def test_fm5_a_non_permutation_completes_and_returns_the_card(
    requested: list[str], expected: list[str], writes_entry: bool
) -> None:
    user_id, thread_id = _seed()
    try:
        accepted, _ = await _drag(user_id, thread_id, requested)
        assert accepted.status == "completed"
        card = _cards(thread_id)[-1]
        assert [c["media_id"] for c in card["cards"]] == expected and card["total"] == 5
        assert card["rebased"] is True or requested == expected
        assert bool(_entries(thread_id)) is writes_entry
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm6_the_same_drag_twice_is_one_entry_and_a_changed_body_is_a_key_reuse() -> None:
    user_id, thread_id = _seed()
    revision = _revision(thread_id)
    order = ["m3", "m1", "m2", "m4", "m5"]
    try:
        first, _ = await _drag(user_id, thread_id, order, event_id="evt-A", revision=revision)
        # a lost response: the app retries the very same request
        again, should_publish = await _drag(
            user_id, thread_id, order, event_id="evt-A", revision=revision
        )
        assert again.replayed is True and again.turn_id == first.turn_id
        assert should_publish is False
        assert len(_entries(thread_id)) == 1
        assert len(_cards(thread_id)) == 1
        with pytest.raises(RuntimeFailure) as reused:
            await _drag(user_id, thread_id, order[::-1], event_id="evt-A", revision=revision)
        assert reused.value.code == "idempotency_key_reused"  # a different body under one id
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm7_two_quick_drags_the_last_full_order_wins_in_one_undo_step() -> None:
    user_id, thread_id = _seed()
    try:
        await _drag(user_id, thread_id, ["m2", "m1", "m3", "m4", "m5"])
        await _drag(user_id, thread_id, ["m5", "m2", "m1", "m3", "m4"])
        entries = _entries(thread_id)
        assert [e.coalesce for e in entries] == [False, True]
        state = ledger.current_clip_order(attachment_ids=IDS, entries=entries)
        assert list(state.order) == ["m5", "m2", "m1", "m3", "m4"]
        assert state.previous == tuple(IDS)  # one undo restores the order before the gesture
        # a chat message in between ends the gesture: the next drag is its own undo step
        with sync_session() as db:
            thread = db.get(CreationThread, thread_id, with_for_update=True)
            from app.tasks.kria_runtime import _append_sync_event  # noqa: PLC0415

            _append_sync_event(
                db,
                thread,
                role="assistant",
                event_type="assistant_response",
                content="ok",
                payload={"turn_value": "progress"},
            )
            db.commit()
        await _drag(user_id, thread_id, ["m1", "m5", "m2", "m3", "m4"])
        assert [e.coalesce for e in _entries(thread_id)] == [False, True, False]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm9_confirm_marks_the_card_a_later_drag_clears_it() -> None:
    user_id, thread_id = _seed()
    try:
        await _drag(user_id, thread_id, ["m2", "m1", "m3", "m4", "m5"], confirm=True)
        confirmed = _cards(thread_id)[-1]
        assert confirmed["confirmed"] is True
        # "use this order" on the order already shown: confirmed, nothing moved, no new entry
        await _drag(user_id, thread_id, ["m2", "m1", "m3", "m4", "m5"], confirm=True)
        same = _cards(thread_id)[-1]
        assert same["confirmed"] is True and not any(c["moved"] for c in same["cards"])
        assert len(_entries(thread_id)) == 1
        await _drag(user_id, thread_id, ["m1", "m2", "m3", "m4", "m5"])
        moved = _cards(thread_id)[-1]
        assert moved["confirmed"] is False
        assert [c["media_id"] for c in moved["cards"] if c["moved"]] in (["m1"], ["m2"])
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm11_no_visible_string_of_a_drag_card_shows_an_id() -> None:
    user_id, thread_id = _seed(3, notes=False)
    with sync_session() as db:
        item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
        item.clip_assignments = [
            {"media_id": f"analysis-proxy-ios-{n}-5C6E0D6A-B7C3-5C1E-A4D1-1F2B3C4D5E6F.mp4"}
            for n in range(3)
        ]
        db.commit()
        ids = [r["media_id"] for r in item.clip_assignments]
    try:
        await _drag(user_id, thread_id, [ids[2], ids[0], ids[1]])
        card = _cards(thread_id)[-1]
        visible = json.dumps(
            [{k: v for k, v in c.items() if k != "media_id"} for c in card["cards"]]
        )
        assert "analysis-proxy" not in visible and ".mp4" not in visible
        assert [c["label"] for c in card["cards"]] == ["Clip 1", "Clip 2", "Clip 3"]
        assert [c["media_id"] for c in card["cards"]] == [ids[2], ids[0], ids[1]]
    finally:
        await async_engine.dispose()


# ── the gate: a drag from a build or server that cannot take it ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["flag_off", "old_build", "kill_switch"])
async def test_fm1_a_drag_the_server_cannot_take_is_one_plain_line_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    user_id, thread_id = _seed()
    if case == "flag_off":
        monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    elif case == "kill_switch":
        monkeypatch.setattr(settings, "clip_order_card_min_client_protocol", 999)
    try:
        if case == "old_build":
            set_client_protocol(CLIP_ORDER_CARD_PROTOCOL - 1)
            async with AsyncSessionLocal() as db:
                accepted, should_publish = await _submit(
                    db,
                    user_id,
                    thread_id,
                    SubmitTurnBody(
                        message="Order updated",
                        client_event_id=f"old-{uuid.uuid4().hex}",
                        expected_thread_revision=_revision(thread_id),
                        clip_order_ops=ClipOrderOpsIn(ordered_media_ids=IDS[::-1]),
                    ),
                )
        else:
            accepted, should_publish = await _drag(user_id, thread_id, IDS[::-1])
        assert accepted.status == "completed" and should_publish is False
        last = _events(thread_id)[-1]
        assert last.event_type == "status_update" and "can't change the clip order" in last.content
        assert not _entries(thread_id) and not _cards(thread_id)
    finally:
        await async_engine.dispose()


# ── FM10: a video exists / an approval prepared before the drag ────────────────────────────────


@pytest.mark.asyncio
async def test_fm10_after_a_video_exists_the_timeline_is_the_order_and_the_card_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drag recorded after a render would be overruled by the timeline on the next turn (the
    reader switches to it), so it is not recorded: the card answers with the order that plays."""
    user_id, thread_id = _seed()
    target = SimpleNamespace(
        snapshot={"slots": [{"media_id": m} for m in ["m2", "m1", "m3", "m4", "m5"]]},
        variant={},
    )

    async def _loaded(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        return target

    monkeypatch.setattr("app.kria.planner._load_editor_target", _loaded)
    try:
        accepted, _ = await _drag(user_id, thread_id, ["m5", "m4", "m3", "m2", "m1"])
        assert accepted.status == "completed"
        card = _cards(thread_id)[-1]
        assert card["editable"] is False and card["rebased"] is True
        assert [c["media_id"] for c in card["cards"]] == ["m2", "m1", "m3", "m4", "m5"]
        assert not [e for e in _entries(thread_id) if e.actor == "creator_drag"]
    finally:
        await async_engine.dispose()


# ── FM10: a drag while an approval is pending is carried INTO the draft at approve time ───────
# The creator drags, taps "use this order", then "make the video": the video must come out in the
# dragged order. Never a 409 and never a cancelled plan. The real submit, approve and dispatch path;
# the oracle is the order the render dispatch is handed (the approval-bound snapshot's stamp, the
# one carrier the contract, the preflight and the worker all seat on).


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """The draft carries a brief binding (production's writer): the channel a drag travels by."""
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)


def _pending(turn_id: str) -> tuple[uuid.UUID, str, int]:
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == uuid.UUID(turn_id))
        ).scalar_one()
        return approval.id, approval_fingerprint(approval), approval.draft_revision


async def _approve(user_id: uuid.UUID, thread_id: uuid.UUID, turn_id: str, *, revision=None):  # noqa: ANN001, ANN202
    approval_id, token, draft_revision = _pending(turn_id)
    async with AsyncSessionLocal() as db:
        return await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision="approve",
            body=ApprovalDecisionBody(
                expected_thread_revision=_revision(thread_id) if revision is None else revision,
                expected_draft_revision=draft_revision,
                expected_approval_fingerprint=token,
            ),
        )


def _capture_dispatch(
    monkeypatch: pytest.MonkeyPatch, user_id: uuid.UUID, thread_id: uuid.UUID
) -> list[dict[str, Any]]:
    from app.tasks.content_plan_build import DispatchResult

    seen: list[dict[str, Any]] = []

    def _fake(*_args, **kwargs) -> DispatchResult:  # noqa: ANN002, ANN003
        seen.append(kwargs)
        with sync_session() as db:
            item = db.get(
                PlanItem,
                db.get(CreationThread, thread_id).active_plan_item_id,
                with_for_update=True,
            )
            job = Job(
                user_id=user_id,
                status="queued",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                assembly_plan={"variants": []},
            )
            db.add(job)
            db.flush()
            item.current_job_id = job.id
            db.commit()
            return DispatchResult("dispatched", job_id=str(job.id))

    monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _fake)
    return seen


async def _render(approval_turn: str) -> None:
    from app.tasks.kria_runtime import execute_kria_approval

    approval_id, _token, _rev = _pending_any(approval_turn)
    result = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
    assert result["status"] == "dispatched", result


def _pending_any(turn_id: str) -> tuple[uuid.UUID, str, int]:
    return _pending(turn_id)


def _dispatched_order(seen: list[dict[str, Any]]) -> list[str]:
    binding = seen[-1]["creator_brief_binding"]
    return ledger.snapshot_order(binding["media_snapshot"])


def _draft_hash(thread_id: uuid.UUID) -> str:
    with sync_session() as db:
        return db.execute(
            select(CreatorEditDraft.snapshot_hash).where(CreatorEditDraft.thread_id == thread_id)
        ).scalar_one()


@pytest.mark.asyncio
async def test_fm10_drag_then_approve_renders_the_dragged_order(
    monkeypatch: pytest.MonkeyPatch, bound: None
) -> None:
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        before = _draft_hash(thread_id)
        seen = _capture_dispatch(monkeypatch, user_id, thread_id)
        await _drag(user_id, thread_id, ["m3", "m1", "m2", "m4", "m5"], confirm=True)
        decision, _ = await _approve(user_id, thread_id, turn_id)
        assert decision.status == "approved"
        assert _draft_hash(thread_id) != before  # the approved draft IS the rebound one
        await _render(turn_id)
        assert _dispatched_order(seen) == ["m3", "m1", "m2", "m4", "m5"]
        assert not [e for e in _events(thread_id) if e.event_type == "assistant_error"]
        with sync_session() as db:
            assert db.get(CreatorAgentApproval, _pending(turn_id)[0]).status in {
                "approved",
                "consumed",
            }
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm10_drag_drag_approve_renders_the_last_order(
    monkeypatch: pytest.MonkeyPatch, bound: None
) -> None:
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        seen = _capture_dispatch(monkeypatch, user_id, thread_id)
        await _drag(user_id, thread_id, ["m2", "m1", "m3", "m4", "m5"])
        await _drag(user_id, thread_id, ["m5", "m2", "m1", "m3", "m4"], confirm=True)
        await _approve(user_id, thread_id, turn_id)
        await _render(turn_id)
        assert _dispatched_order(seen) == ["m5", "m2", "m1", "m3", "m4"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm10_drag_then_a_chat_reorder_then_approve_renders_the_drag_and_the_chat_waits(
    monkeypatch: pytest.MonkeyPatch, bound: None
) -> None:
    """The chat message waits behind the pending approval (as every message does); approving renders
    what the creator confirmed, and the message is planned next, on the order that rendered."""
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        seen = _capture_dispatch(monkeypatch, user_id, thread_id)
        await _drag(user_id, thread_id, ["m3", "m1", "m2", "m4", "m5"], confirm=True)
        async with AsyncSessionLocal() as db:
            waiting, _ = await _submit(
                db,
                user_id,
                thread_id,
                SubmitTurnBody(
                    message="Swap the first two",
                    client_event_id=f"chat-{uuid.uuid4().hex}",
                    expected_thread_revision=_revision(thread_id),
                ),
            )
        assert waiting.status == "queued"
        await _approve(user_id, thread_id, turn_id)
        await _render(turn_id)
        assert _dispatched_order(seen) == ["m3", "m1", "m2", "m4", "m5"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm10_an_approval_with_no_drag_since_is_untouched(
    monkeypatch: pytest.MonkeyPatch, bound: None
) -> None:
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        before = _draft_hash(thread_id)
        seen = _capture_dispatch(monkeypatch, user_id, thread_id)
        decision, _ = await _approve(user_id, thread_id, turn_id)
        assert decision.status == "approved" and _draft_hash(thread_id) == before
        await _render(turn_id)
        assert _dispatched_order(seen) == []  # nothing was stamped: the upload order plays
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm10_an_old_build_draft_approves_untouched(
    monkeypatch: pytest.MonkeyPatch, bound: None
) -> None:
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(
            user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL - 1
        )
        before = _draft_hash(thread_id)
        decision, _ = await _approve(user_id, thread_id, turn_id)
        assert decision.status == "approved" and _draft_hash(thread_id) == before
    finally:
        await async_engine.dispose()


def _append_entry(thread_id: uuid.UUID, actor: str, before: list[str], after: list[str]) -> None:
    from app.tasks.kria_runtime import _append_sync_event

    with sync_session() as db:
        thread = db.get(CreationThread, thread_id, with_for_update=True)
        _append_sync_event(
            db,
            thread,
            role="assistant",
            event_type="status_update",
            content=None,
            payload={
                ledger.LEDGER_KEY: [
                    ledger.LedgerEntry(actor, tuple(before), tuple(after), "t").to_payload()
                ]
            },
        )
        db.commit()


@pytest.mark.asyncio
async def test_fm10_an_ai_or_server_order_change_never_rebinds_an_approval(
    monkeypatch: pytest.MonkeyPatch, bound: None
) -> None:
    """Only a creator drag may change what an approval executes. An AI entry after the draft leaves
    the approval exactly as prepared; mixed with a drag it is not carried silently either: the
    approval is closed with the 409 fallback instead of rendering a guess."""
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        before = _draft_hash(thread_id)
        _append_entry(thread_id, "ai_story", IDS, ["m2", "m1", "m3", "m4", "m5"])
        decision, _ = await _approve(user_id, thread_id, turn_id)
        assert decision.status == "approved" and _draft_hash(thread_id) == before
    finally:
        await async_engine.dispose()
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        await _drag(user_id, thread_id, ["m3", "m1", "m2", "m4", "m5"])
        _append_entry(
            thread_id, "ai_story", ["m3", "m1", "m2", "m4", "m5"], ["m1", "m3", "m2", "m4", "m5"]
        )
        with pytest.raises(RuntimeFailure) as refused:
            await _approve(user_id, thread_id, turn_id)
        assert refused.value.code == "clip_order_changed"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm10_a_draft_with_no_binding_cannot_carry_a_drag_so_it_falls_back_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is the old guard, never a silent render of the old order: with no brief binding
    there is no channel for the order, so the approval is closed with the refreshed card."""
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        await _drag(user_id, thread_id, ["m3", "m1", "m2", "m4", "m5"])
        with pytest.raises(RuntimeFailure) as refused:
            await _approve(user_id, thread_id, turn_id)
        assert refused.value.code == "clip_order_changed"
        closed = _events(thread_id)[-1]
        assert closed.event_type == "assistant_error"
        assert closed.payload[card_mod.CARD_KEY]["cards"][0]["media_id"] == "m3"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("round_", range(2))
async def test_fm10_an_approve_and_a_drag_racing_never_deadlock_or_lose_the_drag(
    monkeypatch: pytest.MonkeyPatch, bound: None, round_: int
) -> None:
    """Both orders of the race are legal: approve first (the video plays the order it was approved
    on, the drag then shows `editable:false`) or drag first (the video plays the drag). Never a
    deadlock, never a half-written draft."""
    user_id, thread_id = _seed()
    dragged = ["m3", "m1", "m2", "m4", "m5"]
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        seen = _capture_dispatch(monkeypatch, user_id, thread_id)
        revision = _revision(thread_id)
        set_client_protocol(CLIP_ORDER_CARD_PROTOCOL)
        approved, dragged_turn = await asyncio.gather(
            _approve(user_id, thread_id, turn_id, revision=revision),
            _drag(user_id, thread_id, dragged, revision=revision),
            return_exceptions=True,
        )
        assert not isinstance(dragged_turn, BaseException), dragged_turn
        if isinstance(approved, RuntimeFailure):
            assert approved.code == "thread_revision_stale", approved.code
            approved, _ = await _approve(user_id, thread_id, turn_id)
        assert not isinstance(approved, BaseException), approved
        approved_decision = approved[0] if isinstance(approved, tuple) else approved
        assert approved_decision.status == "approved"
        await _render(turn_id)
        recorded = [e for e in _entries(thread_id) if e.actor == "creator_drag"]
        if recorded:
            assert _dispatched_order(seen) == dragged  # the drag landed first: the video plays it
        else:
            assert _dispatched_order(seen) == []  # approved first: the order it was approved on
            assert _cards(thread_id)[-1]["editable"] is False
    finally:
        await async_engine.dispose()


# ── FM17 / FM18 / FM19 / FM20 ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fm17_a_draft_that_plays_a_subset_shows_only_those_clips_and_drags_inside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _seed()
    try:
        await _draft(
            user_id,
            thread_id,
            monkeypatch,
            protocol=CLIP_ORDER_CARD_PROTOCOL,
            selected_media_ids=["m1", "m3", "m5"],
        )
        card = _draft_event(thread_id).payload[card_mod.CARD_KEY]
        assert [c["media_id"] for c in card["cards"]] == ["m1", "m3", "m5"]
        assert [c["position"] for c in card["cards"]] == [1, 2, 3]
        # the creator drags inside what they see; the clips the edit leaves out keep their place
        await _drag(user_id, thread_id, ["m5", "m1", "m3"], proposal_id=card["proposal_id"])
        after = _cards(thread_id)[-1]
        assert [c["media_id"] for c in after["cards"]] == ["m5", "m1", "m3"]
        assert "rebased" not in after  # applied exactly as sent
        entry = _entries(thread_id)[-1]
        assert list(entry.after_ids) == ["m5", "m2", "m1", "m4", "m3"]
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["approved_render", "job_exists"])
async def test_fm18_a_drag_while_the_video_is_being_made_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The render reads the order it was approved on, and the timeline overrules a drag recorded
    after it: the card shows what plays and records nothing."""
    user_id, thread_id = _seed()
    try:
        turn_id = await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        if how == "approved_render":
            with sync_session() as db:
                approval = db.execute(
                    select(CreatorAgentApproval).where(
                        CreatorAgentApproval.turn_id == uuid.UUID(turn_id)
                    )
                ).scalar_one()
                approval.status = "approved"
                db.commit()
        else:
            with sync_session() as db:
                item = db.get(PlanItem, db.get(CreationThread, thread_id).active_plan_item_id)
                job = Job(
                    user_id=user_id,
                    status="queued",
                    mode="generative",
                    raw_storage_path="",
                    selected_platforms=["tiktok"],
                    content_plan_item_id=item.id,
                    content_plan_ownership_epoch=0,
                    assembly_plan={"variants": []},
                )
                db.add(job)
                db.flush()
                item.current_job_id = job.id
                db.commit()
        await _drag(user_id, thread_id, ["m5", "m4", "m3", "m2", "m1"])
        card = _cards(thread_id)[-1]
        assert card["editable"] is False and card["rebased"] is True
        assert [c["media_id"] for c in card["cards"]] == IDS  # what plays
        assert not _entries(thread_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("dragged", [True, False])
async def test_fm19_a_turn_planning_while_a_drag_lands_is_requeued_not_committed_on_the_old_order(
    monkeypatch: pytest.MonkeyPatch, dragged: bool
) -> None:
    """The approval guard only sees a drag after the draft. A drag during planning is caught by the
    thread-revision fence: the draft is not committed and the turn plans again on the new order."""
    user_id, thread_id = _seed()
    set_client_protocol(CLIP_ORDER_CARD_PROTOCOL)
    try:
        async with AsyncSessionLocal() as db:
            accepted, _ = await _submit(
                db,
                user_id,
                thread_id,
                SubmitTurnBody(
                    message="Make the matcha update feel quick",
                    client_event_id=f"plan-{uuid.uuid4().hex}",
                    expected_thread_revision=_revision(thread_id),
                ),
            )
        claimed = _claim(uuid.UUID(accepted.turn_id), "owner-1")
        assert isinstance(claimed, tuple)
        _snapshot, _message, lease_epoch, claimed_revision, *_rest = claimed
        if dragged:
            await _drag(user_id, thread_id, ["m3", "m1", "m2", "m4", "m5"])  # lands mid-planning
        planned = PlannedKriaTurn(
            plan=_strategy_plan(), manifest_hash="a" * 64, context_hash="b" * 64
        )
        completion = await asyncio.to_thread(
            _complete_draft_turn,
            uuid.UUID(accepted.turn_id),
            lease_owner="owner-1",
            lease_epoch=lease_epoch,
            claimed_thread_revision=claimed_revision,
            planned=planned,
            client_protocol=CLIP_ORDER_CARD_PROTOCOL,
        )
        drafts = [e for e in _events(thread_id) if e.event_type == "draft_applied"]
        if dragged:
            assert completion.committed is False
            assert completion.requeue_turn_id == accepted.turn_id
            assert not drafts
        else:  # control: with no drag in the gap the same completion commits
            assert completion.committed is True and len(drafts) == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_fm20_the_phone_reads_the_card_and_the_card_event_through_read_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id = _seed()
    try:
        await _draft(user_id, thread_id, monkeypatch, protocol=CLIP_ORDER_CARD_PROTOCOL)
        await _drag(user_id, thread_id, ["m2", "m1", "m3", "m4", "m5"], confirm=True)
        async with AsyncSessionLocal() as db:
            delta = await read_delta(
                db, thread_id=thread_id, creator_id=user_id, after_sequence=-1, limit=50
            )
        wire = json.loads(delta.model_dump_json())["events"]
        draft = next(e for e in wire if e["event_type"] == "draft_applied")
        assert draft["payload"][card_mod.CARD_KEY]["cards"][0]["media_id"] == "m1"
        card_event = next(e for e in wire if e["event_type"] == "clip_order_card")
        assert card_event["role"] == "assistant" and card_event["content"] is None
        card = card_event["payload"][card_mod.CARD_KEY]
        assert card["confirmed"] is True and card["cards"][0]["media_id"] == "m2"
        assert [c["media_id"] for c in card["cards"] if c["moved"]] in (["m1"], ["m2"])
        user_event = [e for e in wire if e["role"] == "user"][-1]
        assert user_event["content"] is None
        assert user_event["payload"][card_mod.OPS_KEY]["confirm"] is True
    finally:
        await async_engine.dispose()

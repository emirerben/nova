"""KRI-1594: the gaps #2461 left in the clip-order history (`KRIA_CLIP_ORDER_LEDGER_ENABLED`).

#2461 made the order one event-folded history every reader shares. It named five places where the
history and another reader could still disagree (KRI-503: planner, contract, receipt and the
worker's preflight must agree on the seating). This pins four of them; the fifth (the native
program, a recorded voiceover, still plays the model's `selected_media_ids` order) is left on the
issue with what it needs, because it changes what voiceover edits play.

Ways this can fail, written before the code (each is pinned below; "FM" = failure mode):

 FM1  Undo of a draft before a render leaves the undone turn's order entry in the history: the
      next plan, the snapshot pin and the AI's order line keep the order the creator undid.
 FM2  Undoing the undo (the app offers Undo again) loses the restored draft's order.
 FM3  Undo of a reorder AFTER a render leaves the reorder in the history, so "undo my reorder"
      restores the order the creator already undid.
 FM4  A removed or stopped message (`turn_cancelled`) keeps its draft's order entry; or a
      cancelled turn whose worker finishes after the cancel slips its entry in.
 FM5  "Change my request", then a new request that names no order: the old request's order is
      still pinned on the approval snapshot and the worker plays it.
 FM6  `starts_fresh` keeps the old order; or the reset also wipes the new turn's own order ask.
 FM7  A restart AFTER a render rewrites history the timeline owns (the reset fires anyway).
 FM8  The retraction rows break a reader that only replays entries (receipt, card, the drag-turn
      fold, `entries_after_draft`, `LedgerEntry.from_payload`).
 FM9  With the flag off a byte changes (the `turn_cancelled` payload, the Main Creator prompt).
 FM10 A reader that folds several chats at once lets one chat's reset or undo take back another's.
 FM11 The narrated (voiceover) worker pins first / last / groups from a row an earlier turn
      already seated, so it plays the old order the planner no longer seats.
 FM12 The narrated receipt still says a clip was seated "first" by an already-seated row.
 FM13 The order-basis question gate counts a basis order (`order_by`) or a stale row as a placed
      sequence while the render contract does not (or the reverse): a dead end.
 FM14 A creator's note that is exactly another clip's media id, file name or file stem is shown
      as a clip's name; or the guard rejects an ordinary note that merely shares a word.
 FM15 The card names a clip by the creator's group words while the AI's order line, the order
      listing and the reorder receipt call it "Clip N" (or its upload note).

Runs the REAL planner order hook (`brief_order_fields`), `plan_live_turn`'s ledger wrapper, the
render contract, approval readiness and the worker's own phone montage plan
(`preflight_montage_plan`), on the incident fixtures of `test_unstated_order_default.py` (the
Toroko thread, reconstructed). No model is called: every model answer is authored by hand.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.kria import planner as planner_module
from app.kria.brief import CreativeBrief
from app.kria.contracts import KriaToolIntent, KriaTurnPlan
from app.kria.planner import PlannedKriaTurn
from app.kria.runtime import _clip_order_retraction
from app.services import clip_order_card as card_mod
from app.services import clip_order_ledger as ledger
from app.services.choice_questions import _placed_sequence
from app.services.clip_order_ledger import (
    CREATOR_DRAG,
    CREATOR_TEXT,
    LEDGER_KEY,
    LedgerEntry,
    current_clip_order,
    fold_clip_order,
)
from app.services.creator_render_contract import build_render_contract
from app.tasks.generative_build import _narrated_alignment_record, _narrated_creator_clip_labels
from app.tasks.kria_runtime import _clip_order_payload, _with_order_receipt
from tests.kria import test_unstated_order_default as incident

ATTACHMENT = incident.ATTACHMENT
EXPECTED = incident.EXPECTED  # what the incident request seats
MAP_CLIP = incident._media_id(5)
MAP_WORDS = "haritayı gösterdiğim video"
MAP_LAST = [m for m in EXPECTED if m != MAP_CLIP] + [MAP_CLIP]


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)


# ── the thread, as events ─────────────────────────────────────────────────────────────────────


def _seated(turn: str) -> LedgerEntry:
    """Turn 1: the incident request seats its four rows on the upload order."""
    strategy = incident._drafted(incident._brief())
    entry = ledger.seated_entry(
        ATTACHMENT, strategy["resolved_clip_intents"], actor=CREATOR_TEXT, turn_id=turn
    )
    assert entry is not None and list(entry.after_ids) == EXPECTED
    return entry


def _map_last(turn: str) -> LedgerEntry:
    """Turn 2: "put the map video last" (a new row of the creator's)."""
    return LedgerEntry(CREATOR_TEXT, tuple(EXPECTED), tuple(MAP_LAST), turn, (f"last|{MAP_WORDS}",))


def _draft(draft_id: str, revision: int, turn: str, *rows: dict[str, Any]) -> tuple[str, dict]:
    """A `draft_applied` event: the draft and the ledger rows its turn wrote."""
    return (
        "assistant",
        {
            "turn_id": turn,
            "draft_id": draft_id,
            "draft_revision": revision,
            **_clip_order_payload(rows, turn),  # type: ignore[arg-type]
        },
    )


def _undo(draft_id: str, revision: int, restored_from: str) -> tuple[str, dict]:
    """A `draft_undone` event as `drafts.undo_draft` writes it."""
    return (
        "assistant",
        {"draft_id": draft_id, "draft_revision": revision, "restored_from_draft_id": restored_from},
    )


def _cancelled(turn: str) -> tuple[str, dict]:
    """A `turn_cancelled` event as `runtime.cancel_turn` writes it (the real payload helper)."""
    return ("system", {"turn_id": turn, "approval_ids": [], **_clip_order_retraction(turn)})


def _order(events: list[tuple[str, Any]]) -> list[str]:
    return list(
        current_clip_order(attachment_ids=ATTACHMENT, entries=fold_clip_order(events)).order
    )


def _turn(events: list[tuple[str, Any]], *, rendered: bool = False) -> ledger.TurnClipOrder:
    state = current_clip_order(attachment_ids=ATTACHMENT, entries=fold_clip_order(events))
    return ledger.TurnClipOrder(
        state=state, attachment_ids=tuple(ATTACHMENT), rendered=rendered, labels={}
    )


def _readers(turn: ledger.TurnClipOrder, brief: CreativeBrief, **draft: Any) -> dict[str, Any]:
    """The next turn's draft read by every reader: the planner's own order (the state it plans
    on), the render contract, approval readiness and the worker's phone montage plan."""
    token = ledger.turn_clip_order.set(turn)
    try:
        strategy = incident._drafted(brief, **draft)
    finally:
        ledger.turn_clip_order.reset(token)
    plans_on = ledger.planning_state(turn)
    snapshot = ledger.with_clip_order_ids(incident._snapshot(), plans_on)
    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot=snapshot
    )
    assert contract is not None and not contract.unresolved
    plan = incident._planned(brief, strategy, snapshot)
    from app.kria.brief_checks import plan_facts_from_unified_montage  # noqa: PLC0415

    worker = list(plan_facts_from_unified_montage(plan.record()).clip_ids)
    report = incident._report(brief, strategy, snapshot=snapshot)
    return {
        "strategy": strategy,
        "snapshot": snapshot,
        "contract": list(contract.order_ids),
        "worker": worker,
        "ready": report["ready"],
        "report": report,
        "plans_on": list(plans_on.order),
    }


def _agree(turn: ledger.TurnClipOrder, brief: CreativeBrief, **draft: Any) -> list[str]:
    """Planner, contract, readiness and the worker's preflight seat the same order (KRI-503)."""
    seen = _readers(turn, brief, **draft)
    assert seen["ready"] is True, seen["report"]["requirements"]
    if seen["contract"]:
        assert seen["contract"] == seen["worker"]
    return seen["worker"]


# ═══════════════════════════ gap 1: taking an order back ═══════════════════════════════════════


def test_fm1_undo_before_a_render_takes_the_undone_drafts_order_back_and_every_reader_agrees():
    brief = incident._brief()
    events = [_draft("d1", 1, "t1", _seated("t1").to_payload())]
    events.append(_draft("d2", 2, "t2", _map_last("t2").to_payload()))
    # control: before the undo the history holds the map-last order, and every reader seats it
    assert _order(events) == MAP_LAST
    assert _agree(_turn(events), brief) == MAP_LAST

    events.append(_undo("d3", 3, restored_from="d1"))
    turn = _turn(events)
    assert list(turn.state.order) == EXPECTED
    # the undone step is gone from the history, not stacked: "undo my reorder" now steps back
    # behind the first request, to the upload order
    assert turn.state.previous == tuple(ATTACHMENT)
    assert _agree(turn, brief) == EXPECTED


def test_fm2_undoing_the_undo_brings_the_restored_drafts_order_back():
    brief = incident._brief()
    events = [
        _draft("d1", 1, "t1", _seated("t1").to_payload()),
        _draft("d2", 2, "t2", _map_last("t2").to_payload()),
        _undo("d3", 3, restored_from="d1"),
    ]
    assert _order(events) == EXPECTED
    events.append(_undo("d4", 4, restored_from="d2"))
    assert _order(events) == MAP_LAST
    assert _agree(_turn(events), brief) == MAP_LAST


def test_fm2_undo_keeps_a_drag_made_on_the_restored_draft_and_drops_one_made_on_the_undone():
    on_d1 = LedgerEntry(CREATOR_DRAG, tuple(EXPECTED), tuple(MAP_LAST), "drag-1")
    back = LedgerEntry(CREATOR_DRAG, tuple(MAP_LAST), tuple(EXPECTED), "drag-2")
    events = [
        _draft("d1", 1, "t1", _seated("t1").to_payload()),
        ("assistant", {LEDGER_KEY: [on_d1.to_payload()]}),  # a drag on d1's card
        _draft("d2", 2, "t2"),
        ("assistant", {LEDGER_KEY: [back.to_payload()]}),  # a drag on d2's card
    ]
    assert _order(events) == EXPECTED
    events.append(_undo("d3", 3, restored_from="d1"))
    # back to the moment d1 stopped being the newest draft: its drag stands, d2's goes
    assert _order(events) == MAP_LAST


def test_fm1_undo_to_a_baseline_with_no_event_goes_back_to_before_the_undone_draft():
    events = [
        _draft("d1", 1, "t1", _seated("t1").to_payload()),
        _draft("d2", 2, "t2", _map_last("t2").to_payload()),
        _undo("d3", 3, restored_from="baseline-never-announced"),
    ]
    assert _order(events) == EXPECTED


def test_fm3_undo_of_a_reorder_after_a_render_takes_it_off_the_history():
    first = LedgerEntry(CREATOR_TEXT, tuple(ATTACHMENT), tuple(EXPECTED), "t1")
    reorder = LedgerEntry(CREATOR_TEXT, tuple(EXPECTED), tuple(MAP_LAST), "t2")
    events = [
        _draft("d1", 1, "t1", first.to_payload()),
        _draft("d2", 2, "t2", reorder.to_payload()),
    ]
    rendered = current_clip_order(
        attachment_ids=ATTACHMENT, entries=fold_clip_order(events), rendered_ids=MAP_LAST
    )
    assert rendered.previous == tuple(EXPECTED)
    events.append(_undo("d3", 3, restored_from="d1"))
    after = current_clip_order(
        attachment_ids=ATTACHMENT, entries=fold_clip_order(events), rendered_ids=EXPECTED
    )
    # the timeline is back, and "undo my reorder" now steps behind the first change, not to the
    # map-last order the creator already undid
    assert list(after.order) == EXPECTED
    assert after.previous == tuple(ATTACHMENT)
    # and the next turn sees no hand edit to record: the history and the timeline agree
    assert ledger.observed_entry(after, EXPECTED, None) is None


def test_fm4_a_removed_or_stopped_message_never_counts_wherever_its_entry_lands():
    brief = incident._brief()
    events = [
        _draft("d1", 1, "t1", _seated("t1").to_payload()),
        _draft("d2", 2, "t2", _map_last("t2").to_payload()),
        _cancelled("t2"),
    ]
    turn = _turn(events)
    assert list(turn.state.order) == EXPECTED
    assert _agree(turn, brief) == EXPECTED
    # a worker that finished the stopped turn after the cancel is taken back too
    late = [
        _draft("d1", 1, "t1", _seated("t1").to_payload()),
        _cancelled("t2"),
        _draft("d2", 2, "t2", _map_last("t2").to_payload()),
    ]
    assert _order(late) == EXPECTED


def test_fm9_a_cancel_writes_nothing_new_with_the_flag_off(monkeypatch: pytest.MonkeyPatch):
    assert _clip_order_retraction("t2") == {
        LEDGER_KEY: [{"actor": ledger.RETRACT, "turn_ids": ["t2"]}]
    }
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    assert _clip_order_retraction("t2") == {}


def _restart_turn(
    monkeypatch: pytest.MonkeyPatch,
    events: list[tuple[str, Any]],
    *,
    note: Any,
    brief: CreativeBrief,
    groups: tuple = (),
    rendered_ids: list[str] | None = None,
) -> tuple[PlannedKriaTurn, ledger.TurnClipOrder]:
    """`plan_live_turn` for one turn whose planning ``note``s a restart, as the real planner does
    (`_fresh_if_declared` / the "Change my request" context), and drafts on ``brief``."""
    item = SimpleNamespace(
        id="item",
        clip_assignments=[{"media_id": m, "kind": "video"} for m in ATTACHMENT],
        clip_gcs_paths=[],
    )
    seen: dict[str, Any] = {}

    class Db:
        async def get(self, _model: Any, _id: Any, **_kw: Any) -> Any:
            return item

        async def execute(self, _statement: Any) -> Any:
            return SimpleNamespace(all=lambda: list(events), scalar_one_or_none=lambda: None)

    async def load_target(*_a: Any, **_kw: Any) -> Any:
        if rendered_ids is None:
            return None
        return SimpleNamespace(
            snapshot={"slots": [{"media_id": m} for m in rendered_ids]}, variant={}
        )

    async def plan(_db: Any, **_kw: Any) -> PlannedKriaTurn:
        note()
        seen["turn"] = ledger.turn_clip_order.get()
        strategy = incident._drafted(brief, groups=groups)
        return PlannedKriaTurn(
            plan=KriaTurnPlan(
                mode="act",
                turn_value="action",
                intents=[
                    KriaToolIntent(
                        intent_id="apply-strategy",
                        tool_name="draft.apply_strategy",
                        tool_version=1,
                        arguments={"strategy": strategy, "summary": "x"},
                    )
                ],
            ),
            manifest_hash=None,
            context_hash=None,
            brief_updates=tuple(planner_module._fresh_if_declared(None, (), [])),
            brief_route="replan",
            brief_coverage={"stage": "draft"},
        )

    async def gate(_db: Any, planned: Any, **_kw: Any) -> Any:
        return planned

    monkeypatch.setattr(planner_module, "_load_editor_target", load_target)
    monkeypatch.setattr(planner_module, "_plan_live_turn", plan)
    monkeypatch.setattr(planner_module, "_gate_unresolved_choices", gate)
    planned = asyncio.run(
        planner_module.plan_live_turn(Db(), thread_id="t", item_id="item", creator_id="c")
    )
    return planned, seen["turn"]


def _no_order_brief() -> CreativeBrief:
    """The new request after the restart: "just keep it under a minute" (no order)."""
    brief = incident._brief()
    return brief.model_copy(
        update={"requirements": [r for r in brief.requirements if r.id == "r1"]}
    )


def _change_request() -> None:
    ledger.note_restarted(ledger.RESTART_CHANGE_REQUEST)


def _starts_fresh() -> None:
    # the real hook the Main Creator's typed declaration goes through
    planner_module._fresh_if_declared(None, (), [SimpleNamespace(starts_fresh=True)])


@pytest.mark.parametrize("note", [_change_request, _starts_fresh], ids=["change-request", "fresh"])
def test_fm5_fm6_a_restarted_request_with_no_order_plays_the_upload_order_everywhere(
    monkeypatch: pytest.MonkeyPatch, note: Any
):
    events = [_draft("d1", 1, "t1", _seated("t1").to_payload())]
    brief = _no_order_brief()
    planned, turn = _restart_turn(monkeypatch, events, note=note, brief=brief)
    assert turn.restarted
    # the turn writes the reset ahead of its own rows, and pins no old order on the approval
    assert planned.clip_order_entries[0] == ledger.reset_row()
    assert ledger.SNAPSHOT_KEY not in planned.media_snapshot
    seen = _readers(turn, brief, groups=())
    assert seen["plans_on"] == ATTACHMENT
    assert seen["worker"] == ATTACHMENT
    assert seen["ready"] is True, seen["report"]["requirements"]
    # every later reader of the history sees the reset too
    later = [*events, ("assistant", _clip_order_payload(planned.clip_order_entries, "t3"))]
    assert _order(later) == ATTACHMENT
    assert current_clip_order(attachment_ids=ATTACHMENT, entries=fold_clip_order(later)).basis == (
        "attachment_order"
    )


def test_fm5_negative_control_without_the_restart_the_old_order_is_still_pinned(
    monkeypatch: pytest.MonkeyPatch,
):
    """The bug this removes: the same new request, with no restart noted, keeps the old order."""
    events = [_draft("d1", 1, "t1", _seated("t1").to_payload())]
    brief = _no_order_brief()
    planned, turn = _restart_turn(monkeypatch, events, note=lambda: None, brief=brief)
    assert not turn.restarted
    assert planned.media_snapshot[ledger.SNAPSHOT_KEY] == EXPECTED
    assert _readers(turn, brief, groups=())["worker"] == EXPECTED


def test_fm6_a_restarted_request_that_states_an_order_seats_it_on_the_upload_order(
    monkeypatch: pytest.MonkeyPatch,
):
    events = [_draft("d1", 1, "t1", _map_last("t1").to_payload())]
    brief = incident._brief()
    planned, turn = _restart_turn(
        monkeypatch, events, note=_starts_fresh, brief=brief, groups=incident.GROUPS
    )
    entries = planned.clip_order_entries
    assert entries[0] == ledger.reset_row()
    seated = LedgerEntry.from_payload(entries[1])
    assert seated is not None and seated.actor == CREATOR_TEXT
    assert list(seated.before_ids) == ATTACHMENT and list(seated.after_ids) == EXPECTED
    # like a first request: the rows seat on the upload order, nothing older is pinned
    assert ledger.SNAPSHOT_KEY not in planned.media_snapshot
    assert _agree(turn, brief, groups=incident.GROUPS) == EXPECTED
    later = [*events, ("assistant", _clip_order_payload(entries, "t2"))]
    assert _order(later) == EXPECTED


def test_fm7_a_restart_after_a_render_leaves_the_history_to_the_timeline(
    monkeypatch: pytest.MonkeyPatch,
):
    events = [_draft("d1", 1, "t1", _seated("t1").to_payload())]
    planned, turn = _restart_turn(
        monkeypatch, events, note=_starts_fresh, brief=_no_order_brief(), rendered_ids=EXPECTED
    )
    assert turn.rendered and turn.restarted
    assert ledger.reset_row() not in planned.clip_order_entries
    assert ledger.planning_state(turn) is turn.state


def test_fm8_retraction_rows_are_invisible_to_readers_that_only_replay_entries():
    reset = ledger.reset_row()
    retract = ledger.retract_turns_row(["t2"])
    assert LedgerEntry.from_payload(reset) is None
    assert LedgerEntry.from_payload(retract) is None
    # the receipt reads the turn's last entry: a reset-only turn has no receipt to write
    planned = PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response="x"),
        manifest_hash=None,
        context_hash=None,
        clip_order_entries=(reset,),
    )
    item = SimpleNamespace(clip_assignments=[{"media_id": m} for m in ATTACHMENT])
    assert _with_order_receipt(["Reorder clips"], planned, item) == ["Reorder clips"]
    # the drag card's "what moved since the draft" reads entries only
    rows = [("draft_applied", {"draft_id": "d1"}), ("x", {LEDGER_KEY: [reset, retract]})]
    assert card_mod.entries_after_draft("d1", rows) == []


def test_fm10_one_chats_reset_never_takes_back_another_chats_entries():
    """The `creator_agent` route folds every chat of an item; each is its own history."""
    from app.routes.creator_agent import _ledger_keeps_creator_order

    drag = LedgerEntry(CREATOR_DRAG, tuple(ATTACHMENT), tuple(MAP_LAST), "drag-1")
    rows = [
        ("chat-a", "assistant", {LEDGER_KEY: [{**drag.to_payload()}]}),
        ("chat-b", "assistant", {LEDGER_KEY: [ledger.reset_row()]}),
    ]

    class Db:
        async def execute(self, _statement: Any) -> Any:
            return SimpleNamespace(all=lambda: rows)

    assert asyncio.run(_ledger_keeps_creator_order(Db(), SimpleNamespace(id="i"), "c")) is True


# ═══════════════════ gap 2: raw readers of the order rows read the seated mark ═════════════════


def _narrated_strategy(*, seated: bool) -> dict[str, Any]:
    return {
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "attribute": "the map video",
                "position": "first",
                "intent_id": "map-first",
                "assignments": [{"media_id": "map"}],
                **({"already_seated": True} if seated else {}),
            },
            {
                "op": "order",
                "status": "resolved",
                "attribute": "the friends",
                "intent_id": "friends",
                "assignments": [{"media_id": "friends"}],
                **({"already_seated": True} if seated else {}),
            },
        ]
    }


@pytest.mark.parametrize("seated", [False, True], ids=["fresh-rows", "already-seated"])
def test_fm11_fm12_the_voiceover_worker_seats_and_reports_only_rows_that_seat_now(seated: bool):
    strategy = _narrated_strategy(seated=seated)
    labels, first, last, groups = _narrated_creator_clip_labels(
        {"creator_strategy": strategy},
        {"c-map": "x/map", "c-friends": "x/friends"},
        lambda _clip_id: None,
    )
    # the creator's words still name the clips either way
    assert labels == {"c-map": "the map video", "c-friends": "the friends"}
    record = _narrated_alignment_record(
        generation="g",
        brief_version=None,
        ordering_basis="voiceover",
        steps=[
            SimpleNamespace(step_id="s1", media_id="friends", start_s=0.0, end_s=2.0),
            SimpleNamespace(step_id="s2", media_id="map", start_s=2.0, end_s=4.0),
        ],
        proxy_path_by_media_id={"map": "x/map", "friends": "x/friends"},
        strategy=strategy,
        words=[],
        caption_language=None,
        spoken_language=None,
        caption_language_request=None,
    )
    placed = {step["media_id"]: step["placed"] for step in record["steps"]}
    if seated:
        # the creator moved the clips since: the worker pins nothing and claims no seat
        assert (first, last, groups) == ([], [], [])
        assert placed == {"friends": [], "map": []}
    else:
        assert (first, last, groups) == (["c-map"], [], [["c-friends"]])
        assert placed["map"] == [{"spot": "first", "name": "the map video"}]


def test_fm13_the_question_gate_reads_placed_the_way_the_render_contract_does():
    seated = {"resolved_clip_intents": _narrated_strategy(seated=True)["resolved_clip_intents"]}
    # a row an earlier turn seated WAS placed (it only seats nothing now): no basis question
    assert _placed_sequence(seated) is True
    basis_only = {
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "attribute": "in the order I filmed them",
                "order_by": "capture_time",
                "assignments": [{"media_id": "map"}],
            }
        ]
    }
    # a basis order is not a described sequence: the contract never counts it as placed
    assert _placed_sequence(basis_only) is False
    unresolved = {
        "resolved_clip_intents": [
            {"op": "order", "status": "needs_creator", "attribute": "x", "assignments": []}
        ]
    }
    assert _placed_sequence(unresolved) is False


# ═══════════════ gap 4: a note that IS an id or a file stem is never a clip's name ═════════════


def test_fm14_a_note_equal_to_any_clips_id_or_file_stem_is_not_a_name_an_ordinary_note_is():
    rows = [
        {"media_id": "a", "user_note": "PHA-77Q2", "source_filename": "trip.MOV"},
        {"media_id": "PHA-77Q2", "user_note": "the map video", "filename": "beach_day.mov"},
        {"media_id": "c", "user_note": "Beach_Day"},  # another clip's file stem
        {"media_id": "d", "user_note": "a"},  # another clip's media id
        {"media_id": "e", "user_note": "beach day with friends"},  # shares words: still a name
    ]
    labels = ledger.labels_for(["a", "PHA-77Q2", "c", "d", "e"], rows)
    assert labels == {
        "a": "Clip 1",
        "PHA-77Q2": "the map video",
        "c": "Clip 3",
        "d": "Clip 4",
        "e": "beach day with friends",
    }
    # the same guard covers the creator's group words
    named = ledger.labels_for(["a"], rows, group_words={"a": "trip"})
    assert named == {"a": "Clip 1"}


# ═════════════ gap 5: the creator's group words reach every name the creator reads ═════════════


def _active_plan() -> dict[str, Any]:
    return {"strategy": incident._drafted(incident._brief())}


def test_fm15_the_turn_order_names_clips_by_group_words_like_the_card(
    monkeypatch: pytest.MonkeyPatch,
):
    events = [_draft("d1", 1, "t1", _seated("t1").to_payload())]
    warnings: list[str] = []
    monkeypatch.setattr(planner_module.log, "warning", lambda event, **_kw: warnings.append(event))

    class Db:
        async def execute(self, _statement: Any) -> Any:
            return SimpleNamespace(all=lambda: events, scalar_one_or_none=_active_plan)

    async def no_render(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(planner_module, "_load_editor_target", no_render)
    item = SimpleNamespace(clip_assignments=[{"media_id": m, "kind": "video"} for m in ATTACHMENT])
    turn = asyncio.run(
        planner_module._turn_clip_order(Db(), thread_id="t", item=item, editor_state=None)
    )
    assert warnings == [] and turn is not None
    assert turn.labels[MAP_CLIP] == MAP_WORDS
    # the card names the clip the same way (one reader of group words)
    card = card_mod.build_card(
        turn.state,
        item.clip_assignments,
        events=events,
        brief_version=1,
        groups=card_mod.known_group_words((), _active_plan()["strategy"]["resolved_clip_intents"]),
    )
    by_id = {c["media_id"]: c["label"] for c in card["cards"]}
    assert all(by_id[m] == turn.labels[m] for m in ATTACHMENT)
    line = ledger.order_line(turn, room_chars=4000)
    assert f"1 {MAP_WORDS}" in line


def test_fm15_the_reorder_receipt_uses_the_group_words_the_planner_read():
    entry = LedgerEntry(CREATOR_TEXT, tuple(EXPECTED), tuple(MAP_LAST), None)
    planned = PlannedKriaTurn(
        plan=KriaTurnPlan(mode="respond", turn_value="question", response="x"),
        manifest_hash=None,
        context_hash=None,
        clip_order_entries=(entry.to_payload(),),
        clip_order_groups={MAP_CLIP: "the map video"},
    )
    item = SimpleNamespace(clip_assignments=[{"media_id": m} for m in ATTACHMENT])
    [line] = _with_order_receipt(["Reorder clips"], planned, item)
    assert line == "Moved “the map video” to the end."


def test_fm15_the_newest_card_keeps_a_group_name_a_later_strategy_does_not_restate():
    card = {"cards": [{"media_id": "a", "group": "the map video"}, {"media_id": "b"}]}
    events = [("assistant", {card_mod.CARD_KEY: card})]
    words = card_mod.known_group_words(events, [])
    assert words == {"a": "the map video"}
    strategy = [
        {"op": "order", "attribute": "map", "assignments": [{"media_id": "a"}]},
    ]
    assert card_mod.known_group_words(events, strategy) == {"a": "map"}  # the newest words win


def test_fm1_undo_of_a_draft_with_no_event_that_restores_the_newest_draft_keeps_its_order():
    """The app saved a draft with no event of its own (`write_draft`); undoing it restores the
    draft that is still the newest one on record, so nothing is taken back."""
    events = [
        _draft("d1", 1, "t1", _seated("t1").to_payload()),
        _undo("d3", 3, restored_from="d1"),  # undid the app's d2, which never had an event
    ]
    assert _order(events) == EXPECTED


def test_fm5_a_held_restart_carries_only_its_reset_row():
    from app.tasks.kria_runtime import _retraction_rows

    seated = _seated("t1").to_payload()
    assert _retraction_rows([ledger.reset_row(), seated]) == (ledger.reset_row(),)
    assert _retraction_rows([seated]) == ()

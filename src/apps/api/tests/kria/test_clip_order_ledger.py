"""The clip order is ONE live, AI-aware state (`KRIA_CLIP_ORDER_LEDGER_ENABLED`, PR 2).

Prod threads 666b996f and de471354 (Toroko, 19 clips; groups ordered, the inner order unstated)
started this. PR 1 stopped the hold (`test_unstated_order_default.py`). The rules this adds, from
the owner: anything the creator specifies is honoured exactly; what they did not specify is the
order they added the clips; a creator's drag or message is the NEWEST statement, never an immutable
lock; the AI never reorders on its own; no visible string ever shows a media id, UUID or file name.

Ways this can fail, written before the code (each is pinned below; "FM" = failure mode):

 FM1  the incident shape is held or loses its PR 1 disclosure with the ledger on or off.
 FM2  the flag off changes a byte: Main Creator prompt, ops, strategy dumps, receipts.
 FM3  a hand reorder, then "swap the first two": the swap is applied to the ORIGINAL order.
 FM4  an unrelated title/music edit after a reorder moves a clip, or the model's unasked reorder
      op survives (negative control: a recorded model output that reorders during a title edit
      must leave the order equal, siblings kept).
 FM5  two quick reorders and a chat reorder: the last one does not win in sequence.
 FM6  a chat reorder while an approval is pending leaves the old card live / a dead end.
 FM7  "what order are my clips in?" lists numbers with no names, or leaks an id.
 FM8  a reorder INSIDE a group the creator named is refused, or the old row undoes it.
 FM9  "undo my reorder" restores the wrong step, or the whole history at once.
 FM10 an op naming a clip that was removed discards its siblings instead of being declined alone.
 FM11 a manual editor reorder then a re-plan: the re-plan puts the old order back.
 FM12 a clip added after a reorder is lost, or lands mid-group with no disclosure.
 FM13 the model emits an unasked reorder and it is applied.
 FM14 "clip 2" means upload position instead of play position, or is shown without a name.
 FM15 an old order row is re-applied every turn (two-turn replay) and undoes the newer order.
 FM16 a custom order that omits clips is rejected instead of completed.
 FM17 the planner, the render contract, readiness and the worker's own plan disagree on the
      latest order (the KRI-503 dead end). Oracle: `preflight_montage_plan`.
 FM18 a UUID / media id / file name reaches any creator-visible string.
 FM19 the receipt exists in English only; Turkish or German reads differently or is missing.

Runs the REAL planner order hook (`brief_order_fields`), the render contract, approval readiness
and the worker's own phone montage plan (`preflight_montage_plan`), plus the editor-op compile.
No model is called: the copilot's and the extractor's answers are replayed, authored by hand.

Recorded vs reconstructed: the request (Turkish) and the shape of the outcome are recorded from
the two prod threads; everything else (19 neutral clips, their scrambled capture times, which
clips each group holds, every later message and the model answers a follow-up would get) is
RECONSTRUCTED, reusing the fixtures of `test_unstated_order_default.py`. Whether a live copilot
emits the ops these replays hand it is unmeasured (no paid call was made).
"""

from __future__ import annotations

import copy
import re
from copy import deepcopy
from typing import Any

import pytest

from app.agents.edit_copilot import _ParseState, parse_editor_operation
from app.agents.main_creator import MainCreatorAgent
from app.config import settings
from app.kria.brief import BriefUpdate, CurrentPlanShape, route_requirements
from app.kria.brief_checks import plan_facts_from_unified_montage
from app.kria.contracts import KriaTurnPlan
from app.kria.planner import (
    PlannedKriaTurn,
    _scrubbed_reply,
    _with_clip_order_entries,
    _without_unasked_reorders,
    adapt_editor_action,
    brief_order_fields,
)
from app.kria.reply_language import reply_language_for
from app.services import clip_order_ledger as ledger
from app.services import kria_editor_ops as editor
from app.services.clip_order_ledger import (
    AI_STORY,
    CREATOR_DRAG,
    CREATOR_EDITOR,
    CREATOR_TEXT,
    LEDGER_KEY,
    LedgerEntry,
    apply_order,
    current_clip_order,
    fold_clip_order,
    moved_ids,
)
from app.services.clip_order_sequence import apply_sequence, sequence_rows
from app.services.creator_render_contract import build_render_contract
from app.services.kria_editor_ops import KriaEditorOpError
from app.tasks.kria_runtime import (
    _clip_order_payload,
    _with_editor_order_entry,
    _with_order_receipt,
)
from tests.agents.test_main_creator_agent import _input as _creator_input
from tests.kria import test_unstated_order_default as incident
from tests.services.test_kria_editor_ops import _job, _variant

A, B, C, D, E = "a", "b", "c", "d", "e"
ATTACH = [A, B, C, D, E]

_ID_LEAK = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|analysis-proxy|asset-|legacy-clip|\.mp4|\.mov|IMG_",
    re.IGNORECASE,
)


def _entry(actor: str, before: list[str], after: list[str], turn: str = "t") -> LedgerEntry:
    return LedgerEntry(actor, tuple(before), tuple(after), turn)


def _events(*entries: LedgerEntry) -> list[tuple[str, dict[str, Any]]]:
    return [("assistant", {LEDGER_KEY: [e.to_payload()]}) for e in entries]


# -- the ledger folds from the thread's own events -----------------------------------------------


def test_the_ledger_is_folded_from_assistant_events_and_ignores_everything_else() -> None:
    entry = _entry(CREATOR_TEXT, ATTACH, [C, A, B, D, E])
    events = [
        ("user", {LEDGER_KEY: [entry.to_payload()]}),  # a creator event can never write the ledger
        ("assistant", {LEDGER_KEY: "not a list"}),
        ("assistant", {LEDGER_KEY: [{"actor": "bogus", "before_ids": [], "after_ids": [A]}]}),
        ("assistant", {LEDGER_KEY: [{"actor": CREATOR_TEXT, "after_ids": [A, B]}]}),  # no before
        (
            "assistant",
            {LEDGER_KEY: [{"actor": CREATOR_TEXT, "before_ids": [], "after_ids": [A, A, B]}]},
        ),
        ("assistant", None),
        *_events(entry),
    ]
    folded = fold_clip_order(events)
    assert [e.after_ids for e in folded] == [(A, B), (C, A, B, D, E)]
    assert folded[-1] == entry


# -- FM3 / FM5 / FM9: newest statement wins, one step per undo ------------------------------------


def test_fm5_two_quick_reorders_and_a_chat_reorder_apply_in_sequence_last_wins() -> None:
    first = _entry(CREATOR_EDITOR, ATTACH, [B, A, C, D, E], "t1")
    second = _entry(CREATOR_DRAG, [B, A, C, D, E], [B, A, E, C, D], "t2")
    third = _entry(CREATOR_TEXT, [B, A, E, C, D], [D, B, A, E, C], "t3")
    state = current_clip_order(attachment_ids=ATTACH, entries=[first, second, third])
    assert list(state.order) == [D, B, A, E, C]
    assert state.basis == "confirmed"
    # per-card source = the actor of the LAST entry that moved that card
    assert state.source[E] == CREATOR_DRAG and state.source[D] == CREATOR_TEXT
    # a swap of two neighbours moves one card (which one is the rule's choice, not the creator's)
    assert {state.source.get(A), state.source.get(B)} & {CREATOR_EDITOR}
    assert state.source_of(C) == "server_default"  # C was passed, never moved


def test_fm9_each_undo_is_one_step_of_the_history_not_all_of_it() -> None:
    entries = [
        _entry(CREATOR_TEXT, ATTACH, [C, A, B, D, E], "t1"),
        _entry(CREATOR_TEXT, [C, A, B, D, E], [C, A, E, B, D], "t2"),
    ]
    state = current_clip_order(attachment_ids=ATTACH, entries=entries)
    assert list(state.order) == [C, A, E, B, D]
    assert state.previous == (C, A, B, D, E)  # one step back, not the upload order
    # the undo is itself an entry, so the next undo steps back again
    undone = entries + [_entry(CREATOR_TEXT, list(state.order), list(state.previous), "t3")]
    after = current_clip_order(attachment_ids=ATTACH, entries=undone)
    assert list(after.order) == [C, A, B, D, E]
    # the undo took a step off the history: the next undo goes back to the upload order
    assert after.previous == tuple(ATTACH)
    again = undone + [_entry(CREATOR_TEXT, list(after.order), ATTACH, "t4")]
    assert list(current_clip_order(attachment_ids=ATTACH, entries=again).order) == ATTACH
    assert current_clip_order(attachment_ids=ATTACH, entries=again).previous is None


def test_an_untouched_order_is_the_upload_order_and_says_so() -> None:
    state = current_clip_order(attachment_ids=ATTACH)
    assert list(state.order) == ATTACH and state.basis == "attachment_order"
    server = current_clip_order(
        attachment_ids=ATTACH, entries=[_entry("server_default", ATTACH, ATTACH)]
    )
    assert server.basis == "attachment_order"
    ai = current_clip_order(
        attachment_ids=ATTACH, entries=[_entry(AI_STORY, ATTACH, [B, A, C, D, E])]
    )
    assert list(ai.order) == [B, A, C, D, E] and ai.basis == "attachment_order"


def test_after_a_render_the_timeline_is_the_order_and_a_split_take_counts_once() -> None:
    state = current_clip_order(
        attachment_ids=ATTACH,
        entries=[_entry(CREATOR_TEXT, ATTACH, [C, A, B, D, E])],
        rendered_ids=ledger.first_appearance([D, "", None, D, A, B, A]),
    )
    # the timeline wins; clips it does not show (C, E) are still the creator's, in the order
    # the ledger last put them
    assert list(state.order) == [D, A, B, C, E]


def test_fm11_a_manual_editor_reorder_is_recorded_so_undo_can_restore_what_it_replaced() -> None:
    state = current_clip_order(attachment_ids=ATTACH)
    seen = ledger.observed_entry(state, [C, A, B, D, E], "t9")
    assert seen == _entry(CREATOR_EDITOR, ATTACH, [C, A, B, D, E], "t9")
    # nothing moved, or a subset of the clips shown in the same relative order: no entry
    assert ledger.observed_entry(state, ATTACH, "t9") is None
    assert ledger.observed_entry(state, [A, C, E], "t9") is None


# -- FM16 / FM12 / item 9: partial and stale orders complete, never reject ------------------------


def test_fm16_a_partial_custom_order_is_completed_named_clips_lead_the_rest_keep_order() -> None:
    assert apply_order(ATTACH, [C]) == [C, A, B, D, E]
    assert apply_order(ATTACH, [E, A, "gone"]) == [E, A, B, C, D]
    assert apply_order(ATTACH, []) == ATTACH
    assert apply_order(ATTACH, [C, C, A]) == [C, A, B, D, E]


def test_fm12_a_stale_full_bundle_rebases_a_clip_added_since_keeps_its_place() -> None:
    # the bundle was written when the edit held A..D; X was added since and sits in the middle
    current = [A, B, "x", C, D]
    assert apply_order(current, [D, C, B, A], known=[A, B, C, D]) == [D, C, "x", B, A]
    # a removed clip is dropped, the rest take the requested sequence
    assert apply_order([A, C, D], [D, C, B, A], known=[A, B, C, D]) == [D, C, A]


def test_moved_cards_are_the_minimal_set_not_every_clip_that_shifted() -> None:
    assert moved_ids(ATTACH, [C, A, B, D, E]) == (C,)
    assert moved_ids(ATTACH, [B, A, C, D, E]) in {(A,), (B,)}
    assert moved_ids(ATTACH, ATTACH) == ()


# -- FM14 / FM18: names, never ids ---------------------------------------------------------------


def test_fm14_a_clip_is_named_by_group_words_then_note_then_record_then_its_play_position() -> None:
    label = ledger.clip_label
    assert (
        label(position=2, group_words="map video", note="note", description="stadium")
        == "map video"
    )
    assert label(position=2, note="Sarah's dance", description="stadium") == "Sarah's dance"
    assert label(position=2, description="football stadium crowd") == "football stadium crowd"
    assert label(position=2) == "Clip 2"
    # a candidate that would show an id or a file name is skipped, never shown
    unsafe = "analysis-proxy-ios-5C6E0D6A-B7C3-5C1E-A4D1-1F2B3C4D5E6F.mp4"
    assert (
        label(position=3, group_words=unsafe, note="IMG_7382.MOV", description="beach") == "beach"
    )
    assert label(position=3, group_words=unsafe, note="IMG_7382") == "Clip 3"


def test_fm14_a_number_is_always_paired_with_a_name() -> None:
    assert ledger.clip_phrase(2, "swing video") == "your clip 2, “swing video”"
    assert ledger.clip_phrase(2, "Clip 2") == "your clip 2"
    assert ledger.clip_phrase(11, "swing video") == "your clip 11, “swing video”"
    with reply_language_for("tr"):
        assert ledger.clip_phrase(2, "swing video") == "2. klibin, “swing video”"


def test_fm18_scrub_replaces_every_id_a_model_could_echo() -> None:
    labels = {"analysis-proxy-ios-ABC.mp4": "map video", "asset-7": "Clip 2"}
    text = (
        "Moved analysis-proxy-ios-ABC.mp4 before asset-7, see "
        "5c6e0d6a-b7c3-5c1e-a4d1-1f2b3c4d5e6f and IMG_7382 and beach.mov."
    )
    scrubbed = ledger.scrub_ids(text, labels)
    assert not _ID_LEAK.search(scrubbed), scrubbed
    assert "“map video”" in scrubbed and "clip 2" in scrubbed


# -- FM7 / FM19: receipts and the listing, from before/after ids ---------------------------------

LABELS = {A: "map", B: "entry", C: "swing video", D: "Clip 4", E: "scenery"}


def test_fm19_the_receipt_reads_in_every_language_and_names_the_clips() -> None:
    before, after = ATTACH, [A, C, B, D, E]
    en = ledger.reorder_receipt(before, after, LABELS)
    assert en == "Moved “swing video” before “entry”."
    with reply_language_for("tr"):
        tr = ledger.reorder_receipt(before, after, LABELS)
    assert tr == "“swing video” klibini “entry” klibinin önüne aldım."
    assert (
        ledger.reorder_receipt(before, [C, A, B, D, E], LABELS)
        == "Moved “swing video” to the start."
    )
    assert (
        ledger.reorder_receipt(before, [A, B, D, E, C], LABELS) == "Moved “swing video” to the end."
    )
    assert ledger.reorder_receipt(before, before, LABELS) == "The clips are already in that order."
    for text in (en, tr):
        assert not _ID_LEAK.search(text)


def test_fm7_the_listing_pairs_every_position_with_a_name_and_never_an_id() -> None:
    listing = ledger.order_listing([C, A, B, D, E], LABELS)
    assert listing.splitlines() == [
        "Your clips play in this order:",
        "1. swing video",
        "2. map",
        "3. entry",
        "4. Clip 4",
        "5. scenery",
    ]
    assert not _ID_LEAK.search(listing)


def test_the_ledger_payload_round_trips_and_a_copy_is_independent() -> None:
    entry = LedgerEntry(CREATOR_TEXT, (A, B), (B, A), "t", ("first|map",))
    assert LedgerEntry.from_payload(copy.deepcopy(entry.to_payload())) == entry
    assert "rows" not in _entry(CREATOR_TEXT, [A], [A]).to_payload()
    assert ledger.row_fingerprint("first", "  The  MAP ") == "first|the map"
    assert ledger.row_fingerprint(None, "x") == "then|x"


# ═══════════════════════════ the incident conversation, replayed ═══════════════════════════════
# One conversation: the incident prompt, then the creator moves the map clip by hand, then a
# re-plan for something unrelated. Real planner order hook, render contract, approval readiness
# and the worker's own phone montage plan (`preflight_montage_plan`); no model.


ATTACHMENT = incident.ATTACHMENT
EXPECTED = incident.EXPECTED
MAP_CLIP = incident._media_id(5)


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)


@pytest.fixture
def ledger_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)


def _turn(entries: list[LedgerEntry]) -> ledger.TurnClipOrder:
    state = current_clip_order(attachment_ids=ATTACHMENT, entries=entries)
    return ledger.TurnClipOrder(
        state=state, attachment_ids=tuple(ATTACHMENT), rendered=False, labels={}
    )


def _drafted_on(turn: ledger.TurnClipOrder | None, brief) -> dict[str, Any]:
    """The strategy as the planner writes it for a turn that starts from ``turn``'s order."""
    token = ledger.turn_clip_order.set(turn)
    try:
        return incident._drafted(brief)
    finally:
        ledger.turn_clip_order.reset(token)


def _snapshot_for(turn: ledger.TurnClipOrder | None) -> dict[str, Any]:
    snapshot = incident._snapshot()
    return ledger.with_clip_order_ids(snapshot, turn.state) if turn else snapshot


def _worker_order(brief, strategy, snapshot) -> list[str]:
    plan = incident._planned(brief, strategy, snapshot)
    return list(plan_facts_from_unified_montage(plan.record()).clip_ids)


def _first_turn(brief):
    strategy = _drafted_on(None, brief)
    entry = ledger.seated_entry(
        ATTACHMENT, strategy["resolved_clip_intents"], actor=CREATOR_TEXT, turn_id="t1"
    )
    return strategy, entry


def test_fm1_the_incident_shape_stays_approvable_and_seats_one_ledger_entry(
    ledger_on: None,
) -> None:
    brief = incident._brief()
    strategy, entry = _first_turn(brief)
    assert entry is not None and list(entry.after_ids) == EXPECTED
    assert entry.actor == CREATOR_TEXT and len(entry.rows) == 4
    report = incident._report(brief, strategy)
    assert report["ready"] is True, report["requirements"]
    # the first turn has no earlier order: the carrier is empty-handed, nothing is marked
    assert not any(i.get("already_seated") for i in strategy["resolved_clip_intents"])


def _moved_map_to_the_end() -> list[str]:
    return [m for m in EXPECTED if m != MAP_CLIP] + [MAP_CLIP]


def test_fm11_fm15_a_hand_reorder_survives_a_replan_and_every_reader_agrees(
    ledger_on: None,
) -> None:
    """The creator dragged the map clip (stated FIRST in turn 1) to the end. A later re-plan with
    no order statement of its own must not seat that row again."""
    brief = incident._brief()
    _strategy1, seated = _first_turn(brief)
    dragged = _entry(CREATOR_EDITOR, list(seated.after_ids), _moved_map_to_the_end(), "t2")
    turn = _turn([seated, dragged])
    assert list(turn.state.order) == _moved_map_to_the_end()

    strategy = _drafted_on(turn, brief)
    marks = {i["intent_id"]: i.get("already_seated") for i in strategy["resolved_clip_intents"]}
    assert marks["order_map_first"] is True  # the moved row; the others still agree with the order
    snapshot = _snapshot_for(turn)

    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot=snapshot
    )
    assert contract is not None and not contract.unresolved
    worker = _worker_order(brief, strategy, snapshot)
    assert list(contract.order_ids) == worker == list(turn.state.order)
    assert worker[-1] == MAP_CLIP

    report = incident._report(brief, strategy, snapshot=snapshot)
    assert report["ready"] is True, report["requirements"]
    assert "in the order you have them now" in incident._rows(report)["r2"]["reason"]


def test_fm15_without_the_ledger_the_old_row_undoes_the_hand_reorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: the bug this removes. Flag off, the same re-plan seats the map first."""
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    brief = incident._brief()
    strategy = incident._drafted(brief)
    assert _worker_order(brief, strategy, incident._snapshot())[0] == MAP_CLIP


# ═════════════════════════ the copilot's side: ops on the CURRENT timeline ═════════════════════
# Real parser (`parse_editor_operation`), real compile (`compile_editor_ops`), real per-op split
# (`validate_ops_per_op`). The ops the "model" returns are authored; whether a live copilot emits
# them for these sentences is unmeasured.


M = [f"m{n}" for n in range(1, 6)]


def _timeline(order: list[int] | None = None, *, extra: bool = False) -> dict[str, Any]:
    """A rendered montage of clips m1..m5 (one slot each) in ``order`` (indexes into M)."""
    variant = _variant()
    template = variant["ai_timeline"]["slots"][0]
    rows = []
    for n in order if order is not None else range(5):
        rows.append(
            {
                **deepcopy(template),
                "slot_id": f"slot-{n + 1}",
                "clip_index": 0,
                "media_id": M[n],
                "duration_s": 2.0,
                "source_duration_s": 8.0,
            }
        )
    if extra:
        rows.insert(2, {**deepcopy(template), "slot_id": "slot-new", "media_id": "m-new"})
    variant["ai_timeline"]["slots"] = rows
    return variant


def _media_order(variant: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """The clips' media ids in the compiled draft's timeline order (slot ids map back to media)."""
    media = {s["slot_id"]: s["media_id"] for s in variant["ai_timeline"]["slots"]}
    # a draft that left the timeline alone carries no timeline lane at all
    slots = payload.get("timeline_slots") or variant["ai_timeline"]["slots"]
    return [media[s["slot_id"]] for s in slots if not s.get("removed")]


@pytest.fixture
def timeline_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        editor,
        "_editor_capabilities",
        lambda *a: {"text_elements": True, "timeline": True, "clips": {}},
    )


def _compile(variant: dict[str, Any], *raw_ops: dict[str, Any]) -> list[str]:
    snapshot = editor.build_editor_snapshot(_job(variant), variant)
    snapshot["editor_ops_version"] = 2
    parsed = []
    for raw in raw_ops:
        op = parse_editor_operation(raw, snapshot, _ParseState(0.99))
        assert op is not None, raw
        parsed.append(op)
    draft = editor.compile_editor_ops(_job(variant), variant, parsed)
    return _media_order(variant, draft.payload.model_dump(mode="json", exclude_none=True))


def test_fm3_a_swap_after_a_hand_reorder_is_a_swap_of_the_order_the_creator_sees(
    timeline_open: None,
) -> None:
    """The creator dragged m3 to the front (the saved timeline is m3 m1 m2 m4 m5). "Swap the first
    two" is read against that snapshot, so m3 and m1 trade places: not m1 and m2."""
    dragged = _timeline([2, 0, 1, 3, 4])
    snapshot = editor.build_editor_snapshot(_job(dragged), dragged)
    assert [s["media_id"] for s in snapshot["slots"]] == ["m3", "m1", "m2", "m4", "m5"]
    order = _compile(dragged, {"op": "reorder_clip", "from_index": 1, "to_index": 0})
    assert order == ["m1", "m3", "m2", "m4", "m5"]


def test_fm16_a_custom_order_that_omits_clips_is_completed_with_the_ledger(
    timeline_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _timeline()
    partial = {"op": "reorder_clips_by", "criterion": "custom", "order": [3]}
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    assert _compile(base, partial) == ["m4", "m1", "m2", "m3", "m5"]
    # and an index that names no clip is dropped, the rest completed, nothing raised
    assert _compile(base, {**partial, "order": [4, 9, 0]}) == ["m5", "m1", "m2", "m3", "m4"]


def test_fm16_without_the_ledger_the_partial_order_is_still_refused(
    timeline_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    base = _timeline()
    snapshot = editor.build_editor_snapshot(_job(base), base)
    snapshot["editor_ops_version"] = 2
    state = _ParseState(0.99)
    op = parse_editor_operation(
        {"op": "reorder_clips_by", "criterion": "custom", "order": [3]}, snapshot, state
    )
    assert op is None and state.invalid_values >= 1


def test_fm12_a_bundle_written_before_a_clip_was_added_rebases_and_keeps_the_new_clip(
    timeline_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reorder was planned against m1..m5; a clip was added since. Flag off that is the
    "timeline changed" dead end; on, the named clips take the sequence and the newcomer keeps
    its slot."""
    planned_on = _timeline()
    snapshot = editor.build_editor_snapshot(_job(planned_on), planned_on)
    snapshot["editor_ops_version"] = 2
    op = parse_editor_operation(
        {"op": "reorder_clips_by", "criterion": "custom", "order": [4, 3, 2, 1, 0]},
        snapshot,
        _ParseState(0.99),
    )
    assert op is not None
    later = _timeline(extra=True)  # m1 m2 NEW m3 m4 m5
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    with pytest.raises(KriaEditorOpError, match="timeline changed"):
        editor.compile_editor_ops(_job(later), later, [deepcopy(op)])
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    draft = editor.compile_editor_ops(_job(later), later, [deepcopy(op)])
    order = _media_order(later, draft.payload.model_dump(mode="json", exclude_none=True))
    assert order == ["m5", "m4", "m-new", "m3", "m2", "m1"]


def test_fm10_a_part_naming_a_clip_that_is_gone_is_declined_alone_siblings_applied(
    timeline_open: None,
) -> None:
    base = _timeline()
    ok = {"op": "edit_text", "bar_index": 0, "text": "New hook"}
    gone = {"op": "reorder_clip", "from_index": 9, "to_index": 0}
    kept, refused = editor.validate_ops_per_op(_job(base), base, [ok, gone])
    assert kept == [ok]
    assert [op for op, _why in refused] == [gone]


# ═══════════════════════ the AI never reorders on its own (FM4 / FM13) ═════════════════════════


def _parsed(variant: dict[str, Any], *raw_ops: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot = editor.build_editor_snapshot(_job(variant), variant)
    snapshot["editor_ops_version"] = 2
    out = []
    for raw in raw_ops:
        op = parse_editor_operation(raw, snapshot, _ParseState(0.99))
        assert op is not None, raw
        out.append(op)
    return out


def _editor_turn(ops: list[dict[str, Any]], *updates: BriefUpdate, deferred: bool = False):
    plan = adapt_editor_action(reply="Updated your video.", ops=ops)
    return PlannedKriaTurn(
        plan=plan,
        manifest_hash=None,
        context_hash=None,
        brief_updates=tuple(updates),
        brief_route="editor_ops",
        defer_brief=deferred,
    )


TITLE_ASK = BriefUpdate(
    operation="add", kind="text", scope="title", literal="New hook", description="a new title"
)
ORDER_ASK = BriefUpdate(
    operation="add", kind="order", scope="global", description="put the last clip first"
)
TITLE_OP = {"op": "edit_text", "bar_index": 0, "text": "New hook"}
SHUFFLE_OP = {"op": "reorder_clips_by", "criterion": "custom", "order": [4, 3, 2, 1, 0]}


def _ops_of(planned: PlannedKriaTurn) -> list[dict[str, Any]]:
    return planned.plan.intents[0].arguments["operations"]


def test_fm4_fm13_a_reorder_the_model_added_to_a_title_edit_is_stripped_siblings_kept(
    timeline_open: None, ledger_on: None
) -> None:
    base = _timeline()
    ops = _parsed(base, TITLE_OP, SHUFFLE_OP)
    # control: both ops applied (what the model's output does today) reverses the clips
    assert _compile(base, TITLE_OP, SHUFFLE_OP) == ["m5", "m4", "m3", "m2", "m1"]

    stripped = _without_unasked_reorders(_editor_turn(ops, TITLE_ASK))
    kept = _ops_of(stripped)
    assert [op["op"] for op in kept] == ["edit_text"]
    assert "I left your clip order as it was." in stripped.plan.intents[0].arguments["notes"]
    draft = editor.compile_editor_ops(_job(base), base, kept)
    payload = draft.payload.model_dump(mode="json", exclude_none=True)
    assert _media_order(base, payload) == M  # the order is equal
    assert payload["text_elements"][0]["text"] == "New hook"  # and the sibling was applied


def test_fm13_an_asked_reorder_is_kept_whole(timeline_open: None, ledger_on: None) -> None:
    ops = _parsed(_timeline(), TITLE_OP, SHUFFLE_OP)
    for ask in (
        ORDER_ASK,
        BriefUpdate(operation="add", kind="select", scope="global", description="x"),
    ):
        kept = _without_unasked_reorders(_editor_turn(ops, TITLE_ASK, ask))
        assert [op["op"] for op in _ops_of(kept)] == ["edit_text", "reorder_clips_by"]


def test_fm13_only_reorders_are_stripped_into_a_plain_reply_never_an_error(
    timeline_open: None, ledger_on: None
) -> None:
    ops = _parsed(_timeline(), SHUFFLE_OP)
    answered = _without_unasked_reorders(_editor_turn(ops, TITLE_ASK))
    assert answered.plan.mode == "respond"
    assert answered.plan.response == "I left your clip order as it was."


def test_fm2_the_flag_off_strip_is_not_even_called_and_a_deferred_brief_is_untouched(
    timeline_open: None, ledger_on: None
) -> None:
    ops = _parsed(_timeline(), TITLE_OP, SHUFFLE_OP)
    deferred = _editor_turn(ops, deferred=True)
    assert _without_unasked_reorders(deferred) is deferred  # the brief is not read yet


def test_fm18_a_model_reply_that_echoes_an_id_is_scrubbed_with_the_clips_names() -> None:
    turn = ledger.TurnClipOrder(
        state=current_clip_order(attachment_ids=["asset-7", "asset-8"]),
        attachment_ids=("asset-7", "asset-8"),
        rendered=True,
        labels={"asset-7": "map", "asset-8": "Clip 2"},
    )
    from app.kria.contracts import KriaTurnPlan

    plan = KriaTurnPlan(
        mode="respond",
        turn_value="question",
        response="asset-7 plays first, then 5c6e0d6a-b7c3-5c1e-a4d1-1f2b3c4d5e6f (IMG_7382.MOV).",
    )
    planned = _scrubbed_reply(
        PlannedKriaTurn(plan=plan, manifest_hash=None, context_hash=None), turn
    )
    assert not _ID_LEAK.search(planned.plan.response), planned.plan.response
    assert planned.plan.response.startswith("“map” plays first")


# ═════════════════════════════ what the Main Creator is shown (FM2, FM14) ═════════════════════


def _prompt_of(agent_input) -> str:
    return MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]


def test_fm2_a_thread_with_no_ledger_entry_gets_a_byte_identical_prompt() -> None:
    base = _creator_input()
    ids = [m.media_id for m in base.capability_manifest.media]
    untouched = ledger.TurnClipOrder(
        state=current_clip_order(attachment_ids=ids),
        attachment_ids=tuple(ids),
        rendered=False,
        labels={},
    )
    assert ledger.prompt_view(untouched, base.media_context, base.item_context) is None
    assert ledger.prompt_view(None, base.media_context, base.item_context) is None


def test_fm14_the_model_sees_play_order_with_one_line_saying_which_clip_is_which() -> None:
    base = _creator_input()
    ids = [m.media_id for m in base.capability_manifest.media]
    assert len(ids) >= 2
    moved = [ids[-1], *ids[:-1]]
    entry = _entry(CREATOR_TEXT, ids, moved)
    state = current_clip_order(attachment_ids=ids, entries=[entry])
    turn = ledger.TurnClipOrder(
        state=state,
        attachment_ids=tuple(ids),
        rendered=False,
        labels=ledger.labels_for(state.order, [], group_words={ids[-1]: "map video"}),
    )
    view = ledger.prompt_view(turn, base.media_context, "Item: a trip")
    assert view is not None
    media_context, item_context = view
    assert [row["media_id"] for row in media_context] == moved
    lines = item_context.splitlines()
    assert lines[0] == "Item: a trip" and len(lines) == 2
    assert lines[1].startswith("Clips in the order they play now: 1 map video (you asked);")
    assert not _ID_LEAK.search(item_context)  # fixture ids are plain; the line carries names
    # the prompt TEMPLATE is untouched: only the two inputs differ, and the manifest hash fence
    # is not reordered here
    after = base.model_copy(update={"media_context": media_context, "item_context": item_context})
    assert _prompt_of(after) != _prompt_of(base)
    assert _prompt_of(after).count("Clips in the order they play now") == 1
    assert after.capability_manifest.manifest_hash == base.capability_manifest.manifest_hash


def test_the_order_line_never_overflows_the_item_context_bound() -> None:
    ids = [f"m{n}" for n in range(60)]
    state = current_clip_order(
        attachment_ids=ids, entries=[_entry(CREATOR_TEXT, ids, [ids[-1], *ids[:-1]])]
    )
    labels = {i: f"a rather long descriptive clip name that goes on and on for {i}" for i in ids}
    turn = ledger.TurnClipOrder(state, tuple(ids), False, labels)
    view = ledger.prompt_view(turn, [{"media_id": i} for i in ids], "x" * 3800)
    assert view is not None and len(view[1]) <= 4000
    full = ledger.prompt_view(turn, [{"media_id": i} for i in ids], "")
    assert full is not None and "more." in full[1] and len(full[1]) <= 4000


def test_fm14_clip_n_in_a_name_is_its_play_position_not_its_upload_position() -> None:
    rows = [{"media_id": i} for i in ATTACH]
    labels = ledger.labels_for([C, A, B, D, E], rows)
    assert labels[C] == "Clip 1" and labels[A] == "Clip 2" and labels[E] == "Clip 5"
    named = ledger.labels_for([C, A, B, D, E], rows, group_words={A: "map video"})
    assert named[A] == "map video" and named[C] == "Clip 1"


# ═══════════════════════════════ routing: a move is an editor op ═══════════════════════════════


_SHAPE = CurrentPlanShape(
    has_render=True, can_edit_timeline=True, can_edit_text=True, can_edit_captions=True
)


def _order_req(**facts: Any) -> BriefUpdate:
    return BriefUpdate(
        operation="add", kind="order", scope="global", description="put the map first", facts=facts
    )


@pytest.mark.parametrize("facts", [{"key": "custom"}, {"key": "swap"}, {}])
def test_fm3_a_rendered_edit_serves_any_move_as_an_editor_op_with_the_ledger(
    facts: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    title = BriefUpdate(
        operation="add", kind="text", scope="title", literal="Hook", description="t"
    )
    assert route_requirements([_order_req(**facts), title], _SHAPE, message="x") == "editor_ops"


def test_a_keyed_order_the_editor_cannot_serve_still_replans_and_flag_off_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    assert route_requirements([_order_req(key="route")], _SHAPE, message="x") == "replan"
    # flag off, no move words in the message: a keyed order is the planner's (today's rule)
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    assert route_requirements([_order_req(key="custom")], _SHAPE, message="x") == "replan"


# ═════════════════ order rows are applied ONCE, on the current order (FM12, FM15) ═══════════


def _row(intent_id: str, words: str, members: list[str], position: str | None = None):
    return {
        "op": "order",
        "status": "resolved",
        "attribute": words,
        "intent_id": intent_id,
        **({"position": position} if position else {}),
        "assignments": [
            {"media_id": m, "confidence": 0.9, "evidence": "described"} for m in members
        ],
    }


def _marks(intents: list[dict[str, Any]]) -> list[bool]:
    return [bool(row.get("already_seated")) for row in intents]


def test_fm15_a_row_the_order_has_moved_away_from_is_marked_and_seats_nothing() -> None:
    rows = [_row("r1", "the map", [C], "first"), _row("r2", "the entry", [D])]
    seated = ledger.seated_entry(ATTACH, rows, actor=CREATOR_TEXT, turn_id="t1")
    assert seated is not None and list(seated.after_ids) == [C, D, A, B, E]
    applied = ledger.applied_row_fingerprints([seated])
    # the creator dragged the map clip to the end afterwards
    now = [D, A, B, E, C]
    marked = ledger.mark_seated_rows(rows, applied=applied, fresh_order_ask=False, base_ids=now)
    assert _marks(marked) == [True, False]
    assert apply_sequence(now, sequence_rows(marked)) == now  # nothing undoes the drag
    # a row still satisfied by the order is left alone (it seats as a no-op and is still judged)
    assert _marks(
        ledger.mark_seated_rows(
            rows, applied=applied, fresh_order_ask=False, base_ids=[C, D, A, B, E]
        )
    ) == [False, False]


def test_fm5_a_restated_order_ask_seats_every_row_again_on_the_current_order() -> None:
    rows = [_row("r1", "the map", [C], "first")]
    applied = ledger.applied_row_fingerprints([_entry(CREATOR_TEXT, ATTACH, [C, A, B, D, E])])
    # `fresh_order_ask`: this turn states an order of its own (the newest statement)
    again = ledger.mark_seated_rows(
        rows, applied=applied, fresh_order_ask=True, base_ids=[A, B, D, E, C]
    )
    assert _marks(again) == [False]
    assert apply_sequence([A, B, D, E, C], sequence_rows(again)) == [C, A, B, D, E]


def test_fm12_a_clip_added_after_a_reorder_joins_its_named_group_or_goes_to_the_end() -> None:
    first = [_row("r1", "the map", [C], "first"), _row("r2", "scenery", [D])]
    seated = ledger.seated_entry(ATTACH, first, actor=CREATOR_TEXT, turn_id="t1")
    assert seated is not None
    state = current_clip_order(attachment_ids=[*ATTACH, "n", "n2"], entries=[seated])
    # the new clips were added last: the ledger keeps them in place at the end
    assert list(state.order) == [C, D, A, B, E, "n", "n2"]
    # the resolver now says "n" is scenery too: the row still seats (the order, as the creator left
    # it, satisfies it for the clips it knew), so the newcomer joins its group
    grown = [_row("r1", "the map", [C], "first"), _row("r2", "scenery", [D, "n"])]
    marked = ledger.mark_seated_rows(
        grown,
        applied=ledger.applied_row_fingerprints([seated]),
        fresh_order_ask=False,
        base_ids=state.order,
        known_ids=ledger.applied_known_ids([seated]),
    )
    assert _marks(marked) == [False, False]
    assert apply_sequence(list(state.order), sequence_rows(marked)) == [
        C, D, "n", A, B, E, "n2",
    ]  # fmt: skip


def test_a_turn_that_changed_nothing_writes_no_entry() -> None:
    rows = [_row("r1", "the map", [C], "first")]
    entry = ledger.seated_entry(ATTACH, rows, actor=CREATOR_TEXT, turn_id="t1")
    assert entry is not None
    state = current_clip_order(attachment_ids=ATTACH, entries=[entry])
    again = ledger.seated_entry(list(state.order), rows, actor=CREATOR_TEXT, turn_id="t2")
    assert again is not None and again.before_ids == again.after_ids
    turn = ledger.TurnClipOrder(state, tuple(ATTACH), False, {})
    from app.kria.contracts import KriaTurnPlan

    plan = KriaTurnPlan(mode="respond", turn_value="question", response="ok")
    assert (
        _with_clip_order_entries(
            PlannedKriaTurn(plan=plan, manifest_hash=None, context_hash=None), turn
        ).clip_order_entries
        == ()
    )


# ═══════════════════ the draft's order change is a ledger entry (runtime) ══════════════════════


def test_the_turns_entries_ride_its_assistant_event_stamped_with_the_turn(
    timeline_open: None, ledger_on: None
) -> None:
    base = _timeline()
    ops = _parsed(base, SHUFFLE_OP)
    compiled = editor.compile_editor_ops(_job(base), base, ops)
    asked = _editor_turn(ops, ORDER_ASK)
    entry = _with_editor_order_entry(asked, base, compiled).clip_order_entries
    assert [LedgerEntry.from_payload(e) for e in entry] == [
        LedgerEntry(CREATOR_TEXT, tuple(M), tuple(reversed(M)))
    ]
    unasked = _with_editor_order_entry(_editor_turn(ops, TITLE_ASK), base, compiled)
    assert [e["actor"] for e in unasked.clip_order_entries] == [AI_STORY]
    payload = _clip_order_payload(entry, "turn-1")
    assert fold_clip_order([("assistant", payload)])[0].turn_id == "turn-1"
    assert _clip_order_payload((), "turn-1") == {}


def test_fm2_with_the_flag_off_a_draft_writes_no_clip_order_entry(
    timeline_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    base = _timeline()
    ops = _parsed(base, SHUFFLE_OP)
    compiled = editor.compile_editor_ops(_job(base), base, ops)
    assert (
        _with_editor_order_entry(_editor_turn(ops, ORDER_ASK), base, compiled).clip_order_entries
        == ()
    )


def test_fm8_a_reorder_inside_a_named_group_is_allowed_and_not_undone() -> None:
    rows = [_row("r1", "the map", [C], "first"), _row("r2", "friends", [A, B])]
    seated = ledger.seated_entry(ATTACH, rows, actor=CREATOR_TEXT, turn_id="t1")
    assert seated is not None and list(seated.after_ids) == [C, A, B, D, E]
    inside = [C, B, A, D, E]  # the creator put B before A, inside the group they named
    kept = ledger.mark_seated_rows(
        rows,
        applied=ledger.applied_row_fingerprints([seated]),
        fresh_order_ask=False,
        base_ids=inside,
    )
    assert _marks(kept) == [False, False]  # the group keeps the order its clips already have
    assert apply_sequence(inside, sequence_rows(kept)) == inside


def test_fm15_when_every_old_row_was_moved_away_from_the_old_ask_reads_met_not_blocked(
    ledger_on: None,
) -> None:
    """One described group, moved away from by the creator: nothing seats, the contract pins no
    order, and readiness reads the old ask as replaced by the creator's own order."""
    brief = incident._brief()
    groups = (incident.GROUPS[0],)
    token = ledger.turn_clip_order.set(None)
    try:
        strategy = _drafted_with(groups, None, brief)
    finally:
        ledger.turn_clip_order.reset(token)
    seated = ledger.seated_entry(
        ATTACHMENT, strategy["resolved_clip_intents"], actor=CREATOR_TEXT, turn_id="t1"
    )
    assert seated is not None
    dragged = _entry(
        CREATOR_EDITOR,
        list(seated.after_ids),
        [m for m in seated.after_ids if m != MAP_CLIP] + [MAP_CLIP],
        "t2",
    )
    turn = _turn([seated, dragged])
    strategy = _drafted_with(groups, turn, brief)
    assert [i["already_seated"] for i in strategy["resolved_clip_intents"]] == [True]
    snapshot = _snapshot_for(turn)
    report = incident._report(brief, strategy, snapshot=snapshot)
    assert incident._rows(report)["r2"]["disposition"] == "executable", report["requirements"]
    assert "the order you set is the one that plays" in incident._rows(report)["r2"]["reason"]
    worker = _worker_order(brief, strategy, snapshot)
    assert worker == list(turn.state.order) and worker[-1] == MAP_CLIP
    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot=snapshot
    )
    # no row seats anything any more, yet the creator's order is still the one pinned and played
    assert contract is not None and list(contract.order_ids) == list(turn.state.order)


def _drafted_with(groups: tuple, turn: ledger.TurnClipOrder | None, brief) -> dict[str, Any]:
    token = ledger.turn_clip_order.set(turn)
    try:
        return incident._drafted(brief, groups=groups)
    finally:
        ledger.turn_clip_order.reset(token)


def test_fm11_a_hand_reorder_with_no_order_ask_at_all_is_pinned_and_the_welcome_clip_stays(
    ledger_on: None,
) -> None:
    """The creator moved clips by hand and then asked for something else (a length). No order row,
    no order requirement: the contract still pins THEIR order, so the worker plays it and the
    spoken-welcome reorder (KRI-585) cannot hoist a clip over it."""
    from app.kria.brief import CreativeBrief

    full = incident._brief().model_dump(mode="json")
    brief = CreativeBrief.model_validate({**full, "requirements": full["requirements"][:1]})
    mine = [ATTACHMENT[i] for i in (4, 3, 2, 1, 0, *range(5, 19))]
    turn = _turn([_entry(CREATOR_EDITOR, ATTACHMENT, mine, "t2")])
    strategy = _drafted_with((), turn, brief)
    snapshot = _snapshot_for(turn)
    welcome = snapshot["clip_assignments"][13]["analysis"]
    words = "Hoş geldiniz arkadaşlar"
    welcome["transcript"] = words
    welcome["understanding"]["speech"] = {
        "has_speech": True,
        "to_camera": True,
        "transcript": words,
    }
    snapshot["length_plan"]["spoken_order"] = {"roles": {ATTACHMENT[13]: "opening"}}
    strategy = {**strategy, "voice_mode": None}

    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot=snapshot
    )
    assert contract is not None and contract.order_required
    assert list(contract.order_ids) == mine == _worker_order(brief, strategy, snapshot)


def test_the_ledger_never_pins_an_order_nobody_changed(ledger_on: None) -> None:
    from app.kria.brief import CreativeBrief

    full = incident._brief().model_dump(mode="json")
    brief = CreativeBrief.model_validate({**full, "requirements": full["requirements"][:1]})
    strategy = _drafted_with((), None, brief)
    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot=incident._snapshot()
    )
    assert contract is not None and not contract.order_required and not contract.order_ids


# ═════════════════════════════ one conversation, start to finish (FM3, FM4, FM5, FM7, FM9) ═════
# After a render: reorder -> an unrelated title edit the model also reorders -> "swap the first
# two" -> a hand drag -> "what order are my clips in?" -> "undo my reorder" (twice). The entries
# travel exactly as production writes them: the turn's assistant event payload, folded back.


def test_one_conversation_order_stays_one_state_the_ai_reads_and_changes(
    timeline_open: None, ledger_on: None
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    labels_words = {"m4": "map video"}

    def state_now(rendered: list[str]) -> ledger.ClipOrderState:
        entries = fold_clip_order(events)
        replayed = current_clip_order(attachment_ids=M, entries=entries)
        seen = ledger.observed_entry(replayed, rendered, None, actor=CREATOR_EDITOR)
        return current_clip_order(
            attachment_ids=M, entries=(*entries, *([seen] if seen else [])), rendered_ids=rendered
        )

    def run(variant: dict[str, Any], turn: int, ops: list[dict[str, Any]], *asks: BriefUpdate):
        planned = _without_unasked_reorders(_editor_turn(ops, *asks))
        kept = [op for op in _ops_of(planned)] if planned.plan.mode == "act" else []
        if not kept:
            return planned, variant, [e["media_id"] for e in variant["ai_timeline"]["slots"]]
        compiled = editor.compile_editor_ops(_job(variant), variant, kept)
        planned = _with_editor_order_entry(planned, variant, compiled)
        events.append(("assistant", _clip_order_payload(planned.clip_order_entries, f"t{turn}")))
        payload = compiled.payload.model_dump(mode="json", exclude_none=True)
        order = _media_order(variant, payload)
        return planned, _timeline([M.index(m) for m in order]), order

    variant = _timeline()
    assert state_now(M).basis == "attachment_order"

    # 1. "put the map video first": the copilot's partial custom order is completed
    ops = _parsed(variant, {"op": "reorder_clips_by", "criterion": "custom", "order": [3]})
    _planned1, variant, order = run(variant, 1, ops, ORDER_ASK)
    assert order == ["m4", "m1", "m2", "m3", "m5"]
    assert state_now(order).basis == "confirmed"

    # 2. "make the title say New hook": the model also shuffled; the shuffle is stripped
    ops = _parsed(variant, TITLE_OP, SHUFFLE_OP)
    planned2, variant, order = run(variant, 2, ops, TITLE_ASK)
    assert order == ["m4", "m1", "m2", "m3", "m5"]  # equal: the unasked reorder never applied
    assert planned2.clip_order_entries == ()
    assert len(fold_clip_order(events)) == 1  # and wrote no entry of its own

    # 3. "swap the first two": read against the order the creator sees now, not the upload order
    ops = _parsed(variant, {"op": "reorder_clip", "from_index": 1, "to_index": 0})
    _planned3, variant, order = run(variant, 3, ops, ORDER_ASK)
    assert order == ["m1", "m4", "m2", "m3", "m5"]

    # 4. the creator drags m5 to the front by hand (the app's unsaved timeline), then asks
    hand = ["m5", "m1", "m4", "m2", "m3"]
    state = state_now(hand)
    assert list(state.order) == hand
    assert [e.actor for e in state.entries][-1] == CREATOR_EDITOR
    assert state.source["m5"] == CREATOR_EDITOR

    # 5. "what order are my clips in?": the listing names every clip, no ids
    turn_state = ledger.TurnClipOrder(
        state=state,
        attachment_ids=tuple(M),
        rendered=True,
        labels=ledger.labels_for(
            state.order, [{"media_id": m} for m in M], group_words=labels_words
        ),
    )
    listing = ledger.order_listing(state.order, turn_state.labels)
    assert listing.splitlines()[1:] == [
        "1. Clip 1",
        "2. Clip 2",
        "3. map video",
        "4. Clip 4",
        "5. Clip 5",
    ]
    assert not _ID_LEAK.search(listing)

    # 6. "undo my reorder": the snapshot hands the copilot the exact order that restores the step
    # before the hand drag; the undo is one step, and the next one goes one further back
    handed = _timeline([M.index(m) for m in hand])
    snapshot = editor.build_editor_snapshot(
        _job(handed), handed, clip_context={"previous_media_order": list(state.previous or ())}
    )
    assert snapshot["previous_order"] and [
        snapshot["slots"][i]["media_id"] for i in snapshot["previous_order"]
    ] == ["m1", "m4", "m2", "m3", "m5"]
    undo = {"op": "reorder_clips_by", "criterion": "custom", "order": snapshot["previous_order"]}
    # the hand drag itself is recorded first (the turn that finds it writes it)
    events.append(
        (
            "assistant",
            _clip_order_payload(
                (_entry(CREATOR_EDITOR, ["m1", "m4", "m2", "m3", "m5"], hand).to_payload(),), "t4"
            ),
        )
    )
    _planned6, handed, order = run(handed, 6, _parsed(handed, undo), ORDER_ASK)
    assert order == ["m1", "m4", "m2", "m3", "m5"]
    after = state_now(order)
    assert after.previous == ("m4", "m1", "m2", "m3", "m5")  # the next undo: before the swap


def test_fm19_the_receipt_and_the_listing_read_in_german_where_the_catalog_has_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)
    with reply_language_for("de"):
        receipt = ledger.reorder_receipt(ATTACH, [A, C, B, D, E], LABELS)
        listing = ledger.order_listing([C, A], LABELS)
        phrase = ledger.clip_phrase(2, "swing video")
    assert receipt == "Ich habe “swing video” vor “entry” verschoben."
    assert listing.splitlines()[0] == "Deine Clips laufen in dieser Reihenfolge:"
    assert phrase == "dein Clip 2, „swing video“"
    for text in (receipt, listing, phrase):
        assert not _ID_LEAK.search(text)


# ═════════════════ the planner's entry point reads the same state it writes ═══════════════════
# `_turn_clip_order` swallows its own errors (a ledger must never break a turn), so every case
# here also asserts its warning never fired: a bug there would otherwise read as "ledger off".

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.kria import planner as planner_module  # noqa: E402


class _FakeDb:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.rows = [("assistant", payload) for payload in payloads]

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(all=lambda: self.rows)


def _turn_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[dict[str, Any]] | None = None,
    slots: list[str] | None = None,
    variant: dict[str, Any] | None = None,
    editor_state: Any = None,
):
    warnings: list[str] = []
    monkeypatch.setattr(planner_module.log, "warning", lambda event, **kw: warnings.append(event))
    target = (
        SimpleNamespace(snapshot={"slots": [{"media_id": m} for m in slots]}, variant=variant or {})
        if slots is not None
        else None
    )

    async def load(*_a: Any, **_kw: Any) -> Any:
        return target

    monkeypatch.setattr(planner_module, "_load_editor_target", load)
    item = SimpleNamespace(clip_assignments=[{"media_id": m, "kind": "video"} for m in M])
    turn = asyncio.run(
        planner_module._turn_clip_order(
            _FakeDb(events or []), thread_id="t", item=item, editor_state=editor_state
        )
    )
    assert warnings == []
    assert turn is not None
    return turn


def test_the_entry_point_before_a_render_replays_the_ledger_over_the_upload_order(
    monkeypatch: pytest.MonkeyPatch, ledger_on: None
) -> None:
    entry = _entry(CREATOR_TEXT, M, ["m3", "m1", "m2", "m4", "m5"], "t1")
    turn = _turn_state(monkeypatch, events=[{LEDGER_KEY: [entry.to_payload()]}])
    assert list(turn.state.order) == ["m3", "m1", "m2", "m4", "m5"] and not turn.rendered
    assert turn.observed is None and turn.state.basis == "confirmed"
    assert ledger.with_clip_order_ids({"clip_paths": []}, turn.state)["clip_order_ids"] == list(
        turn.state.order
    )


def test_a_timeline_the_plan_laid_out_is_the_ais_order_and_is_never_pinned(
    monkeypatch: pytest.MonkeyPatch, ledger_on: None
) -> None:
    turn = _turn_state(monkeypatch, slots=["m2", "m1", "m3", "m4", "m5"])
    assert turn.observed is not None and turn.observed.actor == AI_STORY
    assert list(turn.state.order) == ["m2", "m1", "m3", "m4", "m5"] and turn.rendered
    assert turn.state.basis == "attachment_order"
    snapshot = {"clip_paths": [], "clip_assignments": []}
    assert "clip_order_ids" not in ledger.with_clip_order_ids(snapshot, turn.state)


def test_a_timeline_the_creator_edited_by_hand_is_theirs_and_is_pinned(
    monkeypatch: pytest.MonkeyPatch, ledger_on: None
) -> None:
    saved = _turn_state(
        monkeypatch,
        slots=["m2", "m1", "m3", "m4", "m5"],
        variant={"user_timeline": {"slots": []}},
    )
    assert saved.observed is not None and saved.observed.actor == CREATOR_EDITOR
    assert saved.state.basis == "confirmed"
    unsaved = _turn_state(
        monkeypatch,
        slots=["m2", "m1", "m3", "m4", "m5"],
        editor_state={"base_generation": "g", "lanes": {"timeline_slots": [{"slot_id": "s"}]}},
    )
    assert unsaved.observed is not None and unsaved.observed.actor == CREATOR_EDITOR
    # an unsaved TEXT edit alone says nothing about the clips' order
    text_only = _turn_state(
        monkeypatch,
        slots=["m2", "m1", "m3", "m4", "m5"],
        editor_state={"base_generation": "g", "lanes": {"text_elements": [{"id": "t"}]}},
    )
    assert text_only.observed is not None and text_only.observed.actor == AI_STORY


def test_when_the_timeline_matches_the_ledger_nothing_is_observed(
    monkeypatch: pytest.MonkeyPatch, ledger_on: None
) -> None:
    entry = _entry(CREATOR_TEXT, M, ["m3", "m1", "m2", "m4", "m5"], "t1")
    turn = _turn_state(
        monkeypatch,
        events=[{LEDGER_KEY: [entry.to_payload()]}],
        slots=["m3", "m1", "m1", "m2", "m4", "m5"],  # a split take shows twice, counts once
    )
    assert turn.observed is None and list(turn.state.order) == ["m3", "m1", "m2", "m4", "m5"]


# ═════════════════════════════ guards the review asked for ═════════════════════════════════════


def test_a_model_written_already_seated_mark_never_cancels_a_stated_order(ledger_on: None) -> None:
    from app.schemas.clip_intents import ResolvedClipIntent

    brief = incident._brief()
    intent = ResolvedClipIntent.model_validate(
        {**incident._intent("r1", "the map", "first", (5,)), "already_seated": True}
    )
    token = ledger.turn_clip_order.set(_turn([]))
    try:
        kept, _choice = brief_order_fields([intent], brief, None)
    finally:
        ledger.turn_clip_order.reset(token)
    assert kept[0].already_seated is False
    assert len(sequence_rows([k.model_dump(mode="json") for k in kept])) == 1


def test_the_strip_leaves_a_tapped_card_and_a_degraded_turn_alone(
    timeline_open: None, ledger_on: None
) -> None:
    ops = _parsed(_timeline(), TITLE_OP, SHUFFLE_OP)
    tapped = _editor_turn(ops, TITLE_ASK)
    tapped = PlannedKriaTurn(**{**tapped.__dict__, "suggestion_pin": {"id": "opener"}})
    assert _without_unasked_reorders(tapped) is tapped
    degraded = PlannedKriaTurn(
        **{**_editor_turn(ops, TITLE_ASK).__dict__, "brief_coverage": {"degraded_from": "x"}}
    )
    assert _without_unasked_reorders(degraded) is degraded


@pytest.mark.parametrize("key", ["capture_time", "random", "any_order", "attachment", "route"])
def test_the_router_keeps_every_other_order_key_on_its_own_route(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    unaided = route_requirements([_order_req(key=key)], _SHAPE, message="x")
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    assert route_requirements([_order_req(key=key)], _SHAPE, message="x") == unaided


def test_two_clips_that_read_the_same_are_told_apart_by_their_number() -> None:
    rows = [{"media_id": i} for i in ATTACH]
    labels = ledger.labels_for(ATTACH, rows, group_words={A: "friends", B: "friends", C: "map"})
    assert labels[A] == "friends (1)" and labels[B] == "friends (2)" and labels[C] == "map"


def test_dispatch_starts_from_the_order_the_snapshot_carries() -> None:
    from app.tasks.content_plan_build import _ledger_ordered_paths

    item = SimpleNamespace(
        clip_assignments=[{"media_id": m, "gcs_path": f"u/{m}.mp4"} for m in ATTACH]
    )
    paths = [f"u/{m}.mp4" for m in ATTACH]
    assert _ledger_ordered_paths(item, paths, [C, A, B]) == [
        "u/c.mp4",
        "u/a.mp4",
        "u/b.mp4",
        *paths[3:],
    ]
    assert _ledger_ordered_paths(item, paths, []) == paths


def test_the_draft_says_its_reorder_by_name_and_never_by_id(
    timeline_open: None, ledger_on: None
) -> None:
    base = _timeline()
    ops = _parsed(base, {"op": "reorder_clips_by", "criterion": "custom", "order": [2, 0, 1, 3, 4]})
    compiled = editor.compile_editor_ops(_job(base), base, ops)
    planned = _with_editor_order_entry(_editor_turn(ops, ORDER_ASK), base, compiled)
    item = SimpleNamespace(
        clip_assignments=[
            {"media_id": "m1", "user_note": "map"},
            {"media_id": "m2", "user_note": "IMG_7382.MOV"},  # a file name is never a label
            {"media_id": "m3", "user_note": "swing video"},
            {"media_id": "m4"},
            {"media_id": "m5"},
        ]
    )
    changes = _with_order_receipt(["Reorder clips", "Updated the title"], planned, item)
    assert changes == ["Moved “swing video” to the start.", "Updated the title"]
    assert not any(_ID_LEAK.search(c) for c in changes)
    with reply_language_for("tr"):
        assert _with_order_receipt(["Reorder clips"], planned, item) == [
            "“swing video” klibini başa aldım."
        ]
    # nothing moved, nothing said
    assert _with_order_receipt(["Updated the title"], _editor_turn(ops), item) == [
        "Updated the title"
    ]


def test_an_editorial_plan_under_a_creator_moved_order_never_raises_the_contract(
    ledger_on: None,
) -> None:
    """A story the model laid out in its own order must not turn a creator's hand reorder into a
    refusal ("the proposed story does not match ... confirmed order"): the ledger adds no pin
    under an editorial plan, so the contract builds exactly as it did before the ledger."""
    from app.kria.brief import CreativeBrief

    full = incident._brief().model_dump(mode="json")
    brief = CreativeBrief.model_validate({**full, "requirements": full["requirements"][:1]})
    mine = [ATTACHMENT[i] for i in (4, 3, 2, 1, 0, *range(5, 19))]
    turn = _turn([_entry(CREATOR_EDITOR, ATTACHMENT, mine, "t2")])
    plan = {
        "viewer_goal": "see the trip",
        "beats": [{"purpose": "the whole trip", "media_ids": list(ATTACHMENT)}],
    }
    strategy = {**_drafted_with((), turn, brief), "editorial_plan": plan}
    snapshot = _snapshot_for(turn)
    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot=snapshot
    )
    assert contract is not None
    assert list(contract.order_ids) == list(ATTACHMENT)  # the story's own order, as before
    assert contract.order_basis == "approved_editorial_plan"


def test_a_clip_out_of_its_seat_after_a_creator_reorder_never_reads_met(
    ledger_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative control for FM17: the worker's own receipt on a plan seated on the creator's order.
    Control first (the real seating reads met), then a seater that swaps the first and last clip."""
    from app.kria.brief_checks import build_receipts
    from app.pipeline import unified_montage

    brief = incident._brief()
    _s1, seated = _first_turn(brief)
    moved = _entry(CREATOR_EDITOR, list(seated.after_ids), _moved_map_to_the_end(), "t2")
    turn = _turn([seated, moved])
    strategy = _drafted_on(turn, brief)
    snapshot = _snapshot_for(turn)

    def receipt():
        plan = incident._planned(brief, strategy, snapshot)
        facts = plan_facts_from_unified_montage(plan.record())
        [r2] = build_receipts([brief.live()[1]], facts, include_unchecked=True)
        return r2

    assert receipt().status == "met"
    seat = unified_montage._apply_sequence

    def swapped(ordered, rows):
        result = list(seat(ordered, rows))
        result[0], result[-1] = result[-1], result[0]
        return result

    monkeypatch.setattr(unified_montage, "_apply_sequence", swapped)
    wrong = receipt()
    assert wrong.status == "partial", wrong


# ═════════════════ plan_live_turn through a fake session: what writes the ledger ═════════════════
# Seam: the planner's public entry (`plan_live_turn`) runs for real: the order state it reads from
# the thread's events and the editor target, the strip, the carrier stamp and the entries it
# returns. Patched: `_plan_live_turn` (the copilot's authored ops), `_load_editor_target` (the
# rendered timeline) and `_gate_unresolved_choices` (a pass-through). The draft commit
# (`_complete_draft_turn`, which needs Postgres) is replaced by its pure pieces:
# `_with_editor_order_entry` + `_clip_order_payload`, the same calls it makes, then the event is
# folded back by the next call.


def test_plan_live_turn_a_chat_reorder_writes_one_entry_an_unrelated_edit_writes_none(
    timeline_open: None, ledger_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[dict[str, Any]] = []
    state = {"variant": _timeline()}
    item = SimpleNamespace(
        id="item",
        clip_assignments=[{"media_id": m, "kind": "video"} for m in M],
        clip_gcs_paths=[],
    )

    class Db:
        async def get(self, _model: Any, _id: Any, **_kw: Any) -> Any:
            return item

        async def execute(self, _statement: Any) -> Any:
            return SimpleNamespace(all=lambda: [("assistant", p) for p in events])

    async def load_target(*_a: Any, **_kw: Any) -> Any:
        slots = [{"media_id": s["media_id"]} for s in state["variant"]["ai_timeline"]["slots"]]
        return SimpleNamespace(snapshot={"slots": slots}, variant={})

    async def gate(_db: Any, planned: Any, **_kw: Any) -> Any:
        return planned

    monkeypatch.setattr(planner_module, "_load_editor_target", load_target)
    monkeypatch.setattr(planner_module, "_gate_unresolved_choices", gate)

    def turn(number: int, raw_ops: list[dict[str, Any]], *asks: BriefUpdate):
        ops = _parsed(state["variant"], *raw_ops)

        async def plan(_db: Any, **_kw: Any) -> PlannedKriaTurn:
            return _editor_turn(ops, *asks)

        monkeypatch.setattr(planner_module, "_plan_live_turn", plan)
        planned = asyncio.run(
            planner_module.plan_live_turn(Db(), thread_id="t", item_id="item", creator_id="c")
        )
        kept = _ops_of(planned) if planned.plan.mode == "act" else []
        if kept:
            variant = state["variant"]
            compiled = editor.compile_editor_ops(_job(variant), variant, kept)
            planned = _with_editor_order_entry(planned, variant, compiled)
            payload = compiled.payload.model_dump(mode="json", exclude_none=True)
            state["variant"] = _timeline([M.index(m) for m in _media_order(variant, payload)])
        if planned.clip_order_entries:
            events.append(_clip_order_payload(planned.clip_order_entries, f"t{number}"))
        return planned

    reorder = {"op": "reorder_clips_by", "criterion": "custom", "order": [3, 0, 1, 2, 4]}
    first = turn(1, [reorder], ORDER_ASK)
    assert [e["actor"] for e in first.clip_order_entries] == [CREATOR_TEXT]
    assert len(events) == 1
    order_after = [s["media_id"] for s in state["variant"]["ai_timeline"]["slots"]]
    assert order_after == ["m4", "m1", "m2", "m3", "m5"]

    # an unrelated title edit the model also reordered: the reorder is stripped, no entry is
    # written, and the order the next turn reads is the order before it
    second = turn(2, [TITLE_OP, SHUFFLE_OP], TITLE_ASK)
    assert second.clip_order_entries == ()
    assert len(events) == 1
    assert [s["media_id"] for s in state["variant"]["ai_timeline"]["slots"]] == order_after
    carried = asyncio.run(
        planner_module._turn_clip_order(Db(), thread_id="t", item=item, editor_state=None)
    )
    assert list(carried.state.order) == order_after and carried.observed is None


# ═══════════════ before a render: a move nothing can serve is declined in plain words ═════════════


def _draft_turn(*updates: BriefUpdate) -> PlannedKriaTurn:
    from app.kria.contracts import KriaToolIntent

    plan = KriaTurnPlan(
        mode="act",
        turn_value="action",
        intents=[
            KriaToolIntent(
                intent_id="apply-strategy",
                tool_name="draft.apply_strategy",
                tool_version=1,
                arguments={"strategy": {}, "summary": "x"},
            )
        ],
    )
    return PlannedKriaTurn(
        plan=plan,
        manifest_hash=None,
        context_hash=None,
        brief_updates=tuple(updates),
        brief_route="replan",
        brief_coverage={"stage": "draft"},
    )


def test_a_relative_move_or_swap_before_a_render_is_declined_plainly_in_every_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.kria.planner import _declined_pre_render_move

    swap = _order_req(key="swap")
    declined = _declined_pre_render_move(_draft_turn(swap))
    assert declined.plan.mode == "respond" and declined.brief_updates == ()
    en = declined.plan.response
    assert en.startswith("I can move a clip, swap two clips or undo a reorder once your video")
    assert not _ID_LEAK.search(en)
    with reply_language_for("tr"):
        assert "videon hazır olunca" in _declined_pre_render_move(_draft_turn(swap)).plan.response
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)
    with reply_language_for("de"):
        de = _declined_pre_render_move(_draft_turn(swap)).plan.response
    assert de != en and "Video" in de


def test_first_last_then_and_mixed_asks_keep_their_draft_before_a_render() -> None:
    from app.kria.planner import _declined_pre_render_move

    for turn in (
        _draft_turn(_order_req()),  # key-less "put X first": a clip-intent row, served
        _draft_turn(_order_req(key="capture_time")),
        _draft_turn(_order_req(key="undo"), TITLE_ASK),  # more than the move: the draft stays
    ):
        assert _declined_pre_render_move(turn) is turn


def test_plan_live_turn_declines_the_move_only_with_the_flag_on_and_before_a_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = SimpleNamespace(id="i", clip_assignments=[{"media_id": m} for m in M], clip_gcs_paths=[])

    class Db:
        async def get(self, *_a: Any, **_k: Any) -> Any:
            return item

        async def execute(self, _s: Any) -> Any:
            return SimpleNamespace(all=lambda: [])

    async def no_target(*_a: Any, **_k: Any) -> Any:
        return None

    async def plan(_db: Any, **_k: Any) -> PlannedKriaTurn:
        return _draft_turn(_order_req(key="swap"))

    async def gate(_db: Any, planned: Any, **_k: Any) -> Any:
        return planned

    for name, value in (
        ("_load_editor_target", no_target),
        ("_plan_live_turn", plan),
        ("_gate_unresolved_choices", gate),
    ):
        monkeypatch.setattr(planner_module, name, value)

    def run() -> PlannedKriaTurn:
        return asyncio.run(
            planner_module.plan_live_turn(Db(), thread_id="t", item_id="i", creator_id="c")
        )

    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    assert run().plan.mode == "act"  # flag off: today's behaviour
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    assert run().plan.mode == "respond"


def test_no_media_id_or_file_name_can_become_a_clips_name() -> None:
    """Every route to a visible label: group words, the upload note, the record's subject. A
    candidate that holds an id, a UUID, a file name (with or without extension) or the clip's own
    source file name is skipped; with nothing left the name is "Clip N" by play position."""
    uuid_id = "analysis-proxy-ios-5C6E0D6A-B7C3-5C1E-A4D1-1F2B3C4D5E6F.mp4"
    row = {
        "media_id": uuid_id,
        "source_filename": "IMG_7382.MOV",
        "user_note": "IMG_7382 at the lake",
        "analysis": {"understanding": {"subject": "5c6e0d6a-b7c3-5c1e-a4d1-1f2b3c4d5e6f"}},
    }
    bare = {**row, "media_id": "m-lake", "user_note": "IMG7382", "source_filename": "IMG7382.mov"}
    labels = ledger.labels_for(
        [uuid_id, "m-lake"], [row, bare], group_words={uuid_id: uuid_id, "m-lake": "asset-9"}
    )
    assert labels == {uuid_id: "Clip 1", "m-lake": "Clip 2"}
    # two clips with the same note, or none with any facts: a name plus the number, never an id
    same = ledger.labels_for(
        ["a", "b", "c"],
        [
            {"media_id": "a", "user_note": "lake"},
            {"media_id": "b", "user_note": "lake"},
            {"media_id": "c"},
        ],
    )
    assert same == {"a": "lake (1)", "b": "lake (2)", "c": "Clip 3"}
    for text in (
        ledger.order_listing(["a", "b", "c"], same),
        ledger.reorder_receipt(["a", "b", "c"], ["b", "a", "c"], same),
    ):
        assert not _ID_LEAK.search(text), text

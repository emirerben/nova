"""The clip-order card: the draft message's numbered strip and the drag behind it (server half).

Built on `KRIA_CLIP_ORDER_LEDGER_ENABLED` (PR 2) behind a client-protocol capability. The card is a
projection of the ledger; a drag is one more ledger entry. Owner rules: no lock (the newest
statement wins), the creator's own order is never silently changed by the AI, every visible string
names a clip by its label (never an id, UUID or file name), and a drag is never a 409/422 dead end.

Ways this can fail, written before the code (each is pinned; "FM" = failure mode). The pure ones
are in this module; the ones that need the real submit path, worker and approval are in
`test_clip_order_card_turns_postgres.py` (named after the FM they pin).

 FM1  flag off, or an old build: the draft/reply events gain a key, a character of prose, or a
      byte of the digest; an old build gets a card it cannot render (Postgres module).
 FM2  the strip is shown when the creator stated every position, for one clip, or for a draft whose
      order is not the upload order; or it is hidden for the incident shape (groups ordered, the
      inner order unstated).
 FM3  a drag makes a model call, or waits behind a running turn (Postgres module).
 FM4  a stale proposal id is refused (409/422) instead of rebased (Postgres module + the pure
      rebase here).
 FM5  a non-permutation (a missing, extra, repeated or unknown id, an empty list) errors or
      reorders clips the creator never touched.
 FM6  the same client_event_id sent twice writes two entries (Postgres module).
 FM7  two quick drags are two undo steps, or the first wins instead of the last.
 FM8  after a drag, "swap the first two" reads the UPLOAD order; an unrelated edit moves a clip;
      a reorder the model adds on its own survives (negative control).
 FM9  "use this order" is lost on the next card, survives a later change, or a drag leaves it set.
 FM10 a drag after a video exists is recorded and then overruled by the timeline; an approval
      prepared before a drag renders the OLD order, is closed with a 409 / cancelled plan instead
      of rendering the dragged one, or rewrites itself on an AI / server order change, loses a
      concurrent drag, or deadlocks with it (Postgres module; the pure half is here).
 FM11 a media id, UUID or file name reaches any card string other than the opaque `media_id`;
      two clips that read the same are not told apart by position.
 FM12 the card for 19 or 40 clips is large or changes between two builds of the same state.
 FM13 an old build is told the order without names, or in one language only.
 FM14 `request_digest` changes for a body that carries no `clip_order_ops` (Postgres module).
 FM15 the card reaches the strategy, the draft snapshot, the readiness digest or the policy
      re-check, so a "simpler version?" card appears for it (Postgres module + the key test here).
 FM16 the render contract, readiness and the worker's own plan disagree on a dragged order (the
      KRI-503 dead end): oracle `preflight_montage_plan`, as in the ledger tests.
 FM17 the strip lists clips the edit does not play (a draft that selects a subset), so the number
      on a card is not the clip's play position.
 FM18 a drag while the render is being made, or after its approval, is recorded and then overruled
      by the timeline (Postgres module); the drag's placeholder message is read as creator words
      by the planner's transcript readers.
 FM19 a worker planning while a drag lands commits a draft on the order it read before the drag
      (Postgres module: the revision fence requeues it).
 FM20 the phone cannot read the card: the draft event and the card event must come through
      `read_delta`, the phone's own read path (Postgres module).

Recorded vs reconstructed: the Toroko incident request and the shape of its outcome are recorded
(see `test_unstated_order_default.py`); every drag, the card payloads and the later chat messages
are RECONSTRUCTED by hand. No model is called anywhere.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from app.config import settings
from app.kria.planner import PlannedKriaTurn, _without_unasked_reorders
from app.kria.reply_language import reply_language_for
from app.services import clip_order_card as card_mod
from app.services import clip_order_ledger as ledger
from app.services import kria_editor_ops as editor
from app.services.clip_order_card import build_card, rebase_drag
from app.services.clip_order_ledger import (
    CREATOR_DRAG,
    CREATOR_TEXT,
    LEDGER_KEY,
    LedgerEntry,
    current_clip_order,
    fold_clip_order,
)
from app.services.creator_render_contract import build_render_contract
from tests.kria import test_unstated_order_default as incident
from tests.kria.test_clip_order_ledger import (  # noqa: F401 - fixtures and harness
    SHUFFLE_OP,
    TITLE_ASK,
    TITLE_OP,
    M,
    _compile,
    _editor_turn,
    _job,
    _media_order,
    _ops_of,
    _parsed,
    _timeline,
    _worker_order,
)

A, B, C, D, E = "a", "b", "c", "d", "e"
ATTACH = [A, B, C, D, E]
ROWS = [{"media_id": m, "kind": "video"} for m in ATTACH]

_ID_LEAK = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|analysis-proxy|asset-|legacy-clip|\.mp4|\.mov|IMG_",
    re.IGNORECASE,
)


@pytest.fixture
def ledger_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)


@pytest.fixture
def timeline_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        editor,
        "_editor_capabilities",
        lambda *a: {"text_elements": True, "timeline": True, "clips": {}},
    )


@pytest.fixture(autouse=True)
def _prod(prod_profile, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", True)
    monkeypatch.setattr(settings, "clip_facts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    monkeypatch.setattr(settings, "kria_unstated_order_default_enabled", True)


def _entry(actor: str, before: list[str], after: list[str], turn: str = "t", **kw: Any):
    return LedgerEntry(actor, tuple(before), tuple(after), turn, **kw)


def _state(*entries: LedgerEntry, attach: list[str] = ATTACH):
    return current_clip_order(attachment_ids=attach, entries=entries)


def _card(*entries: LedgerEntry, events=(), applies: bool | None = True, **kw: Any):
    return build_card(
        _state(*entries), ROWS, events=list(events), brief_version=3, applies=applies, **kw
    )


def _event(card: dict[str, Any], *entries: LedgerEntry) -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = {card_mod.CARD_KEY: card}
    if entries:
        payload[LEDGER_KEY] = [e.to_payload() for e in entries]
    return "assistant", payload


# ── FM2: when the strip is shown ──────────────────────────────────────────────────────────────


def test_fm2_the_strip_is_shown_when_the_order_was_decided_for_the_creator() -> None:
    card = _card()
    assert card["shown"] is True and card["total"] == 5 and card["editable"] is True
    assert [c["position"] for c in card["cards"]] == [1, 2, 3, 4, 5]
    assert {c["source"] for c in card["cards"]} == {"server_default"}
    assert not any(c["moved"] for c in card["cards"])  # no card seen before: nothing "moved"


@pytest.mark.parametrize(
    ("why", "kwargs"),
    [
        ("the creator stated every position", {"stated": ATTACH}),
        ("the draft's order is not the upload order", {"applies": False}),
    ],
)
def test_fm2_the_strip_is_hidden_when_nothing_was_decided_for_the_creator(
    why: str, kwargs: dict[str, Any]
) -> None:
    assert _card(**kwargs)["shown"] is False, why


def test_fm2_one_clip_is_never_a_strip() -> None:
    state = current_clip_order(attachment_ids=[A])
    card = build_card(state, ROWS[:1], events=[], brief_version=1, applies=True)
    assert card["shown"] is False and card["total"] == 1


def test_fm2_the_incident_shape_shows_the_strip_but_a_full_listed_order_does_not(
    ledger_on: None,
) -> None:
    """Toroko: groups ordered, the inner order unstated. Only the clip the creator pinned alone
    (the map video, first) is theirs; every other position was decided for them."""
    brief = incident._brief()
    strategy = incident._drafted(brief)
    assert card_mod.stated_ids(strategy) == {incident._media_id(5)}
    entry = ledger.seated_entry(
        incident.ATTACHMENT, strategy["resolved_clip_intents"], actor=CREATOR_TEXT, turn_id="t1"
    )
    state = current_clip_order(attachment_ids=incident.ATTACHMENT, entries=[entry])
    rows = [{"media_id": m} for m in incident.ATTACHMENT]
    card = build_card(
        state,
        rows,
        events=[],
        brief_version=2,
        applies=card_mod.card_applies({**strategy, "edit_format": "montage"}),
        stated=card_mod.stated_ids(strategy),
        groups=ledger.group_words_by_clip(strategy["resolved_clip_intents"]),
    )
    assert card["shown"] is True
    assert [c["media_id"] for c in card["cards"]] == incident.EXPECTED
    assert not any(_ID_LEAK.search(c["label"]) for c in card["cards"])
    # a listed order: every clip is its own row, so every position is the creator's
    listed = {
        "resolved_clip_intents": [
            {
                "op": "order",
                "status": "resolved",
                "ordered_members": True,
                "attribute": "in this order",
                "assignments": [{"media_id": m} for m in ATTACH],
            }
        ]
    }
    assert card_mod.stated_ids(listed) == set(ATTACH)


def test_fm2_a_drag_keeps_the_strip_in_the_chat_and_marks_what_moved() -> None:
    first = _card()
    drag = card_mod.drag_entry(ATTACH, [C, A, B, D, E], "t2", coalesce=False)
    after = _card(drag, events=[_event(first)], applies=None, confirm=True)
    assert after["shown"] is True and after["confirmed"] is True
    assert [c["media_id"] for c in after["cards"]] == [C, A, B, D, E]
    assert [c["media_id"] for c in after["cards"] if c["moved"]] == [C]
    assert {c["media_id"]: c["source"] for c in after["cards"]}[C] == CREATOR_DRAG
    # the creator dragging every other clip does not make the strip vanish from the chat
    everything = card_mod.drag_entry([C, A, B, D, E], [E, D, C, B, A], "t3", coalesce=False)
    last = _card(drag, everything, events=[_event(first), _event(after)], applies=None)
    assert last["shown"] is True


def test_fm2_applies_reads_fields_never_words() -> None:
    montage = {"edit_format": "montage", "audio_strategy": "licensed_music"}
    assert card_mod.card_applies(montage) is True
    for override in (
        {"ordering_choice": "random"},
        {"ordering_choice": "group_first"},
        {"ordering_choice": "chronological"},
        {"render_program": "native"},
        {"audio_strategy": "voiceover"},
        {"edit_format": "talking_head"},
        {"resolved_clip_intents": [{"op": "order", "order_by": "capture_time"}]},
    ):
        assert card_mod.card_applies({**montage, **override}) is False, override
    assert card_mod.card_applies(None) is False


# ── FM4 / FM5: a drag rebases by media id, it never refuses ───────────────────────────────────


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ([B, A, C, D, E], [B, A, C, D, E]),  # the order the creator sees
        ([E, D, C, B, A], [E, D, C, B, A]),
        ([B, A], [B, A, C, D, E]),  # a partial list: the named clips take their slots
        ([C, B, A, D], [C, B, A, D, E]),  # a clip added since (E) keeps its place
        ([B, "gone", A, C, D, E], [B, A, C, D, E]),  # a clip removed since is dropped
        ([B, B, A, C, D, E], [B, A, C, D, E]),  # a repeated id counts once
        ([], ATTACH),  # an empty list changes nothing
        (["x", "y"], ATTACH),  # an all-unknown list changes nothing
    ],
)
def test_fm4_fm5_every_shape_of_drag_completes(requested: list[str], expected: list[str]) -> None:
    assert rebase_drag(ATTACH, requested) == expected


def test_fm4_a_drag_written_against_an_older_order_keeps_a_clip_added_since_in_place() -> None:
    current = [A, B, "new", C, D]  # "new" was attached after the card was shown
    assert rebase_drag(current, [D, C, B, A]) == [D, C, "new", B, A]
    assert rebase_drag([A, C, D], [D, C, B, A]) == [D, C, A]  # B was removed meanwhile


def test_fm5_a_drag_that_changes_nothing_writes_no_entry() -> None:
    assert card_mod.drag_entry(ATTACH, rebase_drag(ATTACH, []), "t", coalesce=False) is None
    assert card_mod.drag_entry(ATTACH, ATTACH, "t", coalesce=True) is None


def test_the_proposal_id_names_the_offer_not_the_moment() -> None:
    same = card_mod.proposal_id(3, ATTACH)
    assert same == card_mod.proposal_id(3, list(ATTACH)) == _card()["proposal_id"]
    assert same != card_mod.proposal_id(3, [B, A, C, D, E])  # another order
    assert same != card_mod.proposal_id(4, ATTACH)  # another brief version
    assert same != card_mod.proposal_id(3, [*ATTACH, "f"])  # another clip set


# ── FM7: two quick drags are one gesture, one undo step; the last order wins ──────────────────


def test_fm7_two_quick_drags_coalesce_into_one_undo_step_and_the_last_order_wins() -> None:
    first = _entry(CREATOR_DRAG, ATTACH, [B, A, C, D, E], "t1")
    second = _entry(CREATOR_DRAG, [B, A, C, D, E], [E, B, A, C, D], "t2", coalesce=True)
    state = _state(first, second)
    assert list(state.order) == [E, B, A, C, D]
    assert state.previous == tuple(ATTACH)  # ONE undo restores the order before the gesture
    # control: without the flag the same two entries are two steps
    apart = _state(first, _entry(CREATOR_DRAG, [B, A, C, D, E], [E, B, A, C, D], "t2"))
    assert apart.previous == (B, A, C, D, E)


def test_fm7_a_coalesced_drag_back_to_where_it_started_leaves_nothing_to_undo() -> None:
    first = _entry(CREATOR_DRAG, ATTACH, [B, A, C, D, E], "t1")
    back = _entry(CREATOR_DRAG, [B, A, C, D, E], ATTACH, "t2", coalesce=True)
    state = _state(first, back)
    assert list(state.order) == ATTACH and state.previous is None


def test_fm7_the_coalesce_marker_round_trips_and_is_absent_otherwise() -> None:
    plain = _entry(CREATOR_DRAG, ATTACH, [B, A, C, D, E])
    assert "coalesce" not in plain.to_payload()  # every existing payload keeps its bytes
    marked = _entry(CREATOR_DRAG, ATTACH, [B, A, C, D, E], coalesce=True)
    assert LedgerEntry.from_payload(marked.to_payload()) == marked
    drag = card_mod.drag_entry(ATTACH, [B, A, C, D, E], "t", coalesce=True)
    first = ("assistant", {LEDGER_KEY: [plain.to_payload()]})
    assert card_mod.latest_event_is_a_drag([first]) is True
    assert card_mod.latest_event_is_a_drag([first, ("assistant", {"turn_value": "x"})]) is False
    assert card_mod.latest_event_is_a_drag([]) is False
    assert drag is not None and drag.coalesce is True


# ── FM9: "use this order" ─────────────────────────────────────────────────────────────────────


def test_fm9_confirm_marks_the_card_and_holds_only_for_that_exact_order() -> None:
    confirmed = _card(confirm=True)
    assert confirmed["confirmed"] is True
    # the next card for the same order (a later draft, an unrelated reply) stays confirmed
    same = _card(events=[_event(confirmed)], applies=None)
    assert same["confirmed"] is True
    # a drag clears it, a confirm after it sets it again
    drag = card_mod.drag_entry(ATTACH, [B, A, C, D, E], "t2", coalesce=False)
    moved = _card(drag, events=[_event(confirmed)], applies=None, confirm=False)
    assert moved["confirmed"] is False
    derived = _card(drag, events=[_event(confirmed)], applies=None)
    assert derived["confirmed"] is False  # a changed order is no longer the confirmed one
    again = _card(drag, events=[_event(moved)], applies=None, confirm=True)
    assert again["confirmed"] is True
    assert [c["media_id"] for c in again["cards"] if c["moved"]] == []  # nothing moved since


# ── FM11 / FM12 / FM13: labels, size, old builds ──────────────────────────────────────────────


def test_fm11_no_visible_string_shows_an_id_and_duplicates_are_told_apart() -> None:
    unsafe = "analysis-proxy-ios-5C6E0D6A-B7C3-5C1E-A4D1-1F2B3C4D5E6F.mp4"
    rows = [
        {"media_id": unsafe, "user_note": "IMG_7382.MOV"},
        {"media_id": "asset-77", "user_note": "friends"},
        {"media_id": "m3", "user_note": "friends"},
        {"media_id": "m4", "analysis": {"understanding": {"subject": "beach at dusk"}}},
        {"media_id": "m5"},
    ]
    state = current_clip_order(attachment_ids=[r["media_id"] for r in rows])
    card = build_card(
        state, rows, events=[], brief_version=1, applies=True, groups={"m5": "the map video"}
    )
    labels = [c["label"] for c in card["cards"]]
    assert labels == ["Clip 1", "friends (2)", "friends (3)", "beach at dusk", "the map video"]
    visible = json.dumps([{k: v for k, v in c.items() if k != "media_id"} for c in card["cards"]])
    assert not _ID_LEAK.search(visible), visible
    # the id appears only as the opaque field the phone matches to its own assets
    assert [c["media_id"] for c in card["cards"]][0] == unsafe


@pytest.mark.parametrize("count", [19, 40])
def test_fm12_a_large_card_is_small_and_stable(count: int) -> None:
    ids = [f"analysis-proxy-ios-{n:08d}-1111-2222-3333-444444444444.mp4" for n in range(count)]
    rows = [{"media_id": m, "kind": "video", "user_note": f"take {n}"} for n, m in enumerate(ids)]
    drag = card_mod.drag_entry(ids, [*ids[7:], *ids[:7]], "t", coalesce=False)
    state = current_clip_order(attachment_ids=ids, entries=[drag])
    built = [build_card(state, rows, events=[], brief_version=4, applies=True) for _ in range(2)]
    assert built[0] == built[1]
    size = len(json.dumps(built[0], separators=(",", ":")))
    assert size < 200 * count, size  # about 150 bytes a card: no urls, no analysis
    assert built[0]["total"] == count == len(built[0]["cards"])


def test_fm13_an_old_build_is_told_the_order_by_name_in_every_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)
    rows = [
        {"media_id": A, "user_note": "map"},
        {"media_id": B, "user_note": "entry"},
        {"media_id": C},
    ]
    card = build_card(
        current_clip_order(attachment_ids=[A, B, C]),
        rows,
        events=[],
        brief_version=1,
        applies=True,
    )
    for language, head in (
        ("en", "Your clips play in this order:"),
        ("tr", "Kliplerin şu sırayla oynuyor:"),
        ("de", "Deine Clips laufen in dieser Reihenfolge:"),
    ):
        with reply_language_for(language):
            text = card_mod.listing_text(card)
        assert text.splitlines() == [head, "1. map", "2. entry", "3. Clip 3"], language
        assert not _ID_LEAK.search(text)
    with reply_language_for("en"):
        assert card_mod.with_listing("Here is your plan.", card).startswith(
            "Here is your plan.\n\nYour clips play in this order:"
        )


def test_the_unavailable_line_reads_in_every_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)
    with reply_language_for("en"):
        assert card_mod.unavailable_reply().startswith("I can't change the clip order")
    with reply_language_for("tr"):
        assert "Klip sırasını" in card_mod.unavailable_reply()
    with reply_language_for("de"):
        assert "Reihenfolge" in card_mod.unavailable_reply()


# ── FM1 / gate: which build gets what ─────────────────────────────────────────────────────────


def test_fm1_the_gate_by_build_and_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.client_protocol import CLIP_ORDER_CARD_PROTOCOL

    floor = settings.clip_order_card_min_client_protocol
    assert floor == CLIP_ORDER_CARD_PROTOCOL
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    assert card_mod.card_gate(floor) == "card"
    assert card_mod.card_gate(floor - 1) == "text"  # an old build: the order as plain text
    assert card_mod.card_gate(None) == "none"  # the web / a headerless caller: nothing changes
    monkeypatch.setattr(settings, "clip_order_card_min_client_protocol", 999)
    assert card_mod.card_gate(floor + 10) == "text"  # the kill switch: text for every build
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    assert card_mod.card_gate(floor) == "none"  # flag off: byte-identical to today


def test_fm15_the_card_is_a_projection_and_never_part_of_what_the_policy_reads() -> None:
    """No registry of strategy / binding / readiness fields carries the card key: it rides the
    assistant event beside the ledger entries, so the policy re-check has nothing to re-write."""
    from app.kria import approval_readiness, brief_binding, prepared_execution
    from app.services import creator_render_contract

    for module in (approval_readiness, brief_binding, prepared_execution, creator_render_contract):
        source = open(module.__file__, encoding="utf-8").read()
        assert card_mod.CARD_KEY not in source and card_mod.OPS_KEY not in source, module


# ── FM8: after a drag, chat composes with it; nothing moves a clip on its own ──────────────────


def test_fm8_a_drag_then_a_swap_then_an_unrelated_edit(
    timeline_open: None, ledger_on: None
) -> None:
    """The creator dragged m3 to the front on the card (the first video was made on that order).
    "Swap the first two" is read against it, so m3 and m1 trade places. A title edit the model
    also shuffled keeps every clip where it is, and a model-added reorder is stripped."""
    events: list[tuple[str, dict[str, Any]]] = []
    drag = card_mod.drag_entry(M, ["m3", "m1", "m2", "m4", "m5"], "t1", coalesce=False)
    assert drag is not None
    events.append(("assistant", {LEDGER_KEY: [drag.to_payload()]}))
    state = current_clip_order(attachment_ids=M, entries=fold_clip_order(events))
    assert list(state.order) == ["m3", "m1", "m2", "m4", "m5"]
    assert state.source["m3"] == CREATOR_DRAG

    dragged = _timeline([M.index(m) for m in state.order])
    snapshot = editor.build_editor_snapshot(_job(dragged), dragged)
    assert [s["media_id"] for s in snapshot["slots"]] == list(state.order)
    swapped = _compile(dragged, {"op": "reorder_clip", "from_index": 1, "to_index": 0})
    assert swapped == ["m1", "m3", "m2", "m4", "m5"]  # not m1 and m2: the dragged order is read

    # an unrelated title edit the model also shuffled: the shuffle is stripped, the order equal
    ops = _parsed(dragged, TITLE_OP, SHUFFLE_OP)
    assert _compile(dragged, TITLE_OP, SHUFFLE_OP) == ["m5", "m4", "m2", "m1", "m3"]  # control
    stripped = _without_unasked_reorders(_editor_turn(ops, TITLE_ASK))
    assert [op["op"] for op in _ops_of(stripped)] == ["edit_text"]
    draft = editor.compile_editor_ops(_job(dragged), dragged, _ops_of(stripped))
    payload = draft.payload.model_dump(mode="json", exclude_none=True)
    assert _media_order(dragged, payload) == list(state.order)
    assert isinstance(stripped, PlannedKriaTurn) and not stripped.clip_order_entries


def test_fm8_the_card_after_a_chat_swap_marks_the_clip_the_swap_moved(
    timeline_open: None, ledger_on: None
) -> None:
    drag = card_mod.drag_entry(M, ["m3", "m1", "m2", "m4", "m5"], "t1", coalesce=False)
    first = build_card(
        current_clip_order(attachment_ids=M, entries=[drag]),
        [{"media_id": m} for m in M],
        events=[],
        brief_version=2,
        applies=True,
    )
    swap = _entry(CREATOR_TEXT, ["m3", "m1", "m2", "m4", "m5"], ["m1", "m3", "m2", "m4", "m5"])
    after = build_card(
        current_clip_order(attachment_ids=M, entries=[drag, swap]),
        [{"media_id": m} for m in M],
        events=[_event(first, drag)],
        brief_version=2,
        applies=None,
    )
    assert [c["media_id"] for c in after["cards"]][:2] == ["m1", "m3"]
    assert len([c for c in after["cards"] if c["moved"]]) == 1
    assert after["shown"] is True


# ── FM16: every reader agrees on a dragged order ──────────────────────────────────────────────


def test_fm16_the_render_contract_readiness_and_the_worker_agree_on_a_dragged_order(
    ledger_on: None,
) -> None:
    """Toroko, then the creator drags the map clip to the end on the card, then a re-plan for
    something unrelated: the contract, readiness and the worker's own plan play the dragged order
    (not the map first again), exactly as they do for a hand edit."""
    from tests.kria.test_clip_order_ledger import _drafted_on, _first_turn, _snapshot_for, _turn

    brief = incident._brief()
    _strategy1, seated = _first_turn(brief)
    to_the_end = [m for m in seated.after_ids if m != incident._media_id(5)] + [
        incident._media_id(5)
    ]
    drag = card_mod.drag_entry(seated.after_ids, to_the_end, "t2", coalesce=False)
    assert drag is not None and drag.actor == CREATOR_DRAG
    turn = _turn([seated, drag])
    strategy = _drafted_on(turn, brief)
    snapshot = _snapshot_for(turn)
    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot=snapshot
    )
    assert contract is not None and not contract.unresolved
    worker = _worker_order(brief, strategy, snapshot)
    assert list(contract.order_ids) == worker == to_the_end
    report = incident._report(brief, strategy, snapshot=snapshot)
    assert report["ready"] is True, report["requirements"]


# ── FM17: the strip lists what the edit plays ─────────────────────────────────────────────────


def test_fm17_a_draft_that_plays_a_subset_lists_only_those_clips_in_ledger_order() -> None:
    drag = card_mod.drag_entry(ATTACH, [E, A, B, C, D], "t1", coalesce=False)
    state = _state(drag)
    subset = card_mod.played_ids({"selected_media_ids": [A, C, E]})
    assert subset == {A, C, E}
    card = build_card(state, ROWS, events=[], brief_version=1, applies=True, only=subset)
    assert [c["media_id"] for c in card["cards"]] == [E, A, C]
    assert [c["position"] for c in card["cards"]] == [1, 2, 3] and card["total"] == 3
    assert card_mod.played_ids({"selected_media_ids": []}) is None  # all of them play
    assert card_mod.played_ids(None) is None


def test_fm17_the_strip_is_the_order_the_render_contract_plays_for_a_subset(
    ledger_on: None,
) -> None:
    """The oracle: the clips the contract pins, in the order it pins them, are the strip's."""
    from tests.kria.test_clip_order_ledger import _drafted_on, _first_turn, _snapshot_for, _turn

    brief = incident._brief()
    _strategy1, seated = _first_turn(brief)
    played = [m for i, m in enumerate(seated.after_ids) if i % 3 != 1]  # 13 of the 19 play
    drag = card_mod.drag_entry(
        seated.after_ids, list(reversed(seated.after_ids)), "t2", coalesce=False
    )
    turn = _turn([seated, drag])
    strategy = {**_drafted_on(turn, brief), "selected_media_ids": played}
    contract = build_render_contract(
        strategy, generation_id="g", brief=brief, media_snapshot=_snapshot_for(turn)
    )
    assert contract is not None
    card = build_card(
        turn.state,
        [{"media_id": m} for m in incident.ATTACHMENT],
        events=[],
        brief_version=1,
        applies=None,
        only=card_mod.played_ids(strategy),
    )
    assert [c["media_id"] for c in card["cards"]] == [
        m for m in contract.order_ids if m in set(played)
    ]
    assert set(contract.order_ids) >= set(played)


# ── FM18: the transcript never holds the drag's placeholder ───────────────────────────────────


def test_fm18_a_drag_is_not_the_creators_words_in_any_transcript_reader() -> None:
    from types import SimpleNamespace

    from app.kria.planner import _full_creator_request

    typed = SimpleNamespace(role="user", content="Put the map video first", payload={})
    drag = SimpleNamespace(
        role="user", content=None, payload={card_mod.OPS_KEY: {"ordered_media_ids": ATTACH}}
    )
    assert _full_creator_request([typed, drag], current_message="Make it 20s") == (
        "Put the map video first\nMake it 20s"
    )


# ── FM14 / capability ─────────────────────────────────────────────────────────────────────────


def test_fm14_the_request_digest_of_a_body_without_the_field_is_what_it_always_was() -> None:
    from app.kria.api_schemas import ClipOrderOpsIn, SubmitTurnBody
    from app.kria.runtime import request_digest

    body = SubmitTurnBody(
        message="Put the map video first", client_event_id="evt-1", expected_thread_revision=4
    )
    # recorded on origin/main before this field existed
    assert request_digest(body) == (
        "0c1ffb37d56fab4475d1222a0d43096529b6e0f5ab53f7599508dfb157678515"
    )
    with_ops = body.model_copy(update={"clip_order_ops": ClipOrderOpsIn(ordered_media_ids=["a"])})
    assert request_digest(with_ops) != request_digest(body)  # a different request under one id


@pytest.mark.asyncio
async def test_the_capability_is_advertised_with_the_flag_and_the_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from types import SimpleNamespace

    from app.routes.creation_threads import capabilities
    from app.services.client_protocol import CLIP_ORDER_CARD_PROTOCOL

    async def caps() -> dict:
        return await capabilities(
            SimpleNamespace(id=uuid.uuid4()),
            native_client=True,
            client_protocol=CLIP_ORDER_CARD_PROTOCOL,
        )

    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", True)
    assert (await caps())["clip_order_cards"] is True
    monkeypatch.setattr(settings, "clip_order_card_min_client_protocol", 999)
    assert (await caps())["clip_order_cards"] is False
    monkeypatch.setattr(settings, "clip_order_card_min_client_protocol", CLIP_ORDER_CARD_PROTOCOL)
    monkeypatch.setattr(settings, "kria_clip_order_ledger_enabled", False)
    manifest = await caps()
    assert manifest["clip_order_cards"] is False
    assert manifest["moment_order_questions"] is True  # its neighbour is untouched


# ── the route: the header the gate reads, and the body the schema takes ───────────────────────


def test_the_turn_route_takes_clip_order_ops_and_the_gate_sees_the_declared_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the real middleware: `X-Kria-Client-Protocol` becomes the protocol `card_gate`
    reads inside the same request, and a drag body (an empty list, an unknown id, a stale offer)
    is accepted by the schema: nothing about it is a 422."""
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app.auth import get_current_user, is_native_client, kria_client_protocol
    from app.database import get_db
    from app.kria.api_schemas import TurnAccepted
    from app.limiter import limiter
    from app.main import app
    from app.routes import kria_runtime
    from app.services.client_protocol import CLIP_ORDER_CARD_PROTOCOL, current_client_protocol

    seen: list[tuple[int | None, Any]] = []

    async def _submit(db, *, thread_id, creator_id, body, locale=None):  # noqa: ANN001, ANN202
        seen.append((current_client_protocol(), body.clip_order_ops))
        return TurnAccepted(turn_id=str(uuid.uuid4()), thread_revision=3, status="completed"), False

    limiter.reset()
    app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(kria_runtime, "submit_turn", _submit)
    user = SimpleNamespace(id=uuid.uuid4(), email="creator@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    app.dependency_overrides[is_native_client] = lambda: True
    app.dependency_overrides[kria_client_protocol] = lambda: CLIP_ORDER_CARD_PROTOCOL
    try:
        client = TestClient(app, raise_server_exceptions=False)
        for ops in (
            {"proposal_id": "stale", "ordered_media_ids": ["x", "y", "x"], "confirm": True},
            {"ordered_media_ids": []},
        ):
            response = client.post(
                f"/creation-threads/{uuid.uuid4()}/turns",
                json={
                    "message": "Order updated",
                    "client_event_id": "evt-1",
                    "expected_thread_revision": 0,
                    "clip_order_ops": ops,
                },
                headers={
                    "Authorization": "Bearer native-session",
                    "X-Kria-Client-Protocol": str(CLIP_ORDER_CARD_PROTOCOL),
                },
            )
            assert response.status_code == 202, response.text
            assert response.json()["status"] == "completed"
        assert [protocol for protocol, _ in seen] == [CLIP_ORDER_CARD_PROTOCOL] * 2
        assert seen[0][1].ordered_media_ids == ["x", "y", "x"] and seen[0][1].confirm is True
        assert seen[1][1].proposal_id == ""
    finally:
        limiter.reset()
        app.dependency_overrides.clear()


# ── FM10 (pure half): a draft carried to the dragged order, on the incident ───────────────────


def test_fm10_a_draft_carried_to_a_drag_is_ready_on_the_same_evidence_and_plays_the_drag(
    ledger_on: None,
) -> None:
    """Toroko: the draft was made on "map video first". The creator drags the map clip to the end
    and approves. `rebound` (what approval applies inside its transaction) must leave a draft whose
    readiness is ready on the SAME evidence (only the strategy digest differs), whose old order row
    no longer undoes the drag, and which the contract and the worker play in the dragged order."""
    from tests.kria.test_clip_order_ledger import _first_turn

    brief = incident._brief()
    strategy = incident._drafted(brief)
    _s1, seated = _first_turn(brief)
    saved = incident._report(brief, strategy, snapshot=incident._snapshot())
    assert saved["ready"] is True
    map_clip = incident._media_id(5)
    to_the_end = [m for m in seated.after_ids if m != map_clip] + [map_clip]
    drag = card_mod.drag_entry(seated.after_ids, to_the_end, "t2", coalesce=False)
    assert drag is not None
    new_strategy, media, state = card_mod.rebound(
        strategy, incident._snapshot(), [seated, drag], incident.ATTACHMENT
    )
    assert list(state.order) == to_the_end
    marks = {i["intent_id"]: i.get("already_seated") for i in new_strategy["resolved_clip_intents"]}
    assert marks["order_map_first"] is True  # the row the drag moved away from
    assert ledger.snapshot_order(media) == to_the_end
    fresh = incident._report(brief, new_strategy, snapshot=media)
    assert fresh["ready"] is True, fresh["requirements"]
    for key in ("brief_digest", "media_digest", "prepared_evidence_digest", "capabilities_digest"):
        assert fresh[key] == saved[key], key
    contract = build_render_contract(
        new_strategy, generation_id="g", brief=brief, media_snapshot=media
    )
    assert contract is not None and not contract.unresolved
    assert list(contract.order_ids) == _worker_order(brief, new_strategy, media) == to_the_end
    # control: the draft as it was (not carried) plays the map clip first again
    assert _worker_order(brief, strategy, incident._snapshot())[0] == map_clip


def test_entries_after_a_draft_are_read_from_the_event_that_made_it() -> None:
    drag = card_mod.drag_entry(ATTACH, [B, A, C, D, E], "t", coalesce=False)
    rows = [
        ("assistant_response", {LEDGER_KEY: [drag.to_payload()]}),  # before the draft: ignored
        ("draft_applied", {"draft_id": "d1"}),
        ("status_update", {LEDGER_KEY: [_entry("ai_story", ATTACH, [C, B, A, D, E]).to_payload()]}),
        ("clip_order_card", {LEDGER_KEY: [drag.to_payload()]}),
        ("clip_order_card", {"x": 1}),
    ]
    after = card_mod.entries_after_draft("d1", rows)
    assert [e.actor for e in after] == ["ai_story", CREATOR_DRAG]
    assert card_mod.entries_after_draft("other", rows) == []

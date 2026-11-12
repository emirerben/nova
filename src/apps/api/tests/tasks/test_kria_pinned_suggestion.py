"""Pinned suggestion taps through the real ``run_kria_turn`` path.

Failure catalogue, written before any seam code. Every row is one fixture below that drives
submit -> claim -> plan -> row-locked commit on real Postgres; none restates an implementation
detail. The rule each row defends: a tap runs the action the card was proven with, through the
ONE existing draft path, or says plainly that it no longer fits. It never reaches the free planner.

Pin intake (submit and claim)
  FM1  flag off: a valid pin is inert. No stamp on the event, none on the claim, the legacy planner
       plans the turn, the pinned planner never runs.
  FM2  unresolvable pin: an `ss_` id missing from THIS thread's ring (another thread's set, an
       evicted set, an unknown id) answers "no longer fits". The planner never sees it.
  FM3  typed words win: a pin whose message is not the stored prompt is the creator's own request
       (KRI-129); it goes to the planner unpinned.
  FM4  a legacy `sc_` chip id with the flag on stays on the KRI-756 path, never pinned.
  FM20 the ring entry changes between submit and run (stored action no longer the stamped one):
       refuse, apply nothing.

Execution
  FM5  exact base, per allowed kind (sfx, transition, music level, caption meta, remove_clip): a new
       draft revision with a real diff, receipts written and met, one typed brief version owned by
       the turn, `can_undo` with the previous head as parent, no approval, no render, no Job, no
       model constructed (copilot, Main Creator, extractor all poisoned).
  FM6  the deferred brief extraction is not scheduled for a pinned turn.
  FM7  stale base (a draft head appeared after the set was served): the action is rebuilt from the
       registry and the current facts and applied once. An index-addressed op (remove_clip
       slot_index) never lands on whichever clip now sits at that index.
  FM8  stale base and the change is gone (already present / no longer available): "no longer fits",
       head unchanged, no brief version, the suggester is asked to refresh.
  FM9  the state moves between planning and the row lock (an editor Save): the in-lock check over
       `editor_base.projected` refuses; nothing is written.
  FM10 the creator's unsaved editor state: the card is rebuilt against it, their edits become the
       parent revision so Undo returns to exactly what they had. A stale client state is the honest
       stale-editor reply.
  FM11 unsaved editor state + a speech-cut op: refused before compile. With the lane allow-list
       widened (a later kind), the existing in-lock refusal still fires: there is no second path.
  FM12 allow-list: a stored action carrying a text-carrying op (add_text, rewrite_text, set_title)
       is refused, so no agent copy reaches the screen through this seam.
  FM13 lost turn lease (another worker reclaimed the turn): the commit refuses, nothing is written.
  FM14 conversation-revision requeue: a real message lands between claim and commit; the turn goes
       back to pending, writes no brief version or draft, and the second run applies exactly once.
  FM15 queued behind an in-flight turn: the stamp survives the queue and the promoted turn plans
       against the state the predecessor left.
  FM16 creative-copy consent (KRI-506): pending or approved copy blocks the draft with the existing
       reply; nothing is applied.
  FM17 duplicate tap: the same client_event_id replays one turn; a late second tap on an applied
       card is "no longer fits", never a second change.
  FM18 a failed render's recovery target, or a render in flight: no apply, an honest reply.
  FM19 Creative Brief off for the creator: the draft applies with no brief rows, like the copilot.

Applied cards (a card is one offer)
  FM21 a card whose value is relative to the current edit (music level: 60% of whatever the mix is
       now) tapped again after it applied: every later tap sees a moved base and would rebuild a
       new value, so one card would stack changes the creator was never offered. The applied card
       is marked on its ring entry in the same transaction as the draft; a rebuild of a marked
       card is refused ("no longer fits") and the suggester is asked for a fresh set.
  FM22 a tap that applied nothing (refused at the row lock) never marks its card: it stays
       tappable once the cause is gone.
  FM23 the suggester writes a new set into the ring between the turn's first read of the thread and
       its row lock: the mark is written from the row as it is under the lock, so that set survives.

Database (collection)
  FM24 the fixtures above skip when no `*_test` Postgres answers, so a laptop run can look green
       with none of them having run. Where `NOVA_REQUIRE_POSTGRES_TESTS` is set (the api shards,
       which provide `nova_test` migrated to head) a missing or unmigrated database fails
       collection instead.
"""

from __future__ import annotations

import sys

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from tests.postgres_required import postgres_required
from tests.tasks import kria_pinned_suggestion_harness as h


def _database_problem() -> str | None:
    name = make_url(settings.database_url).database or ""
    if not name.endswith("_test"):
        return f"refusing to write to non-test database {name!r}"
    try:
        with sync_session() as probe:
            probe.execute(text("select 1 from creative_brief_versions limit 1"))
    except Exception as exc:  # noqa: BLE001 - whatever is wrong, the fixtures cannot run
        return f"nova_test Postgres not reachable or not migrated ({type(exc).__name__})"
    return None


_PROBLEM = _database_problem()
if _PROBLEM and postgres_required():
    # The api shards provide nova_test migrated to head. A skip here would let every fixture
    # in this module go unrun behind a green check.
    pytest.fail(f"{_PROBLEM}: the pinned-tap fixtures must run in CI", pytrace=False)
if _PROBLEM:
    pytest.skip(_PROBLEM, allow_module_level=True)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture
def world(monkeypatch):
    return h.install_world(monkeypatch)


NO_LONGER_FITS = (
    "This suggestion no longer fits your edit. Nothing was changed, and I'm refreshing the ideas."
)
STAGED = "Updated your edit \u2014 it's in the editor now. Save when you're happy with it."
HAND_EDITED = {
    "base_generation": "generation-1",
    "client_state_id": "state-1",
    "lanes": {"text_elements": [{**h.HOOK, "text": "Creator hook"}]},
}


def _last_reply(thread_id) -> str:  # noqa: ANN001
    return [e for e in h.events(thread_id) if e.role == "assistant"][-1].content


def _pin_record(turn_id: str) -> dict:
    return h.turn_row(turn_id).plan_json["diagnostics"]["suggestion_pin"]


def _receipts(thread_id) -> list[dict]:  # noqa: ANN001
    return h.events(thread_id, "draft_applied")[-1].payload.get("requirement_receipts") or []


def _lanes(snap: dict) -> dict:
    return snap["editor_payload"]


async def _tap_and_run(seeded: h.Seeded, kind: str, **kwargs):  # noqa: ANN003, ANN202
    accepted, _ = await h.tap(seeded, kind, **kwargs)
    return accepted, await h.run(accepted.turn_id)


def _refused_untouched(seeded, before, reply=NO_LONGER_FITS):  # noqa: ANN001, ANN202
    assert _last_reply(seeded.thread_id) == reply
    assert h.counts(seeded) == before


# ── Pin intake ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fm1_flag_off_a_valid_pin_is_inert(world, monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    legacy = h.legacy_planner_answers(monkeypatch)
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    before = h.counts(seeded)
    accepted, result = await _tap_and_run(seeded, "sfx")
    assert result["status"] == "completed"
    assert "pinned_suggestion" not in h.events(seeded.thread_id, "user_message")[-1].payload
    legacy.assert_awaited_once()
    assert "pinned_suggestion" not in legacy.await_args.args[0]
    assert _last_reply(seeded.thread_id) == "The planner answered."
    assert h.counts(seeded) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "flag", ["kria_suggestion_pinned_taps_enabled", "kria_suggestion_chips_enabled"]
)
async def test_fm1_either_flag_off_leaves_the_planner_in_charge(world, monkeypatch, flag):
    monkeypatch.setattr(settings, flag, False)
    legacy = h.legacy_planner_answers(monkeypatch)
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await _tap_and_run(seeded, "transition")
    legacy.assert_awaited_once()
    assert h.counts(seeded)["drafts"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["unknown_id", "other_threads_set", "evicted_set"])
async def test_fm2_an_unresolvable_pin_answers_no_longer_fits(world, how):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    tapped = seeded.cards["sfx"]
    pin = {"set_id": seeded.set_id, "chip_id": tapped.id}
    if how == "unknown_id":
        pin["chip_id"] = "ss_000000000000"
    elif how == "other_threads_set":
        other = h.seed_ready_thread()
        await h.serve(other)
        pin = {"set_id": other.set_id, "chip_id": other.cards["sfx"].id}
    else:
        for _ in range(5):  # the ring keeps five sets
            await h.serve(seeded)
        pin = {"set_id": pin["set_id"], "chip_id": tapped.id}
    before = h.counts(seeded)
    accepted, result = await _tap_and_run(seeded, "sfx", tap=pin)
    assert result["status"] == "completed"
    assert h.events(seeded.thread_id, "user_message")[-1].payload["pinned_suggestion"]["unresolved"]
    _refused_untouched(seeded, before)
    assert _pin_record(accepted.turn_id)["reason"] == "unresolved"
    assert h.world_refreshes(world) == ["pinned_stale"]


@pytest.mark.asyncio
async def test_fm2_a_stamping_crash_never_plans_the_card_as_free_text(world, monkeypatch):
    from app.services.suggestions import pinned

    def crash(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("ring unreadable")

    monkeypatch.setattr(pinned, "stamp_pin", crash)
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    before = h.counts(seeded)
    await _tap_and_run(seeded, "sfx")
    _refused_untouched(seeded, before)


@pytest.mark.asyncio
async def test_fm2_no_longer_fits_speaks_the_chats_language(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    before = h.counts(seeded)
    accepted, _ = await h.submit(
        seeded, "Müziği kıs", tap={"set_id": seeded.set_id, "chip_id": "ss_000000000000"}
    )
    await h.run(accepted.turn_id)
    _refused_untouched(
        seeded,
        before,
        reply="Bu öneri artık düzenlemene uymuyor. Hiçbir şey değişmedi, önerileri yeniliyorum.",
    )


@pytest.mark.asyncio
async def test_fm3_typed_words_win_over_a_pin(world, monkeypatch):
    legacy = h.legacy_planner_answers(monkeypatch)
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await _tap_and_run(seeded, "sfx", message="add some sound effects, but quiet ones")
    assert "pinned_suggestion" not in h.events(seeded.thread_id, "user_message")[-1].payload
    legacy.assert_awaited_once()
    assert h.counts(seeded)["drafts"] == 0


@pytest.mark.asyncio
async def test_fm4_a_legacy_chip_id_keeps_the_kri756_path(world, monkeypatch):
    legacy = h.legacy_planner_answers(monkeypatch)
    seeded = h.seed_ready_thread()
    await _tap_and_run_raw(
        seeded, "Add sound effects", {"set_id": "sg_legacy", "chip_id": "sc_abcdef"}
    )
    assert "pinned_suggestion" not in h.events(seeded.thread_id, "user_message")[-1].payload
    legacy.assert_awaited_once()


async def _tap_and_run_raw(seeded, message, tap):  # noqa: ANN001, ANN202
    accepted, _ = await h.submit(seeded, message, tap=tap)
    return accepted, await h.run(accepted.turn_id)


@pytest.mark.asyncio
async def test_fm20_a_ring_entry_that_changed_after_the_tap_applies_nothing(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    accepted, _ = await h.tap(seeded, "sfx")
    with sync_session() as db:
        from app.models import CreationThread

        thread = db.get(CreationThread, seeded.thread_id)
        state = h.copy.deepcopy(thread.state)
        for entry in state["suggestion_sets"]:
            for stored in entry["suggestions"]:
                if stored["id"] == seeded.cards["sfx"].id:
                    stored["action"]["ops"][0]["at_s"] = 1.5
        thread.state = state
        db.commit()
    before = h.counts(seeded)
    assert (await h.run(accepted.turn_id))["status"] == "completed"
    _refused_untouched(seeded, before)
    assert _pin_record(accepted.turn_id)["reason"] == "ring_changed"


# ── Execution ────────────────────────────────────────────────────────────────────────

LANE = {
    "sfx": "sound_effects",
    "transition": "timeline_slots",
    "music": "mix",
    "captions": "caption_meta",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sfx", "transition", "music", "captions"])
async def test_fm5_exact_base_tap_applies_through_the_one_draft_path(world, kind):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    before = h.counts(seeded)
    accepted, result = await _tap_and_run(seeded, kind)
    assert result["status"] == "completed", result
    assert _pin_record(accepted.turn_id) == {
        "set_id": seeded.set_id,
        "id": seeded.cards[kind].id,
        "outcome": "planned",
        "kind": kind,
        "exact": True,
    }
    chat = h.events(seeded.thread_id, "user_message")[-1]
    assert chat.content == seeded.cards[kind].prompt, "the tap reads as the creator's own message"
    assert chat.payload["pinned_suggestion"]["id"] == seeded.cards[kind].id
    # One new head draft, a real change in the lane the card promised, nothing else.
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] + 1
    head = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    assert LANE[kind] in _lanes(head["snap"])
    assert head["snap"]["kind"] == "editor"
    # Receipts are written and met, and a typed requirement is owned by this turn.
    receipts = _receipts(seeded.thread_id)
    assert [r["status"] for r in receipts] == ["met"] and receipts[0]["verification"] == "checked"
    [version] = h.briefs(seeded.thread_id)
    [req] = version.requirements
    assert req["description"] == seeded.cards[kind].prompt
    assert req["source_turn_id"] == accepted.turn_id
    # Render approval stays a separate tap; no model ran; the manifest hash needed no model call.
    assert after["approvals"] == 0 and after["jobs"] == before["jobs"]
    assert h.events(seeded.thread_id, "draft_applied")[-1].payload["render_requested"] is False
    assert not h.events(seeded.thread_id, "approval_requested")
    assert (
        h.turn_row(accepted.turn_id).plan_json["intents"][0]["tool_name"]
        == "draft.apply_editor_ops"
    )
    assert len(h.turn_row(accepted.turn_id).plan_json["intents"]) == 1
    assert h.draft_execution_manifest(seeded) == "b" * 64
    # FM6: the typed update is the extraction, so no deferred extraction is scheduled.
    assert world.extracted == [] and world.refreshes == []


@pytest.mark.asyncio
async def test_fm5_remove_clip_exact_base(world, monkeypatch):
    h.install_dedupe_kind(monkeypatch)
    seeded = h.seed_ready_thread()
    base = await h.current_digest(seeded)
    h.put_cards(
        seeded,
        h.card(
            "duplicates",
            "Remove clip 3, it repeats clip 2",
            [{"op": "remove_clip", "slot_index": 2}],
            base_digest=base,
            media_id="clip-c",
        ),
    )
    accepted, result = await _tap_and_run(seeded, "duplicates")
    assert result["status"] == "completed", result
    head = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    removed = {r["slot_id"] for r in _lanes(head["snap"])["timeline_slots"] if r["removed"]}
    assert removed == {"slot-2"}
    [version] = h.briefs(seeded.thread_id)
    assert version.requirements[0]["scope"] == "clip:clip-c"
    assert [r["status"] for r in _receipts(seeded.thread_id)] == ["met"]


@pytest.mark.asyncio
async def test_undo_restores_the_parent_draft(world):
    from app.kria.drafts import undo_draft

    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await _tap_and_run(seeded, "transition")
    [first] = [d for d in h.drafts(seeded.thread_id) if d["head"]]
    await _tap_and_run(seeded, "sfx")
    second = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    assert second["parent"] == first["id"]
    assert h.events(seeded.thread_id, "draft_applied")[-1].payload["can_undo"] is True
    assert {"timeline_slots", "sound_effects"} <= set(_lanes(second["snap"]))
    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=second["rev"],
        )
    restored = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    assert _lanes(restored["snap"]) == _lanes(first["snap"])
    assert "sound_effects" not in _lanes(restored["snap"])


# ── A base that moved ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fm7_a_stale_base_is_rebuilt_from_the_registry_and_applied_once(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await _tap_and_run(seeded, "transition")  # the draft moves under the other cards
    accepted, result = await _tap_and_run(seeded, "sfx")
    assert result["status"] == "completed", result
    assert _pin_record(accepted.turn_id)["exact"] is False
    head = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    assert {"timeline_slots", "sound_effects"} <= set(_lanes(head["snap"]))
    assert len(_lanes(head["snap"])["sound_effects"]) == 1
    assert len(h.briefs(seeded.thread_id)) == 2


@pytest.mark.asyncio
async def test_fm7_an_index_addressed_op_never_lands_on_the_clip_now_at_that_index(
    world, monkeypatch
):
    h.install_dedupe_kind(monkeypatch)
    seeded = h.seed_ready_thread()
    base = await h.current_digest(seeded)
    h.put_cards(
        seeded,
        h.card(
            "duplicates",
            "Remove clip 3, it repeats clip 2",
            [{"op": "remove_clip", "slot_index": 2}],
            base_digest=base,
            media_id="clip-c",
        ),
    )

    def reorder(variant):  # an editor Save moved clip-c to the middle
        slots = variant["ai_timeline"]["slots"]
        variant["ai_timeline"]["slots"] = [slots[0], slots[2], slots[1]]

    h.edit_variant(seeded, reorder)
    moved = h.job_variant(seeded)["ai_timeline"]["slots"]
    assert moved[2]["media_id"] == "clip-b", "the stored index now addresses another clip"
    accepted, result = await _tap_and_run(seeded, "duplicates")
    assert result["status"] == "completed", result
    assert _pin_record(accepted.turn_id)["exact"] is False
    head = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    rows = _lanes(head["snap"])["timeline_slots"]
    assert [(r["slot_id"], r["removed"]) for r in rows] == [
        ("slot-0", False),
        ("slot-2", True),
        ("slot-1", False),
    ], "clip-c was removed, wherever it sits; clip-b was left alone"


@pytest.mark.asyncio
async def test_fm8_a_stale_card_whose_change_is_already_there_no_longer_fits(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)

    def add_effect(variant):
        variant["sound_effects"] = [
            {
                "id": "fx-1",
                "sound_effect_id": "fx-whoosh",
                "at_s": 0.2,
                "gain": 1.0,
                "source": "user",
            }
        ]

    h.edit_variant(seeded, add_effect)
    before = h.counts(seeded)
    accepted, result = await _tap_and_run(seeded, "sfx")
    assert result["status"] == "completed"
    _refused_untouched(seeded, before)
    assert _pin_record(accepted.turn_id)["reason"] == "base_moved"
    assert h.world_refreshes(world) == ["pinned_stale"]
    assert world.extracted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("switched_on", [False, True])
async def test_fm8_a_card_whose_kind_flag_was_pulled_after_serving_no_longer_fits(
    world, monkeypatch, switched_on
):
    """The kill switch stops a card that was served before it was pulled; with it on, the same
    card applies (so the refusal is the flag's doing)."""
    h.install_dedupe_kind(monkeypatch, flag="kria_suggestion_duplicates_enabled")
    monkeypatch.setattr(settings, "kria_suggestion_duplicates_enabled", switched_on)
    seeded = h.seed_ready_thread()
    h.put_cards(
        seeded,
        h.card(
            "duplicates",
            "Remove clip 3, it repeats clip 2",
            [{"op": "remove_clip", "slot_index": 2}],
            base_digest=await h.current_digest(seeded),
            media_id="clip-c",
        ),
    )
    before = h.counts(seeded)
    await _tap_and_run(seeded, "duplicates")
    if switched_on:
        assert h.counts(seeded)["drafts"] == before["drafts"] + 1
    else:
        _refused_untouched(seeded, before)


@pytest.mark.asyncio
async def test_fm9_state_that_moves_between_planning_and_the_row_lock_is_refused(
    world, monkeypatch
):
    h.install_dedupe_kind(monkeypatch)
    seeded = h.seed_ready_thread()
    base = await h.current_digest(seeded)
    h.put_cards(
        seeded,
        h.card(
            "duplicates",
            "Remove clip 3, it repeats clip 2",
            [{"op": "remove_clip", "slot_index": 2}],
            base_digest=base,
            media_id="clip-c",
        ),
    )

    def reorder(variant):
        slots = variant["ai_timeline"]["slots"]
        variant["ai_timeline"]["slots"] = [slots[0], slots[2], slots[1]]

    h.after_planning(monkeypatch, lambda: h.edit_variant(seeded, reorder))
    before = h.counts(seeded)
    accepted, result = await _tap_and_run(seeded, "duplicates")
    assert result["status"] == "completed", result
    _refused_untouched(seeded, before)
    assert h.world_refreshes(world) == ["pinned_stale"]


@pytest.mark.asyncio
async def test_fm9_red_control_without_the_in_lock_check_the_wrong_clip_is_removed(
    world, monkeypatch
):
    """The fixture is sensitive: neutralise the in-lock comparison and the same race removes
    whichever clip now sits at the stored index."""
    from app.services.suggestions import pinned

    h.install_dedupe_kind(monkeypatch)
    monkeypatch.setattr(pinned, "projected_digest", lambda projected: "same")
    seeded = h.seed_ready_thread()
    base = await h.current_digest(seeded)
    h.put_cards(
        seeded,
        h.card(
            "duplicates",
            "Remove clip 3, it repeats clip 2",
            [{"op": "remove_clip", "slot_index": 2}],
            base_digest=base,
            media_id="clip-c",
        ),
    )

    def reorder(variant):
        slots = variant["ai_timeline"]["slots"]
        variant["ai_timeline"]["slots"] = [slots[0], slots[2], slots[1]]

    h.after_planning(monkeypatch, lambda: h.edit_variant(seeded, reorder))
    await _tap_and_run(seeded, "duplicates")
    head = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    rows = _lanes(head["snap"])["timeline_slots"]
    assert [(r["slot_id"], r["removed"]) for r in rows] == [
        ("slot-0", False),
        ("slot-2", False),
        ("slot-1", True),
    ], "without the guard the tap removed clip-b, the clip the card never named"


# ── The creator's own editor state ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fm10_the_card_is_rebuilt_on_unsaved_editor_state_and_undo_returns_to_it(world):
    from app.kria.drafts import undo_draft

    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    accepted, result = await _tap_and_run(seeded, "sfx", editor_state=HAND_EDITED)
    assert result["status"] == "completed", result
    assert _pin_record(accepted.turn_id)["exact"] is False
    mine, chat = h.drafts(seeded.thread_id)
    assert chat["head"] and chat["parent"] == mine["id"]
    assert _lanes(mine["snap"])["text_elements"][0]["text"] == "Creator hook"
    assert _lanes(chat["snap"])["text_elements"][0]["text"] == "Creator hook"
    assert len(_lanes(chat["snap"])["sound_effects"]) == 1
    assert h.events(seeded.thread_id, "draft_applied")[-1].payload["can_undo"] is True
    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=chat["rev"],
        )
    restored = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    assert _lanes(restored["snap"]) == _lanes(mine["snap"]), "Undo returns to exactly what they had"
    assert h.job_variant(seeded)["text_elements"][0]["text"] == "Old matcha hook"


@pytest.mark.asyncio
async def test_fm10_a_stale_client_state_builds_the_tap_on_the_latest(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    stale = {**HAND_EDITED, "base_generation": "generation-0"}
    accepted, result = await _tap_and_run(seeded, "sfx", editor_state=stale)
    assert result["status"] == "completed", result
    (chat,) = [d for d in h.drafts(seeded.thread_id) if d["head"]]
    assert chat["gen"] == "generation-1", "stamped on the variant's current generation"
    lanes = _lanes(chat["snap"])
    assert len(lanes["sound_effects"]) == 1
    # The creator's dirty text lane rides on the latest; nothing was refused.
    assert lanes["text_elements"][0]["text"] == "Creator hook"


@pytest.mark.asyncio
async def test_fm5_a_tap_that_compiles_to_no_real_change_never_reads_as_done(world, monkeypatch):
    import dataclasses

    from app.services.kria_editor_ops_diff import EditorDiff
    from app.tasks import kria_runtime

    real = kria_runtime.compile_editor_ops

    def compiles_to_nothing(*args, **kwargs):  # noqa: ANN002, ANN003
        return dataclasses.replace(real(*args, **kwargs), diff=EditorDiff())

    monkeypatch.setattr(kria_runtime, "compile_editor_ops", compiles_to_nothing)
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    before = h.counts(seeded)
    accepted, _ = await _tap_and_run(seeded, "sfx")
    _refused_untouched(seeded, before)
    assert _pin_record(accepted.turn_id)["reason"] == "no_change"


@pytest.mark.asyncio
async def test_fm11_a_speech_cut_card_is_refused_before_anything_compiles(world, monkeypatch):
    h.install_dedupe_kind(monkeypatch)
    seeded = h.seed_ready_thread()
    h.put_cards(
        seeded,
        h.card(
            "duplicates",
            "Cut the silences",
            [{"op": "apply_speech_cut_candidate", "candidate_id": "cut_1"}],
        ),
    )
    before = h.counts(seeded)
    accepted, _ = await _tap_and_run(seeded, "duplicates", editor_state=HAND_EDITED)
    _refused_untouched(seeded, before)
    assert _pin_record(accepted.turn_id)["reason"] == "op_not_allowed"


@pytest.mark.asyncio
async def test_fm11_with_the_lane_widened_the_existing_in_lock_refusal_still_fires(
    world, monkeypatch
):
    """No second apply path: a later kind that allows a speech cut still meets the guard that
    refuses it on unsaved editor state."""
    from app.services.kria_editor_ops import SPEECH_CUT_NEEDS_SAVE_REPLY
    from app.services.suggestions import pinned

    h.install_dedupe_kind(monkeypatch)
    monkeypatch.setattr(
        pinned, "PINNED_OP_ALLOW", pinned.PINNED_OP_ALLOW | {"apply_speech_cut_candidate"}
    )
    monkeypatch.setattr(
        "app.services.kria_editor_ops._editor_capabilities",
        lambda *_: {**h.CAPABILITIES, "automatic_cut": True},
    )
    variant = h.ready_variant()
    variant["speech_cut_candidates"] = [
        {"candidate_id": "cut_1", "source": "silence", "status": "pending"}
    ]
    seeded = h.seed_ready_thread(variant)
    base = await h.current_digest(seeded, HAND_EDITED)
    h.put_cards(
        seeded,
        h.card(
            "duplicates",
            "Cut the silences",
            [{"op": "apply_speech_cut_candidate", "candidate_id": "cut_1"}],
            base_digest=base,
        ),
    )
    before = h.counts(seeded)
    await _tap_and_run(seeded, "duplicates", editor_state=HAND_EDITED)
    _refused_untouched(seeded, before, reply=SPEECH_CUT_NEEDS_SAVE_REPLY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op",
    [
        {"op": "add_text", "text": "Agent copy"},
        {"op": "rewrite_text", "bar_index": 0, "text": "Agent copy"},
        {"op": "set_title", "text": "Agent copy"},
        {"op": "edit_text", "bar_index": 0, "text": "Agent copy"},
    ],
)
async def test_fm12_a_text_carrying_op_in_a_stored_action_is_never_applied(world, op):
    seeded = h.seed_ready_thread()
    h.put_cards(seeded, h.card("sfx", "Add sound effects", [op]))
    before = h.counts(seeded)
    accepted, _ = await _tap_and_run(seeded, "sfx")
    _refused_untouched(seeded, before)
    assert _pin_record(accepted.turn_id)["reason"] == "op_not_allowed"
    assert h.job_variant(seeded)["text_elements"][0]["text"] == "Old matcha hook"


# ── The turn's own fences ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fm12_a_stored_op_the_copilot_parser_refuses_is_never_compiled(world):
    seeded = h.seed_ready_thread()
    base = await h.current_digest(seeded)
    h.put_cards(
        seeded,
        h.card(
            "sfx",
            "Add sound effects",
            [{"op": "add_sfx", "effect_id": "fx-not-in-the-catalog", "at_s": 0.5}],
            base_digest=base,
        ),
    )
    before = h.counts(seeded)
    accepted, _ = await _tap_and_run(seeded, "sfx")
    _refused_untouched(seeded, before)
    assert _pin_record(accepted.turn_id)["reason"] == "op_refused"


@pytest.mark.asyncio
async def test_fm13_a_lost_lease_writes_nothing(world, monkeypatch):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    accepted, _ = await h.tap(seeded, "sfx")

    def reclaimed():  # another worker took the turn over while this one planned
        with sync_session() as db:
            turn = db.get(h.CreatorAgentTurn, h.uuid.UUID(accepted.turn_id), with_for_update=True)
            turn.lease_owner = "another-worker"
            turn.lease_epoch = int(turn.lease_epoch) + 1
            db.commit()

    h.after_planning(monkeypatch, reclaimed)
    before = h.counts(seeded)
    assert (await h.run(accepted.turn_id))["status"] == "ignored"
    assert h.counts(seeded) == before
    assert h.turn_row(accepted.turn_id).status == "planning"


@pytest.mark.asyncio
async def test_fm14_a_revision_change_requeues_the_turn_and_it_applies_exactly_once(
    world, monkeypatch
):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    accepted, _ = await h.tap(seeded, "sfx")
    moved = {"done": False}

    def a_real_change():
        if moved["done"]:
            return
        moved["done"] = True
        with sync_session() as db:
            thread = db.get(h.CreationThread, seeded.thread_id, with_for_update=True)
            thread.revision = int(thread.revision) + 1
            db.add(
                h.CreationThreadEvent(
                    thread_id=seeded.thread_id,
                    sequence=int(thread.revision) + 50,
                    revision=int(thread.revision),
                    role="system",
                    event_type="media_added",
                    content=None,
                    payload=None,
                )
            )
            db.commit()

    h.after_planning(monkeypatch, a_real_change)
    before = h.counts(seeded)
    assert (await h.run(accepted.turn_id))["status"] == "requeued"
    assert h.counts(seeded) == before, "a requeued turn writes no draft and no brief version"
    assert h.turn_row(accepted.turn_id).status == "pending"
    assert h.published_turns(world) == [accepted.turn_id]
    assert (await h.run(accepted.turn_id))["status"] == "completed"
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] + 1 and after["briefs"] == before["briefs"] + 1


@pytest.mark.asyncio
async def test_fm15_a_queued_pinned_tap_plans_against_what_its_predecessor_left(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    first, _ = await h.tap(seeded, "transition")
    second, _ = await h.tap(seeded, "sfx")
    assert h.turn_row(second.turn_id).status == "queued"
    assert h.events(seeded.thread_id, "user_message")[-1].payload["pinned_suggestion"]
    assert (await h.run(first.turn_id))["status"] == "completed"
    assert second.turn_id in h.published_turns(world), "the successor was promoted"
    assert (await h.run(second.turn_id))["status"] == "completed"
    assert _pin_record(second.turn_id)["exact"] is False
    head = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    assert {"timeline_slots", "sound_effects"} <= set(_lanes(head["snap"]))


@pytest.mark.asyncio
async def test_fm16_a_decided_title_does_not_block_a_tap_that_leaves_the_title_alone(world):
    """R5: the consent gate reads the ops of a tapped card, not only that a title was decided. A
    sound effect carries no copy, so the creator's own title is none of its business; the cards
    that write the title stay gated (``test_r5_serve_copy_gate.py``)."""
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    digest = await h.media_digest_of(seeded)
    with sync_session() as db:
        thread = db.get(h.CreationThread, seeded.thread_id, with_for_update=True)
        thread.revision = int(thread.revision) + 1
        db.add(
            h.CreationThreadEvent(
                thread_id=seeded.thread_id,
                sequence=10,
                revision=int(thread.revision),
                role="assistant",
                event_type="assistant_response",
                content="Got it.",
                payload={
                    "creative_copy_resolution": {
                        "target": "opening_title",
                        "status": "creator_supplied",
                        "dependency_digest": digest,
                        "text": "My own title",
                    }
                },
            )
        )
        db.commit()
    before = h.counts(seeded)
    _accepted, result = await _tap_and_run(seeded, "sfx")
    assert result["status"] == "completed"
    assert _last_reply(seeded.thread_id) != (
        "Let's review the edit plan with your saved wording before changing this video."
    )
    assert h.counts(seeded)["drafts"] == before["drafts"] + 1


@pytest.mark.asyncio
async def test_fm17_one_client_event_id_is_one_turn(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    asked_at = h.revision(seeded.thread_id)
    first, _ = await h.tap(seeded, "sfx", event_id="one-tap", expected_revision=asked_at)
    replay, _ = await h.tap(seeded, "sfx", event_id="one-tap", expected_revision=asked_at)
    assert replay.replayed and replay.turn_id == first.turn_id
    assert (await h.run(first.turn_id))["status"] == "completed"
    assert (await h.run(first.turn_id))["status"] == "ignored"
    assert h.counts(seeded)["drafts"] == 1 and h.counts(seeded)["briefs"] == 1


@pytest.mark.asyncio
async def test_fm17_the_same_card_tapped_again_changes_nothing_twice(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    first, _ = await h.tap(seeded, "sfx")
    # A second tap while the first is in flight is the identical re-send of the latest message
    # (KRI-552): answered in one line, never planned.
    second, publish = await h.tap(seeded, "sfx")
    assert not publish and second.status == "completed" and second.turn_id != first.turn_id
    assert (await h.run(first.turn_id))["status"] == "completed"
    applied = h.counts(seeded)
    assert applied["drafts"] == 1
    # After it ran, the same card on the old set is a deliberate new ask on a changed edit.
    third, _ = await h.tap(seeded, "sfx")
    assert (await h.run(third.turn_id))["status"] == "completed"
    assert _last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == applied
    head = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    assert len(_lanes(head["snap"])["sound_effects"]) == 1


# ── An applied card is one offer ─────────────────────────────────────────────────────


def _loud_music_variant() -> dict:
    return {**h.ready_variant(), "mix": 0.9}


def _head_mix(seeded: h.Seeded) -> float:
    head = next(d for d in h.drafts(seeded.thread_id) if d["head"])
    return _lanes(head["snap"])["mix"]["music_level"]


def _ring(thread_id) -> list[dict]:  # noqa: ANN001
    with sync_session() as db:
        state = db.execute(
            text("select state from creation_threads where id = :id"), {"id": thread_id}
        ).scalar_one()
    return state["suggestion_sets"]


def _ring_entry(thread_id, set_id: str) -> dict:  # noqa: ANN001
    return next(e for e in _ring(thread_id) if e["set_id"] == set_id)


@pytest.mark.asyncio
async def test_fm21_a_relative_card_tapped_again_never_stacks(world):
    seeded = h.seed_ready_thread(_loud_music_variant())
    await h.serve(seeded)
    first, result = await _tap_and_run(seeded, "music")
    assert result["status"] == "completed" and _pin_record(first.turn_id)["exact"] is True
    applied, level = h.counts(seeded), _head_mix(seeded)
    assert level == seeded.cards["music"].action.ops[0]["music_level"] < 0.9
    for _ in range(2):
        again, result = await _tap_and_run(seeded, "music")
        assert result["status"] == "completed", result
        assert _pin_record(again.turn_id)["reason"] == "consumed"
        assert _last_reply(seeded.thread_id) == NO_LONGER_FITS
        assert h.counts(seeded) == applied and _head_mix(seeded) == level
    assert h.world_refreshes(world) == ["pinned_stale", "pinned_stale"]
    assert world.extracted == []


@pytest.mark.asyncio
async def test_fm21_red_control_without_the_applied_mark_each_tap_steps_the_level_again(
    world, monkeypatch
):
    """The fixture is sensitive: ignore the mark and the same card stacks 0.9 -> 0.54 -> 0.32."""
    from app.services.suggestions import pinned

    monkeypatch.setattr(pinned, "is_consumed", lambda *_a: False)
    seeded = h.seed_ready_thread(_loud_music_variant())
    await h.serve(seeded)
    levels = []
    for _ in range(3):
        await _tap_and_run(seeded, "music")
        levels.append(_head_mix(seeded))
    assert levels[0] > levels[1] > levels[2], levels


@pytest.mark.asyncio
async def test_fm21_a_fresh_set_offers_the_change_again(world):
    seeded = h.seed_ready_thread(_loud_music_variant())
    await h.serve(seeded)
    await _tap_and_run(seeded, "music")
    old_set, old_card = seeded.set_id, seeded.cards["music"]
    await h.serve(seeded)  # the refresh: a new set, built from the edit as it is now
    assert seeded.set_id != old_set and seeded.cards["music"].id != old_card.id
    accepted, result = await _tap_and_run(seeded, "music")
    assert result["status"] == "completed", result
    assert _pin_record(accepted.turn_id)["outcome"] == "planned"
    assert _head_mix(seeded) < 0.54
    assert h.counts(seeded)["drafts"] == 2


@pytest.mark.asyncio
async def test_fm21_the_mark_names_the_card_and_the_turn_that_applied_it(world):
    seeded = h.seed_ready_thread(_loud_music_variant())
    await h.serve(seeded)
    accepted, _ = await _tap_and_run(seeded, "music")
    entry = _ring_entry(seeded.thread_id, seeded.set_id)
    assert entry["consumed"] == {seeded.cards["music"].id: accepted.turn_id}
    assert len(entry["suggestions"]) == len(seeded.cards)


@pytest.mark.asyncio
async def test_fm22_a_tap_refused_at_the_row_lock_leaves_its_card_tappable(world, monkeypatch):
    h.install_dedupe_kind(monkeypatch)
    seeded = h.seed_ready_thread()
    base = await h.current_digest(seeded)
    h.put_cards(
        seeded,
        h.card(
            "duplicates",
            "Remove clip 3, it repeats clip 2",
            [{"op": "remove_clip", "slot_index": 2}],
            base_digest=base,
            media_id="clip-c",
        ),
    )

    def reorder(variant):
        slots = variant["ai_timeline"]["slots"]
        variant["ai_timeline"]["slots"] = [slots[0], slots[2], slots[1]]

    raced: list[bool] = []

    def race_once():
        if not raced:
            raced.append(True)
            h.edit_variant(seeded, reorder)

    h.after_planning(monkeypatch, race_once)
    await _tap_and_run(seeded, "duplicates")
    assert _last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert "consumed" not in _ring_entry(seeded.thread_id, seeded.set_id)
    # The race is over: the same card is asked about the edit as it now stands.
    accepted, result = await _tap_and_run(seeded, "duplicates")
    assert result["status"] == "completed", result
    assert _pin_record(accepted.turn_id)["outcome"] == "planned"
    assert h.counts(seeded)["drafts"] == 1


@pytest.mark.asyncio
async def test_fm23_a_set_written_while_the_turn_commits_survives_the_mark(world, monkeypatch):
    from app.tasks import kria_runtime

    seeded = h.seed_ready_thread(_loud_music_variant())
    await h.serve(seeded)
    newer = {}

    real = kria_runtime._owns_turn_lease

    def lease_after_a_ring_write(*args, **kwargs):  # noqa: ANN002, ANN003
        # Only the draft commit's check: after it first read the thread, before it locks the row.
        if not newer and sys._getframe(1).f_code.co_name == "_complete_draft_turn":
            card = h.card("music", "Turn the music down", [{"op": "set_mix", "music_level": 0.2}])
            newer["set_id"] = h.put_cards(seeded, card)
        return real(*args, **kwargs)

    monkeypatch.setattr(kria_runtime, "_owns_turn_lease", lease_after_a_ring_write)
    set_before_tap = seeded.set_id
    chosen = seeded.cards["music"]
    accepted, _ = await h.tap(
        seeded, "music", tap={"set_id": set_before_tap, "chip_id": chosen.id}, message=chosen.prompt
    )
    assert (await h.run(accepted.turn_id))["status"] == "completed"
    ring = {entry["set_id"]: entry for entry in _ring(seeded.thread_id)}
    assert newer["set_id"] in ring, "the mark overwrote the set the suggester wrote meanwhile"
    assert "consumed" in ring[set_before_tap]


@pytest.mark.asyncio
async def test_fm18_a_render_in_flight_is_said_plainly_and_nothing_changes(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    h.edit_variant(seeded, lambda v: v.update(render_status="rendering"))
    before = h.counts(seeded)
    await _tap_and_run(seeded, "sfx")
    _refused_untouched(
        seeded,
        before,
        reply="Your edit is still rendering \u2014 give it a moment, then ask again.",
    )


@pytest.mark.asyncio
async def test_fm18_a_recovery_target_is_never_pinned(world, monkeypatch):
    """A failed render's correction needs a recovery basis only the copilot path mints."""
    from app.kria import planner

    real = planner._load_editor_target

    async def with_basis(*args, **kwargs):  # noqa: ANN002, ANN003
        target = await real(*args, **kwargs)
        return h.SimpleNamespace(**{**target.__dict__, "recovery_basis": object()})

    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    monkeypatch.setattr(planner, "_load_editor_target", with_basis)
    before = h.counts(seeded)
    await _tap_and_run(seeded, "sfx")
    _refused_untouched(seeded, before)


@pytest.mark.asyncio
async def test_fm19_creative_brief_off_applies_with_no_brief_rows(world, monkeypatch):
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    accepted, result = await _tap_and_run(seeded, "captions")
    assert result["status"] == "completed", result
    assert h.counts(seeded)["drafts"] == 1 and h.counts(seeded)["briefs"] == 0
    assert _last_reply(seeded.thread_id) == STAGED
    assert not _receipts(seeded.thread_id)

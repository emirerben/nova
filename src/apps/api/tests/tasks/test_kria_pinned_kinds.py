"""Every suggestion kind executes on tap through the ONE draft path, or refuses honestly.

Failure catalogue, written before any seam code (the seam so far ran only editor-ops cards). Every
row is a fixture below that drives submit -> claim -> plan -> row-locked commit on real Postgres
through ``run_kria_turn``; none restates an implementation detail. The rule each row defends: a tap
runs the action the card was proven with through ``_complete_draft_turn`` (a new draft revision, a
typed brief version, receipts, Undo to the parent), or says plainly that it no longer fits and
tells the phone so in a machine-readable way. It never reaches the free planner and never writes
half a change.

Per kind (hook, duplicates, mute_clip, filler_words; captions/sfx/transition/music are pinned by
``test_kria_pinned_suggestion`` and re-run here in the all-kinds table)
  K1  stale base. The editor moved since the card was served (index-addressed ``remove_clip``
      lands on another clip), the head draft moved (a strategy restate would replace newer work),
      the brief moved (``expected_version`` of a typed update), a title appeared, the analysis row
      was replaced. Rebuild from the registry and the facts as they are now, or "no longer fits".
  K2  unsaved editor state. A creator's hand edits must survive: editor-ops cards rebuild against
      them (their edits become the parent revision); a strategy restate would re-plan over them, so
      it refuses instead.
  K3  lost lease / requeue by a newer message. Nothing is written for the stale run; the second
      run applies exactly once (the generic fixtures FM13/FM14, re-proven here for a copy card and
      a strategy restate).
  K4  double tap. One ``client_event_id`` replays one turn; a late second tap on an applied card
      (title now present, ask now live, setting now on, clip now gone) is "no longer fits" and
      writes nothing, never a second change.
  K5  consent missing (KRI-506). An open creative-copy question blocks the hook tap inside the lock
      with the existing reply. The tap's own decision never blocks itself (the consent gate does
      not fold ``source="suggestion"``) and never reads as copy the creator wrote.
  K6  KRI-129. A live title requirement hides the hook card; a clip a request names is never the
      one a duplicates card removes; a second mute of a clip the brief already turns off is "no
      longer fits".
  K7  ``expected_version`` conflict. The brief moves between the plan and the row lock: a refusal
      reply, not a "please retry" crash, and no brief version is written.
  K8  ``analysis_redone``. The scheduler re-opens the cleanup analysis on the tap: the setting is
      not flipped, but the re-queued analysis row is committed and published (rolling back would
      orphan it), and the creator gets "no longer fits".
  K9  duplicate keeper already removed / pair changed. The rebuilt card must be exactly the offer:
      the closed prompt rendered from the rebuilt pair equals the stored prompt, else refuse.
  K10 flag off. A kind switched off after serve refuses at the tap; a kill switch stops a card
      that was served before it was pulled. Flag-off ``TurnAccepted`` and the assistant event
      carry no new field (byte-identical).
  K11 old iOS ignoring the new ack field. The field is additive and omitted when absent.
  K12 an old build (no ``suggestion`` field in the ack) still reads the refusal from the reply.
  K13 hook copy laundering. The creator's chat message is the closed card sentence, never the copy
      (``creator_quoted`` bypass); the applied title equals the card's text exactly; Gemini
      metadata can never become the title (the copy gate re-runs at the tap with the clip texts);
      text-carrying ops on an editor-ops card stay refused (FM12) while a hook card may run only
      its own single ``set_title``.
  K14 mute: ``PINNED_TAP_APPLIES_CLIP_SOUND`` stays False, so nothing is served; the seam is proven
      to be able to run the typed update (the constant is patched on only inside the fixture that
      builds the card), and an unproven surface never reaches a creator.
  K15 filler: the setting flip is atomic with the draft (a later raise rolls it back), is refused
      when the capability closed, and hands the scheduled analysis to the publisher after commit.
  K16 a restate never replaces newer work. A strategy restate (mute, filler) is only the plan's own
      next revision when the head draft IS the plan. After any editor-op tap (captions, sfx,
      transition, hook, duplicates) the head is an editor draft whose chain a restated strategy
      would drop (only Undo could bring it back): the tap refuses ``base_moved`` and writes no
      draft, approval, brief version or setting flip. Undo back to the plan makes it tappable again.

Refusals
  R1  every refusal (planner-side or at the commit) answers with the same typed signal on the
      assistant event (``suggestion_pin``: set_id, id, outcome, reason); a transient refusal
      (a render in flight) keeps the card tappable and is not marked.
  R2  a refused card is marked on its ring entry in the refusal's own transaction; the GET hides
      it, mints a NEW set_id (so the phone's "hidden until the set changes" ledger releases and the
      "refreshing" claim is true) and stores the derived set so a tap on a card in it resolves.
  R3  an applied card is hidden by id on the GET even with the editor triggers flag off.
  R5  no refusal is silent. Every end a pinned tap can reach without applying (the consent gate,
      the protected-region mark gate, the unmet-requirement / approval-readiness gate, the choice
      gate, and the exception replies: stale editor state, an op the recipe cannot hold, a text
      timing conflict, a request too long to keep) carries the same typed record on the assistant
      event AND on the turn's plan diagnostics (the replayed ack reads that), marks the card
      refused on its ring entry and so mints a new set on the next read.
  R6  a transient refusal is never marked. Unsaved hand edits say nothing about the card: it stays
      tappable, the reply tells the creator to save first (not "no longer fits"), and the same tap
      applies once the edits are saved.
  R4  ``ready_context`` carries the kinds' inputs (the shared loader), so a rebuild sees what the
      serve saw: the hook's clip texts, the brief, the filler evidence, the duplicates' ``needed``.

Database (collection): same CI-aware guard as ``test_kria_pinned_suggestion`` (FM24).

Per-kind evidence (each row: tap -> new draft revision or the documented setting flip + approval,
the typed requirement, Undo to the parent, and a refusal path)
  captions / sfx / transition / music  test_kria_pinned_suggestion: FM5 (applied, receipts
                                       met), ``test_undo_restores_the_parent_draft``, FM7/8/12
  hook         ``test_hook_tap_applies_the_cards_exact_text_...`` (editor draft, ``set_title``)
  duplicates   ``test_duplicates_tap_removes_the_weaker_take_...`` (editor draft, ``remove_clip``)
  mute_clip    ``test_mute_tap_restates_the_plan_...`` (strategy draft + approval, typed update)
  filler_words ``test_filler_tap_flips_the_setting_...`` (strategy draft + approval, setting flip)

The editor-ops kinds (hook, duplicates and the four above) produce a draft whose diff is a lane of
the editor payload. mute_clip and filler_words cannot be expressed as an editor op, so the tap
restates the thread's newest strategy draft unchanged as a new revision plus its exact render
approval: the typed brief update (or the item setting) is what the tap adds, and the normal
approval is the render. Known limit, by design: speech cleanup is an item setting (like the PATCH
route), so Undo of that draft revision withdraws the revision and its approval, not the toggle.

Fixture realism. The kinds' inputs (the brief, the clip texts, the filler evidence, the duplicates'
``needed``) come from ``kria_pinned_kinds_harness.install_inputs``, a stand-in for the serve lane's
shared loader that both the serve and the seam call; swap it for the real loader at integration.
The measurements are real: ``audio_quality`` over a committed wind recording, the near-duplicate
lane's recorded hashes. The filler analysis row is a stub (the engine run is the kind lane's).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.services.kria_editor_ops import KriaEditorOpError, TextTimingConflict
from app.tasks.kria_runtime import ApprovedCreatorRequestTooLongError
from tests.postgres_required import postgres_required
from tests.tasks import kria_pinned_kinds_harness as k
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
    # FM24: a skip here would let every fixture go unrun behind a green check.
    pytest.fail(f"{_PROBLEM}: the pinned-kind fixtures must run in CI", pytrace=False)
if _PROBLEM:
    pytest.skip(_PROBLEM, allow_module_level=True)


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture
def world(monkeypatch):
    world = h.install_world(monkeypatch)
    k.enable_kinds(monkeypatch)
    return world


NO_LONGER_FITS = (
    "This suggestion no longer fits your edit. Nothing was changed, and I'm refreshing the ideas."
)
HOOK_TEXT = "Whisk first and ask later"
CONSENT_REPLY = "Let's review the edit plan with your saved wording before changing this video."
SAVE_FIRST = "Save your edits first, then tap this suggestion again. Nothing was changed."
HAND_EDITED = {
    "base_generation": "generation-1",
    "client_state_id": "state-1",
    "lanes": {"text_elements": [{**h.HOOK, "text": "Creator hook"}]},
}


async def tap_and_run(seeded, kind, **kwargs):  # noqa: ANN001, ANN003, ANN202
    accepted, _ = await h.tap(seeded, kind, **kwargs)
    return accepted, await h.run(accepted.turn_id)


def last_reply(thread_id) -> str:  # noqa: ANN001
    return [e for e in h.events(thread_id) if e.role == "assistant"][-1].content


def pin_record(turn_id: str) -> dict:
    return h.turn_row(turn_id).plan_json["diagnostics"]["suggestion_pin"]


def head(thread_id):  # noqa: ANN001, ANN202
    return next(d for d in h.drafts(thread_id) if d["head"])


# ── Hook: a copy decision, applied as the card's own exact title ─────────────────────


@pytest.mark.asyncio
async def test_hook_tap_applies_the_cards_exact_text_and_records_the_decision(world, monkeypatch):
    from app.services.creative_copy_decisions import fold_creative_copy

    k.install_inputs(monkeypatch)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT), k.pick("transition", rank=2))
    card = seeded.cards["hook"]
    assert card.copy_text == HOOK_TEXT and card.action.type == "copy_decision"
    _, first = await tap_and_run(seeded, "transition")  # a head draft for the hook to follow
    assert first["status"] == "completed"
    parent = head(seeded.thread_id)
    before = h.counts(seeded)

    accepted, result = await tap_and_run(seeded, "hook")

    assert result["status"] == "completed", result
    assert pin_record(accepted.turn_id)["outcome"] == "planned"
    # The creator's own chat line is the closed card sentence: the copy is never in it (K13).
    chat = h.events(seeded.thread_id, "user_message")[-1]
    assert chat.content == card.prompt and HOOK_TEXT not in chat.content
    # One new head draft whose opening title is exactly the card's text.
    assert h.counts(seeded)["drafts"] == before["drafts"] + 1
    snap = head(seeded.thread_id)["snap"]
    assert snap["editor_payload"]["title"] == HOOK_TEXT
    # The tap is recorded as a suggestion decision, never as words the creator wrote.
    applied = h.events(seeded.thread_id, "draft_applied")[-1].payload
    resolution = applied["creative_copy_resolution"]
    assert resolution["source"] == "suggestion" and resolution["text"] == HOOK_TEXT
    events = [(e.role, e.payload) for e in h.events(seeded.thread_id)]
    digest = await h.media_digest_of(seeded)
    assert fold_creative_copy(events, dependency_digest=digest) == {}  # no consent state (K5)
    folded = fold_creative_copy(events, dependency_digest=digest, include_suggestions=True)
    assert folded["opening_title"].status == "approved"
    assert folded["opening_title"].provenance == "suggestion"
    # Receipts are written for the turn and Undo returns to the parent.
    assert applied["can_undo"] is True and applied["requirement_receipts"]
    assert head(seeded.thread_id)["parent"] == parent["id"]
    [receipt] = applied["requirement_receipts"]
    assert receipt["status"] == "met" and receipt["verification"] == "checked"
    version = h.briefs(seeded.thread_id)[-1]
    req = version.requirements[-1]
    assert req["kind"] == "text" and req["scope"] == "title"
    assert req["description"] == card.prompt, "the requirement's words are the closed sentence"
    assert req["literal"] == HOOK_TEXT, "the approved words are the literal, marked as the server's"
    assert req["source_turn_id"] == accepted.turn_id
    assert h.counts(seeded)["approvals"] == 0 and not h.events(
        seeded.thread_id, "approval_requested"
    )

    # Undo returns to the parent draft (and the title lane is free again).
    from app.kria.drafts import undo_draft

    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=head(seeded.thread_id)["rev"],
        )
    assert "title" not in (head(seeded.thread_id)["snap"]["editor_payload"] or {}) or not head(
        seeded.thread_id
    )["snap"]["editor_payload"].get("title")


def add_creator_title(seeded, digest, text="My own title"):  # noqa: ANN001, ANN201
    """A creator-written opening title lands (the creative-copy consent state the gate reads)."""
    with sync_session() as db:
        thread = db.get(h.CreationThread, seeded.thread_id, with_for_update=True)
        thread.revision = int(thread.revision) + 1
        db.add(
            h.CreationThreadEvent(
                thread_id=seeded.thread_id,
                sequence=50,
                revision=int(thread.revision),
                role="assistant",
                event_type="assistant_response",
                content="Got it.",
                payload={
                    "creative_copy_resolution": {
                        "target": "opening_title",
                        "status": "creator_supplied",
                        "dependency_digest": digest,
                        "text": text,
                    }
                },
            )
        )
        db.commit()


@pytest.mark.asyncio
async def test_hook_consent_the_planner_missed_is_still_stopped_under_the_row_lock(
    world, monkeypatch
):
    """K5: the consent gate (KRI-506) stays inside the lock, with the existing reply.

    The planner's read of the thread started before the creator's own title landed (the inputs
    stand-in is stale on purpose), so only the gate under the row lock can stop the tap."""
    k.install_inputs(monkeypatch, stale_copy_states=True)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT))
    add_creator_title(seeded, await h.media_digest_of(seeded))
    before = h.counts(seeded)
    accepted, result = await tap_and_run(seeded, "hook")
    assert result["status"] == "completed"
    assert (
        last_reply(seeded.thread_id)
        == "Let's review the edit plan with your saved wording before changing this video."
    )
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] and after["briefs"] == before["briefs"]


@pytest.mark.asyncio
async def test_hook_a_title_decided_since_serve_no_longer_fits(world, monkeypatch):
    """K1/K6: the creator's own title wins; the card is not rebuilt around it."""
    k.install_inputs(monkeypatch)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT))
    add_creator_title(seeded, await h.media_digest_of(seeded))
    before = h.counts(seeded)
    accepted, result = await tap_and_run(seeded, "hook")
    assert result["status"] == "completed"
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before
    assert pin_record(accepted.turn_id)["reason"] == "base_moved"


@pytest.mark.asyncio
async def test_hook_a_second_tap_on_the_applied_card_changes_nothing(world, monkeypatch):
    """K4: the title now exists, so the same card is refused and writes nothing."""
    k.install_inputs(monkeypatch)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT))
    await tap_and_run(seeded, "hook")
    applied = h.counts(seeded)
    accepted, result = await tap_and_run(seeded, "hook")
    assert result["status"] == "completed"
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == applied
    assert pin_record(accepted.turn_id)["reason"] == "consumed"


@pytest.mark.asyncio
async def test_hook_a_live_title_requirement_refuses_the_tap(world, monkeypatch):
    """K6 (KRI-129): the creator asked for a title in their own words after the card was served."""
    from app.kria.brief import BriefUpdate

    k.install_inputs(monkeypatch)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT))
    k.write_brief(
        seeded,
        BriefUpdate(operation="add", kind="text", scope="title", description="a title about tea"),
    )
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "hook")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_hook_the_copy_gate_runs_again_at_the_tap_against_the_clip_texts(world, monkeypatch):
    """K13: words the footage analysis wrote can never become the title, even for a stored card.

    The card was served when the analysis said nothing about this phrase; it says so by the time
    of the tap (a late understanding), so the witness at the tap refuses it.
    """
    k.install_inputs(monkeypatch)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT))
    k.understood(seeded, "clip-a", summary="whisk first and ask later at the cafe counter")
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "hook")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_hook_a_text_carrying_op_on_an_editor_ops_card_stays_refused(world):
    """K13/FM12: only a hook card may run its own single set_title."""
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    base = await h.current_digest(seeded)
    h.put_cards(
        seeded,
        h.card(
            "hook",
            "Add a hook that matches the video",
            [{"op": "set_title", "title": "X Y"}],
            base_digest=base,
        ),
    )
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "hook")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert pin_record(accepted.turn_id)["reason"] == "op_not_allowed"
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_hook_kind_switched_off_after_serve_refuses(world, monkeypatch):
    """K10: a kill switch stops a card that was served before it was pulled."""
    k.install_inputs(monkeypatch)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT))
    monkeypatch.setattr(settings, "kria_suggestion_hook_copy_enabled", False)
    before = h.counts(seeded)
    await tap_and_run(seeded, "hook")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before


# ── Duplicates: keep the better of two takes ─────────────────────────────────────────

DUP_PROMPT = "Keep the better of clips 1, 2"


async def serve_duplicates(seeded, monkeypatch, *, needed=None, also_transition=False):  # noqa: ANN001, ANN201
    k.install_inputs(monkeypatch, needed=needed if needed is not None else [])
    k.seed_duplicate_pair(seeded)
    picks = [k.pick("duplicates", targets=("c1", "c2"))]
    if also_transition:
        picks.append(k.pick("sfx", rank=2))
    await k.serve_cards(seeded, *picks)
    return seeded.cards["duplicates"]


def removed_slots(thread_id) -> set[str]:  # noqa: ANN001
    snap = head(thread_id)["snap"]["editor_payload"]
    return {r["slot_id"] for r in snap.get("timeline_slots") or [] if r["removed"]}


@pytest.mark.asyncio
async def test_duplicates_tap_removes_the_weaker_take_and_undo_restores_it(world, monkeypatch):
    seeded = h.seed_ready_thread()
    card = await serve_duplicates(seeded, monkeypatch, also_transition=True)
    assert card.prompt == DUP_PROMPT and card.anchor.media_id == "clip-a"
    assert card.action.type == "editor_ops" and card.action.ops == [
        {"op": "remove_clip", "slot_index": 0}
    ]
    await tap_and_run(seeded, "sfx")  # a head draft to follow
    parent = head(seeded.thread_id)
    before = h.counts(seeded)

    accepted, result = await tap_and_run(seeded, "duplicates")

    assert result["status"] == "completed", result
    assert pin_record(accepted.turn_id)["kind"] == "duplicates"
    assert h.counts(seeded)["drafts"] == before["drafts"] + 1
    assert removed_slots(seeded.thread_id) == {"slot-0"}, "the weaker take (clip-a) went"
    assert head(seeded.thread_id)["parent"] == parent["id"]
    applied = h.events(seeded.thread_id, "draft_applied")[-1].payload
    [receipt] = applied["requirement_receipts"]
    assert receipt["status"] == "met" and receipt["verification"] == "checked"
    req = h.briefs(seeded.thread_id)[-1].requirements[-1]
    # The requirement says what the tap did, not the pair sentence a replan could read as "keep".
    assert req["scope"] == "clip:clip-a" and req["description"] == "Remove clip 1"
    assert h.counts(seeded)["approvals"] == 0

    from app.kria.drafts import undo_draft

    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=head(seeded.thread_id)["rev"],
        )
    assert removed_slots(seeded.thread_id) == set()


@pytest.mark.asyncio
async def test_duplicates_a_reordered_edit_rebuilds_the_pair_from_the_anchor(world, monkeypatch):
    """K1/K9: the stored op addressed slot 0; the edit was reordered, so the same index is now
    another clip. The rebuild finds the pair again by media id and removes the right one."""
    seeded = h.seed_ready_thread()
    await serve_duplicates(seeded, monkeypatch)

    def reorder(variant):  # clip-b, clip-a, clip-c
        slots = variant["ai_timeline"]["slots"]
        variant["ai_timeline"]["slots"] = [slots[1], slots[0], slots[2]]

    h.edit_variant(seeded, reorder)
    accepted, result = await tap_and_run(seeded, "duplicates")

    assert result["status"] == "completed", result
    assert pin_record(accepted.turn_id)["exact"] is False
    assert removed_slots(seeded.thread_id) == {"slot-0"}, "clip-a, wherever it sits now"
    assert h.counts(seeded)["drafts"] == 1


@pytest.mark.asyncio
async def test_duplicates_the_keeper_removed_since_serve_no_longer_fits(world, monkeypatch):
    """K9: with the better take gone there is no pair; nothing is removed."""
    seeded = h.seed_ready_thread()
    await serve_duplicates(seeded, monkeypatch)

    def drop_keeper(variant):
        for slot in variant["ai_timeline"]["slots"]:
            if slot["media_id"] == "clip-b":
                slot["removed"] = True

    h.edit_variant(seeded, drop_keeper)
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "duplicates")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before
    assert pin_record(accepted.turn_id)["reason"] in {"base_moved", "op_refused"}


@pytest.mark.asyncio
async def test_duplicates_a_clip_the_request_now_names_is_never_removed(world, monkeypatch):
    """K6 (KRI-129): the creator asked for clip 1 after the card was served."""
    needed: list[str] = []
    seeded = h.seed_ready_thread()
    await serve_duplicates(seeded, monkeypatch, needed=needed)
    needed.append("clip-a")
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "duplicates")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before
    assert pin_record(accepted.turn_id)["reason"] in {"base_moved", "op_refused"}


@pytest.mark.asyncio
async def test_duplicates_a_changed_group_is_not_what_the_creator_tapped(world, monkeypatch):
    """K9: the base moved AND a third clip now matches the pair. The rebuilt card would remove
    two clips; the closed sentence it renders is not the one the creator tapped, so it refuses."""
    from app.services.clip_signals import near_dup_hash

    seeded = h.seed_ready_thread()
    await serve_duplicates(seeded, monkeypatch, also_transition=True)
    await tap_and_run(seeded, "sfx")  # the base moves
    third = {c["media_id"]: c for c in k.dup_case()["clips"]}["m2"]
    k.set_analysis(
        seeded,
        "clip-c",
        {
            "signals": {
                near_dup_hash.NAME: {
                    "v": near_dup_hash.VERSION,
                    "data": {
                        "hashes": third["hashes"],
                        "sharpness": 90,
                        "mean_luma": third["mean_luma"],
                    },
                }
            }
        },
    )
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "duplicates")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before
    assert pin_record(accepted.turn_id)["reason"] == "base_moved"


# ── Refusals: a typed signal, a new set, and no card offered twice ───────────────────


def signal_of(thread_id):  # noqa: ANN001, ANN201
    """The typed record the phone reads off the last assistant event."""
    return [e for e in h.events(thread_id) if e.role == "assistant"][-1].payload.get(
        "suggestion_pin"
    )


@pytest.mark.asyncio
async def test_a_refused_tap_tells_the_phone_in_a_typed_field(world, monkeypatch):
    """R1/K12: planner-side and commit-side refusals carry the same record; an old build that
    ignores it still reads the refusal from the reply."""
    k.install_inputs(monkeypatch)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT), k.pick("sfx", rank=2))
    monkeypatch.setattr(settings, "kria_suggestion_hook_copy_enabled", False)
    accepted, _ = await tap_and_run(seeded, "hook")  # planner-side: the kind was pulled
    assert signal_of(seeded.thread_id) == {
        "set_id": seeded.set_id,
        "id": seeded.cards["hook"].id,
        "outcome": "refused",
        "reason": "op_not_allowed",
    }
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS

    # Commit-side: the editor moved between the plan and the row lock.
    def reorder(variant):
        slots = variant["ai_timeline"]["slots"]
        variant["ai_timeline"]["slots"] = [slots[1], slots[0], slots[2]]

    h.after_planning(monkeypatch, lambda: h.edit_variant(seeded, reorder))
    await tap_and_run(seeded, "sfx")
    assert signal_of(seeded.thread_id)["outcome"] == "refused"
    assert signal_of(seeded.thread_id)["id"] == seeded.cards["sfx"].id
    assert signal_of(seeded.thread_id)["reason"] == "base_moved"


@pytest.mark.asyncio
async def test_a_tap_that_applied_carries_no_refusal_record(world):
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    await tap_and_run(seeded, "sfx")
    assert signal_of(seeded.thread_id) is None


@pytest.mark.asyncio
async def test_the_ack_says_refused_for_a_card_the_thread_never_had_and_nothing_else(world):
    """R1/K11: additive, and absent everywhere else (an old build sees the ack it always did)."""
    seeded = h.seed_ready_thread()
    await h.serve(seeded)
    unknown_pin = {"set_id": seeded.set_id, "chip_id": "ss_000000000000"}
    asked_at = h.revision(seeded.thread_id)
    unknown, _ = await h.tap(
        seeded, "sfx", message="Make it punchier", tap=unknown_pin, expected_revision=asked_at
    )
    assert unknown.suggestion_outcome == "refused"
    assert '"suggestion_outcome":"refused"' in unknown.model_dump_json()
    # A replay of the turn that was refused says so too, once the worker has answered it.
    await h.run(unknown.turn_id)
    replay, _ = await h.submit(
        seeded,
        "Make it punchier",
        tap=unknown_pin,
        event_id=h.turn_row(unknown.turn_id).client_event_id,
        expected_revision=asked_at,
    )
    assert replay.replayed and replay.suggestion_outcome == "refused"
    # An ordinary tap: no field at all, so an old build reads the ack it always did.
    good, _ = await h.tap(seeded, "sfx")
    assert good.suggestion_outcome is None
    assert "suggestion_outcome" not in good.model_dump_json()


@pytest.mark.asyncio
async def test_a_refusal_mints_a_new_set_and_the_refused_card_is_not_offered_again(
    world, monkeypatch
):
    """R2: the phone hides a tapped card until the set id changes, so a refusal must change it;
    the new set is stored, so a tap on a card in it resolves."""
    seeded = h.seed_ready_thread()
    await k.serve_cards(seeded)  # the deterministic set: what the GET serves with no agent
    served = await k.get_suggestions(seeded)
    assert served.set_id == seeded.set_id and {c.id for c in served.suggestions} == {
        c.id for c in seeded.cards.values()
    }
    refused = seeded.cards["sfx"]
    # Refused: the stored op no longer proves (tampered ring), on the exact base.
    with sync_session() as db:
        thread = db.get(h.CreationThread, seeded.thread_id, with_for_update=True)
        state = h.copy.deepcopy(thread.state)
        for entry in state["suggestion_sets"]:
            for stored in entry["suggestions"]:
                if stored["id"] == refused.id:
                    stored["action"]["ops"][0]["effect_id"] = "fx-not-in-the-catalog"
        thread.state = state
        db.commit()
    accepted, _ = await tap_and_run(seeded, "sfx")
    assert signal_of(seeded.thread_id)["reason"] in {"ring_changed", "op_refused"}
    entry = k.ring_entry(seeded.thread_id, seeded.set_id)
    assert entry["refused"] == {refused.id: accepted.turn_id}

    again = await k.get_suggestions(seeded)
    assert again.set_id != seeded.set_id, "a new set, so the phone's ledger lets go"
    assert refused.id not in {c.id for c in again.suggestions}
    assert {c.id for c in again.suggestions} == {
        c.id for c in seeded.cards.values() if c.id != refused.id
    }
    assert [c.rank for c in again.suggestions] == list(range(1, len(again.suggestions) + 1))
    # The same read twice is the same set (stable), and it is in the ring with its cards.
    assert (await k.get_suggestions(seeded)).set_id == again.set_id
    minted = k.ring_entry(seeded.thread_id, again.set_id)
    assert {c["id"] for c in minted["suggestions"]} == {c.id for c in again.suggestions}
    assert "refused" not in minted and "consumed" not in minted
    # A tap on a card of the new set resolves and applies.
    seeded.set_id = again.set_id
    _, result = await tap_and_run(seeded, "transition")
    assert result["status"] == "completed"
    assert h.counts(seeded)["drafts"] == 1


@pytest.mark.asyncio
async def test_an_applied_card_is_hidden_by_id_even_with_the_editor_triggers_flag_off(world):
    """R3: the card the creator just applied is not offered again from the same set."""
    seeded = h.seed_ready_thread()
    await k.serve_cards(seeded)
    assert not settings.kria_suggestion_editor_triggers_enabled
    before = await k.get_suggestions(seeded)
    assert {c.id for c in before.suggestions} == {c.id for c in seeded.cards.values()}
    await tap_and_run(seeded, "sfx")
    after = await k.get_suggestions(seeded)
    assert seeded.cards["sfx"].id not in {c.id for c in after.suggestions}
    assert seeded.cards["transition"].id in {c.id for c in after.suggestions}


@pytest.mark.asyncio
async def test_a_transient_refusal_keeps_the_card_tappable(world, monkeypatch):
    """R1: a render in flight says nothing about the card; it is not marked and no set is minted."""
    seeded = h.seed_ready_thread()
    await h.serve(seeded)

    def in_flight(variant):
        variant["render_status"] = "rendering"

    h.edit_variant(seeded, in_flight)
    accepted, _ = await tap_and_run(seeded, "sfx")
    assert pin_record(accepted.turn_id)["reason"] == "target_unavailable"
    assert signal_of(seeded.thread_id)["reason"] == "target_unavailable"
    assert "refused" not in k.ring_entry(seeded.thread_id, seeded.set_id)


# ── No refusal is silent: every gate and error reply of a tap is typed, marked and replayable ──


async def tap_run_and_replay(seeded, kind, **kwargs):  # noqa: ANN001, ANN003, ANN202
    """Tap, let the worker answer, then submit the very same tap again (a client retry).

    Returns the first ack and the replayed ack: the phone reads ``suggestion_outcome`` off both."""
    card = seeded.cards[kind]
    asked_at = h.revision(seeded.thread_id)
    accepted, _ = await h.tap(seeded, kind, expected_revision=asked_at, **kwargs)
    await h.run(accepted.turn_id)
    replay, _ = await h.submit(
        seeded,
        card.prompt,
        tap={"set_id": seeded.set_id, "chip_id": card.id},
        event_id=h.turn_row(accepted.turn_id).client_event_id,
        expected_revision=asked_at,
        editor_state=kwargs.get("editor_state"),
    )
    return accepted, replay


async def assert_typed_refusal(seeded, kind, accepted, replay, reason, *, marked=True):  # noqa: ANN001
    """R5: the same record on the assistant event and the plan, the replayed ack says refused, and
    a card the refusal says something about is marked (the next read hides it, mints a set)."""
    card = seeded.cards[kind]
    record = {"set_id": seeded.set_id, "id": card.id, "outcome": "refused", "reason": reason}
    assert signal_of(seeded.thread_id) == record
    assert pin_record(accepted.turn_id) == record
    assert replay.replayed and replay.suggestion_outcome == "refused"
    entry = k.ring_entry(seeded.thread_id, seeded.set_id)
    if not marked:
        assert "refused" not in entry
        return
    assert entry["refused"] == {card.id: accepted.turn_id}
    again = await k.get_suggestions(seeded)
    assert again.set_id != seeded.set_id, "a new set, so the phone's ledger lets go"
    assert card.id not in {c.id for c in again.suggestions}


@pytest.mark.asyncio
async def test_the_consent_gate_refusal_is_typed_marked_and_replayable(world, monkeypatch):
    """R5/K5: the creative-copy consent gate (KRI-506) ends the hook tap inside the row lock; that
    reply carries the same record as every other refusal."""
    k.install_inputs(monkeypatch, stale_copy_states=True)
    seeded = h.seed_ready_thread(k.ready_variant_without_title())
    await k.serve_cards(seeded, k.pick("hook", copy_text=HOOK_TEXT), k.pick("sfx", rank=2))
    add_creator_title(seeded, await h.media_digest_of(seeded))
    before = h.counts(seeded)

    accepted, replay = await tap_run_and_replay(seeded, "hook")

    assert last_reply(seeded.thread_id) == CONSENT_REPLY
    await assert_typed_refusal(seeded, "hook", accepted, replay, "consent_required")
    assert h.counts(seeded)["drafts"] == before["drafts"]


@pytest.mark.asyncio
async def test_the_readiness_gate_refusal_is_typed_marked_and_replayable(
    world, monkeypatch, published
):
    """R5: a live ask no draft can meet holds a restate back (approval readiness); the creator is
    told what is missing, and the tap's card is spent in the same typed way."""
    from app.kria.brief import BriefUpdate

    monkeypatch.setattr(settings, "kria_approval_readiness_enabled", True)
    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, "filler_words")
    k.write_brief(
        seeded,
        BriefUpdate(
            kind="text", scope="title", literal="Hello Moon", description="Title says Hello Moon"
        ),
    )
    drafts_before = h.counts(seeded)["drafts"]

    accepted, replay = await tap_run_and_replay(seeded, "filler_words")

    assert last_reply(seeded.thread_id).startswith("I can't make this yet")
    await assert_typed_refusal(seeded, "filler_words", accepted, replay, "requirements_not_ready")
    assert h.counts(seeded)["drafts"] == drafts_before and h.counts(seeded)["approvals"] == 0
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False, "the flip rolled back with it"


@pytest.mark.parametrize(
    ("gate", "reason"),
    [("_protected_region_gate_sync", "mark_needed"), ("_unresolved_choice_plan", "choice_needed")],
)
@pytest.mark.asyncio
async def test_the_question_gates_refusal_is_typed_marked_and_replayable(
    world, monkeypatch, published, gate, reason
):
    """R5: the mark gate and the choice gate each end a restate with a question for the creator.
    Their own predicates are exercised by their own suites (they need a protected-object brief
    and an open conflict); here the real turn path is driven with the gate's verdict forced, so
    what is proven is that the reply a gate produces never leaves a tap silent."""
    from app.kria.contracts import KriaTurnPlan
    from app.tasks import kria_runtime

    asked = KriaTurnPlan(mode="respond", turn_value="question", response="Which one do you mean?")
    verdict = (
        (asked, {"requirement_ids": ["r1"]})
        if gate == "_protected_region_gate_sync"
        else (
            asked,
            "unresolved_choice",
        )
    )
    monkeypatch.setattr(kria_runtime, gate, lambda *args, **kwargs: verdict)
    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, "filler_words")

    accepted, replay = await tap_run_and_replay(seeded, "filler_words")

    assert last_reply(seeded.thread_id) == "Which one do you mean?"
    await assert_typed_refusal(seeded, "filler_words", accepted, replay, reason)
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False


def _raises(error):  # noqa: ANN001, ANN202
    def compile_that_fails(*args, **kwargs):  # noqa: ANN002, ANN003
        raise error

    return compile_that_fails


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (lambda: KriaEditorOpError("speed is not supported on this recipe"), "op_refused"),
        (lambda: TextTimingConflict(1.0, 2.0, ("t1",)), "op_refused"),
        (lambda: ApprovedCreatorRequestTooLongError(), "request_too_long"),
    ],
    ids=["op_unsupported", "text_timing", "request_too_long"],
)
@pytest.mark.asyncio
async def test_a_commit_error_reply_is_typed_marked_and_replayable(
    world, monkeypatch, error, reason
):
    """R5: the errors the draft commit answers with a reply (not a crash) are refusals too."""
    from app.tasks import kria_runtime

    seeded = h.seed_ready_thread()
    await k.serve_cards(seeded)
    monkeypatch.setattr(kria_runtime, "compile_editor_ops", _raises(error()))
    before = h.counts(seeded)

    accepted, replay = await tap_run_and_replay(seeded, "sfx")

    await assert_typed_refusal(seeded, "sfx", accepted, replay, reason)
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_a_state_gone_stale_under_the_lock_is_typed_but_leaves_the_card_tappable(
    world, monkeypatch
):
    """R5/R6: the phone's unsaved state was built on a render that a Save replaced between the
    plan and the row lock. Nothing is wrong with the card, so the record is typed (the phone can
    tell) and the card is not spent."""
    seeded = h.seed_ready_thread()
    await k.serve_cards(seeded)

    def a_save_replaced_the_render(variant):
        variant["render_generation_id"] = "generation-2"

    h.after_planning(monkeypatch, lambda: h.edit_variant(seeded, a_save_replaced_the_render))
    before = h.counts(seeded)

    accepted, replay = await tap_run_and_replay(seeded, "sfx", editor_state=HAND_EDITED)

    await assert_typed_refusal(seeded, "sfx", accepted, replay, "target_unavailable", marked=False)
    assert h.counts(seeded) == before


# ── Mute a noisy clip: a typed brief update, re-planned from the thread's own plan ───


def mute_world(monkeypatch):  # noqa: ANN001, ANN201
    """The kind's dark constant is patched on only here, to BUILD the card and run its tap; as
    shipped (False) nothing is ever offered (see the last mute fixture)."""
    from app.services.suggestions.kinds import mute_clip

    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    k.install_inputs(monkeypatch)


async def serve_mute(seeded):  # noqa: ANN001, ANN201
    k.seed_strategy_draft(seeded)
    k.noisy_audio(seeded, "clip-b")
    await k.serve_cards(seeded, k.pick("mute_clip", targets=("c2",)))
    return seeded.cards["mute_clip"]


@pytest.mark.asyncio
async def test_mute_tap_restates_the_plan_with_the_typed_update_and_asks_for_approval(
    world, monkeypatch
):
    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    card = await serve_mute(seeded)
    assert card.action.type == "brief_update" and card.prompt == "Mute the sound of clip 2"
    parent = head(seeded.thread_id)
    before = h.counts(seeded)

    accepted, result = await tap_and_run(seeded, "mute_clip")

    assert result["status"] == "awaiting_approval", result
    assert pin_record(accepted.turn_id)["kind"] == "mute_clip"
    # The typed update the card carries is the brief version this turn owns (K7 bound it).
    version = h.briefs(seeded.thread_id)[-1]
    [req] = version.requirements
    assert req["kind"] == "audio" and req["scope"] == "clip:clip-b"
    assert req["facts"] == {"clip_sound": "off"} and req["source_turn_id"] == accepted.turn_id
    # A new draft revision restating the plan (strategy kind), a render approval, no Job yet.
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] + 1 and after["approvals"] == 1
    assert after["jobs"] == before["jobs"]
    new = head(seeded.thread_id)
    assert new["snap"]["kind"] == "strategy" and new["parent"] == parent["id"]
    assert new["snap"]["strategy"] == parent["snap"]["strategy"], "the plan itself is unchanged"
    plan = h.turn_row(accepted.turn_id).plan_json
    assert [i["tool_name"] for i in plan["intents"]] == ["draft.apply_strategy", "render.request"]
    assert h.events(seeded.thread_id, "approval_requested")
    # The chat line is the card's closed sentence; no model ran.
    assert h.events(seeded.thread_id, "user_message")[-1].content == card.prompt
    assert world.extracted == []


def approvals_pending(thread_id) -> int:  # noqa: ANN001
    with sync_session() as db:
        return len(
            db.execute(
                h.select(h.CreatorAgentApproval).where(
                    h.CreatorAgentApproval.thread_id == thread_id,
                    h.CreatorAgentApproval.status == "pending",
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_mute_undo_restores_the_parent_plan_and_withdraws_the_approval(world, monkeypatch):
    from app.kria.drafts import undo_draft

    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    await serve_mute(seeded)
    parent = head(seeded.thread_id)
    await tap_and_run(seeded, "mute_clip")
    assert approvals_pending(seeded.thread_id) == 1
    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=head(seeded.thread_id)["rev"],
        )
    assert head(seeded.thread_id)["snap"] == parent["snap"]
    assert approvals_pending(seeded.thread_id) == 0
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False  # nothing else was touched


@pytest.mark.asyncio
async def test_mute_as_shipped_nothing_is_offered_and_a_stored_card_is_refused(world, monkeypatch):
    """K14: PINNED_TAP_APPLIES_CLIP_SOUND is False in the code. An unproven surface never reaches
    a creator, and a card stored while it was patched on stops working the moment it is off."""
    from app.services.suggestions.kinds import mute_clip

    assert mute_clip.PINNED_TAP_APPLIES_CLIP_SOUND is False
    k.install_inputs(monkeypatch)
    seeded = h.seed_ready_thread()
    k.seed_strategy_draft(seeded)
    k.noisy_audio(seeded, "clip-b")
    await k.serve_cards(seeded, k.pick("mute_clip", targets=("c2",)))
    assert "mute_clip" not in seeded.cards

    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    await k.serve_cards(seeded, k.pick("mute_clip", targets=("c2",)))
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", False)
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "mute_clip")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_mute_a_brief_that_moved_since_serve_is_rebuilt_at_the_current_version(
    world, monkeypatch
):
    """K1: the update is bound to a brief version; an unrelated requirement landed since."""
    from app.kria.brief import BriefUpdate

    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    card = await serve_mute(seeded)
    assert card.action.expected_version == 0
    k.write_brief(
        seeded, BriefUpdate(operation="add", kind="style", scope="global", description="warmer")
    )
    accepted, result = await tap_and_run(seeded, "mute_clip")
    assert result["status"] == "awaiting_approval", result
    assert pin_record(accepted.turn_id)["exact"] is False
    version = h.briefs(seeded.thread_id)[-1]
    assert version.version == 2 and version.requirements[-1]["facts"] == {"clip_sound": "off"}


@pytest.mark.asyncio
async def test_mute_a_brief_that_moves_between_plan_and_lock_is_a_refusal_not_a_crash(
    world, monkeypatch
):
    """K7: expected_version conflict inside the lock: no retry error, no brief version, no draft."""
    from app.kria.brief import BriefUpdate

    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    await serve_mute(seeded)
    moved = {"done": False}

    def brief_moves():
        if not moved["done"]:
            moved["done"] = True
            k.write_brief(
                seeded, BriefUpdate(operation="add", kind="style", scope="global", description="x")
            )

    h.after_planning(monkeypatch, brief_moves)
    before = h.counts(seeded)
    accepted, result = await tap_and_run(seeded, "mute_clip")
    assert result["status"] == "completed", result
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] and after["approvals"] == 0
    assert after["briefs"] == before["briefs"] + 1, "only the version that moved it"
    assert signal_of(seeded.thread_id)["reason"] == "base_moved"


@pytest.mark.asyncio
async def test_mute_a_live_sound_ask_blocks_the_card(world, monkeypatch):
    """K6 (KRI-129): the creator said to keep that clip's sound after the card was served."""
    from app.kria.brief import BriefUpdate

    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    await serve_mute(seeded)
    k.write_brief(
        seeded,
        BriefUpdate(
            operation="add",
            kind="audio",
            scope="clip:clip-b",
            description="keep the sound of clip 2",
            facts={"clip_sound": "on"},
        ),
    )
    before = h.counts(seeded)
    await tap_and_run(seeded, "mute_clip")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_mute_a_second_tap_on_the_applied_card_changes_nothing(world, monkeypatch):
    """K4: while the first tap's approval waits, the identical re-send is answered in one line
    (KRI-552); after the draft is withdrawn the card is a deliberate new ask on a changed brief
    and is refused as already applied. Neither writes anything."""
    from app.kria.drafts import undo_draft

    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    await serve_mute(seeded)
    await tap_and_run(seeded, "mute_clip")
    applied = h.counts(seeded)
    second, publish = await h.tap(seeded, "mute_clip")
    assert not publish and second.status == "completed"
    assert "same message as the draft waiting" in last_reply(seeded.thread_id)
    assert h.counts(seeded) == applied
    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=head(seeded.thread_id)["rev"],
        )
    undone = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "mute_clip")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == undone
    assert pin_record(accepted.turn_id)["reason"] == "consumed"


@pytest.mark.asyncio
async def test_mute_unsaved_editor_edits_are_never_re_planned_over(world, monkeypatch):
    """K2: restating the plan would drop the creator's hand edits, so it refuses instead."""
    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    await serve_mute(seeded)
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "mute_clip", editor_state=HAND_EDITED)
    assert last_reply(seeded.thread_id) == SAVE_FIRST
    assert pin_record(accepted.turn_id)["reason"] == "unsaved_edits"
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_mute_a_thread_with_no_plan_to_restate_refuses(world, monkeypatch):
    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    k.noisy_audio(seeded, "clip-b")  # no strategy draft was ever written
    await k.serve_cards(seeded, k.pick("mute_clip", targets=("c2",)))
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "mute_clip")
    assert pin_record(accepted.turn_id)["reason"] == "no_strategy"
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_mute_a_newer_head_draft_under_the_lock_refuses_and_a_requeue_applies_once(
    world, monkeypatch
):
    """K3/K17: another turn moved the head draft after planning; and a message that lands between
    claim and commit requeues the turn, which then applies exactly once."""
    mute_world(monkeypatch)
    seeded = h.seed_ready_thread()
    await serve_mute(seeded)
    accepted, _ = await h.tap(seeded, "mute_clip")
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
    assert (await h.run(accepted.turn_id))["status"] == "awaiting_approval"
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] + 1 and after["approvals"] == 1
    assert after["briefs"] == before["briefs"] + 1


# ── Filler words: an item setting, applied inside the draft's own transaction ────────

FILLER_PROMPT = "Cut the filler words"


async def serve_filler(seeded, monkeypatch, **kwargs):  # noqa: ANN001, ANN003, ANN201
    row = k.filler_world(monkeypatch, **kwargs)
    k.seed_talking_item(seeded)
    k.seed_strategy_draft(seeded)
    await k.serve_cards(seeded, k.pick("filler_words"))
    return row, seeded.cards["filler_words"]


@pytest.fixture
def published(monkeypatch):
    """Analyses handed to the publisher (the broker is not reached)."""
    calls: list = []
    monkeypatch.setattr(
        "app.services.plan_item_media.publish_preflight_after_commit",
        lambda analysis_id: calls.append(analysis_id) or True,
    )
    return calls


@pytest.mark.asyncio
async def test_filler_tap_flips_the_setting_and_asks_for_the_normal_approval(
    world, monkeypatch, published
):
    seeded = h.seed_ready_thread()
    _, card = await serve_filler(seeded, monkeypatch)
    assert card.action.type == "item_setting" and card.prompt == FILLER_PROMPT
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    parent = head(seeded.thread_id)
    before = h.counts(seeded)

    accepted, result = await tap_and_run(seeded, "filler_words")

    assert result["status"] == "awaiting_approval", result
    # The setting flipped in the draft's own transaction, like the item PATCH does.
    assert k.item_row(seeded) == {
        "speech_cleanup_enabled": True,
        "user_edited": True,
        "edit_format": "subtitled",
    }
    # A new draft revision restating the plan, a render approval (the normal flow stamps the
    # cleanup contract from the setting), one typed requirement owned by this turn, no Job yet.
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] + 1 and after["approvals"] == 1
    assert after["jobs"] == before["jobs"]
    new = head(seeded.thread_id)
    assert new["snap"]["kind"] == "strategy" and new["parent"] == parent["id"]
    assert new["snap"]["strategy"] == parent["snap"]["strategy"]
    [req] = h.briefs(seeded.thread_id)[-1].requirements
    assert req["facts"] == {"speech_cleanup": True} and req["source_turn_id"] == accepted.turn_id
    assert h.events(seeded.thread_id, "user_message")[-1].content == FILLER_PROMPT
    assert h.events(seeded.thread_id, "approval_requested")
    assert published == [], "the scheduler had nothing to queue (preflight off)"


@pytest.mark.asyncio
async def test_filler_undo_restores_the_parent_draft_and_withdraws_the_approval(
    world, monkeypatch, published
):
    from app.kria.drafts import undo_draft

    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    parent = head(seeded.thread_id)
    await tap_and_run(seeded, "filler_words")
    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=head(seeded.thread_id)["rev"],
        )
    assert head(seeded.thread_id)["snap"] == parent["snap"]
    assert approvals_pending(seeded.thread_id) == 0


@pytest.mark.asyncio
async def test_filler_a_queued_analysis_is_published_after_the_commit(
    world, monkeypatch, published
):
    """K15: whatever the scheduler queued for the flipped setting is published once the commit
    has landed (Beat owns durable recovery)."""
    from app.services import speech_cleanup_preflight as preflight

    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    queued = k.uuid.uuid4()
    monkeypatch.setattr(preflight, "schedule_item_preflight_sync", lambda _db, _item: queued)
    accepted, result = await tap_and_run(seeded, "filler_words")
    assert result["status"] == "awaiting_approval" and k.item_row(seeded)["speech_cleanup_enabled"]
    assert published == [queued]


@pytest.mark.asyncio
async def test_filler_analysis_redone_commits_and_publishes_it_and_flips_nothing(
    world, monkeypatch, published
):
    """K8: the scheduler re-opens the analysis (the language cross-check) and clears the choice.
    The tap did not take, but the re-queued row must survive: rolling back would orphan it."""
    from app.models import SpeechCleanupAnalysis
    from app.services import speech_cleanup_preflight as preflight

    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    requeued = k.uuid.uuid4()

    def scheduler(db, item):  # noqa: ANN001, ANN202
        item.speech_cleanup_enabled = False  # what the real scheduler does when it re-opens a row
        db.add(
            SpeechCleanupAnalysis(
                id=requeued,
                plan_item_id=item.id,
                source_kind="embedded_spine",
                source_media_identity="clip-a",
                source_storage_path="uploads/u1/clip.mp4",
                source_generation="g1",
                source_policy_fingerprint="policy-fp-2",
                engine_version="e1",
                detector_version="d1",
                status="queued",
            )
        )
        db.flush()
        return requeued

    monkeypatch.setattr(preflight, "schedule_item_preflight_sync", scheduler)
    before = h.counts(seeded)
    accepted, result = await tap_and_run(seeded, "filler_words")

    assert result["status"] == "completed", result
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert signal_of(seeded.thread_id)["reason"] == "analysis_redone"
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert k.item_row(seeded)["user_edited"] is False
    assert k.queued_analyses(seeded) == ["queued"], "the re-queued analysis was kept"
    assert published == [requeued]
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] and after["approvals"] == 0
    assert after["briefs"] == before["briefs"]


@pytest.mark.asyncio
async def test_filler_the_capability_closing_after_serve_refuses_and_flips_nothing(
    world, monkeypatch, published
):
    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    k.set_item(seeded, edit_format="montage")
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "filler_words")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_filler_the_flip_is_atomic_with_the_draft(world, monkeypatch, published):
    """K15: a failure after the flip rolls the setting back with the rest of the transaction."""
    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)

    def boom(*_a, **_k):  # noqa: ANN002, ANN003
        raise RuntimeError("the draft could not be written")

    monkeypatch.setattr("app.tasks.kria_runtime.plan_facts_from_strategy", boom)
    before = h.counts(seeded)
    with pytest.raises(RuntimeError, match="could not be written"):
        await tap_and_run(seeded, "filler_words")
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert k.item_row(seeded)["user_edited"] is False
    assert h.counts(seeded) == before


@pytest.mark.asyncio
async def test_filler_a_newer_head_draft_under_the_lock_refuses_before_flipping(
    world, monkeypatch, published
):
    """K17: another turn moved the head after planning; the restate must not replace it."""
    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)

    def another_turn_wrote_a_draft():
        with sync_session() as db:
            old = db.execute(
                h.select(h.CreatorEditDraft).where(
                    h.CreatorEditDraft.thread_id == seeded.thread_id,
                    h.CreatorEditDraft.is_head.is_(True),
                )
            ).scalar_one()
            old.is_head = False
            db.flush()
            db.add(
                h.CreatorEditDraft(
                    creator_id=seeded.user_id,
                    thread_id=seeded.thread_id,
                    item_id=seeded.item_id,
                    variant_key=old.variant_key,
                    base_job_id=old.base_job_id,
                    base_generation_id=old.base_generation_id,
                    draft_revision=old.draft_revision + 1,
                    parent_draft_id=old.id,
                    snapshot_json=old.snapshot_json,
                    snapshot_hash="f" * 64,
                    is_head=True,
                )
            )
            db.commit()

    h.after_planning(monkeypatch, another_turn_wrote_a_draft)
    before = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "filler_words")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert signal_of(seeded.thread_id)["reason"] == "base_moved"
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    after = h.counts(seeded)
    assert after["drafts"] == before["drafts"] + 1, "only the other turn's draft"
    assert after["approvals"] == 0


@pytest.mark.asyncio
async def test_filler_a_second_tap_after_it_applied_is_refused(world, monkeypatch, published):
    from app.kria.drafts import undo_draft

    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    await tap_and_run(seeded, "filler_words")
    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=head(seeded.thread_id)["rev"],
        )
    undone = h.counts(seeded)
    accepted, _ = await tap_and_run(seeded, "filler_words")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert h.counts(seeded) == undone
    assert pin_record(accepted.turn_id)["reason"] == "consumed"


@pytest.mark.asyncio
async def test_filler_kind_switched_off_after_serve_refuses(world, monkeypatch, published):
    seeded = h.seed_ready_thread()
    await serve_filler(seeded, monkeypatch)
    monkeypatch.setattr(settings, "kria_suggestion_filler_words_enabled", False)
    before = h.counts(seeded)
    await tap_and_run(seeded, "filler_words")
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert h.counts(seeded) == before


# ── A restate never replaces newer work ──────────────────────────────────────────────

RESTATE_KINDS = ("mute_clip", "filler_words")


async def serve_restate_beside_transition(seeded, monkeypatch, kind):  # noqa: ANN001, ANN201
    """The thread's plan as a strategy draft, and a restating card next to an editor-op card."""
    if kind == "mute_clip":
        mute_world(monkeypatch)
        k.seed_strategy_draft(seeded)
        k.noisy_audio(seeded, "clip-b")
        restating = k.pick("mute_clip", targets=("c2",))
    else:
        k.filler_world(monkeypatch)
        k.seed_talking_item(seeded)
        k.seed_strategy_draft(seeded)
        restating = k.pick("filler_words")
    await k.serve_cards(seeded, restating, k.pick("transition", rank=2))


@pytest.mark.parametrize("kind", RESTATE_KINDS)
@pytest.mark.asyncio
async def test_a_restate_after_an_editor_tap_does_not_replace_the_newer_draft(
    world, monkeypatch, published, kind
):
    """K16: the head is the transition draft the creator just staged; restating the older plan
    over it would drop that edit from the head chain (only Undo could bring it back)."""
    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, kind)
    await tap_and_run(seeded, "transition")
    staged = head(seeded.thread_id)
    assert staged["snap"]["kind"] == "editor"
    before = h.counts(seeded)

    accepted, result = await tap_and_run(seeded, kind)

    assert result["status"] == "completed"
    assert last_reply(seeded.thread_id) == NO_LONGER_FITS
    assert signal_of(seeded.thread_id)["reason"] == "base_moved"
    assert h.counts(seeded) == before, "no draft, approval, brief version or job"
    assert head(seeded.thread_id)["id"] == staged["id"], "the staged edit is still the head"
    assert k.item_row(seeded)["speech_cleanup_enabled"] is False
    assert published == []


@pytest.mark.parametrize("kind", RESTATE_KINDS)
@pytest.mark.asyncio
async def test_a_restate_applies_again_once_undo_returns_the_head_to_the_plan(
    world, monkeypatch, published, kind
):
    """K16: the refusal is about where the head is, not about the card. Undo the editor tap and
    the same card restates the plan (a new strategy revision plus its approval)."""
    from app.kria.drafts import undo_draft

    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, kind)
    await tap_and_run(seeded, "transition")
    async with h.AsyncSessionLocal() as db:
        await undo_draft(
            db,
            thread_id=seeded.thread_id,
            creator_id=seeded.user_id,
            expected_revision=head(seeded.thread_id)["rev"],
        )
    assert head(seeded.thread_id)["snap"]["kind"] == "strategy"

    accepted, result = await tap_and_run(seeded, kind)

    assert result["status"] == "awaiting_approval", result
    assert head(seeded.thread_id)["snap"]["kind"] == "strategy"
    assert signal_of(seeded.thread_id) is None


@pytest.mark.parametrize("kind", RESTATE_KINDS)
@pytest.mark.asyncio
async def test_unsaved_edits_do_not_spend_a_restating_card(world, monkeypatch, published, kind):
    """R6: hand edits not saved yet say nothing about the card. The refusal is typed but does not
    mark the card, the read mints no new set, and the very same tap applies once the edits are
    saved."""
    seeded = h.seed_ready_thread()
    await serve_restate_beside_transition(seeded, monkeypatch, kind)
    card = seeded.cards[kind]
    before = h.counts(seeded)

    await tap_and_run(seeded, kind, editor_state=HAND_EDITED)

    assert last_reply(seeded.thread_id) == SAVE_FIRST
    assert signal_of(seeded.thread_id) == {
        "set_id": seeded.set_id,
        "id": card.id,
        "outcome": "refused",
        "reason": "unsaved_edits",
    }
    assert h.counts(seeded) == before
    assert "refused" not in k.ring_entry(seeded.thread_id, seeded.set_id)
    assert (await k.get_suggestions(seeded)).set_id == seeded.set_id, "no new set was minted"

    _, result = await tap_and_run(seeded, kind)  # saved: no unsaved lanes on the phone any more
    assert result["status"] == "awaiting_approval", result
    assert h.counts(seeded)["drafts"] == before["drafts"] + 1

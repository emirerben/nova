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

Refusals
  R1  every refusal (planner-side or at the commit) answers with the same typed signal on the
      assistant event (``suggestion_pin``: set_id, id, outcome, reason); a transient refusal
      (a render in flight) keeps the card tappable and is not marked.
  R2  a refused card is marked on its ring entry in the refusal's own transaction; the GET hides
      it, mints a NEW set_id (so the phone's "hidden until the set changes" ledger releases and the
      "refreshing" claim is true) and stores the derived set so a tap on a card in it resolves.
  R3  an applied card is hidden by id on the GET even with the editor triggers flag off.
  R4  ``ready_context`` carries the kinds' inputs (the shared loader), so a rebuild sees what the
      serve saw: the hook's clip texts, the brief, the filler evidence, the duplicates' ``needed``.

Database (collection): same CI-aware guard as ``test_kria_pinned_suggestion`` (FM24).
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
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
if _PROBLEM and os.environ.get("CI"):
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

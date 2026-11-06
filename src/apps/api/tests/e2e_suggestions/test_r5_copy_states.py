"""R5-A: the on-screen title's decision, in every state, before and after the set was served.

The creator's consent over an on-screen title (KRI-506) is a thread-level fact the pinned tap's
commit checks (``creative_copy_problem``). The gate reads what a tap CHANGES: the hook card is the
one tapped card that writes the title (one ``set_title`` op), so any decision about the title
refuses it; an editor-ops card whose ops leave the title alone carries no copy, so it is refused
only under an OPEN wording discussion; a restating card is committed against the head plan, so only
a plan that disagrees with the decision refuses it. The serve must ask the same question, whenever
the decision was made. (The first version of this file expected every editor-ops card to go with a
settled decision; the round-5 serve lane refined that on purpose, see
``test_r5_serve_copy_gate.py``, and the expectations below follow it.)

Failure catalogue (written first; each row is a test below):

  A1  a served card refused at the tap by the consent gate. For every state of the decision (an
      open authorship or wording question, a stale answer, a creator-supplied title, an approved or
      cancelled wording, the closing title) and every footage profile, every card the unmodified GET
      serves executes end to end when tapped: accepted, one real change, receipts met, Undo restores
      everything, a second tap refused with a fresh set.
  A2  the decision lands AFTER the set was frozen. The creator answers a wording question (or
      supplies their own title) while the phone holds a set served before. The next GET serves
      nothing under an open discussion, and under a settled decision it leaves out the hook (and a
      restating card the plan disagrees with) and keeps every card that leaves the title alone;
      each card it shows executes; each card it dropped is refused in words by a tap on the phone
      that still holds it, never applied.
  A3  the hook card is the only card that carries on-screen text. In every decided state the hook
      is not on offer (the decision exists), and in the undecided state its copy lands as the title
      exactly as the card showed it.
  A4  a restating card is served exactly when the head plan agrees with the decision, and the cards
      that leave the title alone stay beside it.

The states and the events that make them are the real thing: the assistant's choice questions, the
creator's tapped answers and the resolution records, appended after the thread's newest event.
"""

from __future__ import annotations

import pytest

from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.flow import RESTATE, tap_every_card, verify_render_kick
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r4_serve_copy_gate import (
    STATES,
    append,
    build_events,
    folded_status,
)
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h

KEY = "nermin-matcha-update"
PROFILES = (EDITOR, SIGNALS, TALKING)
STATE_IDS = [state for state, _folds, _held in STATES]
# The cards whose ops leave the title lane alone (the gate's TITLE_NEUTRAL_OPS): a settled decision
# is none of their business. The hook writes the title and is the only card that is gated by one.
NEUTRAL = frozenset({"captions", "music", "sfx", "transition", "duplicates"})


async def with_state(world, state: str | None, profile: str, scn=None) -> s.Scene:  # noqa: ANN001
    scene = s.build(world, KEY, profile, scn=scn)
    if state is not None:
        append(scene.seeded, *build_events(state, await h.media_digest_of(scene.seeded)))
        world.refreshes.clear()
    return scene


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", PROFILES)
@pytest.mark.parametrize(("state", "folds_to", "held"), STATES, ids=STATE_IDS)
async def test_a1_a3_every_card_served_in_a_decided_state_executes(
    talking_world, monkeypatch, state, folds_to, held, profile
):
    rec = Recorder(f"copy A1 {state} {profile}")
    scene = await with_state(talking_world, state, profile)
    target = "closing_title" if state == "closing_supplied" else "opening_title"
    assert folded_status(scene.seeded, target) == folds_to, "the fixture is not the state it names"
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    rec.negative(
        f"A3 {state}: the hook card is not on offer once the title was decided",
        f"{KEY}/{profile}",
        "no hook card",
        "hook" not in kinds,
        f"served {kinds}",
    )

    async def probe(cell, probed):  # noqa: ANN001, ANN202
        twin = await with_state(talking_world, state, profile)
        await verify_render_kick(cell, twin.phone, probed, monkeypatch)

    await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        profile,
        expect=kinds,
        state=f"title-{state}",
        probe_render=probe,
    )
    rec.finish()


# ── A2: the decision lands after the set was frozen ──────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", (EDITOR, TALKING))
@pytest.mark.parametrize(("state", "folds_to", "held"), STATES, ids=STATE_IDS)
async def test_a2_the_decision_lands_after_the_set_was_frozen(
    talking_world, state, folds_to, held, profile
):
    rec = Recorder(f"copy A2 {state} {profile}")
    scene = await with_state(talking_world, None, profile)
    out = await scene.phone.serve()
    before = {c.kind: c for c in out.suggestions}
    append(scene.seeded, *build_events(state, await h.media_digest_of(scene.seeded)))
    talking_world.refreshes.clear()

    shown = await scene.phone.get()
    shown_kinds = s.kinds_of(shown)
    dropped = [k for k in before if k not in shown_kinds]
    where = f"{KEY}/{profile}/{state}"
    open_discussion = held
    if open_discussion:
        rec.negative(
            f"A2 {state}: an open wording discussion holds the whole set back",
            where,
            "nothing served",
            not shown_kinds,
            f"held {list(before)}; shown now {shown_kinds}",
        )
    else:
        rec.negative(
            f"A2 {state}: the read after the settled decision drops the hook and only cards the "
            "decision covers; every card that leaves the title alone stays",
            where,
            "no hook; every held neutral card still served",
            "hook" not in shown_kinds and all(k in shown_kinds for k in before if k in NEUTRAL),
            f"held {list(before)}; shown now {shown_kinds}; dropped {dropped}",
        )

    # Every card still shown executes (each on its own twin: a tap changes the thread).
    for kind in shown_kinds:
        twin = await with_state(talking_world, None, profile)
        t_out = await twin.phone.serve()
        append(twin.seeded, *build_events(state, await h.media_digest_of(twin.seeded)))
        card = s.card_of(t_out, kind)
        tap = await twin.phone.tap(card, t_out)
        rec.negative(
            f"A2 {state}: the {kind} card still shown after the decision executes",
            where,
            "planned, one revision (or one approval for a restating card)",
            s.executed(tap) and (tap.new_drafts == 1),
            f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
        )
    # Every card dropped is refused in words when the phone taps the copy it still holds.
    for kind in dropped:
        twin = await with_state(talking_world, None, profile)
        t_out = await twin.phone.serve()
        append(twin.seeded, *build_events(state, await h.media_digest_of(twin.seeded)))
        card = s.card_of(t_out, kind)
        tap = await twin.phone.tap(card, t_out)
        rec.negative(
            f"A2 {state}: the held {kind} card (dropped from the read) is refused in words",
            where,
            "typed refusal, nothing written",
            s.is_refused(tap) or (kind in RESTATE and s.executed(tap)),
            f"signal={tap.refused} reply={tap.reply[:70]!r} planned={tap.planned} "
            f"rows {tap.before} -> {tap.after}",
        )
    rec.finish()


# ── A4: a restating card over a plan that agrees with the decision ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "plan", "offered"),
    [
        ("creator_supplied", {"opening_title": "My own title"}, True),
        ("cancelled_resolution", {"omitted_copy_targets": ["opening_title"]}, True),
        ("creator_supplied", {"opening_title": "Another title"}, False),
        ("creator_supplied", {}, False),
        ("cancelled_resolution", {"opening_title": "A leftover title"}, False),
    ],
    ids=["supplied-same", "cancelled-omitted", "supplied-other", "supplied-none", "cancelled-kept"],
)
async def test_a4_a_restating_card_is_served_exactly_when_the_plan_agrees(
    talking_world, monkeypatch, state, plan, offered
):
    """The gate accepts a restate only against a plan that agrees with the title decision. Served
    exactly then; and every card served that way executes end to end (the filler card)."""
    from dataclasses import replace  # noqa: PLC0415

    from tests.e2e_suggestions import threads  # noqa: PLC0415

    base = threads.SCENARIOS[KEY]
    scn = replace(base, strategy={**base.strategy, **plan})
    rec = Recorder(f"copy A4 {state} {sorted(plan)}")
    scene = await with_state(talking_world, state, TALKING, scn)
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    rec.negative(
        f"A4 {state} (plan fields {sorted(plan) or 'none'}): the filler card is "
        f"{'served' if offered else 'withheld'}",
        KEY,
        "served exactly when the head plan agrees with the decision; no hook; the cards that "
        "leave the title alone stay",
        ("filler_words" in kinds) == offered
        and "hook" not in kinds
        and {"captions", "music", "sfx"} <= set(kinds),
        f"served {kinds}",
    )
    if offered:

        async def probe(cell, probed):  # noqa: ANN001, ANN202
            twin = await with_state(talking_world, state, TALKING, scn)
            await verify_render_kick(cell, twin.phone, probed, monkeypatch)

        await tap_every_card(
            rec,
            scene.phone,
            scn,
            TALKING,
            expect=kinds,
            state=f"title-{state}-plan-agrees",
            probe_render=probe,
        )
    rec.finish()

"""R8-A: a card is served only while its tap would not be refused ``request_too_long``.

The draft commit pins the creator's whole conversation into the approval binding. Once the
creator's text (plus the tap's own message) passes 12,000 characters, or the bound request (the
text, the rendered brief with the tap's own requirement, the superseded lines) does, the commit
refuses every pinned tap, editor-op taps included. The serve never asked, so every card of such a
thread was a dead tap that burned a derived set one refusal at a time.

Failure catalogue (written first; each row is a test below):

  A1  a thread far over the budget (4 messages near the per-message cap): no v2 card is served,
      the legacy chips are what they were (the strip only touches v2 cards).
  A2  a thread comfortably under the budget: every card is served and every one executes.
  A3  the edge. The thread's own text is under the budget, and the tap's message (and the typed
      requirement it writes, and the "Latest message" line the binding adds) tips the bound request
      over. Walk the size across the edge: for every size, each card the control set offered is
      served exactly when its tap executes. A card withheld here is refused ``request_too_long``
      when tapped from the control set the phone still holds; a card served here executes.
  A4  the same walk with a thick brief (many live requirements): the rendered brief is part of the
      bound request, so the edge moves down and the card must follow it.
  A5  an account without brief binding: the commit never builds a binding, so the budget is not
      its gate and nothing is withheld (flag-off is what it was).

Every check drives the real path: GET -> worker (cassette) -> GET -> ``submit_turn`` ->
``run_kria_turn`` -> ``_complete_draft_turn``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import sync_session
from app.kria.brief import BriefUpdate
from app.models import CreationThreadEvent
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.test_r4_serve_copy_gate import append
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

KEY = "nermin-matcha-update"
BUDGET = 12_000
FILLER = "this is the part of the script the creator pasted about the matcha restock. "


def creator_chars(seeded) -> int:  # noqa: ANN001
    """The chars the commit joins: every user message, one newline apart."""
    with sync_session() as db:
        rows = db.execute(
            select(CreationThreadEvent.content).where(
                CreationThreadEvent.thread_id == seeded.thread_id,
                CreationThreadEvent.role == "user",
                CreationThreadEvent.content.is_not(None),
            )
        ).all()
    return len("\n".join(str(c).strip() for (c,) in rows if str(c).strip()))


def grow_to(seeded, total: int) -> None:  # noqa: ANN001
    """Append user messages (each under the 4,000-char cap) until the creator text is ``total``."""
    have = creator_chars(seeded)
    rows = []
    while have < total:
        size = min(3_900, total - have - 1)  # the join adds one newline before the message
        if size < 1:
            break
        text = (FILLER * 100)[: size - 1] + "x"  # strip() must not eat the last char
        rows.append(("user", "user_message", text, {"turn_status": "completed"}))
        rows.append(("assistant", "assistant_response", "Got it.", {}))
        have += size + 1
    append(seeded, *rows)
    assert creator_chars(seeded) == total


def thick_brief(seeded, n: int = 12) -> None:  # noqa: ANN001
    for i in range(n):
        k.write_brief(
            seeded,
            BriefUpdate(
                operation="add",
                kind="style",
                scope="global",
                description=f"note {i}: " + "keep the colours warm and the pace gentle. " * 6,
                facts={},
            ),
        )


KINDS = ["hook", "captions", "music", "transition", "sfx"]


async def probe(world, total: int, kind: str, *, thick: bool = False):  # noqa: ANN001, ANN201
    """One card on a fresh thread grown to ``total`` chars: ``(served, executed, reason)``.

    A fresh thread per card: every tap leaves its own message and requirement in the thread, so a
    second tap on the same thread would stand on a longer request than the serve judged.
    """
    scene = s.build(world, KEY, EDITOR)
    if thick:
        thick_brief(scene.seeded)
    control = await scene.phone.serve()
    if s.card_of(control, kind) is None:
        pytest.skip(f"the control thread does not serve {kind}")
    grow_to(scene.seeded, total)
    world.redis.advance(60)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    served = card is not None
    tap = await scene.phone.tap(card or s.card_of(control, kind), out if served else control)
    return served, s.executed(tap), (tap.refused or {}).get("reason"), control, out


def assert_exact(served: bool, executed: bool, reason: str | None, where: str) -> None:
    assert served == executed, (
        f"{where}: served={served} but the tap {'executes' if executed else f'is refused {reason}'}"
    )
    if not executed:
        assert reason == "request_too_long", f"{where}: refused for {reason}"


@pytest.mark.asyncio
async def test_a1_a_thread_over_the_budget_serves_no_card_and_keeps_the_chips(talking_world):
    scene = s.build(talking_world, KEY, EDITOR)
    control = await scene.phone.serve()
    grow_to(scene.seeded, 4 * 3_900)
    talking_world.redis.advance(60)
    out = await scene.phone.serve()
    assert out.suggestions == [], f"served {s.kinds_of(out)} over the budget"
    assert [c.model_dump() for c in out.chips] == [c.model_dump() for c in control.chips]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a2_a_thread_under_the_budget_serves_and_executes(talking_world, kind):
    served, executed, reason, _c, _o = await probe(talking_world, 11_000, kind)
    assert served and executed, (served, executed, reason)


# The edge: the thread's own text under 12,000, the tap's own message (and the typed requirement
# and the "Latest message" line it adds to the bound request) tips it over.
EDGE = [11_700, 11_740, 11_780, 11_810, 11_840, 11_870, 11_900, 11_990, 12_001]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("total", EDGE)
async def test_a3_a_card_across_the_edge_is_served_exactly_when_it_executes(
    talking_world, total, kind
):
    served, executed, reason, _c, _o = await probe(talking_world, total, kind)
    assert_exact(served, executed, reason, f"{kind}@{total}")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("total", [4_000, 7_000, 8_000, 8_150, 8_300, 8_400, 8_500])
async def test_a4_a_thick_brief_moves_the_edge_and_the_card_follows(talking_world, total, kind):
    served, executed, reason, _c, _o = await probe(talking_world, total, kind, thick=True)
    assert_exact(served, executed, reason, f"thick {kind}@{total}")


@pytest.mark.asyncio
async def test_a5_no_binding_no_budget_gate(talking_world, monkeypatch):
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    scene = s.build(talking_world, KEY, EDITOR)
    control = await scene.phone.serve()
    grow_to(scene.seeded, 4 * 3_900)
    talking_world.redis.advance(60)
    out = await scene.phone.serve()
    assert s.kinds_of(out) == s.kinds_of(control)

"""R4: a restating card is offered only while the variant's head draft is the plan.

A restating tap (``filler_words``, ``mute_clip``) re-drafts the thread's plan as the variant's next
revision, so it is only that revision when the head draft IS the plan. Over an editor draft (any
caption, sound, transition, hook or duplicates tap) it is refused ``base_moved`` and the card is
hidden for good: a creator who taps any editor card used to lose the filler card on that thread
until Undo, and was shown a card that could not work in between.

Failure modes:

  a. the card is served over an editor head (the tap is then refused);
  b. the card is not served again once the head is the plan again (Undo), or ever returns with a
     tap that is still refused;
  c. the hiding reaches an editor-ops card (it must only touch the restating kinds);
  d. the same read is used by the GET, the worker and the tap-time rebuild, so the three disagree.
"""

from __future__ import annotations

import pytest

from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.test_ring_store import KEY, card_of, prefs
from tests.e2e_suggestions.threads import SIGNALS, TALKING


def kinds_of(out) -> list[str]:  # noqa: ANN001
    return [c.kind for c in out.suggestions]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "profile"), [("filler_words", TALKING), ("mute_clip", SIGNALS)])
async def test_r4_a_restating_card_follows_the_head_draft(
    talking_world, monkeypatch, kind, profile
):
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, talking_world, prefs(), language=scn.language)

    out = await phone.serve()
    assert kind in kinds_of(out), f"precondition: {kind} is served over the plan: {kinds_of(out)}"
    editor_kinds = [k_ for k_ in kinds_of(out) if k_ in {"captions", "sfx"}]
    assert editor_kinds, kinds_of(out)

    tap = await phone.tap(card_of(out, editor_kinds[0]), out)
    assert tap.planned and tap.new_drafts == 1, (tap.pin, tap.reply)
    head = d.head_of(seeded.thread_id)
    assert head is not None and head["snap"]["kind"] == "editor", "the head is an editor draft now"

    over_editor = await phone.serve()
    assert kind not in kinds_of(over_editor), (
        f"{kind} would be refused base_moved at the tap, so it is not offered: "
        f"{kinds_of(over_editor)}"
    )
    still = [k_ for k_ in kinds_of(out) if k_ not in {kind, editor_kinds[0]}]
    assert all(k_ in kinds_of(over_editor) for k_ in still), (
        "only the restating kinds are hidden",
        still,
        kinds_of(over_editor),
    )

    restored = await phone.undo()
    assert restored is not None and restored["snap"]["kind"] == "strategy"
    back = await phone.serve()
    assert kind in kinds_of(back), f"the plan is the head again: {kinds_of(back)}"

    again = await phone.tap(card_of(back, kind), back)
    assert again.planned, (again.pin, again.reply)
    assert again.result["status"] == "awaiting_approval", again.result
    assert again.refused is None, again.refused

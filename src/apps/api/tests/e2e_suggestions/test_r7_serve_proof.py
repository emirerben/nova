"""R7: a card is served only while the tap's own proof still holds (served implies executes).

Failure-mode list (written first; each row is a test below).

  A  a hook card tapped and then undone. The tap wrote a creative-copy decision (the creator's
     approval, ``source="suggestion"``) that Undo does not take back, so the hook kind's own
     witness (``copy_decided``) refuses the card from then on. The GET served the frozen set
     again, hook included, because the strip asked the consent gate (which does not fold a tapped
     card's own decision) and never the kind's witness. The tap was refused ``base_moved``.
       A1 brief off, editor triggers on (Undo returns the digest to its first value: GAP-17)
       A2 brief off, triggers off
       A3 brief on, triggers off (the legacy digest ignores the brief version Undo moves)
       A4 brief on, triggers on (the control: a new set, the hook never comes back)
     In all four the hook stays spent, as the documented limit says, and everything else the GET
     serves still executes.
  B  the Undo of every other kind on an account without the creative brief, with editor triggers
     on and off: Undo clears the card's applied mark (its footprint is written with or without a
     brief), the card is an offer again, and EVERY card the next GET serves executes.
  C  with editor triggers off nothing moves the digest, so a frozen set outlives what the creator
     says after it was served. A card whose tap would be refused (or that the creator's word now
     withholds, KRI-129) must not be served.
       C1 duplicates after a stated length (the tap is refused ``op_refused``)
       C2 hook after a title requirement (the tap is refused ``base_moved``)
       C3 captions, music, sound effects and transition after the creator's own ask: withheld, as
          they are when the editor triggers are on
     The control of each runs with the triggers on.

Every check drives the real path: GET -> worker (cassette) -> GET -> ``submit_turn`` ->
``run_kria_turn`` -> ``_complete_draft_turn``. The mute kind is dark as shipped; its cells run with
``PINNED_TAP_APPLIES_CLIP_SOUND`` patched on and are marked not shipped.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.brief import BriefUpdate
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_kinds_harness as k

KEYS = ["nermin-matcha-update", "nermin-matcha-update/tr"]
LANGS = ["en", "tr"]
RESTATE = frozenset({"filler_words", "mute_clip"})


def ask(kind: str, scope: str, text: str | None = None, **facts) -> BriefUpdate:  # noqa: ANN003
    return BriefUpdate(operation="add", kind=kind, scope=scope, description=text, facts=facts or {})


async def tap_every_served_card(rec: Recorder, scene, out, where: str, *, why: str) -> list[str]:  # noqa: ANN001
    """Tap each card of ``out`` (the phone holds them all), Undoing each, and record whether it
    executed. Returns the kinds that were refused."""
    refused: list[str] = []
    for card in out.suggestions:
        tap = await scene.phone.tap(card, out)
        ok = s.executed(tap)
        if not ok:
            refused.append(card.kind)
        rec.negative(
            f"{why}: the served {card.kind} card executes",
            where,
            "planned, no typed refusal",
            ok,
            f"signal={tap.refused} reply={tap.reply[:70]!r} drafts +{tap.new_drafts}",
        )
        if tap.new_drafts == 1:
            await scene.phone.undo()
        # A refused tap hides its card and mints a new set; the next card is still the phone's.
    return refused


# ── A: the hook after its own Undo ──────────────────────────────────────────────────────

HOOK_STATES = [
    pytest.param(False, True, id="A1-brief-off-triggers-on"),
    pytest.param(False, False, id="A2-brief-off-triggers-off"),
    pytest.param(True, False, id="A3-brief-on-triggers-off"),
    pytest.param(True, True, id="A4-brief-on-triggers-on"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("brief", "triggers"), HOOK_STATES)
@pytest.mark.parametrize("key", KEYS, ids=LANGS)
async def test_a_the_hook_stays_spent_after_its_own_undo_and_the_rest_executes(
    talking_world, monkeypatch, key, brief, triggers
):
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", brief)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", triggers)
    rec = Recorder(f"proof R7-A {key} brief={brief} triggers={triggers}")
    where = f"{key}/hook/brief-{'on' if brief else 'off'}/triggers-{'on' if triggers else 'off'}"
    scene = s.build(talking_world, key, EDITOR)
    out = await scene.phone.serve()
    card = s.card_of(out, "hook")
    assert card is not None, f"the thread did not serve the hook: {s.kinds_of(out)}"
    first = await scene.phone.tap(card, out)
    rec.negative("A the first tap applies", where, "planned", s.executed(first))
    await scene.phone.undo()
    talking_world.redis.advance(60)
    after = await scene.phone.serve()
    rec.negative(
        "A the hook is not served again after its own Undo (its decision stays in the log)",
        where,
        "no hook card",
        s.card_of(after, "hook") is None,
        f"served {s.kinds_of(after)}",
    )
    await tap_every_served_card(rec, scene, after, where, why="A after Undo")
    rec.finish()


# ── B: the Undo of every kind without the creative brief ─────────────────────────────────

UNDO_KINDS = [
    ("captions", EDITOR),
    ("music", EDITOR),
    ("transition", EDITOR),
    ("sfx", EDITOR),
    ("duplicates", SIGNALS),
    ("filler_words", TALKING),
    ("mute_clip", SIGNALS),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("triggers", [True, False], ids=["triggers-on", "triggers-off"])
@pytest.mark.parametrize(("kind", "profile"), UNDO_KINDS, ids=[k_ for k_, _ in UNDO_KINDS])
@pytest.mark.parametrize("key", KEYS, ids=LANGS)
async def test_b_every_kind_undone_without_the_brief_is_an_offer_again_and_all_served_execute(
    talking_world, monkeypatch, key, kind, profile, triggers
):
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", triggers)
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"proof R7-B {key} {kind} triggers={triggers}")
    where = f"{key}/{profile}/{kind}/brief-off/triggers-{'on' if triggers else 'off'}"
    scene = s.build(talking_world, key, profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    if card is None:
        # Without the brief a restating card that writes a typed requirement is not served (its
        # tap needs the brief), and then there is nothing dead to tap.
        rec.negative(
            f"B {kind}: not served without the brief",
            where,
            "only a kind that needs the brief is absent",
            kind in {"mute_clip"},
            f"served {s.kinds_of(out)}",
        )
        rec.finish()
        return
    tap = await scene.phone.tap(card, out)
    rec.negative(
        f"B {kind}: the tap applies",
        where,
        "planned, one revision",
        s.executed(tap) and tap.new_drafts == 1,
        f"signal={tap.refused} reply={tap.reply[:70]!r}",
    )
    await scene.phone.undo()
    talking_world.redis.advance(60)
    after = await scene.phone.serve()
    rec.negative(
        f"B {kind}: Undo takes the tap back, the card is an offer again",
        where,
        f"{kind} served",
        s.card_of(after, kind) is not None,
        f"served {s.kinds_of(after)}",
    )
    refused = await tap_every_served_card(rec, scene, after, where, why=f"B {kind} after Undo")
    assert not refused or kind == "mute_clip", refused
    rec.finish()


# ── C: a frozen set outliving what the creator says (editor triggers off) ────────────────

LENGTH = ask("timing", "global", "6 seconds long", duration_s=6)
TITLE = ask("text", "title", "a title about tea")
CAPTIONS = ask("text", "global", "no captions please", captions="off")
MUSIC_ASK = ask("audio", "global", "turn the sound down a little")
SFX_ASK = ask("audio", "global", "no sound effects please")
TRANSITION_ASK = ask("style", "global", "use hard cuts only")

STALE = [
    ("duplicates", SIGNALS, LENGTH, "C1"),
    ("hook", EDITOR, TITLE, "C2"),
    ("captions", EDITOR, CAPTIONS, "C3"),
    ("music", EDITOR, MUSIC_ASK, "C3"),
    ("sfx", EDITOR, SFX_ASK, "C3"),
    ("transition", EDITOR, TRANSITION_ASK, "C3"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("triggers", [False, True], ids=["triggers-off", "triggers-on"])
@pytest.mark.parametrize(
    ("kind", "profile", "update", "row"), STALE, ids=[f"{r}-{k_}" for k_, _, _, r in STALE]
)
@pytest.mark.parametrize("key", KEYS, ids=LANGS)
async def test_c_a_card_the_creator_has_since_ruled_out_is_not_served_and_the_rest_executes(
    talking_world, monkeypatch, key, kind, profile, update, row, triggers
):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", triggers)
    rec = Recorder(f"proof R7-C {row} {key} {kind} triggers={triggers}")
    where = f"{key}/{profile}/{kind}/triggers-{'on' if triggers else 'off'}"
    scene = s.build(talking_world, key, profile)
    served = await scene.phone.serve()
    assert s.card_of(served, kind) is not None, (
        f"the control did not serve {kind}: {s.kinds_of(served)}"
    )
    k.write_brief(scene.seeded, update)
    talking_world.redis.advance(60)
    out = await scene.phone.serve()
    rec.negative(
        f"{row} the creator's word after the serve takes the {kind} card off the next read",
        where,
        f"no {kind} card",
        s.card_of(out, kind) is None,
        f"served {s.kinds_of(out)}",
    )
    await tap_every_served_card(rec, scene, out, where, why=f"{row} after the creator's word")
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS, ids=LANGS)
async def test_c4_the_mute_card_held_over_a_brief_that_moved_after_the_serve(
    talking_world, monkeypatch, key
):
    """The restating mute card writes a typed requirement bound to the brief version it was built
    on; with the editor triggers off the frozen set outlives a version change, so what is served
    next must still execute (or be left out)."""
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    rec = Recorder(f"proof R7-C4 {key} mute_clip")
    where = f"{key}/signals/mute_clip/triggers-off"
    scene = s.build(talking_world, key, SIGNALS)
    served = await scene.phone.serve()
    assert s.card_of(served, "mute_clip") is not None, f"no control: {s.kinds_of(served)}"
    k.write_brief(scene.seeded, ask("style", "global", "warm colours"))
    talking_world.redis.advance(60)
    out = await scene.phone.serve()
    await tap_every_served_card(rec, scene, out, where, why="C4 after a brief version moved")
    rec.finish()

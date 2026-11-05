"""R5-E: a switch pulled after a set was frozen, for every kind that has one.

A set is frozen for 24 h. Between the freeze and the next GET an operator can pull a kill switch.
The rule under test: no served card is dead, and no switch works behind another one.

Failure catalogue (written first; each row is a test below):

  K1  a pulled per-kind switch (hook copy, duplicates, filler words, mute) leaves the card on the
      phone. The next GET leaves that kind out and nothing else: the other cards keep their ids,
      their order, their ranks (renumbered without a hole) and their taps.
  K2  the tap on the card the phone still holds goes anywhere but a typed refusal in the thread's
      language: not the planner, not a draft, not a brief version, not an approval.
  K3  the switch put back does not give the card back. The frozen set returns the same card under
      the same id (no new model run), and its tap executes end to end.
  K4  a card of the same held set (another kind) stops working because a neighbour was pulled.
  K5  the signals switch: duplicates and mute read measured signals, so both go with
      ``KRIA_SUGGESTION_SIGNALS_ENABLED``; the cards that read none do not.
  K6  the master switch (pinned taps) reaches every kind of a frozen set, and its chips stay.
  K7  the capability levers a card rides on (``SILENCE_CUT_ENABLED``,
      ``SUBTITLED_ARCHETYPE_ENABLED``, ``SPEECH_CLEANUP_MODE``): the card must not stay on the
      phone for a tap that is refused.
  K8  no switch works behind another one, through the product's own call sites. With the chip
      capability off the real editor-commit route and the real turn completion enqueue no
      suggester run and take no debounce slot; with it on they do.

The mute kind runs with its dark constant patched on (``PINNED_TAP_APPLIES_CLIP_SOUND``): that is
not what ships, and the report says so.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import gaps
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.flow import tap_every_card, verify_render_kick
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h

KEYS = ["nermin-matcha-update", "nermin-matcha-update/tr"]
# kind -> (its own switch, the footage that serves it)
PER_KIND = {
    "hook": ("kria_suggestion_hook_copy_enabled", EDITOR),
    "duplicates": ("kria_suggestion_duplicates_enabled", SIGNALS),
    "filler_words": ("kria_suggestion_filler_words_enabled", TALKING),
    "mute_clip": ("kria_suggestion_mute_clip_enabled", SIGNALS),
}
COMBOS = [(key, kind) for key in KEYS for kind in PER_KIND]
IDS = [f"{key}:{kind}" for key, kind in COMBOS]


def lang_reply(key: str) -> str:
    return d.NO_LONGER_FITS_TR if key.endswith("/tr") else d.NO_LONGER_FITS


def ranks(out) -> list[int]:  # noqa: ANN001
    return [c.rank for c in out.suggestions]


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "kind"), COMBOS, ids=IDS)
async def test_k1_k4_a_pulled_switch_takes_only_its_own_card(talking_world, monkeypatch, key, kind):
    flag, profile = PER_KIND[kind]
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"kill K1-K4 {key} {kind}")
    where = f"{key}/{profile}/{kind}"
    scene = s.build(talking_world, key, profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    assert card is not None, f"the thread did not serve {kind}: {s.kinds_of(out)}"
    held = {c.id: c for c in out.suggestions}
    talking_world.refreshes.clear()

    monkeypatch.setattr(settings, flag, False)
    after = await scene.phone.get()
    kept = [c for c in out.suggestions if c.kind != kind]
    rec.negative(
        f"K1 {flag} off: the GET leaves only that kind out, in the same order, ids kept",
        where,
        f"cards {[c.kind for c in kept]} with ids {[c.id for c in kept]}",
        [c.id for c in after.suggestions] == [c.id for c in kept]
        and after.set_id == out.set_id
        and ranks(after) == list(range(1, len(kept) + 1)),
        f"served {s.kinds_of(after)} ranks {ranks(after)} set {after.set_id} (was {out.set_id})",
    )
    rec.negative(
        f"K1 {flag} off: no new model run is asked for (the freeze is not edited)",
        where,
        "no suggester job enqueued by the read",
        not talking_world.refreshes,
        f"enqueued {talking_world.refreshes}",
    )

    tap = await scene.phone.tap(card, out)
    rec.negative(
        f"K2 {flag} off: the held card's tap is a typed refusal in words, nothing written",
        where,
        "refused (typed), the 'no longer fits' reply in the thread's language, no row written",
        s.is_refused(tap) and tap.reply == lang_reply(key) and tap.pin.get("outcome") != "planned",
        f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
    )

    # K4: the neighbour on the same held set still works.
    others = [c for c in out.suggestions if c.kind != kind]
    neighbour = next((c for c in others if c.kind not in {"filler_words", "mute_clip"}), None)
    if neighbour is not None:
        talking_world.refreshes.clear()
        nxt = await scene.phone.tap(neighbour, out)
        rec.negative(
            f"K4 {flag} off: the {neighbour.kind} card of the same held set still executes",
            where,
            "planned with one new revision",
            s.executed(nxt) and nxt.new_drafts == 1,
            f"signal={nxt.refused} reply={nxt.reply[:70]!r} drafts +{nxt.new_drafts}",
        )
        await scene.phone.undo()

    # K3: the switch comes back; the same card (same id) is served and executes end to end.
    monkeypatch.setattr(settings, flag, True)
    talking_world.redis.advance(60)
    back = await scene.phone.serve()
    again = s.card_of(back, kind)
    rec.negative(
        f"K3 {flag} back on: the same card returns under the same id",
        where,
        f"{kind} served as {card.id}",
        again is not None and again.id == card.id,
        f"served {[(c.kind, c.id) for c in back.suggestions]}; it was {card.id}; "
        f"set {back.set_id} (was {out.set_id})",
    )
    if again is not None:

        async def probe(cell, probed):  # noqa: ANN001, ANN202
            twin = s.build(talking_world, key, profile)
            await verify_render_kick(cell, twin.phone, probed, monkeypatch)

        await tap_every_card(
            rec,
            scene.phone,
            scene.scn,
            profile,
            expect=s.kinds_of(back),
            shipped=kind != "mute_clip",
            only=frozenset({kind}),
            state="switch-pulled-and-restored",
            probe_render=probe,
        )
    _ = held
    rec.finish()


# ── K5: the signals switch ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("key", KEYS, ids=KEYS)
async def test_k5_the_signals_switch_takes_the_cards_that_read_signals(
    talking_world, monkeypatch, key
):
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"kill K5 signals {key}")
    scene = s.build(talking_world, key, SIGNALS)
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    assert {"duplicates", "mute_clip"} <= set(kinds), kinds
    monkeypatch.setattr(settings, "kria_suggestion_signals_enabled", False)
    # The signals version is part of the digest, so the next read is a new set: the worker answers
    # it (a wait, not a dead card). Cards that read no signal are in it, the readers are not.
    after = await scene.phone.serve()
    readers = {"duplicates", "mute_clip"}
    kept = [k for k in kinds if k not in readers]
    rec.negative(
        "K5 signals off: duplicates and mute go, the cards that read no signal stay",
        key,
        f"no reader; still {kept} (a freed lane may admit another card)",
        not (set(s.kinds_of(after)) & readers) and set(kept) <= set(s.kinds_of(after)),
        f"served {s.kinds_of(after)} (was {kinds})",
    )
    for kind in sorted(readers):
        tap = await scene.phone.tap(s.card_of(out, kind), out)
        rec.negative(
            f"K5 signals off: the held {kind} card's tap is refused typed, nothing written",
            key,
            "typed refusal",
            s.is_refused(tap) and tap.reply == lang_reply(key),
            f"signal={tap.refused} rows {tap.before} -> {tap.after}",
        )
    held_captions = s.card_of(out, "captions")
    tap = await scene.phone.tap(held_captions, out)
    rec.negative(
        "K5 signals off: a held card that reads no signal still executes",
        key,
        "planned with one revision",
        s.executed(tap) and tap.new_drafts == 1,
        f"signal={tap.refused} reply={tap.reply[:60]!r}",
    )
    rec.finish()


# ── K6: the master switch, for every kind ────────────────────────────────────────────

ALL = [
    ("captions", EDITOR),
    ("music", EDITOR),
    ("transition", EDITOR),
    ("sfx", EDITOR),
    ("hook", EDITOR),
    ("duplicates", SIGNALS),
    ("filler_words", TALKING),
    ("mute_clip", SIGNALS),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "profile"), ALL, ids=[k for k, _ in ALL])
async def test_k6_the_master_switch_takes_every_card_and_keeps_the_chips(
    talking_world, monkeypatch, kind, profile
):
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"kill K6 master {kind}")
    scene = s.build(talking_world, "nermin-matcha-update", profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    assert card is not None
    chips = [c.id for c in out.chips]
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    planner = h.legacy_planner_answers(monkeypatch)
    after = await scene.phone.get()
    rec.negative(
        f"K6 master off [{kind}]: no v2 card on the wire (a legacy set), chips untouched",
        "nermin-matcha-update",
        "no suggestions or digest on the wire; same chip ids",
        not after.suggestions
        and "suggestions" not in scene.phone.last_wire
        and [c.id for c in after.chips] == chips,
        f"cards {s.kinds_of(after)}; chips {[c.id for c in after.chips]} (were {chips})",
    )
    tap = await scene.phone.tap(card, out)
    stamped = h.events(scene.seeded.thread_id, "user_message")[-1].payload.get("pinned_suggestion")
    rec.negative(
        f"K6 master off: a tap on the held {kind} card is the legacy planner's, once, unpinned",
        "nermin-matcha-update",
        "planner asked once; no pin stamp; no draft; no setting flipped",
        planner.await_count == 1
        and stamped is None
        and not tap.pin
        and tap.new_drafts == 0
        and not d.item_flags(scene.seeded)["speech_cleanup_enabled"],
        f"planner calls {planner.await_count}; stamp {stamped}; pin {tap.pin}",
    )
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    talking_world.redis.advance(60)
    back = await scene.phone.get()
    rec.negative(
        f"K6 master back on: the frozen set serves its {kind} card again, same id",
        "nermin-matcha-update",
        f"{kind} card {card.id}",
        any(c.id == card.id for c in back.suggestions),
        f"served {[(c.kind, c.id) for c in back.suggestions]}",
    )
    rec.finish()


# ── K7: the capability levers a restating card rides on ──────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("silence_cut_enabled", False),
        ("subtitled_archetype_enabled", False),
        ("speech_cleanup_mode", "off"),
    ],
    ids=["silence_cut_enabled", "subtitled_archetype_enabled", "speech_cleanup_mode"],
)
async def test_k7_a_capability_lever_pulled_after_the_serve(
    talking_world, monkeypatch, flag, value
):
    rec = Recorder(f"kill K7 capability {flag}")
    scene = s.build(talking_world, "nermin-matcha-update", TALKING)
    out = await scene.phone.serve()
    card = s.card_of(out, "filler_words")
    assert card is not None
    monkeypatch.setattr(settings, flag, value)
    after = await scene.phone.get()
    tap = await scene.phone.tap(card, out)
    served = s.card_of(after, "filler_words") is not None
    refused = s.is_refused(tap)
    rec.negative(
        f"K7 {flag}={value!r}: the filler card is not on the phone for a tap that is refused",
        "nermin-matcha-update",
        "not served, or its tap executes",
        (not served) or s.executed(tap),
        f"served after: {s.kinds_of(after)}; tap planned={tap.planned} "
        f"refused={(tap.refused or {}).get('reason')} reply={tap.reply[:60]!r}",
        gap=gaps.CAPABILITY_LEVER_DEAD_CARD if (served and refused) else None,
    )
    rec.finish()


# ── K8: no switch works behind another one ───────────────────────────────────────────


def debounce_keys(world) -> list[str]:  # noqa: ANN001
    return [key for key in world.redis.data if "debounce" in key]


@pytest.mark.asyncio
@pytest.mark.parametrize("chips_on", [False, True], ids=["chips-off", "chips-on"])
async def test_k8_the_real_save_and_the_real_turn_refresh_only_with_the_chip_capability(
    talking_world, monkeypatch, chips_on
):
    rec = Recorder(f"kill K8 chip capability {'on' if chips_on else 'off'}")
    scene = s.build(talking_world, "nermin-matcha-update", TALKING)
    await scene.phone.serve()
    talking_world.refreshes.clear()
    talking_world.redis.data.clear()
    if not chips_on:
        monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
        monkeypatch.setattr(settings, "kria_suggestion_chips_user_ids", [])

    await s.route_save(scene.seeded, caption_meta={"size_px": 96})
    after_save = (list(talking_world.refreshes), debounce_keys(talking_world))
    talking_world.refreshes.clear()
    talking_world.redis.data.clear()
    # A completed turn: the editor tap of a card (chips off means there is no card to tap, so the
    # creator types the change; both end in ``_complete_draft_turn``'s refresh).
    h.legacy_planner_answers(monkeypatch)
    accepted, _ = await h.submit(scene.seeded, "Make the captions bigger")
    await h.run(accepted.turn_id)
    after_turn = (list(talking_world.refreshes), debounce_keys(talking_world))
    if chips_on:
        rec.negative(
            "K8 chips on: the real Save asks for exactly one suggester run and takes one slot",
            "nermin-matcha-update",
            "1 run, 1 debounce key",
            len(after_save[0]) == 1 and len(after_save[1]) == 1,
            f"enqueued {after_save[0]}; keys {after_save[1]}",
        )
    else:
        rec.negative(
            "K8 chips off: the real Save and the real turn enqueue nothing and take no slot",
            "nermin-matcha-update",
            "0 runs, no debounce key, for the Save and for the turn",
            not after_save[0] and not after_save[1] and not after_turn[0] and not after_turn[1],
            f"Save: enqueued {after_save[0]} keys {after_save[1]}; "
            f"turn: enqueued {after_turn[0]} keys {after_turn[1]}",
        )
    rec.finish()

"""What must NOT happen: the stale, the pulled, the switched-off and the dark.

Failure catalogue (each row is a line of the "Negative cases" table in
``.chip-eval/e2e-report.md``):

  N1  stale base. The edit moved under a card the phone still shows (another card applied, or a
      native editor Save). A card that still fits is rebuilt from the registry on the facts as they
      are NOW and applied exactly once; one whose change is already there answers "no longer fits";
      an index-addressed ``remove_clip`` never lands on whichever clip now sits at that index.
  N2  a kind no longer offered. A kill switch pulled after the serve, the dark mute constant, a
      title the creator wrote themselves, or an id the thread never held: the tap is refused in
      words with the typed signal and nothing is written. It never reaches the free planner.
  N3  flag off. With the pinned-taps flag off a stale card's tap is just words for the legacy
      planner (no stamp, no pin); with the v2 flags off the wire carries no ``suggestions``; with
      chips off nothing is served; a kind whose signal or flag is off is not served.
  N4  the creator's request wins (KRI-129). The east run's recorded title requirement keeps the
      hook card out and its capture-time order keeps every clip out of a duplicates card, while the
      same footage before the creator stated anything serves both.
  N5  copy laundering. Hook text that echoes a transcript or a footage label, carries markup, or is
      not plain words never becomes a card (Gemini metadata never becomes on-screen text).
  N6  mute is dark. The evidence qualifies (a measured noisy, speechless, understood clip) but the
      card is not served while ``PINNED_TAP_APPLIES_CLIP_SOUND`` is False. With the constant patched
      on, the seam applies the typed ``clip_sound`` update and stages the approval (not shipped).
  N7  first prompt: ``test_first_prompt.py``.
  N8  stale base and kill switches for every kind, the creator's word for the five ported kinds, and
      the gates a hook's copy must pass: ``test_kill_and_stale.py``, ``test_creators_word.py``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.flow import (
    ensure_stored,
    removed_slots,
    tap_every_card,
    verify_render_kick,
)
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks import kria_pinned_suggestion_harness as h

CASSETTES = Path(__file__).resolve().parent / "cassettes"


def prefs(key: str) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


async def thread_and_set(world, key: str, profile: str, *, prefs_override=None):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, profile)
    phone = Phone(seeded, world, prefs_override or prefs(key), language=scn.language)
    out = await phone.serve()
    return scn, seeded, phone, out


def card_of(out, kind: str):  # noqa: ANN001, ANN202
    return next(c for c in out.suggestions if c.kind == kind)


def untouched(tap: d.Tap) -> bool:
    return tap.new_drafts == 0 and tap.before == tap.after


def refusal(tap: d.Tap, reason: str | None = None) -> bool:
    signal = tap.refused or {}
    return bool(
        signal.get("outcome") == "refused"
        and (reason is None or signal.get("reason") == reason)
        and untouched(tap)
    )


# ── N1: stale base ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n1_stale_base(talking_world):
    rec = Recorder("negative N1 stale base")
    key = "nermin-matcha-update"

    # A card that still fits after another card applied is rebuilt on the facts as they are now.
    scn, seeded, phone, out = await thread_and_set(talking_world, key, EDITOR)
    await ensure_stored(phone, out)
    first = await phone.tap(card_of(out, "transition"), out)
    second = await phone.tap(card_of(out, "sfx"), out)  # the SAME set, now one revision behind
    sfx = [
        e
        for e in (d.head_of(seeded.thread_id)["snap"].get("editor_payload") or {}).get(
            "sound_effects", []
        )
    ]
    rec.negative(
        "stale base, change still fits: rebuilt on the current edit and applied once",
        key,
        "completed, planned, exact=False, one new revision on top of the transition, one sfx",
        first.planned
        and second.planned
        and second.pin.get("exact") is False
        and second.new_drafts == 1
        and second.head_after["parent"] == first.head_after["id"]
        and len(sfx) == 1
        and "timeline_slots" in d.lane_diff(first.head_before, second.head_after),
        f"pin={second.pin} sfx={len(sfx)}",
    )

    # A native editor Save wrote a title: the hook card's change is already there.
    scn, seeded, phone, out = await thread_and_set(talking_world, key, EDITOR)
    await ensure_stored(phone, out)
    h.edit_variant(
        seeded,
        lambda v: v.update(text_elements=[{**copy.deepcopy(h.HOOK), "text": "Creator title"}]),
    )
    talking_world.refreshes.clear()
    tap = await phone.tap(card_of(out, "hook"), out)
    rec.negative(
        "stale base, change already there: 'no longer fits', nothing written, "
        "fresh cards asked for",
        key,
        "refused base_moved, rows unchanged, pinned_stale",
        refusal(tap, "base_moved") and "pinned_stale" in h.world_refreshes(talking_world),
        f"signal={tap.refused} reply={tap.reply[:60]!r}",
    )

    # An index-addressed remove_clip must follow the clip, not the index.
    scn, seeded, phone, out = await thread_and_set(talking_world, key, SIGNALS)
    await ensure_stored(phone, out)

    def swap(variant: dict) -> None:
        slots = variant["ai_timeline"]["slots"]
        slots[0], slots[1] = slots[1], slots[0]
        for index, slot in enumerate(slots):
            slot["clip_index"] = index

    h.edit_variant(seeded, swap)
    tap = await phone.tap(card_of(out, "duplicates"), out)
    head = d.head_of(seeded.thread_id)
    rec.negative(
        "stale base, index-addressed remove_clip: the weaker take goes, not the clip at that index",
        key,
        "clip-1 (slot-0, now at index 1) removed and clip-2 (slot-1) kept",
        tap.planned and tap.pin.get("exact") is False and removed_slots(head) == {"slot-0"},
        f"pin={tap.pin} removed={sorted(removed_slots(head))}",
    )
    rec.finish()


# ── N2: a kind no longer offered ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "flag", "profile"),
    [
        ("hook", "kria_suggestion_hook_copy_enabled", EDITOR),
        ("duplicates", "kria_suggestion_duplicates_enabled", SIGNALS),
    ],
)
async def test_n2_a_kill_switch_pulled_after_the_serve(
    talking_world, monkeypatch, kind, flag, profile
):
    rec = Recorder(f"negative N2 {kind} switched off after serve")
    key = "nermin-matcha-update"
    scn, seeded, phone, out = await thread_and_set(talking_world, key, profile)
    await ensure_stored(phone, out)
    monkeypatch.setattr(settings, flag, False)
    tap = await phone.tap(card_of(out, kind), out)
    rec.negative(
        f"{kind} switched off after serve",
        key,
        "refused in words with the typed signal, nothing written, never the planner",
        refusal(tap) and tap.reply == d.NO_LONGER_FITS,
        f"signal={tap.refused}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_n2_the_creators_own_title_and_an_unknown_card(talking_world):
    from app.kria.brief import BriefUpdate

    rec = Recorder("negative N2 evidence gone, unknown card")
    key = "nermin-matcha-update"
    scn, seeded, phone, out = await thread_and_set(talking_world, key, EDITOR)
    await ensure_stored(phone, out)
    k.write_brief(
        seeded,
        BriefUpdate(operation="add", kind="text", scope="title", description="a title about tea"),
    )
    tap = await phone.tap(card_of(out, "hook"), out)
    rec.negative(
        "the creator asks for their own title after the hook card was served (KRI-129)",
        key,
        "refused, rows unchanged",
        refusal(tap),
        f"signal={tap.refused}",
    )
    unknown = card_of(out, "captions")
    tap = await phone.tap(unknown, out, chip_id="ss_000000000000")
    rec.negative(
        "an id the thread never held",
        key,
        "refused unresolved, never planned as free text",
        refusal(tap, "unresolved"),
        f"signal={tap.refused}",
    )
    rec.finish()


# ── N3: flags off ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n3_flags_off(talking_world, monkeypatch):
    rec = Recorder("negative N3 flags off")
    key = "nermin-matcha-update"

    # Pinned taps off: the phone still holds a card; its tap is only words for the legacy planner.
    scn, seeded, phone, out = await thread_and_set(talking_world, key, EDITOR)
    await ensure_stored(phone, out)
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    planner = h.legacy_planner_answers(monkeypatch)
    card = card_of(out, "captions")
    tap = await phone.tap(card, out)
    stamped = h.events(seeded.thread_id, "user_message")[-1].payload.get("pinned_suggestion")
    rec.negative(
        "pinned taps off: a held card is just words for the legacy planner",
        key,
        "the planner is asked once with the card's words; no stamp, no pin, no draft",
        planner.await_count == 1 and stamped is None and not tap.pin and tap.new_drafts == 0,
        f"planner calls={planner.await_count} stamp={stamped} pin={tap.pin}",
    )
    fresh = await phone.get()
    rec.negative(
        "pinned taps off: the wire carries no v2 cards",
        key,
        "suggestions empty and absent from the JSON",
        not fresh.suggestions and "suggestions" not in json.loads(fresh.model_dump_json()),
        f"cards={len(fresh.suggestions)}",
    )

    # Pinned taps on, editor triggers off (a partial rollout): the cards are served, none is stored.
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", False)
    _scn, _seeded, lone, served = await thread_and_set(talking_world, key, EDITOR)
    entry = lone.ring_entry(served.set_id) or {}
    rec.negative(
        "pinned taps on and editor triggers off: every served card can be tapped",
        key,
        "the ring holds the cards the GET served",
        bool(served.suggestions) and bool(entry.get("suggestions")),
        f"served {[c.kind for c in served.suggestions]}, "
        f"stored {len(entry.get('suggestions') or [])}",
    )
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", True)

    # Chips off: nothing is served at all.
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", False)
    off = await phone.get()
    rec.negative(
        "chips off: unavailable, no cards, no chips",
        key,
        "status unavailable",
        off.status == "unavailable" and not off.suggestions and not off.chips,
        f"status={off.status}",
    )
    monkeypatch.setattr(settings, "kria_suggestion_chips_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)

    # Per-kind flags and the signals flag: a kind that is off is not served.
    for flag, kind, profile in (
        ("kria_suggestion_hook_copy_enabled", "hook", EDITOR),
        ("kria_suggestion_duplicates_enabled", "duplicates", SIGNALS),
        ("kria_suggestion_signals_enabled", "duplicates", SIGNALS),
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(settings, flag, False)
            _scn, _seeded, _phone, served = await thread_and_set(talking_world, key, profile)
        rec.negative(
            f"{flag} off: {kind} is not served",
            key,
            f"no {kind} card",
            kind not in [c.kind for c in served.suggestions],
            f"served {[c.kind for c in served.suggestions]}",
        )
    rec.finish()


# ── N4: the creator's request wins ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n4_the_recorded_brief_keeps_cards_out(talking_world):
    rec = Recorder("negative N4 KRI-129")
    recorded = "east-run-brief"
    bare = "east-run-brief/footage-only"
    _s, _sd, _p, with_brief = await thread_and_set(talking_world, recorded, EDITOR)
    _s, _sd, _p, without = await thread_and_set(talking_world, bare, EDITOR)
    rec.negative(
        "a recorded title requirement keeps the hook card out",
        recorded,
        "hook absent with the brief, present without it",
        "hook" not in [c.kind for c in with_brief.suggestions]
        and "hook" in [c.kind for c in without.suggestions],
        f"with brief {[c.kind for c in with_brief.suggestions]}; "
        f"without {[c.kind for c in without.suggestions]}",
    )
    _s, _sd, _p, with_brief = await thread_and_set(talking_world, recorded, SIGNALS)
    _s, _sd, _p, without = await thread_and_set(talking_world, bare, SIGNALS)
    rec.negative(
        "a capture-time order names every clip, so no clip may be removed as a duplicate",
        recorded,
        "duplicates absent with the brief, present without it",
        "duplicates" not in [c.kind for c in with_brief.suggestions]
        and "duplicates" in [c.kind for c in without.suggestions],
        f"with brief {[c.kind for c in with_brief.suggestions]}; "
        f"without {[c.kind for c in without.suggestions]}",
    )
    rec.finish()


# ── N5: copy laundering ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("echoes the transcript", "the restock just landed"),
        ("echoes a footage label", "first sip of the day"),
        ("carries a link", "Whisk first www.matcha.example"),
        ("carries markup", "Whisk <b>first</b> and talk later"),
        ("one word", "Whisk"),
    ],
)
async def test_n5_copy_that_may_not_reach_the_screen(talking_world, label, text):
    rec = Recorder(f"negative N5 hook copy {label}")
    key = "nermin-matcha-update"
    swapped = [{**p, "copy": text} if p["kind"] == "hook" else p for p in copy.deepcopy(prefs(key))]
    _scn, _seeded, _phone, out = await thread_and_set(
        talking_world, key, EDITOR, prefs_override=swapped
    )
    kinds = [c.kind for c in out.suggestions]
    rec.negative(
        f"hook copy that {label} never becomes a card",
        key,
        "no hook card",
        "hook" not in kinds,
        f"served {kinds}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_n5_a_natural_comma_is_dropped_too(talking_world):
    """Observed: validate_copy accepts one separator character between words, so the most ordinary
    hook punctuation, a comma and a space, is dropped, and neither the prompt nor the skill tells
    the model. Recorded so the owner can decide."""
    rec = Recorder("negative N5 comma hook")
    key = "nermin-matcha-update"
    swapped = [
        {**p, "copy": "Whisk first, talk later"} if p["kind"] == "hook" else p
        for p in copy.deepcopy(prefs(key))
    ]
    _scn, _seeded, _phone, out = await thread_and_set(
        talking_world, key, EDITOR, prefs_override=swapped
    )
    rec.negative(
        "observed: 'Whisk first, talk later' (comma + space) is dropped by validate_copy (chars)",
        key,
        "no hook card; the agent prompt and skill never say commas are refused",
        "hook" not in [c.kind for c in out.suggestions],
        f"served {[c.kind for c in out.suggestions]}",
    )
    rec.finish()


# ── N6: mute is dark ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_n6_mute_is_dark_as_shipped(talking_world):
    rec = Recorder("negative N6 mute dark")
    key = "nermin-matcha-update"
    scn, seeded, phone, out = await thread_and_set(talking_world, key, SIGNALS)
    kinds = [c.kind for c in out.suggestions]
    cell = rec.cell(key, SIGNALS, "mute_clip", scn.language)
    cell.notes.append(
        "PINNED_TAP_APPLIES_CLIP_SOUND is False: a phone editor-ops commit never re-applies a "
        "clip_sound fact, so the kind is dark by design even though the evidence qualifies "
        "(a measured noisy, speechless, understood clip)"
    )
    rec.negative(
        "mute is not served while PINNED_TAP_APPLIES_CLIP_SOUND is False",
        key,
        "no mute_clip card on a thread with a measured wind clip",
        mute_clip.PINNED_TAP_APPLIES_CLIP_SOUND is False and "mute_clip" not in kinds,
        f"served {kinds}",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["nermin-matcha-update", "nermin-matcha-update/tr"])
async def test_n6_mute_with_the_constant_patched_on(talking_world, monkeypatch, key):
    """Not shipped. What the seam would do if the constant were flipped: the evidence qualifies, the
    card serves, a tap stages the typed update plus the render approval, Undo takes it back, and
    approving the render hands the render worker the plan with ``clip_sound: off`` for the clip.
    Whether the RENDER then mutes the clip is not provable here (the phone re-plans from the
    brief), which is why the constant stays False."""
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"negative N6 mute patched {key}")
    scn, seeded, phone, out = await thread_and_set(talking_world, key, SIGNALS)
    rec.negative(
        "with the constant patched on the measured wind clip is offered",
        key,
        "a mute_clip card for clip 3",
        "mute_clip" in [c.kind for c in out.suggestions]
        and card_of(out, "mute_clip").anchor.clip_ordinal == 3,
        f"served {[c.kind for c in out.suggestions]}",
    )
    mute = card_of(out, "mute_clip")
    rec.negative(
        "the mute card shows the measurement: an evidence line for the wind in clip 3",
        key,
        "a line that names the noise (the reason, or the evidence when the agent's reason stands)",
        any(
            w in " ".join([mute.why, *mute.evidence]).casefold()
            for w in ("wind", "crowd", "noise", "rüzgâr", "rüzgar", "gürültü")
        ),
        f"why={mute.why!r} evidence={mute.evidence}",
    )

    async def probe(cell, kind):  # noqa: ANN001, ANN202
        _s, twin_seeded, twin, _o = await thread_and_set(talking_world, key, SIGNALS)
        await verify_render_kick(cell, twin, kind, monkeypatch)

    await tap_every_card(
        rec,
        phone,
        scn,
        SIGNALS,
        expect=[c.kind for c in out.suggestions],
        shipped=False,
        only=frozenset({"mute_clip"}),
        probe_render=probe,
    )
    rec.finish()

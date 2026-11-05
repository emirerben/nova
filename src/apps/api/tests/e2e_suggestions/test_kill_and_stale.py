"""N8: a card whose ground moved, and a switch pulled after the card was served.

Failure catalogue (written first; each row is a line of "Negative cases" in the report):

  S1  a stale card applies twice. The creator (or the native editor's Save) already made the change
      the card promises: captions are on, the music is low, a sound effect is in, the cut has a
      transition, the weaker take is gone, a title is there. Tapping the card must answer "no longer
      fits", write nothing and ask for fresh cards. Never apply the change a second time.
  S2  a restating card applies over a video it did not plan. A Save replaced the render
      (``render_generation_id`` moved) without writing a draft, so the restated plan holds none of
      the Save's edits. The tap is refused ``base_moved``, nothing is written.
  K1  a per-kind kill switch pulled after the serve does not stop the tap. For hook, duplicates,
      filler words and mute the tap must be refused in words with the typed signal, never reach the
      free planner, write no draft, approval, brief version or setting.
  K2  the same switch does not hide the card. The phone keeps a card whose tap is now refused for as
      long as the 24 h freeze lasts (GAP-10: the tap is safe, the card is just dead).
  K3  the master switch (pinned taps) pulled after the serve leaves ANY kind's card tappable as a
      pinned card, or its tap reaches nothing: the card's words go to the legacy planner exactly
      once, stamp nothing, write no draft. Every kind, not just captions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import gaps, threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.flow import ensure_stored
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING
from tests.tasks import kria_pinned_suggestion_harness as h

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update"


def prefs(key: str) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


async def served(world, key, profile):  # noqa: ANN001, ANN202
    scn = threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, profile)
    if profile == TALKING:
        threads.seed_cleanup_analysis(seeded, language=scn.language)
    phone = Phone(seeded, world, prefs(key), language=scn.language)
    return scn, seeded, phone, await phone.serve()


def card_of(out, kind):  # noqa: ANN001, ANN202
    return next(c for c in out.suggestions if c.kind == kind)


def untouched(tap: d.Tap) -> bool:
    return tap.new_drafts == 0 and tap.before == tap.after


def refused(tap: d.Tap) -> bool:
    signal = tap.refused or {}
    return bool(signal.get("outcome") == "refused" and untouched(tap))


def _save(**fields):  # noqa: ANN003, ANN202
    return lambda variant: variant.update(fields)


def _stamped_now() -> dict[str, str]:
    """The render stamps every real Save writes (naive UTC with a literal Z): the Save began a
    render after the card was served, and it finished."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
    return {"render_started_at": now, "render_finished_at": now}


def _all_crossfade(variant: dict) -> None:
    for slot in variant["ai_timeline"]["slots"]:
        slot["transition_after"] = "crossfade"


def _weaker_take_gone(variant: dict) -> None:
    variant["ai_timeline"]["slots"][0]["removed"] = True


SOUND = {"id": "fx-whoosh", "label": "Whoosh", "at_s": 0.5, "gain": 1.0, "duration_s": 0.6}
# Each: (kind, profile, what the creator's own Save did to the video, the card's own change).
ALREADY_THERE = {
    "captions": (EDITOR, _save(captions_enabled=True)),
    "music": (EDITOR, _save(mix=0.2)),
    "sfx": (EDITOR, _save(sound_effects=[SOUND])),
    "transition": (EDITOR, _all_crossfade),
    "duplicates": (SIGNALS, _weaker_take_gone),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(ALREADY_THERE))
async def test_s1_the_change_is_already_in_the_edit(talking_world, kind):
    profile, save = ALREADY_THERE[kind]
    rec = Recorder(f"negative S1 {kind} already there")
    _scn, seeded, phone, out = await served(talking_world, KEY, profile)
    assert await ensure_stored(phone, out) == "route"
    h.edit_variant(seeded, save)
    talking_world.refreshes.clear()
    tap = await phone.tap(card_of(out, kind), out)
    signal = tap.refused or {}
    rec.negative(
        f"stale base, {kind}: the creator's Save already made the change",
        KEY,
        "refused in words with the typed signal, nothing written, fresh cards asked for",
        refused(tap)
        and tap.reply == d.NO_LONGER_FITS
        and "pinned_stale" in h.world_refreshes(talking_world),
        f"signal={signal} reply={tap.reply[:60]!r} refreshes={h.world_refreshes(talking_world)}",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "profile"), [("filler_words", TALKING), ("mute_clip", SIGNALS)])
async def test_s2_a_save_replaced_the_video_under_a_restating_card(
    talking_world, monkeypatch, kind, profile
):
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"negative S2 {kind} video moved")
    _scn, seeded, phone, out = await served(talking_world, KEY, profile)
    assert await ensure_stored(phone, out) == "route"
    h.edit_variant(seeded, _save(render_generation_id="generation-2", **_stamped_now()))
    tap = await phone.tap(card_of(out, kind), out)
    signal = tap.refused or {}
    rec.negative(
        f"stale base, {kind}: a Save replaced the render without writing a draft",
        KEY,
        "refused base_moved, no draft, approval, brief version or setting written",
        refused(tap)
        and signal.get("reason") == "base_moved"
        and not d.item_flags(seeded)["speech_cleanup_enabled"],
        f"signal={signal} reply={tap.reply[:60]!r} rows {tap.before} -> {tap.after}",
    )
    rec.finish()


# ── Kill switches ────────────────────────────────────────────────────────────────────

PER_KIND = {
    "hook": ("kria_suggestion_hook_copy_enabled", EDITOR),
    "duplicates": ("kria_suggestion_duplicates_enabled", SIGNALS),
    "filler_words": ("kria_suggestion_filler_words_enabled", TALKING),
    "mute_clip": ("kria_suggestion_mute_clip_enabled", SIGNALS),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(PER_KIND))
async def test_k1_a_per_kind_switch_pulled_after_the_serve(talking_world, monkeypatch, kind):
    flag, profile = PER_KIND[kind]
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"negative K1 {kind} switched off")
    _scn, seeded, phone, out = await served(talking_world, KEY, profile)
    assert await ensure_stored(phone, out) == "route"
    monkeypatch.setattr(settings, flag, False)
    shown_after = [c.kind for c in (await phone.get()).suggestions]
    tap = await phone.tap(card_of(out, kind), out)
    rec.negative(
        f"{flag} off after serve: the tap is refused, never the planner",
        KEY,
        "refused in words with the typed signal, nothing written",
        refused(tap) and tap.reply == d.NO_LONGER_FITS and not tap.pin.get("outcome") == "planned",
        f"signal={tap.refused} reply={tap.reply[:60]!r}",
    )
    rec.negative(
        f"{flag} off after serve: the next GET no longer shows the {kind} card",
        KEY,
        f"{kind} absent from the cards the phone is shown",
        kind not in shown_after,
        f"cards now {shown_after}",
        gap=gaps.FROZEN_KIND_SWITCH,
    )
    rec.finish()


ALL_KINDS = [
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
@pytest.mark.parametrize(("kind", "profile"), ALL_KINDS)
async def test_k3_the_master_switch_pulled_after_the_serve(
    talking_world, monkeypatch, kind, profile
):
    if kind == "mute_clip":
        monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"negative K3 {kind} pinned taps off")
    _scn, seeded, phone, out = await served(talking_world, KEY, profile)
    assert await ensure_stored(phone, out) == "route"
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    planner = h.legacy_planner_answers(monkeypatch)
    wire_after = await phone.get()
    tap = await phone.tap(card_of(out, kind), out)
    stamped = h.events(seeded.thread_id, "user_message")[-1].payload.get("pinned_suggestion")
    rec.negative(
        f"pinned taps off after serve: the {kind} GET carries no v2 card",
        KEY,
        "no suggestions on the wire, byte-for-byte a legacy set",
        not wire_after.suggestions and "suggestions" not in phone.last_wire,
        f"cards now {[c.kind for c in wire_after.suggestions]}",
    )
    rec.negative(
        f"pinned taps off after serve: a tap on the held {kind} card is the legacy planner's, once",
        KEY,
        "the planner is asked once with the card's words; no stamp, no pin, no draft, no setting",
        planner.await_count == 1
        and stamped is None
        and not tap.pin
        and tap.new_drafts == 0
        and not d.item_flags(seeded)["speech_cleanup_enabled"],
        f"planner calls={planner.await_count} stamp={stamped} pin={tap.pin}",
    )
    rec.finish()

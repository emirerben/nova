"""R5-F: a native-editor Save, and every card served before or after it.

A Save changes the saved video without writing a draft. A card is built on what the agent read, so
the Save is the one event that can move the ground under a card without the thread's own history
saying so. The rule under test: a card the GET serves after a Save executes on the SAVED video; a
card served before it either runs on the saved video or is refused in words, never applied to the
video the Save replaced and never dead on its first tap.

Failure catalogue (written first; each row is a test below):

  V1  a card served AFTER a Save is dead on its first tap. Every card the unmodified GET serves
      after the real ``editor-commit`` route ran is tapped with the full end-to-end checks
      (accepted,
      one real change in the promised lane and direction, receipts met, Undo restores everything,
      second tap refused with a fresh set), and the change is made on the saved video: the draft's
      ``base_generation`` is the generation the Save minted, not the one it replaced.
  V2  a restating card is served over a plan that holds none of the Save's edits. The filler card
      restates the PLAN; approving it would render without what the Save changed. It must not be
      served while the Save is unread; its control twin (no Save) serves it.
  V3  a card served BEFORE a Save loses the creator's Save. Tapped after it, an editor card is built
      on the saved video (the Save's edit survives) or refused typed; a restating card is refused
      ``base_moved``. Either way nothing is applied over the stale base and a refusal writes
      nothing.
  V4  a card whose change the Save already made applies again. Captions switched on by the Save,
      music already low in it: refused in words with the typed signal, nothing written, fresh cards
      asked for. The Save is the real route, not an edit of the stored job.
  V5  the withholding never ends. Once the agent has read the saved video (a Kria render completed
      and the session's pointer follows it) the restating card is offered again and executes.

The Save is the real ``POST /plan-items/{id}/variants/{id}/editor-commit`` through the ASGI app
(``scenes.route_save``). Stand-ins: the render kick (no broker), the durable-sources prefix and its
existence check (no bucket), and the render worker's completion write.
"""

from __future__ import annotations

import pytest

from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import gaps
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.flow import LANE, RESTATE, ensure_stored, tap_every_card
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING

# A Save that changes nothing a card is about: the footage's own sound level on a montage, the
# caption size on a Talking take (the only profile whose editor accepts captions).
NEUTRAL = {
    EDITOR: {"mix": {"original_level": 0.8}},
    SIGNALS: {"mix": {"original_level": 0.8}},
    TALKING: {"caption_meta": {"size_px": 96}},
}
CELLS = [
    ("nermin-matcha-update", EDITOR),
    ("nermin-matcha-update", SIGNALS),
    ("nermin-matcha-update", TALKING),
    ("nermin-matcha-update/tr", TALKING),
    ("east-run-brief/footage-only/en", TALKING),
]
IDS = [f"{k}:{p}" for k, p in CELLS]


def saved_generation_of_head(tap: d.Tap) -> str | None:
    return s.payload_of(tap.head_after).get("base_generation")


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "profile"), CELLS, ids=IDS)
async def test_v1_v2_every_card_served_after_a_save_executes(
    talking_world, monkeypatch, key, profile
):
    rec = Recorder(f"save V1/V2 served after a Save {key} {profile}")
    control = s.build(talking_world, key, profile)
    control_kinds = s.kinds_of(await control.phone.serve())

    scene = s.build(talking_world, key, profile)
    before = s.pointer(scene.seeded)
    generation = await s.route_save(scene.seeded, **NEUTRAL[profile])
    assert generation != before, "the Save did not move the video"
    out = await scene.phone.serve()
    served = s.kinds_of(out)

    unread = {"filler_words", "mute_clip"}
    rec.negative(
        "V2: no restating card is served while the Save is unread; every other card still is",
        f"{key}/{profile}",
        f"the control's cards ({control_kinds}) without {sorted(unread)}",
        served == [k for k in control_kinds if k not in unread],
        f"control {control_kinds}; after the Save {served}",
    )
    rec.negative(
        "V2: the control twin (no Save) does serve the restating card where the footage has one",
        f"{key}/{profile}",
        "filler served on the Talking control",
        profile != TALKING or "filler_words" in control_kinds,
        f"control {control_kinds}",
    )

    def on_the_saved_video(cell, _phone, tap) -> None:  # noqa: ANN001
        base = saved_generation_of_head(tap)
        cell.check(
            "on_saved_video",
            base == generation,
            f"draft base_generation {base!r}; the Save minted {generation!r}; before it the "
            f"video was {before!r}",
        )

    async def probe(cell, kind):  # noqa: ANN001, ANN202
        cell.na("render_kick", "the restating card is not served after a Save")

    cells = await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        profile,
        expect=served,
        state="after-save",
        probe_render=probe,
        after_tap=on_the_saved_video,
        # The restating card comes back after an Undo (V7); this test is about what the GET serves
        # right after the Save.
        only=frozenset(served) - RESTATE,
    )
    rec.negative(
        "V1: at least one card was served after the Save and tapped",
        f"{key}/{profile}",
        "a non-empty set",
        bool(cells),
        f"served {served}",
    )
    rec.finish()


# ── V7: the fence a Save puts up must survive an editor tap and its Undo ─────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["nermin-matcha-update", "nermin-matcha-update/tr"], ids=["en", "tr"]
)
async def test_v7_an_editor_tap_and_its_undo_do_not_lift_the_saves_fence(talking_world, key):
    """Save -> tap an editor card -> Undo -> the filler card. The editor tap re-syncs the session's
    pointer to the saved video (a draft built on it has read it), and Undo restores the PLAN as the
    head. The restating card is then offered over a plan that holds none of the Save's edits, which
    is exactly what the fence exists to prevent (``_restate_video_moved``)."""
    rec = Recorder(f"save V7 fence after an editor tap and Undo {key}")
    scene = s.build(talking_world, key, TALKING)
    await s.route_save(scene.seeded, **NEUTRAL[TALKING])
    out = await scene.phone.serve()
    captions = s.card_of(out, "captions")
    assert captions is not None
    await scene.phone.tap(captions, out)
    await scene.phone.undo()
    after = await scene.phone.serve()
    filler = s.card_of(after, "filler_words")
    outcome = "not served"
    held = filler is None
    if filler is not None:
        tap = await scene.phone.tap(filler, after)
        held = s.is_refused(tap, "base_moved")
        reason = (tap.refused or {}).get("reason")
        outcome = (
            f"served and {'refused ' + str(reason) if tap.refused else 'ACCEPTED'}"
            f" (pointer {s.pointer(scene.seeded)!r}, saved {s.baseline(scene.seeded)!r})"
        )
    rec.negative(
        "Save, an editor tap, Undo: the filler card is not served or its tap is refused base_moved",
        key,
        "the plan still holds none of the Save's edits, so the filler card must not run",
        held,
        f"the filler card was {outcome}",
        gap=gaps.SAVE_FENCE_LIFTED,
    )
    rec.finish()


# ── V3: a card served before the Save ────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "profile", "kind"),
    [
        ("nermin-matcha-update", EDITOR, "captions"),
        ("nermin-matcha-update", EDITOR, "music"),
        ("nermin-matcha-update", EDITOR, "transition"),
        ("nermin-matcha-update", EDITOR, "sfx"),
        ("nermin-matcha-update", EDITOR, "hook"),
        ("nermin-matcha-update", SIGNALS, "duplicates"),
        ("nermin-matcha-update", TALKING, "captions"),
        ("nermin-matcha-update", TALKING, "filler_words"),
        ("nermin-matcha-update/tr", TALKING, "filler_words"),
    ],
)
async def test_v3_a_card_served_before_a_save_is_never_applied_over_it(
    talking_world, key, profile, kind
):
    rec = Recorder(f"save V3 served before a Save {key} {profile} {kind}")
    scene = s.build(talking_world, key, profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    assert card is not None, f"the thread did not serve {kind}: {s.kinds_of(out)}"
    assert await ensure_stored(scene.phone, out) == "route"
    pointer_before = s.pointer(scene.seeded)
    generation = await s.route_save(scene.seeded, **NEUTRAL[profile])
    talking_world.refreshes.clear()
    tap = await scene.phone.tap(card, out)
    where = f"{key}/{profile}/{kind}"

    if kind in RESTATE:
        rec.negative(
            f"{kind}: a restating card served before a Save is refused base_moved",
            where,
            "typed refusal, no draft, approval, brief version or setting written",
            s.is_refused(tap, "base_moved")
            and not d.item_flags(scene.seeded)["speech_cleanup_enabled"],
            f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
        )
        rec.negative(
            f"{kind}: the refusal leaves the session's pointer where it was",
            where,
            "the pointer is not re-synced by a refused restate",
            s.pointer(scene.seeded) == pointer_before,
            f"pointer {s.pointer(scene.seeded)!r} (before {pointer_before!r}, "
            f"saved {generation!r})",
        )
    else:
        applied = s.executed(tap)
        refused = s.is_refused(tap)
        rec.negative(
            f"{kind}: served before the Save, the tap runs on the saved video or is refused typed",
            where,
            "planned with one revision, or refused typed with nothing written",
            (applied and tap.new_drafts == 1) or refused,
            f"planned={tap.planned} signal={tap.refused} reply={tap.reply[:70]!r} "
            f"drafts +{tap.new_drafts}",
        )
        if applied:
            base = saved_generation_of_head(tap)
            diff = d.lane_diff(tap.head_before, tap.head_after)
            creative = set(diff) - {
                "base_generation",
                "copilot_receipt_ids",
                "deletions",
                "learning_temporary",
                "remove_music",
                "retry_guided_revision",
            }
            rec.negative(
                f"{kind}: the applied change sits on the saved video and moves only its own lane",
                where,
                f"base_generation == {generation!r}; creative lanes == {{{LANE[kind]}}}",
                base == generation and creative == {LANE[kind]},
                f"base_generation {base!r}; lanes {sorted(creative)}",
            )
        else:
            rec.negative(
                f"{kind}: a refusal tells the creator in words and asks for fresh cards",
                where,
                "the 'no longer fits' reply and a pinned_stale refresh",
                tap.reply == d.NO_LONGER_FITS
                and "pinned_stale" in s.h.world_refreshes(talking_world),
                f"reply={tap.reply[:70]!r} refreshes={s.h.world_refreshes(talking_world)}",
            )
    rec.finish()


# ── V4: the Save already made the change ─────────────────────────────────────────────

MOOT = {
    "captions": (TALKING, {"caption_meta": {"enabled": True}}),
    "music": (EDITOR, {"mix": {"music_level": 0.2}}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(MOOT))
async def test_v4_a_card_whose_change_the_save_already_made_is_refused(talking_world, kind):
    profile, sections = MOOT[kind]
    rec = Recorder(f"save V4 {kind} already made by the Save")
    scene = s.build(talking_world, "nermin-matcha-update", profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    assert card is not None
    await s.route_save(scene.seeded, **sections)
    talking_world.refreshes.clear()
    tap = await scene.phone.tap(card, out)
    rec.negative(
        f"{kind}: the Save (the real route) already made the card's change",
        "nermin-matcha-update",
        "refused in words with the typed signal, nothing written, fresh cards asked for",
        s.is_refused(tap)
        and tap.reply == d.NO_LONGER_FITS
        and "pinned_stale" in s.h.world_refreshes(talking_world),
        f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
    )
    again = await scene.phone.serve()
    rec.negative(
        f"{kind}: the next set does not offer the change the Save made",
        "nermin-matcha-update",
        f"no {kind} card",
        kind not in s.kinds_of(again),
        f"served {s.kinds_of(again)}",
    )
    rec.finish()


# ── V5: the withholding ends when the agent has read the video ───────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["nermin-matcha-update", "nermin-matcha-update/tr"], ids=["en", "tr"]
)
async def test_v5_the_restating_card_returns_once_the_agent_has_read_the_saved_video(
    talking_world, monkeypatch, key
):
    rec = Recorder(f"save V5 restating card returns {key}")
    scene = s.build(talking_world, key, TALKING)
    await s.route_save(scene.seeded, **NEUTRAL[TALKING])
    # The pointer follows the video the moment the agent has read it (a Kria render completing is
    # its only writer besides an editor tap), so the first read after both sees them together.
    generation = s.agent_reads_video(scene.seeded)
    out = await scene.phone.serve()
    rec.negative(
        "once the pointer follows the saved video the filler card is offered",
        key,
        "filler_words served",
        "filler_words" in s.kinds_of(out),
        f"served {s.kinds_of(out)} (pointer {s.pointer(scene.seeded)!r} == {generation!r})",
    )

    async def probe(cell, kind):  # noqa: ANN001, ANN202
        twin = s.build(talking_world, key, TALKING).phone
        await s.route_save(twin.seeded, **NEUTRAL[TALKING])
        s.agent_reads_video(twin.seeded)
        from tests.e2e_suggestions.flow import verify_render_kick  # noqa: PLC0415

        await verify_render_kick(cell, twin, kind, monkeypatch)

    await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        TALKING,
        expect=s.kinds_of(out),
        state="saved-and-read",
        only=frozenset({"filler_words"}),
        probe_render=probe,
    )
    rec.finish()

"""R5-M: the matrix gaps the baseline run leaves, and the taps that do not wait for a refresh.

Failure catalogue (written first; each row is a test below):

  M1  the mute card is untested outside two threads. With its dark constant patched on (NOT what
      ships, said so on every cell) it is served and tapped on every footage the evidence
      qualifies, in English and Turkish: the typed ``clip_sound`` requirement and the render
      approval it stages, Undo takes both back, approving hands the renderer ``clip_sound: off``.
      Where the card is not served the reason is recorded.
  M2  taps that do not wait. The creator taps three cards of the set the phone holds without a
      refresh in between (the phone polls on a backoff). Each tap executes on the edit as the
      earlier ones left it (one revision each, its own lane only) or is refused in words; none is
      applied twice, none writes a second change.
  M3  the same, the restating card first. A staged render approval is pending when the creator
      taps the next card: the second tap must be answered in words (nothing written), never run
      beside an approval it does not know about.
  M4  the coverage claim. Every kind the product ships is served and tapped in English and in
      Turkish on the baseline matrix of this directory (mute only with its constant patched).

Everything here is the real path: GET -> worker -> GET -> ``submit_turn`` -> ``run_kria_turn``.
"""

from __future__ import annotations

import pytest

from app.services.suggestions.kinds import mute_clip
from tests.e2e_suggestions import driver as d
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.flow import (
    BOOKKEEPING,
    LANE,
    RESTATE,
    tap_every_card,
    verify_render_kick,
)
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR, SIGNALS, TALKING

MUTE_SCENARIOS = [
    ("nermin-matcha-update", True),
    ("nermin-matcha-update/tr", True),
    ("nermin-travel-diary", True),
    ("east-run-brief/footage-only", True),
    ("east-run-brief/footage-only/en", True),
    # The recorded brief names every clip (a capture-time order, per-clip text): the card must not
    # be offered over a creator who placed every clip, and its absence is the proof.
    ("east-run-brief", False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "served"), MUTE_SCENARIOS, ids=[k for k, _ in MUTE_SCENARIOS])
async def test_m1_mute_with_the_constant_patched_on(talking_world, monkeypatch, key, served):
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)
    rec = Recorder(f"matrix M1 mute (constant patched) {key}")
    scene = s.build(talking_world, key, SIGNALS)
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    rec.negative(
        "M1 the measured wind clip is offered (or the recorded brief keeps the card out)",
        key,
        "mute_clip served" if served else "mute_clip not served",
        ("mute_clip" in kinds) == served,
        f"served {kinds}",
    )
    if not served:
        cell = rec.cell(key, SIGNALS, "mute_clip", scene.scn.language, state="patched-constant")
        cell.shipped = False
        cell.notes.append(
            "PINNED_TAP_APPLIES_CLIP_SOUND is False as shipped; patched on for this run. The "
            "recorded brief names every clip, so the card is withheld"
        )
        rec.finish()
        return

    async def probe(cell, kind):  # noqa: ANN001, ANN202
        twin = s.build(talking_world, key, SIGNALS)
        await verify_render_kick(cell, twin.phone, kind, monkeypatch)

    await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        SIGNALS,
        expect=kinds,
        shipped=False,
        only=frozenset({"mute_clip"}),
        state="patched-constant",
        probe_render=probe,
    )
    rec.finish()


# ── M2 / M3: taps that do not wait for a refresh ─────────────────────────────────────

HELD = [
    ("nermin-matcha-update", EDITOR),
    ("nermin-matcha-update", SIGNALS),
    ("nermin-matcha-update", TALKING),
    ("nermin-matcha-update/tr", EDITOR),
    ("nermin-matcha-update/tr", TALKING),
    ("east-run-brief/footage-only/en", TALKING),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "profile"), HELD, ids=[f"{k}:{p}" for k, p in HELD])
async def test_m2_m3_every_card_of_one_held_set_tapped_without_a_refresh(
    talking_world, key, profile
):
    rec = Recorder(f"matrix M2/M3 held set {key} {profile}")
    where = f"{key}/{profile}"
    scene = s.build(talking_world, key, profile)
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    editor_cards = [c for c in out.suggestions if c.kind not in RESTATE]
    restating = [c for c in out.suggestions if c.kind in RESTATE]

    applied: list[str] = []
    for card in editor_cards:
        tap = await scene.phone.tap(card, out)
        lane = LANE[card.kind]
        diff = d.lane_diff(tap.head_before, tap.head_after)
        own_lane_only = (set(diff) - BOOKKEEPING) <= {lane}
        if s.executed(tap):
            applied.append(card.kind)
            rec.negative(
                f"M2 {card.kind} (tapped #{len(applied)} on a set the phone never refreshed)",
                where,
                "planned, one revision, only its own lane changed on top of the earlier taps",
                tap.new_drafts == 1 and own_lane_only,
                f"drafts +{tap.new_drafts}; lanes {sorted(diff)}",
            )
        else:
            rec.negative(
                f"M2 {card.kind} (tapped after {applied}) is refused in words, nothing written",
                where,
                "typed refusal, the 'no longer fits' reply",
                s.is_refused(tap) and tap.reply in {d.NO_LONGER_FITS, d.NO_LONGER_FITS_TR},
                f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
            )
    for card in restating:
        tap = await scene.phone.tap(card, out)
        # The head is now the editor work of the earlier taps (or the plan, if none applied): a
        # restate over editor work is refused base_moved; over the plan it runs.
        ran = s.executed(tap)
        rec.negative(
            f"M3 {card.kind}: tapped last, after {applied}",
            where,
            "runs on the plan (nothing applied before) or is refused in words with nothing written",
            (ran and not applied) or (s.is_refused(tap) and bool(applied)),
            f"planned={tap.planned} signal={tap.refused} reply={tap.reply[:70]!r}",
        )
    rec.negative(
        "M2 every served card was tapped",
        where,
        f"{len(kinds)} cards",
        len(editor_cards) + len(restating) == len(kinds),
        f"served {kinds}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_m3_a_pending_approval_answers_the_next_tap_in_words(talking_world):
    """The restating card first, then a card of the same held set while its approval waits."""
    rec = Recorder("matrix M3 pending approval then the next card")
    key = "nermin-matcha-update"
    scene = s.build(talking_world, key, TALKING)
    out = await scene.phone.serve()
    filler = s.card_of(out, "filler_words")
    first = await scene.phone.tap(filler, out)
    assert first.planned and first.result["status"] == "awaiting_approval"
    from app.kria.runtime import RuntimeFailure  # noqa: PLC0415

    for card in [c for c in out.suggestions if c.kind != "filler_words"]:
        before = s.h.counts(scene.seeded)
        try:
            tap = await scene.phone.tap(card, out)
            outcome = f"accepted as a turn: planned={tap.planned} signal={tap.refused}"
            ok = s.untouched(tap) and not s.executed(tap)
        except RuntimeFailure as refusal:
            # The submit itself refuses (HTTP 409): the same answer a typed message would get.
            outcome = f"HTTP {refusal.status_code} {refusal.code}: {refusal.message}"
            ok = refusal.code == "queued_successor_exists" and s.h.counts(scene.seeded) == before
        rec.negative(
            f"M3 {card.kind} tapped while the filler approval is pending",
            key,
            "answered in words, nothing written, no second approval",
            ok,
            outcome,
        )
    rec.negative(
        "M3 the staged approval is still the one filler staged",
        key,
        "exactly one pending approval",
        d.approvals_pending(scene.seeded.thread_id) == 1,
        f"pending {d.approvals_pending(scene.seeded.thread_id)}",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["nermin-matcha-update", "nermin-matcha-update/tr"], ids=["en", "tr"]
)
async def test_m5_a_card_tapped_over_a_pending_approval_runs_once_it_is_denied(talking_world, key):
    """The first tap while a render approval waits is queued as the next turn (status ``ignored``,
    the chat shows the creator's line and no answer yet). Once the approval is denied the queued
    card turn runs: exactly the hook the card showed, on the edit as it is then, with the filler
    setting the first tap flipped already taken back."""
    rec = Recorder(f"matrix M5 queued card turn {key}")
    scene = s.build(talking_world, key, TALKING)
    out = await scene.phone.serve()
    filler, hook = s.card_of(out, "filler_words"), s.card_of(out, "hook")
    flags_before = d.item_flags(scene.seeded)
    first = await scene.phone.tap(filler, out)
    assert first.planned and first.result["status"] == "awaiting_approval"
    queued = await scene.phone.tap(hook, out)
    rec.negative(
        "M5 the hook tapped over a pending approval is queued, not applied and not refused",
        key,
        "status ignored, nothing written, the chat line is the card's prompt",
        queued.result["status"] == "ignored" and s.untouched(queued) and queued.chat == hook.prompt,
        f"result {queued.result} rows {queued.before} -> {queued.after}",
    )
    await s.decide(scene.seeded, "deny")
    ran = await s.h.run(queued.accepted.turn_id)
    head = d.head_of(scene.seeded.thread_id)
    title = s.payload_of(head).get("title")
    rec.negative(
        "M5 once the approval is denied the queued card turn runs and applies the card's own text",
        key,
        "completed; an editor draft whose title is the card's copy; filler setting back",
        ran["status"] == "completed"
        and title == hook.copy_text
        and d.item_flags(scene.seeded) == flags_before,
        f"run {ran}; title {title!r} vs card {hook.copy_text!r}; item {d.item_flags(scene.seeded)} "
        f"(before {flags_before})",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "profile"),
    [
        ("captions", EDITOR),
        ("music", EDITOR),
        ("transition", EDITOR),
        ("sfx", EDITOR),
        ("hook", EDITOR),
        ("duplicates", SIGNALS),
        ("filler_words", TALKING),
    ],
    ids=lambda v: str(v),
)
async def test_m6_a_double_tap_writes_one_change(talking_world, kind, profile):
    """Two taps of one card, both submitted before the first turn has run (a thumb that lands
    twice). One revision (or one approval) is written; the second tap changes nothing."""
    rec = Recorder(f"matrix M6 double tap {kind}")
    scene = s.build(talking_world, "nermin-matcha-update", profile)
    out = await scene.phone.serve()
    card = s.card_of(out, kind)
    tap = {"set_id": out.set_id, "chip_id": card.id}
    before = s.h.counts(scene.seeded)
    first, _ = await s.h.submit(scene.seeded, card.prompt, tap=tap)
    second, _ = await s.h.submit(scene.seeded, card.prompt, tap=tap)
    ran_first = await s.h.run(first.turn_id)
    ran_second = await s.h.run(second.turn_id)
    after = s.h.counts(scene.seeded)
    wrote = {name: after[name] - before[name] for name in after if after[name] != before[name]}
    approvals = d.approvals_pending(scene.seeded.thread_id)
    rec.negative(
        f"M6 {kind}: a double tap applies once",
        "nermin-matcha-update",
        "exactly one revision, at most one approval; the second turn is ignored, not applied",
        wrote.get("drafts") == 1
        and approvals <= 1
        and ran_second["status"] == "ignored"
        and ran_first["status"] in {"completed", "awaiting_approval"},
        f"first {ran_first['status']}, second {ran_second['status']}; rows written {wrote}; "
        f"pending approvals {approvals}",
    )
    rec.finish()


# ── M4: the coverage claim ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_m4_every_shipped_kind_is_served_in_both_languages(talking_world):
    """Served (not tapped: the per-kind matrix taps) on at least one English and one Turkish
    scenario, for every kind that ships. A kind that never serves cannot be proven."""
    rec = Recorder("matrix M4 coverage")
    shipped = {"captions", "sfx", "transition", "music", "hook", "filler_words", "duplicates"}
    seen: dict[str, set[str]] = {kind: set() for kind in shipped}
    for key in threads.SCENARIOS:
        scn = threads.SCENARIOS[key]
        for profile in scn.profiles:
            scene = s.build(talking_world, key, profile)
            for kind in s.kinds_of(await scene.phone.serve()):
                if kind in seen:
                    seen[kind].add(scn.language)
    missing = {
        k: sorted({"en", "tr"} - langs) for k, langs in seen.items() if langs != {"en", "tr"}
    }
    rec.negative(
        "M4 every shipped kind is served in English and in Turkish",
        "all scenarios",
        "every kind in both languages",
        not missing,
        f"languages per kind { ({k: sorted(v) for k, v in seen.items()}) }; missing {missing}",
    )
    rec.finish()

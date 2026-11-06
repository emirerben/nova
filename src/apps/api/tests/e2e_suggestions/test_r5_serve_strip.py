"""R5-S2: a served filler card is a card its tap accepts, whatever moved after the freeze.

The filler card rides on the speech-cleanup capability and on the evidence it was proven on. A set
is frozen for 24 h and a lever can be pulled, or the analysis can change, inside that window. The
tap re-checks all of it and refuses (a dead tap that also hides the card for good).

Failure catalogue (written first; each row is a test below):

  S1  a capability lever pulled after the serve (SILENCE_CUT_ENABLED, SUBTITLED_ARCHETYPE_ENABLED,
      SPEECH_CLEANUP_MODE=off): the frozen set kept the filler card and its tap was refused
      ``base_moved``. With the freshness switch on AND off. The other cards of the set stay, and
      the lever put back gives the same card back (the stored set is not edited).
  S2  the evidence the card was proven on changed after the serve: cleanup switched on from the
      plan item, the creator's decision on the analysis (keep original, clean), the source policy
      fingerprint moved (a detector bump), the analysis re-queued. Same expectations as S1.
  S3  the held card's tap is refused with a typed record in the thread's language and writes
      nothing (the strip is not hiding a card that would have worked).
  S4  the protected-region gate (KRI-901): a restating tap that requests render approval is refused
      ``mark_needed`` when a live ``keep_text_off`` object has no creator mark, on a phone Talking
      draft, for an app build that shows the mark card. The GET leaves the restating cards out in
      exactly that case and in no other: a mark on the clip, a face-only ask, an older build, a
      take that does not render on the phone, no brief binding. The tap decides, so each side
      carries the tap's own verdict.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.config import settings
from app.database import sync_session
from app.models import PlanItem, SpeechCleanupAnalysis
from app.services.client_protocol import bind_sending_build, unbind_sending_build
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r4_serve_copy_gate import append
from tests.e2e_suggestions.test_r5_serve_common import (
    KEYS,
    build,
    card_of,
    executes,
    get_at,
    into_phone_take,
    kinds_of,
    lang_reply,
    prefs,
)
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks import kria_pinned_suggestion_harness as h

FRESHNESS = [True, False]
FRESHNESS_IDS = ["triggers-on", "triggers-off"]
LEVERS = [
    ("silence_cut_enabled", False),
    ("subtitled_archetype_enabled", False),
    ("speech_cleanup_mode", "off"),
]
LEVER_IDS = [name for name, _ in LEVERS]


def set_analysis(seeded: h.Seeded, **fields) -> None:  # noqa: ANN003
    with sync_session() as db:
        row = (
            db.query(SpeechCleanupAnalysis)
            .filter(SpeechCleanupAnalysis.plan_item_id == seeded.item_id)
            .one()
        )
        for name, value in fields.items():
            setattr(row, name, value)
        db.commit()


def set_item_cleanup(seeded: h.Seeded, value: bool) -> None:
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id, with_for_update=True)
        item.speech_cleanup_enabled = value
        db.commit()


def current_fingerprint(seeded: h.Seeded) -> str:
    with sync_session() as db:
        return (
            db.query(SpeechCleanupAnalysis)
            .filter(SpeechCleanupAnalysis.plan_item_id == seeded.item_id)
            .one()
            .source_policy_fingerprint
        )


MUTATIONS = {
    "item_cleanup_on": (
        lambda s: set_item_cleanup(s, True),
        lambda s, _before: set_item_cleanup(s, False),
    ),
    "decision_keep_original": (
        lambda s: set_analysis(s, decision="keep_original", decision_at=datetime.now(UTC)),
        lambda s, _before: set_analysis(s, decision=None, decision_at=None),
    ),
    "decision_clean": (
        lambda s: set_analysis(s, decision="clean", decision_at=datetime.now(UTC)),
        lambda s, _before: set_analysis(s, decision=None, decision_at=None),
    ),
    "fingerprint_moved": (
        lambda s: set_analysis(s, source_policy_fingerprint="detector-bumped"),
        lambda s, before: set_analysis(s, source_policy_fingerprint=before),
    ),
    "analysis_requeued": (
        lambda s: set_analysis(s, status="queued"),
        lambda s, _before: set_analysis(s, status="ready"),
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("triggers", FRESHNESS, ids=FRESHNESS_IDS)
@pytest.mark.parametrize(("lever", "value"), LEVERS, ids=LEVER_IDS)
@pytest.mark.parametrize("key", KEYS)
async def test_s1_a_capability_lever_pulled_after_the_serve_takes_the_filler_card(
    talking_world, monkeypatch, key, lever, value, triggers
):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", triggers)
    rec = Recorder(f"R5 strip S1 {key} {lever} triggers={triggers}")
    where = f"{key}/talking/{lever}"
    scene = build(talking_world, key, "talking")
    out = await scene.phone.serve()
    card = card_of(out, "filler_words")
    assert card is not None, f"the control thread did not serve the filler card: {kinds_of(out)}"
    others = [c.kind for c in out.suggestions if c.kind != "filler_words"]

    original = getattr(settings, lever)
    monkeypatch.setattr(settings, lever, value)
    after = await scene.phone.serve()
    rec.negative(
        f"S1 {lever}={value!r}: the filler card is not on the phone",
        where,
        "no filler_words card; the other cards stay",
        card_of(after, "filler_words") is None and set(others) <= set(kinds_of(after)),
        f"served {kinds_of(after)} (was {kinds_of(out)})",
    )
    monkeypatch.setattr(settings, lever, original)
    back = await scene.phone.serve()
    card_back = card_of(back, "filler_words")
    rec.negative(
        f"S1 {lever} put back: the same filler card returns, no new model run needed",
        where,
        f"filler card {card.id}",
        card_back is not None and card_back.id == card.id,
        f"served {[(c.kind, c.id) for c in back.suggestions]}; it was {card.id}",
    )
    if card_back is not None:
        tap = await scene.phone.tap(card_back, back)
        rec.negative(
            "S1 the card that returned executes",
            where,
            "planned, one revision, approval staged",
            executes(tap),
            f"signal={tap.refused} reply={tap.reply[:70]!r}",
        )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("triggers", FRESHNESS, ids=FRESHNESS_IDS)
@pytest.mark.parametrize("name", list(MUTATIONS))
@pytest.mark.parametrize("key", KEYS)
async def test_s2_evidence_that_moved_after_the_serve_takes_the_filler_card(
    talking_world, monkeypatch, key, name, triggers
):
    monkeypatch.setattr(settings, "kria_suggestion_editor_triggers_enabled", triggers)
    rec = Recorder(f"R5 strip S2 {key} {name} triggers={triggers}")
    where = f"{key}/talking/{name}"
    scene = build(talking_world, key, "talking")
    out = await scene.phone.serve()
    card = card_of(out, "filler_words")
    assert card is not None, f"the control thread did not serve the filler card: {kinds_of(out)}"
    before = current_fingerprint(scene.seeded)
    change, undo = MUTATIONS[name]

    change(scene.seeded)
    after = await scene.phone.serve()
    rec.negative(
        f"S2 {name}: the filler card is not on the phone",
        where,
        "no filler_words card",
        card_of(after, "filler_words") is None,
        f"served {kinds_of(after)} (was {kinds_of(out)})",
    )
    undo(scene.seeded, before)
    back = await scene.phone.serve()
    rec.negative(
        f"S2 {name} undone: the same filler card is served again",
        where,
        f"filler card {card.id}",
        (card_of(back, "filler_words") or card).id == card.id and card_of(back, "filler_words"),
        f"served {kinds_of(back)}",
    )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["item_cleanup_on", "decision_keep_original", "fingerprint_moved"])
@pytest.mark.parametrize("key", KEYS)
async def test_s3_the_held_filler_card_is_a_typed_refusal(talking_world, key, name):
    rec = Recorder(f"R5 strip S3 {key} {name}")
    where = f"{key}/talking/{name}"
    scene = build(talking_world, key, "talking")
    out = await scene.phone.serve()
    card = card_of(out, "filler_words")
    assert card is not None
    MUTATIONS[name][0](scene.seeded)
    tap = await scene.phone.tap(card, out)
    rec.negative(
        f"S3 {name}: the held card is refused typed, in words, and writes nothing",
        where,
        "refused, 'no longer fits' in the thread's language, no draft, no approval",
        tap.refused is not None
        and not tap.planned
        and tap.reply == lang_reply(key)
        and tap.new_drafts == 0
        and tap.after["approvals"] == tap.before["approvals"],
        f"signal={tap.refused} reply={tap.reply[:70]!r} rows {tap.before} -> {tap.after}",
    )
    rec.finish()


# ── S4: the protected-region (mark) gate ─────────────────────────────────────────────


APP = 35  # an app build that shows the mark card (>= protected_region_mark_min_client_protocol)
OLD_APP = 18
OBJECT = {"keep_text_off": [{"kind": "object", "label": "mug"}]}
FACE = {"keep_text_off": [{"kind": "face"}]}


def phone_talking(world, monkeypatch, key, facts=None):  # noqa: ANN001, ANN201
    """A Talking take that renders on the phone: a proxy clip, a phone-rendering account."""
    from app.kria.brief import BriefUpdate

    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    scn = threads.SCENARIOS[key]
    seeded = threads.seed_thread(scn, "talking")
    media_id = into_phone_take(seeded)
    threads.seed_cleanup_analysis(seeded, language=scn.language)
    if facts is not None:
        k.write_brief(
            seeded,
            BriefUpdate.model_validate(
                {
                    "operation": "add",
                    "kind": "style",
                    "scope": "global",
                    "description": "No text on the mug",
                    "facts": facts,
                }
            ),
        )
    return seeded, Phone(seeded, world, prefs(key), language=scn.language), media_id


def mark_the_mug(seeded: h.Seeded, media_id: str) -> None:
    append(
        seeded,
        (
            "user",
            "user_message",
            "Here is the mug",
            {
                "turn_status": "completed",
                "protected_region": {
                    "label": "mug",
                    "media_id": media_id,
                    "source_t_s": 1.0,
                    "box": [0.3, 0.55, 0.7, 0.75],
                },
            },
        ),
        ("assistant", "assistant_review", "Noted.", {"turn_value": "review"}),
    )


async def tap_as(phone: Phone, protocol: int | None, card, out):  # noqa: ANN001, ANN201
    token = bind_sending_build(protocol)
    try:
        return await phone.tap(card, out)
    finally:
        unbind_sending_build(token)


CASES = [
    # (name, facts, marked, protocol, phone take, mark question expected)
    ("object-unmarked", OBJECT, False, APP, True, True),
    ("object-marked", OBJECT, True, APP, True, False),
    ("face-only", FACE, False, APP, True, False),
    ("no-ask", None, False, APP, True, False),
    ("old-build", OBJECT, False, OLD_APP, True, False),
    ("no-build", OBJECT, False, None, True, False),
    ("cloud-take", OBJECT, False, APP, False, False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "facts", "marked", "protocol", "phone_take", "asks"),
    CASES,
    ids=[c[0] for c in CASES],
)
@pytest.mark.parametrize("key", [KEYS[0]])
async def test_s4_the_get_withholds_the_restating_cards_exactly_when_the_tap_asks_for_a_mark(
    talking_world, monkeypatch, key, name, facts, marked, protocol, phone_take, asks
):
    rec = Recorder(f"R5 strip S4 {key} {name}")
    where = f"{key}/talking/{name}"
    if phone_take:
        seeded, phone, media_id = phone_talking(talking_world, monkeypatch, key, facts)
    else:
        scene = build(talking_world, key, "talking")
        seeded, phone, media_id = scene.seeded, scene.phone, "-"
        if facts is not None:
            from app.kria.brief import BriefUpdate

            k.write_brief(
                seeded,
                BriefUpdate.model_validate(
                    {
                        "operation": "add",
                        "kind": "style",
                        "scope": "global",
                        "description": "No text on the mug",
                        "facts": facts,
                    }
                ),
            )
    if marked:
        mark_the_mug(seeded, media_id)
    talking_world.refreshes.clear()

    # the tap's own verdict, on a twin: serve it (the worker answers), tap the filler card.
    out = await phone.serve()
    out = await get_at(phone, protocol)
    for _ in range(3):
        if out.status != "pending":
            break
        await phone.run_worker()
        out = await get_at(phone, protocol)
    served = card_of(out, "filler_words")
    # What the tap says when the phone holds the card: from the pre-gate GET (no header).
    held = card_of(await phone.get(), "filler_words")
    probe = served or held
    assert probe is not None, f"the control served no filler card at all: {kinds_of(out)}"
    tap = await tap_as(phone, protocol, probe, out)
    asked = (tap.refused or {}).get("reason") == "mark_needed"
    rec.negative(
        f"S4 {name}: the tap {'asks for the mark' if asks else 'does not ask for a mark'}",
        where,
        f"mark_needed == {asks}",
        asked == asks,
        f"signal={tap.refused} reply={tap.reply[:70]!r}",
    )
    rec.negative(
        f"S4 {name}: the GET serves the filler card exactly when the tap does not ask",
        where,
        f"filler card served == {not asks}",
        (served is not None) == (not asks),
        f"served {kinds_of(out)}; tap asked mark: {asked}",
    )
    if not asks:
        rec.negative(
            f"S4 {name}: the served filler card executes",
            where,
            "planned, one revision",
            executes(tap),
            f"signal={tap.refused} reply={tap.reply[:70]!r}",
        )
    rec.finish()

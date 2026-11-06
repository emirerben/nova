"""R6 defect 1: a restating card is served only when its tap passes the draft commit.

Failure-mode list (written before the code; each row is a test below).

A filler or mute tap re-drafts the thread's plan with one more requirement and asks for the render
approval. The commit then judges the draft and refuses (writes nothing) when it is not ready. An
account on approval readiness is judged by ``assess_readiness`` on the media the PINNED RESTATE
hands it, and the serve judged only the draft check's receipts. So a card was served that every tap
refused ``requirements_not_ready``: the creator's stated length (the east-run brief's
``{kind: timing, scope: global, facts: {duration_s: 6}}``) was "evidence missing" because the
restate judged the plan on the item's bare media, without the length plan the live planner (and
the head draft's own binding) holds. Probing the same question across more asks found the same
split for captions, per-clip text and order asks, and a choice-gate refusal (``choice_needed``)
on an account that is not on readiness at all.

  D1  Serve and commit never part: for each requirement shape the fixtures contain, an account on
      and off approval readiness, both restating cards, the card is served IFF its tap executes.
      The truth is the tap of the same card with the strip switched off. A card served over a
      refusal is a dead tap; a card withheld over a tap that works is a lost offer.
  D2  The demonstrated case: a readiness account whose head draft pinned its length plan, and a
      brief with an ordinary length ask the footage can make (6 s of an 8 s take). The filler card
      is served and the tap runs. The same account whose head pinned no plan is NOT served the card
      (nothing says the plan makes 6 s), and no tap is dead.
  D3  The plan is borrowed only for the footage it was made on: a head that pinned its plan on
      other clips leaves the card out, for the same reason.
  D4  Off is off: an account NOT on readiness resolves its tap exactly as before (the bare media),
      pinned head or not, so a pinned plan changes nothing there.
  D5  The keys the live planner adds to the media are the keys the restate borrows
      (``PLAN_MEDIA_KEYS``): a new key there that the restate does not carry would silently leave
      part of the plan out of the judgment.

Every check drives the real path: GET -> ring -> ``submit_turn`` -> ``run_kria_turn`` ->
``_complete_draft_turn``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from app.kria.brief import BriefUpdate
from app.services import suggestion_serve
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.test_r5_serve_common import build, executes, keep_mute_card, kinds_of
from tests.e2e_suggestions.threads import SIGNALS, TALKING
from tests.tasks import kria_pinned_kinds_harness as k
from tests.tasks.test_kria_pinned_r5_readiness import plan_names_its_clips, readiness_on

KEY = "nermin-matcha-update"
EAST = "east-run-brief"


def ask(kind, scope, text=None, literal=None, **facts):  # noqa: ANN001, ANN003, ANN201
    return BriefUpdate(
        operation="add", kind=kind, scope=scope, description=text, literal=literal, facts=facts
    )


@dataclass(frozen=True)
class Shape:
    """One requirement shape: where it comes from in the fixtures, and what it adds."""

    scenario: str = KEY
    updates: tuple[BriefUpdate, ...] = ()
    # The plan already carries the title the creator states (test_r4 C3).
    plan_has_title: bool = False


SHAPES: dict[str, Shape] = {
    "none": Shape(),
    # The recorded production brief of the east-run thread, on the nermin take (8 s).
    "length_6s": Shape(updates=(ask("timing", "global", "6 seconds long", duration_s=6),)),
    "length_at_most_8s": Shape(
        updates=(
            ask("timing", "global", "at most 8 seconds", duration_s=8, duration_bound="at_most"),
        )
    ),
    "length_20s_too_long": Shape(updates=(ask("timing", "global", "20 seconds", duration_s=20),)),
    "title_the_plan_has": Shape(
        updates=(ask("text", "title", None, literal="Matcha restock"),), plan_has_title=True
    ),
    "title_the_plan_lacks": Shape(updates=(ask("text", "title", "a title about tea"),)),
    "volume_turkish": Shape(updates=(ask("audio", "global", "sesi biraz kıs"),)),
    "captions_off": Shape(updates=(ask("style", "global", "no captions", captions="off"),)),
    "per_clip_text": Shape(updates=(ask("text", "clip:clip-1", "say hi", literal="hi"),)),
    "order_first": Shape(updates=(ask("order", "global", "start with the harbour shot"),)),
    "east_run_recorded_brief": Shape(scenario=EAST),
}
# The shapes whose answer depends on a pinned length plan.
PINNABLE = (
    "none",
    "length_6s",
    "length_at_most_8s",
    "length_20s_too_long",
    "east_run_recorded_brief",
)
COLUMNS = ("plain", "readiness", "readiness_pinned")
KINDS = ("filler_words", "mute_clip")


def pin_the_plan(seeded, *, scenario: str = KEY, moved_footage: bool = False) -> None:  # noqa: ANN001
    """The head draft pins the plan the live planner made, as a brief-bound account's does.

    The length plan comes from the live planner's own ``_enrich_best_parts_snapshot`` over the
    item's media and the brief as it stands, and goes where the commit puts it: the draft's brief
    binding. ``moved_footage`` pins it on a clip generation the item no longer has.
    """
    from sqlalchemy import select
    from sqlalchemy.orm.attributes import flag_modified

    from app.database import sync_session
    from app.kria import planner
    from app.kria.brief import load_latest_brief_sync
    from app.kria.brief_binding import BriefBinding, snapshot_media
    from app.kria.drafts import KriaDraftDocument, canonical_snapshot
    from app.models import CreatorEditDraft, PlanItem

    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        head = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == seeded.thread_id,
                CreatorEditDraft.is_head.is_(True),
            )
        ).scalar_one()
        brief = load_latest_brief_sync(db, seeded.thread_id)
        strategy = dict(head.snapshot_json["strategy"])
        media = planner._enrich_best_parts_snapshot(strategy, snapshot_media(item), brief)
        assert media.get("length_plan"), "the live planner made no length plan for this footage"
        if moved_footage:
            media = {
                **media,
                "clip_assignments": [
                    {**row, "storage_generation": "1", "generation": "1"}
                    for row in media["clip_assignments"]
                ],
            }
        document = KriaDraftDocument(
            kind="strategy",
            intent=str(head.snapshot_json.get("intent") or "")[:200],
            edit_format=str(head.snapshot_json.get("edit_format") or strategy["edit_format"]),
            strategy=strategy,
            changes=[],
            brief_binding=BriefBinding.create(
                seeded.thread_id, brief, latest_message="", media_snapshot=media
            ),
        )
        head.snapshot_json, head.snapshot_hash = canonical_snapshot(document)
        flag_modified(head, "snapshot_json")
        db.commit()


async def scene_for(world, monkeypatch, shape: Shape, kind: str, column: str, **pin):  # noqa: ANN001, ANN003, ANN201
    """A fresh seeded thread for one row, and its phone."""
    scn = threads.SCENARIOS[shape.scenario]
    if shape.plan_has_title:
        scn = replace(scn, strategy={**scn.strategy, "opening_title": "Matcha restock"})
    scene = build(world, shape.scenario, TALKING if kind == "filler_words" else SIGNALS, scn=scn)
    if shape.updates:
        k.write_brief(scene.seeded, *shape.updates)
    if column != "plain":
        plan_names_its_clips(scene.seeded)
    if column == "readiness_pinned":
        pin_the_plan(scene.seeded, **pin)
    world.refreshes.clear()
    return scene


@dataclass
class Row:
    served: bool
    carded: bool  # the card exists once nothing is stripped
    executes: bool | None
    why: str


async def one_row(world, monkeypatch, name: str, kind: str, column: str, **pin) -> Row:  # noqa: ANN001, ANN003
    shape = SHAPES[name]
    if kind == "mute_clip":
        keep_mute_card(monkeypatch, kind)
    if column != "plain":
        readiness_on(monkeypatch)
    out = await (await scene_for(world, monkeypatch, shape, kind, column, **pin)).phone.serve()
    served = kind in kinds_of(out)

    async def nothing_stripped_but_switches(db, thread, ctx, kinds, cards=None):  # noqa: ANN001, ANN202
        return {x: "kind_off" for x in kinds if suggestion_serve._kind_off(x)}

    with monkeypatch.context() as m:
        m.setattr(suggestion_serve, "dead_card_kinds", nothing_stripped_but_switches)
        phone = (await scene_for(world, monkeypatch, shape, kind, column, **pin)).phone
        everything = await phone.serve()
        card = next((c for c in everything.suggestions if c.kind == kind), None)
        if card is None:
            return Row(served, False, None, "the set holds no such card")
        tap = await phone.tap(card, everything)
    why = (tap.refused or {}).get("reason") or tap.reply[:60]
    return Row(served, True, executes(tap), str(why))


def verdict(name: str, kind: str, column: str, row: Row) -> str | None:
    """The sentence a divergence is, or None when serve and tap agree."""
    if not row.carded:
        assert not row.served, "served a card the unfiltered set does not hold"
        return None
    if row.served and not row.executes:
        return f"DEAD CARD: {kind} served on {column} / {name}, its tap was refused ({row.why})"
    if row.executes and not row.served:
        return f"LOST OFFER: {kind} withheld on {column} / {name}, its tap would have run"
    return None


MATRIX = [
    (column, name, kind)
    for column in COLUMNS
    for name in SHAPES
    for kind in KINDS
    if column != "readiness_pinned" or name in PINNABLE
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("column", "name", "kind"), MATRIX, ids=lambda v: str(v))
async def test_d1_serve_and_tap_never_part(talking_world, monkeypatch, column, name, kind):
    row = await one_row(talking_world, monkeypatch, name, kind, column)
    assert verdict(name, kind, column, row) is None, verdict(name, kind, column, row)


@pytest.mark.asyncio
async def test_d2_a_pinned_length_plan_serves_the_card_and_the_tap_runs(talking_world, monkeypatch):
    pinned = await one_row(
        talking_world, monkeypatch, "length_6s", "filler_words", "readiness_pinned"
    )
    assert pinned.served and pinned.executes, pinned
    bare = await one_row(talking_world, monkeypatch, "length_6s", "filler_words", "readiness")
    assert not bare.served, "nothing pinned says the plan makes 6 s: the card must not be served"
    assert bare.executes is False, bare


@pytest.mark.asyncio
async def test_d3_a_plan_pinned_on_other_footage_is_not_borrowed(talking_world, monkeypatch):
    row = await one_row(
        talking_world,
        monkeypatch,
        "length_6s",
        "filler_words",
        "readiness_pinned",
        moved_footage=True,
    )
    assert not row.served and row.executes is False, row


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["none", "length_6s", "length_20s_too_long"])
async def test_d4_an_account_off_readiness_is_unchanged_by_a_pinned_plan(
    talking_world, monkeypatch, name
):
    """The same plain-account answer with the head's plan pinned or not: the pinned media is
    read for readiness accounts only."""
    plain = await one_row(talking_world, monkeypatch, name, "filler_words", "plain")

    shape = SHAPES[name]
    scene = await scene_for(talking_world, monkeypatch, shape, "filler_words", "plain")
    pin_the_plan(scene.seeded)
    pinned_out = await scene.phone.serve()
    assert ("filler_words" in kinds_of(pinned_out)) == plain.served


@pytest.mark.asyncio
async def test_d5_the_keys_the_planner_adds_are_the_keys_the_restate_borrows(
    talking_world, monkeypatch
):
    from sqlalchemy import select

    from app.database import sync_session
    from app.kria import planner
    from app.kria.brief import load_latest_brief_sync
    from app.kria.brief_binding import snapshot_media
    from app.models import CreatorEditDraft, PlanItem
    from app.services.suggestions import restate_judgment

    scene = await scene_for(
        talking_world, monkeypatch, SHAPES["length_6s"], "filler_words", "readiness"
    )
    with sync_session() as db:
        item = db.get(PlanItem, scene.seeded.item_id)
        head = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.thread_id == scene.seeded.thread_id,
                CreatorEditDraft.is_head.is_(True),
            )
        ).scalar_one()
        media = snapshot_media(item)
        enriched = planner._enrich_best_parts_snapshot(
            dict(head.snapshot_json["strategy"]),
            media,
            load_latest_brief_sync(db, scene.seeded.thread_id),
        )
    added = set(enriched) - set(media)
    assert added, "the live planner added nothing: this guard no longer sees its keys"
    assert added <= set(restate_judgment.PLAN_MEDIA_KEYS), added

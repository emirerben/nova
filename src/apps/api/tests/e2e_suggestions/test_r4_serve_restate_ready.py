"""R4-C: a restating card is served only when its tap can be approved (GAP-5).

A filler or mute tap re-drafts the thread's plan with one more requirement and the normal approval
renders it. The draft check holds a draft back when a requirement the card did not write is still
unmet by the plan ("do not replace a creator's current draft with a known partial edit"), and the
tap answers ``requirements_not_ready`` with a choice question about that unrelated requirement. On
the east-run replay the filler card was served and its tap was refused that way.

Failure catalogue (written first):

  C1  the recorded east-run brief asks for a landmark on every clip; the plan of a single talking
      take cannot carry it. The filler card must not be served, and every card that IS served
      executes. (Turkish: the recorded brief as it is.)
  C2  the same thread told in English (the recorded brief on the English twin of the footage). The
      language must not change what is served.
  C3  too much: the same footage and the same kind of brief with a requirement the plan DOES carry
      (a title the plan already has) still serves the filler card, and its tap executes. A live
      requirement alone must not take the card away.
  C4  the same footage with no brief at all serves the filler card and it executes (the east-run
      footage-only twin, in both languages).
  C5  an account on approval readiness (KRI-721). The pinned restate resolves the readiness
      manifest the way the live planner does, so the account is served the restating cards like
      any other and every card that is served executes. (Before that, every restating tap was
      refused "I haven't established which renderer can make this edit yet" and the serve held
      the cards back from those accounts whole.)

  C6  a check that cannot be made fails closed, and only for the cards it judges: a readiness read
      that raises leaves the restating card out and the editor cards in; a consent read that raises
      leaves every v2 card out and the legacy chips on the wire.

Every check drives the real path: GET -> worker -> GET -> ``submit_turn`` -> ``run_kria_turn``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.kria.brief import BriefUpdate
from app.services import suggestion_serve
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import TALKING
from tests.tasks import kria_pinned_kinds_harness as k

CASSETTES = Path(__file__).resolve().parent / "cassettes"


def prefs(key: str) -> list[dict]:
    path = CASSETTES / f"{key.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


def east_en_with_brief() -> threads.Scenario:
    """The recorded east-run brief on the English twin of the footage (only the words change)."""
    return threads.localized(
        threads.east_run(with_brief=True),
        key="east-run-brief/en",
        language="en",
        request="20K Run · Arnavutkoy to Eminonu. Make this video 20 seconds long.",
        subjects=(
            "runner crossing the bridge",
            "sunrise on the shore road",
            "ferry on the strait",
            "crowd at the finish line",
            "runner talking to the camera",
        ),
        transcript="Twenty kilometres done, we made it to Eminonu.",
        rationale="A sunset story of a twenty kilometre run.",
        profiles=(TALKING,),
    )


async def phone_for(world, scn, cassette: str, *updates):  # noqa: ANN001, ANN002, ANN202
    seeded = threads.seed_thread(scn, TALKING)
    threads.seed_cleanup_analysis(seeded, language=scn.language)
    if updates:
        k.write_brief(seeded, *updates)
    world.refreshes.clear()
    return Phone(seeded, world, prefs(cassette), language=scn.language)


def executes(tap) -> bool:  # noqa: ANN001
    return bool(tap.planned and tap.refused is None and tap.new_drafts == 1)


async def tap_each(world, scn, cassette, kinds, *updates):  # noqa: ANN001, ANN002, ANN202
    """Tap each served kind on a twin thread; ``{kind: executed?}``."""
    results: dict[str, bool] = {}
    for kind in kinds:
        phone = await phone_for(world, scn, cassette, *updates)
        out = await phone.serve()
        card = next(c for c in out.suggestions if c.kind == kind)
        results[kind] = executes(await phone.tap(card, out))
    return results


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "scenario", "cassette"),
    [
        ("tr", lambda: threads.SCENARIOS["east-run-brief"], "east-run-brief"),
        ("en", east_en_with_brief, "east-run-brief"),
    ],
    ids=["tr", "en"],
)
async def test_c1_c2_a_plan_that_cannot_carry_the_ask_serves_no_filler_card(
    talking_world, label, scenario, cassette
):
    rec = Recorder(f"negative C1/C2 east run brief {label}")
    scn = scenario()
    phone = await phone_for(talking_world, scn, cassette)
    out = await phone.serve()
    served = [c.kind for c in out.suggestions]
    rec.negative(
        f"east run, recorded brief, {label}: the filler card is not served",
        "east-run-brief",
        "filler_words absent, the other cards served",
        "filler_words" not in served and bool(served),
        f"served {served}",
    )
    results = await tap_each(talking_world, scn, cassette, served)
    rec.negative(
        f"east run, recorded brief, {label}: every served card executes",
        "east-run-brief",
        "no served card is refused",
        all(results.values()),
        f"executed {results}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_c3_a_live_requirement_the_plan_carries_keeps_the_card(talking_world):
    rec = Recorder("negative C3 filler with a requirement the plan carries")
    key = "nermin-matcha-update"
    scn = threads.SCENARIOS[key]
    scn = replace(scn, strategy={**scn.strategy, "opening_title": "Matcha restock"})
    title = BriefUpdate(
        operation="add", kind="text", scope="title", literal="Matcha restock", facts={}
    )
    phone = await phone_for(talking_world, scn, key, title)
    out = await phone.serve()
    served = [c.kind for c in out.suggestions]
    rec.negative(
        "a title requirement the plan already carries: the filler card is served",
        key,
        "filler_words served",
        "filler_words" in served,
        f"served {served}",
    )
    if "filler_words" in served:
        tap = await phone.tap(next(c for c in out.suggestions if c.kind == "filler_words"), out)
        rec.negative(
            "that filler card executes",
            key,
            "planned, one draft, approval staged",
            executes(tap) and tap.after["approvals"] == tap.before["approvals"] + 1,
            f"refused={tap.refused} reply={tap.reply[:80]!r} rows {tap.before} -> {tap.after}",
        )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["east-run-brief/footage-only", "east-run-brief/footage-only/en"])
async def test_c4_no_brief_serves_the_filler_card_and_it_executes(talking_world, key):
    rec = Recorder(f"negative C4 filler without a brief {key}")
    scn = threads.SCENARIOS[key]
    phone = await phone_for(talking_world, scn, key)
    out = await phone.serve()
    served = [c.kind for c in out.suggestions]
    rec.negative(
        f"{key}: no brief, the filler card is served",
        key,
        "filler_words served",
        "filler_words" in served,
        f"served {served}",
    )
    if "filler_words" in served:
        tap = await phone.tap(next(c for c in out.suggestions if c.kind == "filler_words"), out)
        rec.negative(
            f"{key}: that filler card executes",
            key,
            "planned, one draft, approval staged",
            executes(tap) and tap.after["approvals"] == tap.before["approvals"] + 1,
            f"refused={tap.refused} reply={tap.reply[:80]!r}",
        )
    rec.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["nermin-matcha-update", "nermin-matcha-update/tr"])
async def test_c5_an_account_on_approval_readiness_is_served_the_restating_card_and_it_runs(
    talking_world, monkeypatch, key
):
    from tests.tasks.test_kria_pinned_r5_readiness import plan_names_its_clips, readiness_on

    rec = Recorder(f"negative C5 approval readiness {key}")
    readiness_on(monkeypatch)
    scn = threads.SCENARIOS[key]

    async def phone_on_readiness():  # noqa: ANN202
        phone = await phone_for(talking_world, scn, key)
        plan_names_its_clips(phone.seeded)
        return phone

    out = await (await phone_on_readiness()).serve()
    served = [c.kind for c in out.suggestions]
    rec.negative(
        f"{key}: approval readiness on, the filler card is served with the editor cards",
        key,
        "filler_words served, and at least one editor card",
        "filler_words" in served and len(served) > 1,
        f"served {served}",
    )
    results: dict[str, bool] = {}
    for kind in served:
        phone = await phone_on_readiness()
        twin = await phone.serve()
        card = next(c for c in twin.suggestions if c.kind == kind)
        tapped = await phone.tap(card, twin)
        results[kind] = executes(tapped)
    rec.negative(
        f"{key}: approval readiness on, every served card executes",
        key,
        "no served card is refused",
        all(results.values()),
        f"executed {results}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_c6_a_check_that_cannot_be_made_leaves_out_only_the_cards_it_judges(
    talking_world, monkeypatch
):
    rec = Recorder("negative C6 a check that cannot be made")
    key = "nermin-matcha-update"
    scn = threads.SCENARIOS[key]
    phone = await phone_for(talking_world, scn, key)
    control = await phone.serve()
    before = [c.kind for c in control.suggestions]

    async def broken(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("the database went away")

    restate_held = suggestion_serve._restate_held
    monkeypatch.setattr(suggestion_serve, "_restate_held", broken)
    served = [c.kind for c in (await phone.get()).suggestions]
    rec.negative(
        "the readiness read raises: the restating card is out, the others stay",
        key,
        "filler_words absent, every other card in its place",
        "filler_words" in before and served == [x for x in before if x != "filler_words"],
        f"before {before}; served {served}",
    )
    monkeypatch.setattr(suggestion_serve, "_restate_held", restate_held)

    async def broken_gate(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("the events could not be read")

    monkeypatch.setattr(suggestion_serve, "_copy_gate_dead", broken_gate)
    out = await phone.get()
    rec.negative(
        "the consent read raises: no v2 card is served, the legacy chips are",
        key,
        "no suggestions, no digest, the same chips as the control",
        not out.suggestions
        and out.digest is None
        and [c.kind for c in out.chips] == [c.kind for c in control.chips],
        f"cards {[c.kind for c in out.suggestions]} chips {[c.kind for c in out.chips]}",
    )
    rec.finish()

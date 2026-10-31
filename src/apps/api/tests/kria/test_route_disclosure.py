"""Route disclosure: failure modes listed before coding (agents/DECISIONS.md "Route disclosure").

The Assos scorecard (`test_assos_request_to_recipe.py`) grades the happy path: the card says what
the unified montage route does not read. These pin the ways the disclosure could still go wrong:

1. A false decline: telling the creator "I can't animate" for an ask the route does read (a typed
   title animation), or for words that merely contain "animated" (a quoted title).
2. A claim about a route that is not the unified montage (cloud, talking, voiceover).
3. Overwriting a length the creator stated with the plan's length.
4. A decline that hides a checker's own verdict (the quoted words are missing from the draft).
5. A disclosure that blocks approval, or a recognised-but-undecidable ask that still does.
6. The kill switch not restoring the old behaviour exactly.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.kria import route_disclosure as rd
from app.kria.approval_readiness import assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.contracts import RequirementReceipt

REPO = Path(__file__).resolve().parents[5]


@pytest.fixture(scope="module")
def assos():
    spec = importlib.util.spec_from_file_location(
        "score_assos_for_disclosure", REPO / "scripts" / "kria" / "score_assos.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fx = module.load_fixture()
    with module._prod_flags():
        yield module, fx, module.build_job(fx), module.manifest(fx)


def _req(rid="r1", kind="text", scope="global", description="", literal=None, **facts):
    return BriefRequirement(
        id=rid, kind=kind, scope=scope, description=description, literal=literal, facts=facts
    )


def _ready(assos, req, *, enabled=True, monkeypatch):
    module, _fx, job, manifest = assos
    monkeypatch.setattr(rd.settings, "kria_route_disclosure_enabled", enabled)
    report = assess_readiness(
        CreativeBrief(version=1, requirements=[req]),
        job.strategy,
        media=job.media,
        manifest=manifest,
        creator_request="x",
        creator_id=job.user_id,
    )
    return report["ready"], report["requirements"][0]["disposition"]


def test_a_typed_title_animation_is_read_by_the_route_so_it_is_never_declined() -> None:
    typed = _req(
        scope="title", description="animated title", literal="Summer", animation="typewriter"
    )
    assert rd.ask_declines([typed], {}, route=rd.UNIFIED_MONTAGE) == []
    from_strategy = _req(scope="title", description="animated title", literal="Summer")
    assert (
        rd.ask_declines(
            [from_strategy], {"title_animation": "typewriter"}, route=rd.UNIFIED_MONTAGE
        )
        == []
    )


def test_words_that_only_contain_animated_are_not_an_animation_ask() -> None:
    quoted = _req(scope="title", description="the title", literal="Animated Cities")
    assert rd.ask_declines([quoted], {}, route=rd.UNIFIED_MONTAGE) == []


def test_an_untyped_animation_ask_outside_the_title_is_declined_with_a_typed_reason() -> None:
    labels = _req(scope="per_clip", description="animated place labels")
    (decline,) = rd.ask_declines([labels], {}, route=rd.UNIFIED_MONTAGE)
    assert (decline.code, decline.subject, decline.requirement_id) == (
        rd.NOT_APPLIED,
        "text_animation",
        "r1",
    )


@pytest.mark.parametrize("route", [None, "cloud", "talking"])
def test_nothing_is_claimed_about_a_route_that_is_not_the_unified_montage(route) -> None:
    strategy = {"intro_hook": "A hook", "story_structure": ["a", "b"], "pacing": "relaxed"}
    assert rd.strategy_notices(strategy, route=route) == []
    assert rd.ask_declines([_req(description="animated text")], strategy, route=route) == []
    assert rd.planned_card_length(strategy, {"length_plan": {}}, route=route) is None


def test_a_length_the_creator_stated_is_never_replaced_by_the_plan(assos) -> None:
    _module, _fx, job, _manifest = assos
    assert rd.planned_card_length(job.strategy, job.media, route=rd.UNIFIED_MONTAGE) == 18.005
    for stated in ({"creator_duration_s": 17.0}, {"target_duration_requested": True}):
        strategy = {**job.strategy, **stated}
        assert rd.planned_card_length(strategy, job.media, route=rd.UNIFIED_MONTAGE) is None


def test_a_checkers_own_verdict_is_not_replaced_by_the_decline() -> None:
    checked = RequirementReceipt(
        requirement_id="r1",
        status="partial",
        verification="checked",
        reason="That exact text isn't in this draft.",
    )
    labels = _req(scope="per_clip", description="animated place labels")
    (kept,) = rd.with_route_declines([checked], [labels], {}, route=rd.UNIFIED_MONTAGE)
    assert kept == checked


def test_a_declined_ask_is_approvable_and_never_asks_the_simpler_version_question(
    assos, monkeypatch
) -> None:
    from app.kria.brief_checks import needs_creator_choice

    ready, disposition = _ready(
        assos, _req(description="animated on-screen text"), monkeypatch=monkeypatch
    )
    assert (ready, disposition) == (True, "declined")
    (receipt,) = rd.with_route_declines(
        [], [_req(description="animated on-screen text")], {}, route=rd.UNIFIED_MONTAGE
    )
    assert receipt.status == "not_possible" and not needs_creator_choice(receipt)


def test_a_recognised_but_undecidable_ask_no_longer_holds_the_draft(assos, monkeypatch) -> None:
    """The inversion: "slow pace" (no checker) was approvable, "3 seconds on the beach clip" (a
    checker without the facts) was held behind a generic message. Both approve now."""
    timing = _req(kind="timing", description="show the beach clip for 3 seconds")
    assert _ready(assos, timing, enabled=False, monkeypatch=monkeypatch) == (
        False,
        "evidence_missing",
    )
    assert _ready(assos, timing, enabled=True, monkeypatch=monkeypatch) == (True, "unverified")


def test_a_researched_text_ask_keeps_its_own_preview_flow(assos, monkeypatch) -> None:
    researched = _req(description="facts about the town", research={"topic": "the town"})
    assert _ready(assos, researched, enabled=True, monkeypatch=monkeypatch) == (
        False,
        "evidence_missing",
    )


def test_the_kill_switch_restores_the_old_card_exactly(assos, monkeypatch) -> None:
    _module, _fx, job, _manifest = assos
    monkeypatch.setattr(rd.settings, "kria_route_disclosure_enabled", False)
    brief = job.brief
    assert rd.card_disclosures(brief.live(), job.strategy, route=rd.UNIFIED_MONTAGE) == []
    assert rd.ask_declines(brief.live(), job.strategy, route=rd.UNIFIED_MONTAGE) == []
    assert rd.planned_card_length(job.strategy, job.media, route=rd.UNIFIED_MONTAGE) is None
    assert _ready(
        assos, _req(description="animated on-screen text"), enabled=False, monkeypatch=monkeypatch
    ) == (
        True,
        "unverified",
    )

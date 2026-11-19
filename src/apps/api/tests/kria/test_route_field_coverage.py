"""A planner-settable strategy field cannot be dropped by a render route without anyone saying so.

KRI-1429. Main Creator can set any model-facing field of `CreativeStrategy`. Each render route
reads some of them. For the rest the route's table either lists a card line (`NotApplied` /
`RecordedOnly`) or the field is declared in the route's `UNREAD_DECLARED` with a reason. The
code scan is the consumer gate of `scripts/kria/score_route.py` (until now report-only): a field
that no function of the route's own code reads off the strategy and that its table does not list.

Both directions are enforced, so the declarations stay honest and can only shrink: a new field
nobody classified fails here, and a field the route starts to read or list must leave
`UNREAD_DECLARED`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.kria.route_capabilities import ROUTE_TABLES

REPO = Path(__file__).resolve().parents[5]
ROUTES = {
    str(getattr(route_id, "value", route_id)): module for route_id, module in ROUTE_TABLES.items()
}


@pytest.fixture(scope="module")
def scorecard():
    spec = importlib.util.spec_from_file_location(
        "score_route", REPO / "scripts" / "kria" / "score_route.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _silent(scorecard, route: str, **extra) -> list[str]:
    return scorecard.consumer_gate(route, {}, **extra)["registry_drift"]["unread_but_unlisted"]


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_every_unread_planner_field_is_listed_or_declared(scorecard, route) -> None:
    declared = set(ROUTES[route].UNREAD_DECLARED)
    silent = set(_silent(scorecard, route))
    assert not silent - declared, (
        f"{route}: the planner can set {sorted(silent - declared)} and this route neither "
        "reads it nor says so on the card. Add a NotApplied entry to FIELD_HANDLING, read the "
        "field, or declare it in UNREAD_DECLARED with the reason."
    )


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_declarations_name_only_fields_that_are_still_silent(scorecard, route) -> None:
    stale = set(ROUTES[route].UNREAD_DECLARED) - set(_silent(scorecard, route))
    assert not stale, (
        f"{route}: {sorted(stale)} is now read by the route or listed in FIELD_HANDLING; "
        "remove it from UNREAD_DECLARED."
    )


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_declarations_carry_a_reason(route) -> None:
    empty = [name for name, reason in ROUTES[route].UNREAD_DECLARED.items() if not reason.strip()]
    assert not empty, f"{route}: no reason for {empty}"


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_no_field_is_claimed_applied_without_a_reader(scorecard, route) -> None:
    claimed = scorecard.consumer_gate(route, {})["claimed_applied_unread"]
    assert not claimed, f"{route}: marked Applied but nothing in the route reads {claimed}"


def test_a_new_planner_field_would_fail_the_guard(scorecard) -> None:
    """Red control: a field no route reads or lists shows up as silent on every route."""
    for route in ROUTES:
        silent = _silent(scorecard, route, extra_fields=("zz_new_unconsumed_field",))
        assert "zz_new_unconsumed_field" in silent
        assert "zz_new_unconsumed_field" not in ROUTES[route].UNREAD_DECLARED

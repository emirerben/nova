"""KRI-847: the length-policy helpers that tell a ceiling ("under 10 seconds") from an exact
length, before any gate uses them.

The model records the direction as ``facts["duration_bound"] == "at_most"`` next to
``duration_s``; the server never reads the creator's words for it. Anything else, or no
key, is the legacy exact ten-percent target.
"""

from __future__ import annotations

import math

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.length_policy import (
    CreatorDuration,
    creator_duration,
    creator_duration_matches,
    creator_duration_s,
    creator_duration_satisfied,
    duration_bound,
    has_creator_duration_proof,
)


def _timing(req_id: str, description: str, **facts) -> BriefRequirement:
    return BriefRequirement(
        id=req_id, kind="timing", scope="global", description=description, facts=facts
    )


def _brief(*requirements: BriefRequirement) -> CreativeBrief:
    return CreativeBrief(version=1, requirements=list(requirements))


# ------------------------------------------------------------------- reading the fact


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        pytest.param({"duration_s": 10, "duration_bound": "at_most"}, "at_most", id="ceiling"),
        pytest.param({"duration_s": 10, "duration_bound": "exact"}, "exact", id="explicit-exact"),
        pytest.param({"duration_s": 10}, "exact", id="absent"),
        pytest.param({}, "exact", id="no-facts"),
        pytest.param({"duration_bound": "at_least"}, "exact", id="floor-is-not-a-ceiling"),
        pytest.param({"duration_bound": "AT_MOST "}, "exact", id="not-normalised"),
        pytest.param({"duration_bound": ""}, "exact", id="empty"),
        pytest.param({"duration_bound": None}, "exact", id="null"),
        pytest.param({"duration_bound": True}, "exact", id="bool"),
        pytest.param({"duration_bound": ["at_most"]}, "exact", id="list"),
    ],
)
def test_only_the_exact_value_at_most_is_a_ceiling(facts, expected):
    assert duration_bound(facts) == expected


# ------------------------------------------------------------------- the comparison
#
# "at_most" only removes the LOWER side of the existing +/- 10 % check: it can relax what
# exact already allowed, never tighten it, so a wrong or stale fact can never create a hard
# cap (the merged KRI-629 strict cap owns exclusive wording: "under N", "less than N").


@pytest.mark.parametrize(
    ("actual", "met"),
    [
        pytest.param(7.5, True, id="don-julio-t2"),
        pytest.param(3.0, True, id="much-shorter"),
        pytest.param(8.9, True, id="just-below-the-old-band"),
        pytest.param(10.0, True, id="exactly-at-the-number"),
        pytest.param(10.04, True, id="one-frame-over"),
        # Same upper tolerance as exact: 11.0 s is "10 s + 10 %" and stays met.
        pytest.param(11.0, True, id="upper-boundary-is-the-exact-tolerance"),
        pytest.param(11.5, False, id="over-the-exact-tolerance"),
        pytest.param(25.0, False, id="far-over"),
        pytest.param(0.0, False, id="zero"),
        pytest.param(-1.0, False, id="negative"),
        pytest.param(math.nan, False, id="nan"),
        pytest.param(math.inf, False, id="inf"),
    ],
)
def test_a_ceiling_is_met_below_the_number_and_up_to_the_exact_tolerance(actual, met):
    assert creator_duration_satisfied(actual, 10.0, "at_most") is met


@pytest.mark.parametrize("requested", [0.0, -5.0, math.nan, math.inf])
def test_a_ceiling_with_no_usable_number_is_never_met(requested):
    assert not creator_duration_satisfied(7.5, requested, "at_most")


@pytest.mark.parametrize("actual", [0.5, 3.0, 7.5, 8.9, 9.0, 10.0, 10.9, 11.0, 11.1, 12.0])
def test_a_ceiling_never_accepts_less_than_exact_does_and_differs_only_below_the_band(actual):
    """The ceiling can only relax exact: whatever exact accepts, at_most accepts too, and the
    only extra it accepts is a shorter length."""
    exact = creator_duration_satisfied(actual, 10.0)
    ceiling = creator_duration_satisfied(actual, 10.0, "at_most")

    assert ceiling or not exact
    if ceiling and not exact:
        assert actual < 10.0


@pytest.mark.parametrize("actual", [7.5, 8.9, 9.0, 10.0, 10.9, 11.0, 11.1, 12.0, 0.0, math.nan])
def test_guard_exact_is_the_unchanged_ten_percent_target(actual):
    assert creator_duration_satisfied(actual, 10.0) == creator_duration_matches(actual, 10.0)
    assert creator_duration_satisfied(actual, 10.0, "exact") == creator_duration_matches(
        actual, 10.0
    )


def test_guard_exact_still_misses_a_short_draft_and_accepts_the_band():
    assert not creator_duration_satisfied(7.5, 10.0, "exact")
    assert creator_duration_satisfied(11.0, 10.0, "exact")


def test_the_module_has_no_grace_constants():
    """The semantics need none: the upper side is the exact tolerance."""
    import app.services.length_policy as policy

    assert not hasattr(policy, "CEILING_GRACE_S")
    assert not hasattr(policy, "MEASURED_CEILING_GRACE_S")


def test_the_ceiling_miss_message_names_the_ceiling_in_plain_words():
    from app.services.length_policy import ceiling_miss_message

    assert ceiling_miss_message(10, 11.5) == (
        "You asked for at most 10 seconds; this edit is 11.5 seconds."
    )


# ----------------------------------------------------------- the brief's proven length


def test_a_brief_ceiling_is_reported_with_its_bound():
    brief = _brief(_timing("r1", "Under 10 seconds", duration_s=10, duration_bound="at_most"))

    assert creator_duration(brief=brief) == CreatorDuration(10.0, "at_most")
    assert creator_duration_s(brief=brief) == 10.0
    assert has_creator_duration_proof(brief=brief)


def test_a_brief_length_without_the_fact_is_exact():
    brief = _brief(_timing("r1", "Under 10 seconds", duration_s=10))

    assert creator_duration(brief=brief) == CreatorDuration(10.0, "exact")


def test_two_asks_for_the_same_seconds_are_a_ceiling_only_if_both_are():
    both = _brief(
        _timing("r1", "Under 10 seconds", duration_s=10, duration_bound="at_most"),
        _timing("r2", "max 10 s", duration_s=10, duration_bound="at_most"),
    )
    mixed = _brief(
        _timing("r1", "Under 10 seconds", duration_s=10, duration_bound="at_most"),
        _timing("r2", "make it 10 seconds", duration_s=10),
    )

    assert creator_duration(brief=both) == CreatorDuration(10.0, "at_most")
    assert creator_duration(brief=mixed) == CreatorDuration(10.0, "exact")


def test_two_different_lengths_stay_ambiguous_whatever_their_bounds():
    brief = _brief(
        _timing("r1", "Under 10 seconds", duration_s=10, duration_bound="at_most"),
        _timing("r2", "Under 12 seconds", duration_s=12, duration_bound="at_most"),
    )

    assert creator_duration(brief=brief) is None
    assert creator_duration_s(brief=brief) is None


def test_a_freeze_ceiling_is_not_the_videos_length():
    """KRI-777: "freeze for at most 1 second" is a moment, ceiling or not."""
    brief = _brief(
        _timing(
            "r1", "Freeze the frame for at most 1 second", duration_s=1, duration_bound="at_most"
        )
    )

    assert creator_duration(brief=brief) is None


def test_creator_text_and_choice_answers_are_exact_lengths():
    answer = {"conflict": "duration_vs_count", "duration_s": 30}

    assert creator_duration(creator_texts=["Make it 24 seconds."]) == CreatorDuration(24.0)
    assert creator_duration(authoritative_answers=[answer]) == CreatorDuration(30.0)


def test_a_brief_ceiling_does_not_leak_into_a_choice_answer():
    """An answer is a concrete chosen length: exact, even beside an older ceiling."""
    brief = _brief(_timing("r1", "Under 10 seconds", duration_s=10, duration_bound="at_most"))
    answer = {"conflict": "duration_vs_count", "duration_s": 30}

    # The brief outranks an answer when it names one length (unchanged precedence) ...
    assert creator_duration(brief=brief, authoritative_answers=[answer]).bound == "at_most"
    # ... and with no brief length the answer is exact.
    assert creator_duration(brief=_brief(), authoritative_answers=[answer]).bound == "exact"

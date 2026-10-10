"""A clip/text duration cannot be judged against the whole edit's length."""

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import PlanFacts, check_requirement, reply_from_receipts


@pytest.mark.parametrize("scope", ["per_clip", "clip:opening", "title"])
@pytest.mark.parametrize("total_duration", [1.0, 12.8])
def test_scoped_duration_stays_unverified_without_target_timing(scope, total_duration):
    requirement = BriefRequirement(
        id="r1",
        kind="timing",
        scope=scope,
        description="Keep the selected elements one second long",
        facts={"duration_s": 1.0},
    )
    receipt = check_requirement(requirement, PlanFacts(duration_s=total_duration, editor=True))
    assert receipt.status == "partial"
    assert receipt.reason == "I can't verify this timing automatically."
    reply = reply_from_receipts(CreativeBrief(requirements=[requirement]), [receipt])
    assert "Not everything" not in reply
    assert "you asked for 1s" not in reply


def test_whole_edit_duration_is_still_checked():
    requirement = BriefRequirement(
        id="r1", kind="timing", scope="global", facts={"duration_s": 1.0}
    )
    assert check_requirement(requirement, PlanFacts(duration_s=1)).status == "met"
    receipt = check_requirement(requirement, PlanFacts(duration_s=12.8))
    assert receipt.status == "partial"
    assert "12.8s; you asked for 1s" in receipt.reason

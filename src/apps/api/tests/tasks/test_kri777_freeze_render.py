"""KRI-777: an approved phone montage with a freeze ask renders, and its receipt says "Couldn't".

Café kit M2 job 417eecd4 (2026-10-21): the bound render's receipt for brief r3 "Freeze the frame
for one second on Atakan's bite" (`timing/global {"duration_s": 1, "freeze_frame": true}`) read
"This draft is about 4.7s; you asked for 1s.", a checked miss, so it was one of the reasons the
render was refused as `phone_plan_unsupported`. The freeze was a limit of the edit the creator
had already been told about; its one second was never the video's length.

Failure modes pinned here:
- the freeze still blocks the approved render (`needs_creator_choice` on its receipt);
- its receipt compares the one second against the montage's length;
- the planner takes the freeze's one second as the video's target length;
- an unbound montage records something other than the plain "Couldn't".
"""

from __future__ import annotations

import copy
import uuid

from app.kria.brief import BriefRequirement, CreativeBrief
from app.tasks import generative_build as gb
from tests.tasks.test_unified_montage_dispatch import harness  # noqa: F401 - pytest fixture

_EN = "I can't freeze a frame in this edit yet, so that moment plays normally"


def _freeze_brief() -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r4",
                kind="order",
                scope="global",
                description="in the order I filmed",
                facts={"key": "capture_time"},
            ),
            # Listed last, as a creator who adds the freeze after the rest would.
            BriefRequirement(
                id="r3",
                kind="timing",
                scope="global",
                description="Freeze the frame for one second on Atakan's bite",
                facts={"duration_s": 1, "freeze_frame": True},
            ),
        ],
    )


def _receipts(record):  # noqa: ANN001, ANN202
    return {row["requirement_id"]: row for row in record["requirement_receipts"]}


def test_a_bound_montage_with_a_freeze_ask_renders_and_records_the_couldnt(harness):  # noqa: F811
    from app.kria.brief_binding import BriefBinding

    brief = _freeze_brief()
    job, *_ = harness(brief=brief)
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")

    planned = gb._run_phone_unified_montage_job(
        str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=3
    )

    assert planned is not None and isinstance(planned["guided_edit"], dict)
    assert "request_recovery" not in job.assembly_plan
    receipts = _receipts(planned["unified_montage"])
    assert receipts["r4"]["status"] == "met"
    assert (receipts["r3"]["status"], receipts["r3"]["verification"]) == (
        "not_possible",
        "checked",
    )
    assert receipts["r3"]["reason"] == _EN
    # The planner did not aim the 14 clips at the freeze's one second.
    assert planned["unified_montage"]["duration_s"] > 5.0


def test_an_unbound_montage_records_the_same_receipt(harness):  # noqa: F811
    job, *_ = harness(brief=_freeze_brief())

    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    receipt = _receipts(job.assembly_plan["unified_montage"])["r3"]
    assert (receipt["status"], receipt["reason"]) == ("not_possible", _EN)
    assert "you asked for" not in receipt["reason"]

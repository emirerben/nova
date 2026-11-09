"""Çiğdem S2 "ss2b" (2026-11-08): "changing every 3-5 seconds" with five photos for the talk.

Each photo shows once (S57: a longer scene beats a repeat), so with five photos one holds 6 s; the
finished-video reply said "met" anyway. Through the REAL worker (approved strategy -> ready pool ->
layout -> lane receipt) and the REAL receipt builder. Failure modes:
docs/reviews/cigdem-s2-failure-modes.md C1-C7.
"""

from __future__ import annotations

import copy

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
)
from tests.tasks.test_speaker_background_on_the_word import (
    CADENCE_S,
    COLLAB_POST,
    DRONE_PHOTO,
    DRONE_VIDEO,
    ON_THE_WORD_BACKGROUND,
    SUNRISE,
    TABLET,
    _green3_job,
)

ASK = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description=(
        "Make the whole video like a green screen: me small at the bottom right in front, and "
        "behind me full screen whichever photo or video I'm talking about, changing every 3-5 "
        "seconds"
    ),
    facts={"speaker_background": True, "background_changes": True, "speaker_small": True},
)

# What the ss2b planner really picked (thread B562466E, turn 9304441a): the collab post on the
# closing line "feeds fashion", the runway video on "Pamukkale".
SS2B_PHRASES = {
    SUNRISE: "Sunrise Fashion Show",
    DRONE_VIDEO: "Pamukkale",
    TABLET: "Lumo tablets",
    DRONE_PHOTO: "drones",
    COLLAB_POST: "feeds fashion",
}


def _background(*, cadence: float | None):
    background = copy.deepcopy(ON_THE_WORD_BACKGROUND)
    background["sequence"] = [
        {"media_id": f"asset-{media}", "spoken_phrase": phrase}
        for media, phrase in SS2B_PHRASES.items()
    ]
    background["speaker_frame"] = "small_bottom_right"
    if cadence is None:
        background.pop("cadence_s", None)
    else:
        background["cadence_s"] = cadence
    return background


def _run(monkeypatch, *, cadence: float | None):
    job = _green3_job(monkeypatch, _background(cadence=cadence))
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    (receipt,) = build_receipts(
        [ASK], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return variant["phone_lane_receipt"]["background"], receipt


def test_a_photo_held_past_the_asked_rhythm_is_told_and_partial(monkeypatch):
    lane, receipt = _run(monkeypatch, cadence=CADENCE_S)

    assert lane["cadence_s"] == CADENCE_S
    assert lane["longest_s"] > CADENCE_S + 1.0, lane
    assert receipt.status == "partial"
    assert f"One stays on for {lane['longest_s']:.0f} s" in receipt.reason
    assert "you asked for a change about every 4 s" in receipt.reason
    assert "only 5 photos or videos" in receipt.reason
    assert "Add more for faster changes." in receipt.reason
    # The rest of the ask is still reported.
    assert "you're small at the bottom right" in receipt.reason


def test_no_named_rhythm_keeps_the_old_receipt(monkeypatch):
    # C1 / negative control: the 4 s default is not an ask, so nothing new is recorded or said.
    lane, receipt = _run(monkeypatch, cadence=None)

    assert "cadence_s" not in lane and "longest_s" not in lane
    assert receipt.status == "met"
    assert "One stays on" not in receipt.reason


def test_a_hold_within_the_slack_is_met():
    # C2: word alignment overshoots a little; only more than 1 s over the asked rhythm is told.
    variant = {
        "variant_id": "subtitled",
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "caption_cues": [{"text": "kervan", "start_s": 0.0, "end_s": 1.0}],
        "text_elements": [],
        "phone_lane_receipt": {
            "applied": ["background"],
            "dropped": [],
            "background": {
                "windows": 5,
                "media": 5,
                "speaker_frame": "small_bottom_right",
                "cadence_s": 4.0,
                "longest_s": 4.9,
            },
        },
    }
    (receipt,) = build_receipts(
        [ASK], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert receipt.status == "met"
    assert "One stays on" not in receipt.reason

"""KRI-1304 leftover: the draft's green-screen row matches its lead and the finished list.

Qendresa Q2 run 2 (prod thread 9db44dc2, draft at 18:29:38Z, event 021): the brief merged
"start full screen / recordings full screen / me small bottom right / zoom / the right recording
per repo / the trending page when I talk about what's hot" into ONE requirement (r3). The lead
said "I left out the trending page as it wasn't mentioned in the take", but the r3 row said
"Done: ... show trending page when I talk about what's hot", and the zoom was "Done" although
"I can't confirm it before then". The finished list (event 037) said Partly.

Ways this can fail, written before the fix:
 F1 the incident: a draft row is "Done" while the planner left part of it out (background_media
    .left_out: "not_said" or "no_recording"), so it contradicts the lead.
 F2 a draft row is "Done" for a part only the render can show (the zoom).
 F3 the honest row holds the draft: "Make it without these" would then drop the whole
    green-screen ask for one recording or the zoom.
 F4 over-reach: a real draft shortfall beside the zoom (still full size, not in the corner)
    becomes approvable through the new rule.
 F5 the finished video's verdicts change (KRI-1304: "not_said" stays a note there; KRI-1307:
    aimed / not aimed decides the zoom).
 F6 a draft with nothing left out and no zoom ask changes (still met, executable).
"""

from __future__ import annotations

import pytest

from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import plan_facts_from_phone_variant
from tests.kria.test_brief_checks_speaker_background import (
    CREATOR,
    OTHER,
    PHOTO,
    STRATEGY,
    TAKE,
    _draft_facts,
    _gate,  # noqa: F401 - autouse fixture: the prod phone switches
    _manifest,
    _open_windows,
    _receipt,
)

# r3 of the incident's brief v1, as prod stored it.
R3 = BriefRequirement(
    id="r3",
    kind="style",
    scope="global",
    description=(
        "Start full screen, then put my screen recordings full screen and me cut out small in "
        "the bottom right corner while I talk, zoom into the recordings, show the right one when "
        "I talk about each repo, show trending page when I talk about what's hot"
    ),
    facts={
        "speaker_small": True,
        "background_zoom": True,
        "background_changes": True,
        "speaker_background": True,
    },
)
NO_ZOOM = R3.model_copy(
    update={"facts": {k: v for k, v in R3.facts.items() if k != "background_zoom"}}
)
TRENDING = {"name": "trending page", "why": "not_said"}


def _background(*, left_out: list | None = None, frame: str = "small_bottom_right") -> dict:
    background = {
        "media_id": PHOTO,
        "sequence": [{"media_id": PHOTO}, {"media_id": OTHER}],
        "speaker_frame": frame,
    }
    if left_out:
        background["left_out"] = left_out
    return background


def _row(req: BriefRequirement, background: dict):  # noqa: ANN202
    report = assess_readiness(
        CreativeBrief(version=1, requirements=[req]),
        {**STRATEGY, "background_media": background},
        media={"clip_paths": [TAKE]},
        manifest=_manifest(PHOTO, OTHER),
        creator_id=CREATOR,
    )
    (row,) = report["requirements"]
    return row


@pytest.fixture(autouse=True)
def _windows(monkeypatch: pytest.MonkeyPatch) -> None:
    _open_windows(monkeypatch)


def _draft(req: BriefRequirement, background: dict):  # noqa: ANN202
    return _receipt(req, _draft_facts(_manifest(PHOTO, OTHER), background_media=background))


def test_f1_a_left_out_recording_is_never_done_and_is_named():
    receipt = _draft(NO_ZOOM, _background(left_out=[TRENDING]))
    assert receipt.status != "met"
    assert "trending page" in receipt.reason


def test_f1_a_repo_with_no_recording_is_never_done_either():
    receipt = _draft(NO_ZOOM, _background(left_out=[{"name": "Impeccable", "why": "no_recording"}]))
    assert receipt.status != "met"
    assert "Impeccable" in receipt.reason


def test_f2_the_zoom_is_not_done_before_the_render():
    receipt = _draft(R3, _background())
    assert receipt.status != "met"
    assert "can't confirm it before then" in receipt.reason


def test_the_incident_row_names_both_and_is_not_done():
    receipt = _draft(R3, _background(left_out=[TRENDING]))
    assert receipt.status == "partial"
    assert "trending page" in receipt.reason
    assert "zoom" in receipt.reason


@pytest.mark.parametrize(
    ("req", "left_out"),
    [
        (R3, [TRENDING]),
        (R3, None),
        (NO_ZOOM, [TRENDING]),
        (NO_ZOOM, [{"name": "x", "why": "no_recording"}]),
    ],
)
def test_f3_the_honest_row_still_lets_her_approve(req, left_out):
    row = _row(req, _background(left_out=left_out))
    assert row["disposition"] in APPROVABLE, row
    assert row["disposition"] == "unverified"


def test_f4_a_real_shortfall_beside_the_zoom_still_holds():
    row = _row(R3, _background(left_out=[TRENDING], frame="full"))
    assert row["disposition"] not in APPROVABLE, row


def test_f5_the_finished_video_is_unchanged():
    variant = {
        "variant_id": "subtitled",
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "edit_format": "subtitled",
        "caption_cues": [{"text": "Five repos.", "start_s": 0.0, "end_s": 1.6}],
        "text_elements": [],
        "phone_lane_receipt": {
            "applied": ["background"],
            "dropped": [],
            "background": {
                "windows": 4,
                "media": 4,
                "speaker_frame": "small_bottom_right",
                "aimed": True,
                "left_out": [TRENDING],
            },
        },
    }
    receipt = _receipt(R3, plan_facts_from_phone_variant(variant))
    assert receipt.status == "met"
    assert "you don't talk about “trending page”" in receipt.reason


def test_f6_nothing_left_out_and_no_zoom_stays_met_and_executable():
    assert _draft(NO_ZOOM, _background()).status == "met"
    assert _row(NO_ZOOM, _background())["disposition"] == "executable"

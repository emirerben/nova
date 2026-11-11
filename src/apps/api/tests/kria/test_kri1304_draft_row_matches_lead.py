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
from app.kria.brief_checks import (
    RENDER_PROMISE_REASONS,
    plan_facts_from_phone_variant,
    reply_and_lead_from_receipts,
)
from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS
from app.kria.reply_language import reply_language_for
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


# ---- follow-up: a pending zoom is a render promise (KRI-1285 kept), a left-out recording is not --
# Ways this can fail, written before the change:
#  G1 a draft whose only shortfall is the pending zoom opens with "Not everything you asked for
#     made it in" (the KRI-1285 regression this follow-up removes);
#  G2 the zoom-only row's reason is not exactly a promise (plan sentences around it), so neither
#     the reply label nor readiness reads it as planned;
#  G3 a left-out recording hides behind "Will do at render" (a real shortfall called a plan);
#  G4 with both, the zoom is phrased as an uncertain shortfall, or the left-out part is dropped;
#  G5 a real shortfall beside the zoom (still full size) turns into a promise;
#  G6 the Turkish draft does not read the zoom promise as planned.

SUMMARY = "Here is your green screen edit."
ZOOM_PLAN_WORDS = "I'll zoom onto the text when the video is made"


def _reply(req: BriefRequirement, background: dict) -> tuple[str, str]:
    receipt = _draft(req, background)
    return reply_and_lead_from_receipts(
        CreativeBrief(version=1, requirements=[req]), [receipt], summary=SUMMARY
    )


def test_g1_g2_zoom_only_is_a_render_promise_and_keeps_the_planned_lead():
    receipt = _draft(R3, _background())
    assert receipt.reason in RENDER_PROMISE_REASONS
    assert ZOOM_PLAN_WORDS in receipt.reason
    text, lead = _reply(R3, _background())
    assert text.startswith(SUMMARY)
    assert "Not everything you asked for made it in" not in text
    assert f"Will do at render: {R3.text()}" in text
    assert lead == ""
    assert _row(R3, _background())["disposition"] == "unverified"


def test_g3_g4_the_incident_shape_is_partly_naming_the_trending_page_with_the_zoom_planned():
    receipt = _draft(R3, _background(left_out=[TRENDING]))
    assert receipt.status == "partial"
    assert receipt.reason not in RENDER_PROMISE_REASONS
    assert "trending page" in receipt.reason
    assert ZOOM_PLAN_WORDS in receipt.reason
    text, lead = _reply(R3, _background(left_out=[TRENDING]))
    assert text.startswith("Not everything you asked for made it in:")
    assert f"Partly: {R3.text()}" in text
    assert lead
    assert _row(R3, _background(left_out=[TRENDING]))["disposition"] == "unverified"


def test_g3_a_left_out_recording_alone_is_partly_not_planned():
    text, _lead = _reply(NO_ZOOM, _background(left_out=[TRENDING]))
    assert f"Partly: {NO_ZOOM.text()}" in text
    assert "Will do at render" not in text


def test_g5_a_real_shortfall_beside_the_zoom_is_no_promise():
    receipt = _draft(R3, _background(frame="full"))
    assert receipt.reason not in RENDER_PROMISE_REASONS
    assert _row(R3, _background(frame="full"))["disposition"] not in APPROVABLE


def test_g6_the_turkish_zoom_promise_is_planned_too():
    with reply_language_for("tr"):
        receipt = _draft(R3, _background())
        text, _lead = reply_and_lead_from_receipts(
            CreativeBrief(version=1, requirements=[R3]), [receipt], summary=SUMMARY
        )
    assert "Videoda yapılacak" in text
    assert "İstediklerinin hepsi videoya giremedi" not in text


# ---- KRI-1307 after an editor Save: her 1.0x recordings aimed at their text are zoomed --------
# Q2 run 2: the measured zoom floors at the cover fit, so a 16:9 recording filling the 9:16 frame
# is zoom 1.0 aimed off-centre (x ≈ 0.26) at its text column. The render's own flag reads that as
# zoomed (`portable_speaker_background.focus_zooms_onto_text`, e39c13ec0); the receipt read off
# the editor document must agree. Ways this fails: the document check still wants zoom > 1.0 (her
# aimed recordings "not zoomed"); a centred 1.0x plain crop is called zoomed; one un-aimed
# picture among aimed ones is missed.


def _saved(*focuses: dict) -> dict:
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
            "background": {"windows": 2, "media": 2, "speaker_frame": "small_bottom_right"},
        },
        "speaker_background": {
            "media_id": "m1",
            "speaker_frame": dict(SPEAKER_FRAME_PRESETS["small_bottom_right"]),
            "focus": focuses[0],
            "windows": [{"media_id": f"m{i}", "focus": f} for i, f in enumerate(focuses, 1)],
        },
    }
    return variant


AIMED = {"x": 0.26, "y": 0.5, "zoom": 1.0}
CENTRED = {"x": 0.5, "y": 0.5, "zoom": 1.0}


def test_her_aimed_1x_recordings_read_as_zoomed_after_a_save():
    receipt = _receipt(R3, plan_facts_from_phone_variant(_saved(AIMED, AIMED)))
    assert receipt.status == "met", receipt
    assert "zoomed in on their text" in receipt.reason


def test_a_centred_1x_crop_is_not_zoomed():
    receipt = _receipt(R3, plan_facts_from_phone_variant(_saved(CENTRED, CENTRED)))
    assert receipt.status == "partial"
    assert "small text may stay small" in receipt.reason


def test_one_centred_recording_among_aimed_ones_is_not_zoomed():
    receipt = _receipt(R3, plan_facts_from_phone_variant(_saved(AIMED, CENTRED)))
    assert receipt.status == "partial"

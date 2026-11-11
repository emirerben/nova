"""Green 3 (KRI-1177 / KRI-1164 / count) through the real phone Talking writer.

Prod run 2026-11-03, chat "Green 3", thread 5A650474, plan item 57d612ee, job 9bad2d75
(TestFlight 1876, Fly 72c060c0b). The creator asked, in one message:

    ... me small at the bottom right in front, and behind me full screen whichever photo or
    video I'm talking about, changing every 3-5 seconds. ... Captions small, white, lowercase,
    in the middle. No music.

The first draft's captions came out right-aligned beside the corner speaker (x 0.78, y 0.655),
62 px, grey #767676, in sentence case: the caption parser took "bottom right" from the speaker
clause, missed "white", had no "middle", and no size or case existed anywhere. The finished
video's review said "8 photos or videos take turns behind you" for 8 windows over 5 photos.

This runs the real writer (`_run_generative_job`) on the Cafe T2 harness take with the Green 3
prompt and brief pinned on the job, stamped by the same `stamp_phone_text_style` the dispatch
calls, then judges the finished variant the way the render-ready review does. Failure modes
F7-F14, F17, F20, K1, N6 of `.dev/failure-modes.md`. No model calls: font selection is skipped
(`selection_calls`), caption correction is off, faces are not sampled (harness `_offline`).
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.services import phone_text_styling as styling
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    BITE,
    COTOPAXI,
    OBLEA,
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
)
from tests.tasks.test_speaker_background_strategy_worker import _windows_job

GREEN3_PROMPT = (
    "Yorumla episode 9: Kervan x Lumo Pamukkale show. Make the whole video like a green screen: "
    "me small at the bottom right in front, and behind me full screen whichever photo or video "
    "I'm talking about, changing every 3-5 seconds. Cut the pauses and the sentence I restarted. "
    "Captions small, white, lowercase, in the middle. No music."
)

R1 = BriefRequirement(
    id="r1",
    kind="style",
    scope="global",
    description=(
        "Make the whole video like a green screen: me small at the bottom right in front, and "
        "behind me full screen whichever photo or video I'm talking about, changing every 3-5 "
        "seconds"
    ),
    facts={"speaker_small": True, "background_changes": True, "speaker_background": True},
)
# r3 as the brief writer records it once taught the typed caption look (this PR's prompt).
R3 = BriefRequirement(
    id="r3",
    kind="style",
    scope="global",
    description="Captions small, white, lowercase, in the middle.",
    facts={
        "captions": "on",
        "caption_size": "small",
        "caption_color": "#FFFFFF",
        "caption_case": "lower",
        "caption_position": "middle",
    },
)
R4 = BriefRequirement(
    id="r4",
    kind="audio",
    scope="global",
    description="No music.",
    facts={"audio_intent": "no_music"},
)
SEQUENCE = [f"asset-{COTOPAXI}", f"asset-{OBLEA}", f"asset-{BITE}"]


def _green3_job(monkeypatch, requirements, *, request=GREEN3_PROMPT, stamp=True, faces=()):
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)

    # No source proxy to sample faces / contrast from (the harness take is not on disk).
    # `faces` stands in for the sampled face regions (prod Green 3 sampled five).
    def environment(canvas):
        styling._POLICY.get()["_face_boxes"] = [dict(face) for face in faces]

    monkeypatch.setattr(styling, "_ensure_generation_environment", environment)
    job = _windows_job(monkeypatch, SEQUENCE, cadence_s=4.0, speaker_frame="small_bottom_right")
    job.all_candidates["creator_request"] = request
    brief = CreativeBrief(version=1, requirements=list(requirements)) if requirements else None
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")
    if stamp:
        stamp_phone_text_style(job)
        # No font model call: the harness has no key, and prod fell back the same way.
        job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2
    return job


def _caption_layers(job):
    return [layer for layer in _recipe(job).text_layers if layer.id.startswith("caption-")]


def _layer_text(layer) -> str:
    return " ".join(run.text for run in layer.runs)


# ---------------------------------------------------------------- the stamp reads the brief


def test_the_stamp_reads_the_typed_look_and_never_the_speakers_corner(monkeypatch):
    job = _green3_job(monkeypatch, [R1, R3, R4])
    policy = job.assembly_plan[PHONE_TEXT_STYLE_FIELD]
    # On origin/main: {"caption": {"position": "bottom_right"}} (the speaker's corner).
    assert policy["requested"] == {"caption": {"position": "middle", "color": "#FFFFFF"}}
    assert policy["caption_ask"] == {
        "size": "small",
        "position": "middle",
        "color": "#FFFFFF",
        "case": "lower",
    }


def test_a_superseded_caption_requirement_is_not_applied(monkeypatch):
    superseded = R3.model_copy(update={"status": "superseded"})
    job = _green3_job(monkeypatch, [R1, superseded], request="Make it like a green screen.")
    assert "caption_ask" not in job.assembly_plan[PHONE_TEXT_STYLE_FIELD]


# ------------------------------------------------------------- the first draft carries it


def test_green3_first_draft_draws_the_captions_the_creator_asked_for(monkeypatch):
    job = _green3_job(monkeypatch, [R1, R3, R4])
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    variant = job.assembly_plan["variants"][0]
    # Prod: 62 px, #767676, right-aligned x 0.78 / y 0.655, margin 662.
    assert variant["caption_size_px"] == 42  # "small" (Çiğdem S2: 52 drew 68 px lines)
    assert variant["caption_text_color"] == "#FFFFFF"
    style = variant["caption_editor_style"]
    assert style["alignment"] == "center"
    assert style["position_x_frac"] == pytest.approx(0.5, abs=1e-6)
    assert style["position_y_frac"] == pytest.approx(0.5, abs=1e-6)
    assert variant["caption_margin_v"] == 960
    # Lowercase is the cue text itself, so the editor shows it and a Save keeps it.
    cues = variant["caption_cues"]
    assert cues
    for cue in cues:
        assert cue["text"] == cue["text"].lower(), cue["text"]
        for word in cue.get("words") or []:
            assert word["text"] == word["text"].lower(), word
    layers = _caption_layers(job)
    assert layers
    assert all(_layer_text(layer) == _layer_text(layer).lower() for layer in layers)
    assert variant["caption_look_receipt"] == {
        "size": "small",
        "size_px": 42,
        "color": "#FFFFFF",
        "position": "middle",
        "case": "lower",
        "position_x_frac": 0.5,
        "position_y_frac": 0.5,
        "over_face": False,
    }


def test_the_take_really_has_capitals_to_lowercase(monkeypatch):
    """Control for the lowercase assertions: without the ask the same take keeps its capitals."""
    job = _green3_job(monkeypatch, [R1, R4], request="Make it like a green screen. No music.")
    gb._run_generative_job(str(job.id))
    cues = job.assembly_plan["variants"][0]["caption_cues"]
    assert any(cue["text"] != cue["text"].lower() for cue in cues)


def test_green3_review_says_what_was_done(monkeypatch):
    job = _green3_job(monkeypatch, [R1, R3, R4])
    gb._run_generative_job(str(job.id))
    variant = job.assembly_plan["variants"][0]

    # Count (K1): distinct photos / videos. Since S57 (2026-11-05) each one has exactly one
    # window (it used to be more windows than photos: the tail took turns again).
    background = variant["phone_lane_receipt"]["background"]
    assert background["media"] == 3
    assert background["windows"] == background["media"]

    receipts = {
        r.requirement_id: r
        for r in build_receipts(
            [R1, R3, R4], plan_facts_from_phone_variant(variant), include_unchecked=True
        )
    }
    # Çiğdem S2: three photos cannot change "every 3-5 seconds" over a 30 s talk without a
    # repeat, so the reply says so instead of "met".
    assert receipts["r1"].status == "partial"
    assert receipts["r1"].reason.startswith("3 photos or videos take turns behind you")
    assert "you asked for a change about every 4 s" in receipts["r1"].reason
    # Prod: "I can't check the captions on this draft yet." / KRI-1164's Talking refusal.
    assert receipts["r3"].status == "met", receipts["r3"]
    assert receipts["r3"].reason == "Captions are small, white, lowercase and in the middle."
    assert receipts["r4"].status == "met", receipts["r4"]
    assert receipts["r4"].reason == "No music, as asked."


def test_an_asked_spot_on_the_corner_speaker_is_kept_and_said(monkeypatch):
    """F14: the creator's position wins over the default even on top of the corner speaker."""
    corner = R3.model_copy(
        update={
            "description": "Captions at the bottom right.",
            "facts": {"captions": "on", "caption_position": "bottom_right"},
        }
    )
    job = _green3_job(monkeypatch, [R1, corner], request="Captions at the bottom right.")
    gb._run_generative_job(str(job.id))
    variant = job.assembly_plan["variants"][0]

    assert variant["caption_editor_style"]["alignment"] == "right"
    (receipt,) = build_receipts(
        [corner], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert receipt.status == "met", receipt
    assert "at the bottom right" in receipt.reason
    assert "over you in the corner" in receipt.reason


def test_an_asked_spot_over_a_face_is_kept_and_said(monkeypatch):
    """F14 with faces sampled, as prod always does: the spot stays and the review says why."""
    face = {"start_s": 0.0, "end_s": 30.0, "box": (0.30, 0.40, 0.70, 0.62), "kind": "face"}
    job = _green3_job(monkeypatch, [R1, R3, R4], faces=[face])
    gb._run_generative_job(str(job.id))
    variant = job.assembly_plan["variants"][0]

    style = variant["caption_editor_style"]
    assert (style["alignment"], style["position_y_frac"]) == ("center", pytest.approx(0.5))
    assert variant["caption_size_px"] == 42  # "small" (Çiğdem S2: 52 drew 68 px lines)
    assert variant["caption_look_receipt"]["over_face"] is True
    (receipt,) = build_receipts(
        [R3], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    assert receipt.status == "met", receipt
    assert receipt.reason == (
        "Captions are small, white, lowercase and in the middle; "
        "they cover a face in places, as you asked."
    )


# ------------------------------------------------------------ no ask: nothing else changes

# The caption fields of the same job with no caption ask, pinned from origin/main a2acb8481.
_NO_ASK_GOLDEN: dict = {
    "caption_editor_style": {
        "alignment": "left",
        "max_width_frac": 0.68,
        "position_x_frac": 0.10254629866988571,
        "position_y_frac": 0.64349,
    },
    "caption_margin_v": 684,
    "caption_shadow_enabled": False,
    "caption_size_px": 78,
    "caption_stroke_width": 0,
    "caption_text_color": "#FFFFFF",
    "phone_text_style": {
        "preferences": {"all_text": {}},
        "requested": {},
        "selection_calls": 2,
        "version": 1,
    },
    "voiceover_caption_font": "TikTok Sans Bold",
}


def test_without_a_caption_ask_the_first_draft_is_byte_identical(monkeypatch):
    job = _green3_job(monkeypatch, [R1], request="Make it like a green screen.")
    gb._run_generative_job(str(job.id))
    variant = job.assembly_plan["variants"][0]
    fields = {
        key: variant.get(key)
        for key in (
            "caption_size_px",
            "caption_text_color",
            "caption_margin_v",
            "caption_editor_style",
            "voiceover_caption_font",
            "caption_stroke_width",
            "caption_shadow_enabled",
            "phone_text_style",
        )
    }
    # The measured x is font-raster dependent (macOS 0.102546 vs CI Linux 0.102467): every
    # other field is exact, the fractions within a pixel.
    style, golden_style = fields.pop("caption_editor_style"), _NO_ASK_GOLDEN["caption_editor_style"]
    assert fields == {k: v for k, v in _NO_ASK_GOLDEN.items() if k != "caption_editor_style"}
    assert style.keys() == golden_style.keys()
    assert {k: v for k, v in style.items() if not k.endswith("_frac")} == {
        k: v for k, v in golden_style.items() if not k.endswith("_frac")
    }
    for key in ("position_x_frac", "position_y_frac", "max_width_frac"):
        assert style[key] == pytest.approx(golden_style[key], abs=1 / 1080), key
    assert "caption_look_receipt" not in variant

"""KRI-1027: a montage asked to be vertical crops its sideways clips without cutting faces,
and the finished video's receipt says what happened.

Uses the M1 "maximo 3" plan and the face sampler replayed from the real clips
(`tests/fixtures/kri1027_m1_vertical_fill.json`: OpenCV Haar on 568x320 proxies of the
casa rosada pan, the 1 s square and the bus selfie). Failure modes covered (written before
the code): no face -> plain crop; a recurring face -> the crop slides to it; a face no single
crop can hold -> that clip alone keeps bars; a sampler failure or missing proxy -> plain crop,
never a failed render; "bars" renders sample nothing; asked-for-nothing jobs are untouched;
the receipt never claims "done" without a render receipt.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement, plan_facts_from_phone_variant
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.phone_guided_text_placement import face_box_to_canvas, shown_crop
from app.pipeline.phone_montage_framing import (
    LANDSCAPE_FRAMING_FIELD,
    apply_montage_framing,
    frame_montage,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.phone_sources import PhoneSourceBinding

FIXTURE = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "kri1027_m1_vertical_fill.json"
    ).read_text()
)
BUS = "analysis-proxy-ios-0E376590-356E-4229-97EB-59D03ADBA72D.mp4"
CASA = "analysis-proxy-ios-AB2850CF-EE2D-40F8-A1D6-6874E13740BC.mp4"
SQUARE = "analysis-proxy-ios-2B3DABCB-AEE9-4BF1-87C9-DF2CE6D1A521.mp4"
_BAR_SCALE = 1080 / 1920 * 1080 / 1920


def _plan() -> GuidedStoryExecutionPlan:
    return GuidedStoryExecutionPlan.model_validate(FIXTURE["guided_story_execution_plan"])


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    by_media = {m["media_id"]: m for m in FIXTURE["guided_story_execution_plan"]["story_timeline"]}
    keys = ("sha256", "byte_count", "duration_s", "width", "height", "has_audio")
    return tuple(
        PhoneSourceBinding(
            media_id=media_id,
            proxy_path=by_media[media_id]["gcs_path"],
            generation=by_media[media_id]["generation"],
            original=OriginalMediaDescriptor(
                **{key: original[key] for key in keys},
                orientation_degrees=original["orientation_degrees"],
            ),
        )
        for media_id, original in FIXTURE["clips"].items()
    )


def _replay_sampler():
    """`sample_face_regions` replayed per proxy (the download target names its media)."""
    calls = FIXTURE["face_sampler_replay"]
    seen: list[str] = []

    def sample(path, anchors, **_kwargs):
        media_id = pathlib.Path(path).read_text()
        seen.append(media_id)
        (recorded,) = calls[media_id]
        assert [round(a, 3) for a in anchors] == [round(a, 3) for a in recorded["anchors"]]
        regions = [
            ProtectedRegion(r["start_s"], r["end_s"], NormalizedBox(*r["box"]), r["kind"])
            for r in recorded["regions"]
        ]
        return regions, dict(recorded["receipt"])

    sample.seen = seen  # type: ignore[attr-defined]
    return sample


def _download(source: str, target: str) -> None:
    # The "proxy" is a marker file naming its media, which the replay sampler reads back.
    media_id = source.rsplit("/", 1)[-1]
    pathlib.Path(target).write_text(media_id)


def _frame(tmp_path, *, landscape_fit="fill", ask="fill", sample=None, download=_download):
    return frame_montage(
        _plan(),
        _bindings(),
        ask=ask,
        asked_by=("r2",),
        landscape_fit=landscape_fit,
        workdir=str(tmp_path),
        download=download,
        sample=sample or _replay_sampler(),
    )


def _story(plan: GuidedStoryExecutionPlan, landscape_fit: str = "fill"):
    recipe = compile_phone_guided_plan(plan, _bindings(), landscape_fit=landscape_fit)
    track = next(t for t in recipe.tracks if t.id == "story")
    return {clip.source_asset_id: clip for clip in track.clips}


def test_m1_crops_every_sideways_clip_and_follows_the_selfie_face(tmp_path):
    framing = _frame(tmp_path)
    modes = {clip.media_id: (clip.mode, clip.reason) for clip in framing.clips}
    assert modes == {
        CASA: ("centre_fill", "no_face"),
        SQUARE: ("centre_fill", "no_face"),
        BUS: ("face_fill", "face_core_in_window"),
    }
    raw = apply_montage_framing(FIXTURE["guided_story_execution_plan"], framing)
    clips = _story(GuidedStoryExecutionPlan.model_validate(raw))
    assert clips[CASA].transform.scale == 1 and clips[CASA].transform.position_x == 0
    assert clips[SQUARE].transform.scale == 1 and clips[SQUARE].transform.position_x == 0
    # The bus selfie slides left so the window [0.464, 0.780] of the source shows.
    assert clips[BUS].transform.scale == 1
    assert clips[BUS].transform.position_x == pytest.approx(-416.0)
    # Every portrait clip is byte-identical to the unframed compile.
    plain = _story(_plan())
    for media_id, clip in clips.items():
        if media_id not in {CASA, SQUARE, BUS}:
            assert clip == plain[media_id]


def test_the_selfie_crop_keeps_every_sampled_face_core_on_screen(tmp_path):
    framing = _frame(tmp_path)
    bus = next(clip for clip in framing.clips if clip.media_id == BUS)
    raw = apply_montage_framing(FIXTURE["guided_story_execution_plan"], framing)
    moment = next(
        m for m in GuidedStoryExecutionPlan.model_validate(raw).story_timeline if m.media_id == BUS
    )
    crop = shown_crop(
        moment, source_width=1920, source_height=1080, canvas_width=1080, canvas_height=1920
    )
    assert crop["x"] == pytest.approx(bus.window.left, abs=1e-4)
    (recorded,) = FIXTURE["face_sampler_replay"][BUS]
    faces = [NormalizedBox(*r["box"]) for r in recorded["regions"]]
    speaker = [box for box in faces if box.right - box.left > 0.25]
    assert len(speaker) == 5
    for box in speaker:
        mapped = face_box_to_canvas(
            box,
            source_width=1920,
            source_height=1080,
            canvas_width=1080,
            canvas_height=1920,
            crop=crop,
        )
        assert mapped is not None
        # The eyes, nose and mouth (the middle 70% of the detector box) are inside.
        core_left = box.left + 0.15 * (box.right - box.left)
        core_right = box.left + 0.85 * (box.right - box.left)
        assert crop["x"] <= core_left and core_right <= crop["x"] + crop["width"]


def test_a_face_no_single_crop_can_hold_keeps_its_clip_letterboxed(tmp_path):
    def wandering(path, anchors, **_kwargs):
        boxes = [(0.05, 0.3, 0.30, 0.7), (0.70, 0.3, 0.95, 0.7)] * 3
        regions = [
            ProtectedRegion(at, at, NormalizedBox(*boxes[i % len(boxes)]), "face")
            for i, at in enumerate(anchors)
        ]
        return regions, {"decoded": len(anchors), "detected": len(regions)}

    framing = _frame(tmp_path, sample=wandering)
    assert {clip.mode for clip in framing.clips} == {"letterbox"}
    raw = apply_montage_framing(FIXTURE["guided_story_execution_plan"], framing)
    clips = _story(GuidedStoryExecutionPlan.model_validate(raw))
    for media_id in (CASA, SQUARE, BUS):
        assert clips[media_id].transform.scale == pytest.approx(_BAR_SCALE)


def test_sampler_failure_or_missing_proxy_falls_back_to_a_plain_crop(tmp_path):
    def broken(*_args, **_kwargs):
        raise RuntimeError("opencv exploded")

    def no_proxy(_source, _target):
        raise OSError("gone")

    for framing in (_frame(tmp_path, sample=broken), _frame(tmp_path, download=no_proxy)):
        assert {(c.mode, c.reason) for c in framing.clips} == {("centre_fill", "face_unconfirmed")}
        assert framing.moment_fields() == {}


def test_a_bars_render_samples_nothing_and_letterboxes_every_sideways_clip(tmp_path):
    sample = _replay_sampler()
    chose = _frame(tmp_path, landscape_fit="fit", sample=sample)
    asked = _frame(tmp_path, landscape_fit="fit", ask="fit", sample=sample)
    assert sample.seen == []
    assert {c.reason for c in chose.clips} == {"creator_chose_bars"}
    assert {c.reason for c in asked.clips} == {"asked_bars"}
    assert chose.moment_fields() == {}
    assert (
        apply_montage_framing(FIXTURE["guided_story_execution_plan"], chose)
        is (FIXTURE["guided_story_execution_plan"])
    )


def test_unframed_plans_dump_exactly_as_before():
    raw = FIXTURE["guided_story_execution_plan"]
    dumped = GuidedStoryExecutionPlan.model_validate(raw).model_dump(mode="json")
    assert all(
        "fill_position_x" not in m and "fill_letterbox" not in m for m in dumped["story_timeline"]
    )


# ── the receipt ──────────────────────────────────────────────────────────────


def _r2(description="make it all vertical", fit="fill"):
    """M1's r2 as the v103 brief section records it (`landscape_fit` fact)."""
    return BriefRequirement(
        id="r2", kind="style", scope="global", description=description, facts={"landscape_fit": fit}
    )


def _rendered(tmp_path, **kwargs) -> PlanFacts:
    framing = _frame(tmp_path, **kwargs)
    variant = {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_destination": "device",
        "landscape_fit": framing.landscape_fit,
        "text_elements": [],
        LANDSCAPE_FRAMING_FIELD: framing.receipt(),
    }
    facts = plan_facts_from_phone_variant(variant)
    return dataclasses.replace(facts, rendered_montage=True)


def test_m1_receipt_says_the_clips_were_cropped(tmp_path):
    receipt = check_requirement(_r2(), _rendered(tmp_path))
    assert receipt.status == "met"
    assert receipt.verification != "unchecked"
    assert receipt.reason == (
        "Cropped the 3 sideways clips to fill the vertical frame; "
        "the crop follows the main face on 1"
    )


def test_receipt_when_the_creator_chose_bars_on_the_confirm_screen(tmp_path):
    receipt = check_requirement(_r2(), _rendered(tmp_path, landscape_fit="fit"))
    assert receipt.status == "partial"
    assert receipt.reason == "You chose black bars, so the 3 sideways clips keep them"


def test_receipt_for_keep_the_bars(tmp_path):
    receipt = check_requirement(
        _r2("Keep the black bars", fit="fit"), _rendered(tmp_path, landscape_fit="fit", ask="fit")
    )
    assert receipt.status == "met"
    assert receipt.reason == "Kept black bars on the 3 sideways clips, so nothing is cropped"


def test_receipt_names_a_clip_kept_with_bars(tmp_path):
    def wandering(path, anchors, **_kwargs):
        media_id = pathlib.Path(path).read_text()
        if media_id != BUS:
            return [], {"decoded": len(anchors), "detected": 0}
        boxes = [(0.05, 0.3, 0.30, 0.7), (0.70, 0.3, 0.95, 0.7)]
        return [
            ProtectedRegion(at, at, NormalizedBox(*boxes[i % 2]), "face")
            for i, at in enumerate(anchors)
        ], {"decoded": len(anchors), "detected": len(anchors)}

    receipt = check_requirement(_r2(), _rendered(tmp_path, sample=wandering))
    assert receipt.status == "partial"
    assert receipt.reason.startswith(
        "Cropped 2 of 3 sideways clips to vertical; kept black bars on 1"
    )


def test_a_draft_promises_the_crop_and_a_render_without_a_receipt_never_claims_it():
    draft = check_requirement(_r2(), PlanFacts(edit_format="montage"))
    assert draft.status == "partial" and draft.verification == "unchecked"
    assert draft.reason.startswith("I'll crop the sideways clips to fill the vertical frame")
    old_render = check_requirement(_r2(), PlanFacts(editor=True, rendered_montage=True))
    assert old_render.status == "partial" and old_render.verification == "unchecked"
    assert "didn't record" in old_render.reason


def test_a_talking_edit_keeps_its_own_framing_check():
    # Talking frames its speaker from the KRI-547 words (`phone_speaker_framing`); the
    # montage's sideways-clip receipt never answers for it.
    ask = _r2("make it vertical and keep my face in frame")
    receipt = check_requirement(ask, PlanFacts(edit_format="subtitled", phone_talking=True))
    assert receipt.reason == "I'll check the framing on the finished video."

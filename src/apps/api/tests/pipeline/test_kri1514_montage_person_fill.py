"""KRI-1514: "fill the whole screen" on a sideways montage clip with no face kept the middle
third of the picture, and the hand on the wall (the shot's subject) was cropped out.

Cusco DJI T5 (2026-11-17, job 2207172d): all 8 sideways clips were framed ``centre_fill`` /
``no_face``. In T5_03 the hand sits left of centre, so the 9:16 middle window showed only
fingertips; the review still said "Cropped the 8 sideways clips to fill the vertical frame".

Fix: when the face pass finds no face, the person matte the server already ships
(RobustVideoMatting, `subject_matte.person_masks`) looks for a person or a body part. A clear,
off-centre one moves the window to it (``person_fill``, the same main-track shift the face fill
uses); anything else keeps the middle, and the receipt says which clips kept it.

`tests/fixtures/kri1514_cusco_t5_person_fill.json` holds the recorded execution plan, the clip
originals and, per played moment, the person columns the real `RvmPersonSampler` measured on
the kit clips at analysis-proxy width (its ``_source`` says how), plus the stadium and a
green-screen speaker as controls. The face pass is replayed as "no face" for every moment,
which is what production recorded.

Ways this can go wrong (written before the code):

1. No face and a clear off-centre person / hand: the crop still keeps the middle (the bug).
2. Scenery with no person: the crop moves anyway (a false move).
3. Specks: a few tiny people or matte noise (a stadium crowd) move the crop.
4. A person already in the middle: a pointless shift, so the clip is no longer byte-identical.
5. A face decision (face fill, bars for a moving face) changes, or the person pass runs on it.
6. A face that could not be confirmed (sampler failure, no proxy): the person pass moves the
   crop where an unseen face may be.
7. The person sampler fails (no model, no onnxruntime, decode error): the render fails.
8. The kill switch is off and people are still sampled.
9. A "bars" render samples people.
10. The shift leaves a black edge (not clamped to the cover overflow).
11. The compiled recipe ignores the shift.
12. The receipt still reads as if every clip followed something, or hides which kept the middle.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.config import settings
from app.kria.landscape_fit_ask import judge_landscape_fit
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import GuidedStoryExecutionPlan, _story_canvas
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.pipeline.phone_montage_framing import (
    apply_montage_framing,
    frame_montage,
)
from app.pipeline.phone_person_window import PersonColumns, person_window
from app.pipeline.phone_recipe_shared import max_cover_shift_px
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.services.phone_sources import PhoneSourceBinding

FIXTURE = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kri1514_cusco_t5_person_fill.json"
    ).read_text()
)
PLAN = FIXTURE["guided_story_execution_plan"]
HAND = "analysis-proxy-ios-44E808F7-85C0-4407-860A-BC5E2D581A2D.mp4"  # T5_03
CANVAS = _story_canvas("portrait")


def _plan() -> GuidedStoryExecutionPlan:
    return GuidedStoryExecutionPlan.model_validate(PLAN)


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    by_media = {m["media_id"]: m for m in PLAN["story_timeline"]}
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


def _download(source: str, target: str) -> None:
    pathlib.Path(target).write_text(source.rsplit("/", 1)[-1])


def _no_face(path, anchors, **_kwargs):
    """Production's face pass on these clips: every anchor decoded, no recurring face."""
    return [], {"decoded": len(anchors), "detected": 0}


def _columns(row: dict) -> PersonColumns:
    return PersonColumns(
        frames=row["frames"],
        share=row["share"],
        present=row["present"],
        columns=tuple(row["columns"]),
    )


def _person_replay(calls: list | None = None):
    """The recorded person columns per played moment (the proxy names its media)."""

    def sample(path, source_start_s, source_end_s):
        media_id = pathlib.Path(path).read_text()
        if calls is not None:
            calls.append(media_id)
        row = FIXTURE["person_columns"][media_id]
        assert (round(source_start_s, 3), round(source_end_s, 3)) == tuple(row["window"])
        return _columns(row)

    return sample


def _frame(tmp_path, *, sample=_no_face, sample_person=None, landscape_fit="fill"):
    return frame_montage(
        _plan(),
        _bindings(),
        ask="fill",
        asked_by=("r3",),
        landscape_fit=landscape_fit,
        workdir=str(tmp_path),
        download=_download,
        sample=sample,
        sample_person=sample_person or _person_replay(),
    )


def _story(plan: GuidedStoryExecutionPlan):
    recipe = compile_phone_guided_plan(plan, _bindings(), landscape_fit="fill")
    track = next(t for t in recipe.tracks if t.id == "story")
    return {clip.source_asset_id: clip for clip in track.clips}


# ── the incident ─────────────────────────────────────────────────────────────────────


def test_the_hand_shot_follows_the_hand_and_the_scenery_keeps_the_middle(tmp_path):
    framing = _frame(tmp_path)
    modes = {clip.media_id: (clip.mode, clip.reason) for clip in framing.clips}
    assert modes.pop(HAND) == ("person_fill", "person_in_window")
    assert set(modes.values()) == {("centre_fill", "no_face")}

    hand = next(clip for clip in framing.clips if clip.media_id == HAND)
    # The hand is left of centre (kit EXPECTED.md): the picture slides right, showing its left.
    assert hand.position_x is not None and hand.position_x > 0
    person = hand.receipt()["person"]
    assert person["kept"] >= person["centre_kept"] + 0.10

    clips = _story(GuidedStoryExecutionPlan.model_validate(apply_montage_framing(PLAN, framing)))
    plain = _story(_plan())
    assert clips[HAND].transform.position_x == pytest.approx(hand.position_x)
    for media_id, clip in clips.items():
        if media_id != HAND:
            assert clip == plain[media_id]  # byte-identical to today's centre crop


def test_the_window_keeps_most_of_the_recorded_hand(tmp_path):
    hand = next(c for c in _frame(tmp_path).clips if c.media_id == HAND)
    window, evidence = person_window(
        _columns(FIXTURE["person_columns"][HAND]),
        display_width=1920,
        display_height=1080,
        canvas=CANVAS,
    )
    assert window is not None
    assert window.position_x == pytest.approx(hand.position_x)
    assert window.right <= 0.5  # the window sits in the left half, where the hand is
    assert evidence["kept"] > evidence["centre_kept"]


# ── controls ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("control", ["stadium_crowd", "speaker_in_the_middle", "empty"])
def test_specks_a_centred_person_or_nobody_keep_the_middle(control):
    window, evidence = person_window(
        _columns(FIXTURE["controls"][control]),
        display_width=1920,
        display_height=1080,
        canvas=CANVAS,
    )
    assert window is None
    assert evidence["decision"] in {"no_person", "centre_holds"}


def test_the_shift_never_shows_a_black_edge():
    width = 142
    edge = PersonColumns(frames=12, share=0.2, present=1.0, columns=(1.0,) + (0.0,) * (width - 1))
    window, _ = person_window(edge, display_width=1920, display_height=1080, canvas=CANVAS)
    assert window is not None
    assert window.left == 0.0
    assert window.position_x == pytest.approx(max_cover_shift_px(1920, 1080, CANVAS), abs=0.01)


# ── face decisions, failures, switches ───────────────────────────────────────────────


def test_a_face_decision_wins_and_skips_the_person_pass(tmp_path):
    def recurring_face(path, anchors, **_kwargs):
        box = NormalizedBox(0.70, 0.30, 0.80, 0.50)
        return [ProtectedRegion(a, a, box, "face") for a in anchors], {
            "decoded": len(anchors),
            "detected": len(anchors),
        }

    calls: list = []
    framing = _frame(tmp_path, sample=recurring_face, sample_person=_person_replay(calls))
    assert {clip.mode for clip in framing.clips} == {"face_fill"}
    assert calls == []


def test_an_unconfirmed_face_never_follows_a_person(tmp_path):
    def broken(*_args, **_kwargs):
        raise RuntimeError("opencv exploded")

    calls: list = []
    framing = _frame(tmp_path, sample=broken, sample_person=_person_replay(calls))
    assert {(c.mode, c.reason) for c in framing.clips} == {("centre_fill", "face_unconfirmed")}
    assert calls == []


def test_a_person_sampler_failure_keeps_the_middle(tmp_path):
    def broken(*_args, **_kwargs):
        raise RuntimeError("no onnxruntime")

    framing = _frame(tmp_path, sample_person=broken)
    assert {(c.mode, c.reason) for c in framing.clips} == {("centre_fill", "no_face")}
    assert framing.moment_fields() == {}
    hand = next(c for c in framing.clips if c.media_id == HAND)
    assert hand.receipt()["person"]["sampling"] == "failed"


def test_the_kill_switch_samples_no_people(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "montage_person_fill_enabled", False)
    calls: list = []
    framing = _frame(tmp_path, sample_person=_person_replay(calls))
    assert calls == []
    assert {c.mode for c in framing.clips} == {"centre_fill"}


def test_a_bars_render_samples_no_people(tmp_path):
    calls: list = []
    framing = _frame(tmp_path, landscape_fit="fit", sample_person=_person_replay(calls))
    assert calls == []
    assert {c.mode for c in framing.clips} == {"letterbox"}


# ── what the creator is told ─────────────────────────────────────────────────────────


def test_the_review_names_the_followed_clip_and_the_ones_that_kept_the_middle(tmp_path):
    verdict = judge_landscape_fit(
        "fill", receipt=_frame(tmp_path).receipt(), rendered=True, variant_fit="fill"
    )
    assert verdict.status == "met" and verdict.checked
    assert verdict.reason == (
        "Cropped the 8 sideways clips to fill the vertical frame; "
        "the crop follows the person in the shot on 1; "
        "the other 7 keep the middle of the picture"
    )


def test_the_review_says_when_every_clip_kept_the_middle(tmp_path):
    framing = _frame(tmp_path, sample_person=lambda *_a: None)
    verdict = judge_landscape_fit("fill", receipt=framing.receipt(), rendered=True)
    assert verdict.reason == (
        "Cropped the 8 sideways clips to fill the vertical frame; with no face or person "
        "to follow, the crop keeps the middle of the picture"
    )


def test_the_draft_promises_faces_and_people():
    verdict = judge_landscape_fit("fill", receipt=None, rendered=False)
    assert verdict.reason == (
        "I'll crop the sideways clips to fill the vertical frame, keeping faces and people "
        "in view, and check it on the finished video"
    )

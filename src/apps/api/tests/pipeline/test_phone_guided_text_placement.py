"""KRI-140 / KRI-116: guided-story title placement for phone renders."""

import pytest

from app.pipeline.phone_guided_text_placement import (
    build_phone_face_sampler,
    face_box_to_canvas,
    place_guided_text_off_faces,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from tests.pipeline.test_guided_story import _guided_title_row
from tests.pipeline.test_phone_guided_plan import fixture

CANVAS = {"canvas_width": 1080, "canvas_height": 1920}
LANDSCAPE = {"source_width": 1920, "source_height": 1080, **CANVAS}


def _box(left, top, right, bottom):
    return NormalizedBox(left, top, right, bottom)


def test_landscape_face_is_mapped_through_the_centre_cover_fit():
    # 16:9 into 9:16 keeps the centre 31.6% of the width (x 0.342-0.658), which
    # is stretched to the full canvas width: a 0.1-wide face becomes 0.316 wide.
    mapped = face_box_to_canvas(_box(0.45, 0.1, 0.55, 0.3), **LANDSCAPE)
    assert mapped is not None
    assert mapped.left == pytest.approx(0.342, abs=0.005)
    assert mapped.right == pytest.approx(0.658, abs=0.005)
    assert (mapped.top, mapped.bottom) == (pytest.approx(0.1), pytest.approx(0.3))


def test_face_trimmed_off_by_the_cover_fit_is_dropped():
    assert face_box_to_canvas(_box(0.02, 0.1, 0.12, 0.3), **LANDSCAPE) is None


def test_crop_is_taken_before_the_cover_fit():
    # Crop the left half; a face at x 0.2-0.3 of the source sits at 0.4-0.6 of the crop.
    crop = {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0}
    mapped = face_box_to_canvas(_box(0.2, 0.2, 0.3, 0.4), crop=crop, **LANDSCAPE)
    assert mapped is not None
    assert mapped.left < 0.5 < mapped.right
    # The same face is invisible once the crop moves to the right half.
    away = {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0}
    assert face_box_to_canvas(_box(0.2, 0.2, 0.3, 0.4), crop=away, **LANDSCAPE) is None


def test_portrait_source_on_a_portrait_canvas_is_the_identity():
    mapped = face_box_to_canvas(
        _box(0.3, 0.1, 0.7, 0.3), source_width=1080, source_height=1920, **CANVAS
    )
    assert mapped == _box(0.3, 0.1, 0.7, 0.3)


def _face_sample(box):
    calls = []

    def sample(path, anchors, **kwargs):
        calls.append((path, list(anchors), kwargs))
        regions = [ProtectedRegion(at - 0.5, at + 0.5, box, kind="face") for at in anchors]
        return regions, {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}

    return sample, calls


def test_sampler_reads_the_proxy_at_the_moments_source_time():
    plan, bindings = fixture()  # moment: output 0-3 <- source 2-5
    sample, calls = _face_sample(_box(0.45, 0.1, 0.55, 0.3))
    downloads = []
    sampler = build_phone_face_sampler(
        plan,
        bindings,
        workdir="/tmp/x",
        download=lambda src, dst: downloads.append((src, dst)),
        sample=sample,
    )
    regions, receipt = sampler([0.5, 1.5], 4.0)
    assert downloads == [("user/analysis-proxy-source.mp4", "/tmp/x/proxy-0.mp4")]
    assert calls[0][1] == [2.5, 3.5]
    assert len(regions) == 2 and receipt["decoded"] == 2 and receipt["detected"] == 2
    assert all(0 <= region.box.left < region.box.right <= 1 for region in regions)


def test_anchors_outside_any_footage_moment_are_not_sampled():
    plan, bindings = fixture()
    sample, calls = _face_sample(_box(0.45, 0.1, 0.55, 0.3))
    sampler = build_phone_face_sampler(
        plan, bindings, workdir="/tmp/x", download=lambda *_: None, sample=sample
    )
    regions, receipt = sampler([3.5, 9.0], 4.0)
    assert regions == [] and calls == []
    assert receipt["decoded"] == 0


def test_a_title_over_a_face_moves_off_it():
    plan, bindings = fixture()
    plan.resolved_duration_s = 3.0
    row = _guided_title_row(end_s=3.0)
    # A face sitting in the top third of the (portrait-mapped) frame.
    sample, _calls = _face_sample(_box(0.3, 0.02, 0.7, 0.32))
    placed = place_guided_text_off_faces(
        plan, [row], bindings, job_id="job-140", download=lambda *_: None, sample=sample
    )
    assert placed[0]["y_frac"] != pytest.approx(0.16)
    assert row["y_frac"] == 0.16, "the persisted rows are never mutated in place"


def test_a_well_framed_title_keeps_its_authored_position():
    plan, bindings = fixture()
    # Short single-line title + a face nowhere near it (mirrors the cloud test).
    row = _guided_title_row(text="Barcelona", end_s=3.0)
    sample, _calls = _face_sample(_box(0.30, 0.84, 0.70, 0.95))
    placed = place_guided_text_off_faces(
        plan, [row], bindings, job_id="job-140", download=lambda *_: None, sample=sample
    )
    assert placed == [row]


def test_fails_open_when_the_proxy_cannot_be_downloaded():
    plan, bindings = fixture()
    row = _guided_title_row(end_s=3.0)

    def broken(_src, _dst):
        raise OSError("gcs down")

    sample, calls = _face_sample(_box(0.3, 0.02, 0.7, 0.32))
    placed = place_guided_text_off_faces(
        plan, [row], bindings, job_id="job-140", download=broken, sample=sample
    )
    assert placed == [row] and calls == []


def test_nothing_to_do_without_footage_or_rows():
    plan, bindings = fixture()
    sample, _ = _face_sample(_box(0.3, 0.02, 0.7, 0.32))
    row = _guided_title_row(end_s=3.0)
    assert place_guided_text_off_faces(
        plan, [row], (), job_id="j", download=lambda *_: None, sample=sample
    ) == [row]
    assert (
        place_guided_text_off_faces(
            plan, [], bindings, job_id="j", download=lambda *_: None, sample=sample
        )
        == []
    )

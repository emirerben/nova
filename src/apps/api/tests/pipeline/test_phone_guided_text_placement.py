"""KRI-140 / KRI-116: guided-story title placement for phone renders."""

import uuid

import pytest

from app.agents._schemas.text_element import TextElement
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.pipeline.generative_overlays import build_overlays_from_text_elements
from app.pipeline.guided_story import _story_canvas
from app.pipeline.object_track import ObjectTrack, TrackAnchor, object_collision
from app.pipeline.phone_guided_text_placement import (
    build_phone_face_sampler,
    face_box_to_canvas,
    place_guided_text,
    place_guided_text_off_faces,
    unplaced_guided_receipt,
)
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.pipeline.text_overlay_skia import measure_text_overlay_box
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


# ── KRI-903: "no text on her face or on the plate" on a guided-story phone render ──────────

KEEP_OFF = BriefRequirement(
    id="r8",
    kind="style",
    scope="global",
    description="No text on her face or on the plate",
    facts={"keep_text_off": [{"kind": "face"}, {"kind": "object", "label": "plate"}]},
)
# Centre of the 16:9 frame, at the height the title (y 0.16) is authored.
PLATE_IN_SOURCE = _box(0.45, 0.08, 0.55, 0.24)


def _binding(*, brief_requirements=(KEEP_OFF,), marks=None) -> dict:
    return BriefBinding.create(
        uuid.uuid4(),
        CreativeBrief(requirements=list(brief_requirements)),
        full_creator_request="No text on her face or on the plate",
        protected_regions=marks,
    ).model_dump(mode="json")


def _mark(**changes) -> dict:
    box = PLATE_IN_SOURCE
    return {
        "label": "plate",
        "media_id": "source",
        "source_t_s": 3.0,
        "box": [box.left, box.top, box.right, box.bottom],
        "generation": "123",
    } | changes


def _tracker(box, calls):
    def track(clip_path, **kw):
        calls.append({"clip_path": clip_path, **kw})
        count = int(round((kw["end_s"] - kw["start_s"]) / 0.1)) + 1
        anchors = tuple(
            TrackAnchor(round(kw["start_s"] + i * 0.1, 3), box, "tracked") for i in range(count)
        )
        return ObjectTrack(
            label=kw["label"],
            media_id=kw["media_id"],
            generation=kw["generation"],
            seed_t_s=kw["seed_t_s"],
            seed_box=kw["seed_box"],
            anchors=anchors,
            status="tracked",
            receipt={"anchors": count, "tracked": count, "lost": 0},
        )

    return track


def _looked_and_saw_no_face(path, anchors, **kwargs):
    return [], {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}


def _run(rows, *, binding, sample=_looked_and_saw_no_face, track=None, download=lambda *_: None):
    plan, bindings = fixture()
    plan.resolved_duration_s = 3.0
    calls: list = []
    placed, receipt = place_guided_text(
        plan,
        rows,
        bindings,
        job_id="job-903",
        download=download,
        sample=sample,
        raw_binding=binding,
        track=track or _tracker(PLATE_IN_SOURCE, calls),
    )
    return placed, receipt, calls


def _measured(row) -> object:
    [overlay] = build_overlays_from_text_elements(
        [TextElement.model_validate(row)], video_duration_s=3.0, independent_box_alignment=True
    )
    measured = measure_text_overlay_box(overlay, render_canvas=_story_canvas("portrait"))
    return _box(measured["left"], measured["top"], measured["right"], measured["bottom"])


def test_a_title_moves_off_the_marked_plate_where_no_face_is_in_view():
    row = _guided_title_row(end_s=3.0)
    plate_on_canvas = face_box_to_canvas(
        PLATE_IN_SOURCE, source_width=1920, source_height=1080, **CANVAS
    )
    assert object_collision(_measured(row), plate_on_canvas) > 0.05, "the fixture must collide"

    placed, receipt, calls = _run([row], binding=_binding(marks=[_mark()]))

    assert placed[0]["y_frac"] != pytest.approx(0.16)
    assert object_collision(_measured(placed[0]), plate_on_canvas) <= 0.05
    # The tracker followed the mark on the moment's proxy, over the moment's source span.
    assert calls[0]["clip_path"].endswith("proxy-0.mp4")
    assert (calls[0]["start_s"], calls[0]["end_s"]) == (2.0, 5.0)
    plate = next(t for t in receipt["targets"] if t["label"] == "plate")
    assert plate["status"] == "clear" and plate["provenance"]["media_id"] == "source"
    assert receipt["verdict"] == "clear", receipt


def test_without_a_mark_the_plate_is_never_called_clear():
    row = _guided_title_row(end_s=3.0)
    placed, receipt, calls = _run([row], binding=_binding(marks=None))

    assert calls == []
    plate = next(t for t in receipt["targets"] if t["label"] == "plate")
    assert plate["status"] == "no_selection"
    assert receipt["verdict"] == "no_selection"
    assert placed == [row], "nothing to keep off, so the authored spot stays"


def test_a_mark_on_a_clip_the_story_does_not_play_is_another_source():
    _, receipt, calls = _run(
        [_guided_title_row(end_s=3.0)], binding=_binding(marks=[_mark(media_id="elsewhere")])
    )
    assert calls == []
    assert next(t for t in receipt["targets"] if t["label"] == "plate")["status"] == "other_source"


def test_a_mark_on_older_bytes_of_the_clip_is_not_reused():
    _, receipt, calls = _run(
        [_guided_title_row(end_s=3.0)], binding=_binding(marks=[_mark(generation="1")])
    )
    assert calls == []
    assert next(t for t in receipt["targets"] if t["label"] == "plate")["status"] == "invalidated"


def test_a_proxy_that_cannot_be_read_is_unverified_never_clear():
    def broken(_src, _dst):
        raise OSError("gcs down")

    row = _guided_title_row(end_s=3.0)
    placed, receipt, calls = _run([row], binding=_binding(marks=[_mark()]), download=broken)

    assert calls == []
    assert placed == [row]
    assert receipt["verdict"] == "unverified"


def test_a_face_in_one_anchor_of_six_is_still_kept_clear_when_the_creator_named_a_face():
    row = _guided_title_row(end_s=3.0)
    top_face = _box(0.3, 0.02, 0.7, 0.32)

    def one_glimpse(path, anchors, **kwargs):
        regions = [ProtectedRegion(anchors[0] - 0.5, anchors[0] + 0.5, top_face, kind="face")]
        return regions, {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}

    asked, receipt, _ = _run([row], binding=_binding(marks=None), sample=one_glimpse)
    unasked, none_receipt, _ = _run(
        [row], binding=_binding(brief_requirements=(), marks=None), sample=one_glimpse
    )

    assert asked[0]["y_frac"] != pytest.approx(0.16)
    assert unasked == [row] and none_receipt is None, "the dominant-face rule is unchanged unasked"
    face = next(t for t in receipt["targets"] if t["kind"] == "face")
    assert face["status"] == "clear"


def test_text_the_placement_never_looked_at_keeps_the_verdict_from_being_clear():
    caption = _guided_title_row(id="guided-narration-0", text="and then the plate arrived")
    _, receipt, _ = _run([_guided_title_row(end_s=3.0), caption], binding=_binding(marks=[_mark()]))
    assert receipt["verdict"] == "unverified"
    assert "other_text" in receipt["texts"]


def test_a_render_that_never_asked_is_unchanged():
    row = _guided_title_row(end_s=3.0)
    calls: list = []
    placed, receipt, _ = _run(
        [row], binding=_binding(brief_requirements=(), marks=None), track=_tracker(None, calls)
    )
    assert receipt is None and calls == []
    assert placed == [row]
    assert unplaced_guided_receipt(_binding(brief_requirements=(), marks=None)) is None
    assert unplaced_guided_receipt(None) is None


def test_when_the_text_policy_places_the_text_the_ask_is_reported_not_forgotten():
    receipt = unplaced_guided_receipt(_binding(marks=[_mark()]))
    assert receipt["verdict"] == "unverified"
    assert {t["label"]: t["status"] for t in receipt["targets"]} == {
        "face": "unverified",
        "plate": "unverified",
    }


def test_a_face_ask_is_unverified_when_the_sampler_could_not_look():
    def broken(_src, _dst):
        raise OSError("gcs down")

    face_only = BriefRequirement(
        id="r9",
        kind="style",
        scope="global",
        description="No text on her face",
        facts={"keep_text_off": [{"kind": "face"}]},
    )
    row = _guided_title_row(end_s=3.0)
    placed, receipt, _ = _run(
        [row], binding=_binding(brief_requirements=(face_only,)), download=broken
    )
    assert placed == [row]
    assert receipt["targets"][0]["status"] == "unverified"
    assert receipt["verdict"] == "unverified"


def test_a_story_with_no_footage_to_sample_still_reports_the_ask():
    plan, _ = fixture()
    row = _guided_title_row(end_s=3.0)

    placed, receipt = place_guided_text(
        plan, [row], (), job_id="j", download=lambda *_: None, raw_binding=_binding(marks=None)
    )
    assert placed == [row] and receipt["verdict"] == "no_selection"

    placed, none = place_guided_text(plan, [row], (), job_id="j", download=lambda *_: None)
    assert placed == [row] and none is None

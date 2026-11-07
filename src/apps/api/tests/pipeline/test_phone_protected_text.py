"""KRI-903: the protected-text plan reaches every speaker clip and every caption window.

Failure modes (written before the code):

M1  Only the first speaker clip is tracked and measured: a caption over the second speaker is
    judged against the first one's plate (or none) and the receipt says verified.
M2  An extra speaker's marked object is measured with the first speaker's cut / crop.
M3  A caption line over a speaker clip with no mark reads as clear instead of other_source.
F1  Six face samples over the whole take leave most caption lines with no sample in their
    window; a head that moves between two samples is missed.
F2  Per-anchor boxes (not a band over the window) leave the space between two anchors free.
F3  A sampler that could not look (too few decoded frames) reads as "no face".
C1  A card on the speaker's clock is checked against the object where it was on the OTHER
    clock (cut vs source time).
"""

from __future__ import annotations

import pytest

from app.kria.recipes import Canvas
from app.pipeline.object_track import ObjectTrack, TrackAnchor
from app.pipeline.phone_protected_text import (
    CaptionSubject,
    ProtectedGeometry,
    ProtectedPlan,
    choose_caption_look,
    source_clock_object_regions,
)
from app.pipeline.phone_subtitled_title import caption_window_faces
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion

CANVAS = Canvas(width=1080, height=1920)
IDENTITY = {"display_width": 1080.0, "display_height": 1920.0, "scale": 1.0, "position_x": 0.0}


def _track(label: str, boxes: dict[float, NormalizedBox], *, media_id: str = "c0") -> ObjectTrack:
    anchors = tuple(TrackAnchor(at, box, "tracked") for at, box in sorted(boxes.items()))
    return ObjectTrack(
        label=label,
        media_id=media_id,
        generation="1",
        seed_t_s=0.0,
        seed_box=anchors[0].box,
        anchors=anchors,
        status="tracked",
        receipt={},
    )


def _static(label: str, box: NormalizedBox, seconds: float, *, media_id: str = "c0") -> ObjectTrack:
    return _track(
        label, {round(i * 0.1, 3): box for i in range(int(seconds * 10) + 1)}, media_id=media_id
    )


# --- C1: cards run on the speaker clip's own clock ----------------------------------------------


def test_the_marked_object_is_followed_on_the_speaker_clips_clock():
    left = NormalizedBox(0.05, 0.40, 0.25, 0.55)
    right = NormalizedBox(0.70, 0.40, 0.90, 0.55)
    boxes = {round(i * 0.1, 3): left if i * 0.1 < 2.0 else right for i in range(41)}
    plan = ProtectedPlan(
        targets=(),
        objects=(_track("plate", boxes),),
        geometry=ProtectedGeometry(keep_segments=((0.0, 4.0),), **IDENTITY),
    )

    regions = source_clock_object_regions(plan, canvas=CANVAS, duration_s=4.0)

    early = [r for r in regions if r.end_s <= 1.5]
    late = [r for r in regions if r.start_s >= 2.5]
    assert early and late
    assert all(r.box.right <= 0.26 for r in early), "before 2 s the plate is on the left"
    assert all(r.box.left >= 0.69 for r in late), "after 2 s it has moved right"
    assert {r.kind for r in regions} == {"object"}
    assert regions[0].start_s == 0.0 and regions[-1].end_s == pytest.approx(4.0)


def test_a_plan_without_a_marked_object_adds_no_card_region():
    assert (
        source_clock_object_regions(ProtectedPlan(targets=()), canvas=CANVAS, duration_s=4.0) == []
    )


# --- M1/M2/M3: captions over every speaker clip --------------------------------------------------

PLATE_BOTTOM = NormalizedBox(0.10, 0.74, 0.90, 0.90)
CUES = [
    {"text": "first speaker line", "start_s": 0.0, "end_s": 2.0},
    {"text": "second speaker line", "start_s": 4.0, "end_s": 6.0},
]


def _subject(start_s: float, end_s: float, objects=(), **kw) -> CaptionSubject:
    return CaptionSubject(
        start_s=start_s,
        end_s=end_s,
        objects=tuple(objects),
        faces=kw.get("faces", ()),
        face_windows=kw.get("face_windows", ()),
        keep_segments=((0.0, end_s - start_s),),
        **IDENTITY,
    )


def _look(subjects):
    return choose_caption_look(
        CUES,
        caption_style="sentence",
        canvas=CANVAS,
        objects=(),
        faces=(),
        keep_segments=(),
        display_width=1080.0,
        display_height=1920.0,
        subjects=subjects,
    )


def test_a_caption_over_the_second_speakers_plate_moves_off_it():
    plate = _static("plate", PLATE_BOTTOM, 4.0, media_id="c1")
    only_first = _subject(0.0, 4.0)
    second = _subject(4.0, 8.0, objects=[plate])

    look, receipt = _look([only_first, second])

    assert look is not None and receipt["status"] == "moved", receipt
    assert receipt["object_collision"] is False
    assert look.position_y_frac < 0.74 or look.position_y_frac > 0.90
    assert receipt["objects_verified"] is True


def test_the_first_speakers_plate_does_not_judge_the_second_speakers_captions():
    """The first clip's plate is at the bottom for its own 4 s only; the second clip has none."""
    plate = _static("plate", PLATE_BOTTOM, 4.0, media_id="c0")
    first = _subject(0.0, 4.0, objects=[plate])
    second = _subject(4.0, 8.0)

    look, receipt = _look([first, second])

    assert look is not None, "the first line still moves off the first speaker's plate"
    assert receipt["object_collision"] is False


def test_a_plate_lost_over_the_second_speaker_is_not_verified():
    lost = ObjectTrack(
        label="plate",
        media_id="c1",
        generation="1",
        seed_t_s=0.0,
        seed_box=PLATE_BOTTOM,
        anchors=tuple(TrackAnchor(round(i * 0.1, 3), None, "lost") for i in range(41)),
        status="unverified",
        receipt={},
    )
    _, receipt = _look([_subject(0.0, 4.0), _subject(4.0, 8.0, objects=[lost])])
    assert receipt["objects_verified"] is False


# --- F1-F3: faces sampled per caption window -----------------------------------------------------


def _faces_at(left_until: float):
    """A head on the left until ``left_until`` seconds, then on the right."""
    calls: list[list[float]] = []

    def sample(path, anchors, **kwargs):
        calls.append(list(anchors))
        regions = [
            ProtectedRegion(
                at - 0.5,
                at + 0.5,
                NormalizedBox(0.10, 0.20, 0.30, 0.35)
                if at < left_until
                else NormalizedBox(0.65, 0.20, 0.85, 0.35),
                kind="face",
            )
            for at in anchors
        ]
        return regions, {"attempted": len(anchors), "decoded": len(anchors), "timed_out": False}

    return sample, calls


def _windows(count: int, length: float) -> list[tuple[float, float]]:
    return [(round(i * length, 3), round((i + 1) * length, 3)) for i in range(count)]


def test_every_caption_window_has_a_face_sample_inside_it():
    sample, calls = _faces_at(left_until=99.0)
    windows = _windows(10, 1.2)  # 12 s of short lines: six samples over the take miss most

    caption_window_faces(
        "/tmp/c0.mp4",
        windows=windows,
        keep_segments=[(0.0, 12.0)],
        canvas=CANVAS,
        sample=sample,
        **IDENTITY,
    )

    [anchors] = calls
    for start, end in windows:
        assert any(start <= at < end for at in anchors), (start, end, anchors)


def test_a_head_that_moves_inside_a_line_is_protected_across_the_whole_line():
    sample, _ = _faces_at(left_until=1.0)  # moves from left to right within the 0-2 s line

    flat, bands, receipt = caption_window_faces(
        "/tmp/c0.mp4",
        windows=[(0.0, 2.0), (2.0, 4.0)],
        keep_segments=[(0.0, 4.0)],
        canvas=CANVAS,
        sample=sample,
        **IDENTITY,
    )

    first = next(b for b in bands if b.start_s == 0.0)
    assert first.box.left <= 0.10 + 1e-6 and first.box.right >= 0.85 - 1e-6, "the band spans both"
    second = next(b for b in bands if b.start_s == 2.0)
    assert second.box.left >= 0.65 - 1e-6, "the second line only sees the head on the right"
    assert receipt["faces"] == "detected"
    assert len(flat) >= len(bands)


def test_a_sampler_that_could_not_look_protects_the_fallback_face_not_nothing():
    def blind(path, anchors, **kwargs):
        return [], {"attempted": len(anchors), "decoded": 0, "timed_out": False}

    flat, bands, receipt = caption_window_faces(
        "/tmp/c0.mp4",
        windows=_windows(3, 2.0),
        keep_segments=[(0.0, 6.0)],
        canvas=CANVAS,
        sample=blind,
        **IDENTITY,
    )

    assert receipt["faces"] == "fallback"
    assert flat and bands == []


def test_a_very_long_take_keeps_the_sampling_budget_and_still_covers_the_take():
    sample, calls = _faces_at(left_until=999.0)

    caption_window_faces(
        "/tmp/c0.mp4",
        windows=_windows(150, 1.0),
        keep_segments=[(0.0, 150.0)],
        canvas=CANVAS,
        sample=sample,
        **IDENTITY,
    )

    [anchors] = calls
    assert len(anchors) <= 70
    assert min(anchors) < 10.0 and max(anchors) > 140.0


# --- Review round 2: what a partial sampler saw stays protected; restyles judge each speaker -----

from app.pipeline.phone_protected_text import (  # noqa: E402
    SpeakerSlice,
    keep_styled_rows_clear,
)
from app.pipeline.phone_subtitled_title import _FALLBACK_FACE_BOX  # noqa: E402

SEEN_FACE = NormalizedBox(0.02, 0.55, 0.30, 0.85)


@pytest.mark.parametrize(
    "sampled",
    [
        {"decoded": 2, "timed_out": True, "partial": True},
        {"decoded": 8, "worker_error": "boom"},
        {"decoded": 8, "timed_out": True},
    ],
    ids=["too-few-decoded", "worker-error", "timeout-no-partial"],
)
def test_an_unconfirmed_sampler_keeps_the_faces_it_saw_beside_the_fallback(sampled):
    def partial(path, anchors, **kwargs):
        seen = [ProtectedRegion(a - 0.5, a + 0.5, SEEN_FACE, "face") for a in anchors[:2]]
        return seen, sampled

    flat, _bands, receipt = caption_window_faces(
        "/tmp/c0.mp4",
        windows=[(1.0, 3.0), (4.0, 6.0)],
        keep_segments=[(0.0, 10.0)],
        canvas=CANVAS,
        sample=partial,
        **IDENTITY,
    )

    assert receipt["faces"] == "fallback"
    assert any(SEEN_FACE.coverage_by(box) > 0.99 for box in flat), "the face it saw"
    assert _FALLBACK_FACE_BOX in flat, "and the conservative box"


def _object_track(box: NormalizedBox, media_id: str, seconds: float = 10.0) -> ObjectTrack:
    return _static("plate", box, seconds, media_id=media_id)


def _two_speaker_plan(first_plate: NormalizedBox, last_plate: NormalizedBox) -> ProtectedPlan:
    from app.kria.protected_targets import ProtectedTarget

    target = ProtectedTarget("object", "plate", ("r1",))
    row = {"kind": "object", "label": "plate", "requirement_ids": ["r1"]}
    geometry = ProtectedGeometry(((0.0, 10.0),), **IDENTITY)
    last = ProtectedPlan(
        targets=(target,),
        objects=(_object_track(last_plate, "take2"),),
        target_rows=(row,),
        geometry=geometry,
    )
    return ProtectedPlan(
        targets=(target,),
        objects=(_object_track(first_plate, "take1"),),
        target_rows=(row,),
        geometry=geometry,
        speakers=(SpeakerSlice("take2", 10.0, 20.0, last),),
    )


FIRST_PLATE = NormalizedBox(0.05, 0.05, 0.25, 0.12)
LAST_PLATE = NormalizedBox(0.20, 0.30, 0.80, 0.45)


def _row(start_s: float, end_s: float, y_frac: float, size_px: int) -> dict:
    return {
        "id": "closing-1",
        "text": "Follow for part two",
        "start_s": start_s,
        "end_s": end_s,
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": y_frac,
        "size_px": size_px,
    }


def test_a_restyle_onto_the_last_speakers_plate_is_undone():
    plan = _two_speaker_plan(FIRST_PLATE, LAST_PLATE)
    placed = _row(16.0, 20.0, 0.15, 72)
    restyled = {**placed, "y_frac": 0.38, "size_px": 96}

    out, kept = keep_styled_rows_clear([placed], [restyled], plan=plan, canvas=CANVAS)

    assert kept == ["closing-1"]
    assert out[0]["y_frac"] == 0.15


def test_a_restyle_of_a_row_that_runs_across_two_speakers_is_judged_on_both():
    plan = _two_speaker_plan(FIRST_PLATE, LAST_PLATE)
    placed = _row(8.0, 12.0, 0.70, 72)
    restyled = {**placed, "y_frac": 0.38, "size_px": 96}  # onto the plate for 10-12 s

    out, kept = keep_styled_rows_clear([placed], [restyled], plan=plan, canvas=CANVAS)

    assert kept == ["closing-1"]
    assert out[0]["y_frac"] == 0.70


def test_a_restyle_onto_the_first_speakers_plate_is_undone_while_the_first_speaker_plays():
    plan = _two_speaker_plan(LAST_PLATE, FIRST_PLATE)
    placed = _row(1.0, 4.0, 0.15, 72)
    restyled = {**placed, "y_frac": 0.38, "size_px": 96}

    _out, kept = keep_styled_rows_clear([placed], [restyled], plan=plan, canvas=CANVAS)

    assert kept == ["closing-1"]


def test_the_first_speakers_plate_does_not_veto_a_restyle_while_the_last_speaker_plays():
    plan = _two_speaker_plan(LAST_PLATE, FIRST_PLATE)  # the big plate is on take 1 only
    placed = _row(16.0, 20.0, 0.60, 72)
    restyled = {**placed, "y_frac": 0.38, "size_px": 96}

    out, kept = keep_styled_rows_clear([placed], [restyled], plan=plan, canvas=CANVAS)

    assert kept == []
    assert out[0]["y_frac"] == 0.38

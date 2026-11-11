"""KRI-827 at render: Merope T1's "No text on her face or on the plate" reaches the phone
Talking writer and is kept, or the finished video says exactly why not.

Runs the real writer (`_run_phone_subtitled_job`) with Merope's real request and brief pinned
on the job (plus the creator's mark of the plate), then judges the finished variant the way
the render-ready review does. The tracker is the only stand-in (the footage here is not a
real file; the tracker itself runs for real in `tests/pipeline/test_protected_object_text_e2e`).

Failure modes guarded here (the full list is in that E2E module): F6 a swapped source reuses
the old mark; F10 the face wins and the plate loses; F11/F17 a receipt says clear when it is
not; F12 model-written wording (not the creator's) becomes a protected target; F18 a render
that never asked changes at all.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

import app.pipeline.phone_protected_text as protected_mod
import app.pipeline.phone_subtitled_title as title_mod
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.protected_targets import PROTECTED_TEXT_FIELD
from app.kria.recipes import Canvas
from app.pipeline.object_track import ObjectTrack, TrackAnchor, object_collision
from app.pipeline.phone_subtitled_title import _measure_title_box, source_box_to_canvas
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled, _words

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kri793_t1_caro"
        / "t1_brief_and_speech.json"
    ).read_text(encoding="utf-8")
)
# The stored T1 brief plus what the v77 brief section records for "No text on her face or on
# the plate" (the stored brief predates it and kept the ask only in r6's prose).
KEEP_OFF = BriefRequirement(
    id="r8",
    kind="style",
    scope="global",
    description="No text on her face or on the plate",
    facts={"keep_text_off": [{"kind": "face"}, {"kind": "object", "label": "plate"}]},
)
_STORED = CreativeBrief.model_validate(FIXTURE["brief"])
T1_BRIEF = _STORED.model_copy(update={"requirements": [*_STORED.requirements, KEEP_OFF]})
TITLE = "Colombian dessert, Ecuadorian hands"
CANVAS = Canvas(width=1080, height=1920)
FACE = NormalizedBox(0.34, 0.16, 0.66, 0.40)
# On the table under her face: exactly where the title's fallback rows (0.5, 0.44) sit.
PLATE = NormalizedBox(0.30, 0.42, 0.70, 0.64)


def _mark(**changes) -> dict:
    return {
        "label": "plate",
        "media_id": "c0",
        "source_t_s": 0.5,
        "box": [PLATE.left, PLATE.top, PLATE.right, PLATE.bottom],
        "generation": "1",
    } | changes


def _binding(*, marks: list[dict] | None, brief: CreativeBrief = T1_BRIEF):
    return BriefBinding.create(
        uuid.uuid4(),
        brief,
        full_creator_request=FIXTURE["creator_request"],
        protected_regions=marks,
    ).model_dump(mode="json")


def _job(monkeypatch, binding: dict):
    job, snapshot, _session, _source = _setup_subtitled(
        monkeypatch,
        duration_s=10.0,
        transcript_words=_words(("Hi,", 0.0, 0.4), ("I'm", 0.4, 0.6), ("Karolina", 0.6, 1.2)),
    )
    job.all_candidates["creator_strategy"] = {
        "opening_title": TITLE,
        "opening_title_duration_s": 2.0,
    }
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = binding
    return job, snapshot


def _stand_ins(monkeypatch) -> list[dict]:
    """Her face where a talk-to-camera face is, and a tracker that sees the plate where the
    creator marked it at every 0.1 s anchor. Records each tracker call."""
    calls: list[dict] = []

    def faces(_path, anchors, **_kw):
        return [ProtectedRegion(a, a, FACE, "face") for a in anchors], {"decoded": len(anchors)}

    def track(clip_path, **kw):
        calls.append({"clip_path": clip_path, **kw})
        count = int(round((kw["end_s"] - kw["start_s"]) / 0.1)) + 1
        anchors = tuple(
            TrackAnchor(round(kw["start_s"] + i * 0.1, 3), PLATE, "tracked") for i in range(count)
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

    monkeypatch.setattr(title_mod, "sample_face_regions", faces)
    monkeypatch.setattr(protected_mod, "track_object", track)
    return calls


def _run(job, snapshot) -> dict:
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def _title_row(variant: dict) -> dict:
    return next(row for row in variant["text_elements"] if row["id"] == "opening-title")


def _r8(variant: dict):
    [receipt] = build_receipts(
        [KEEP_OFF], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


def test_merope_title_stays_off_her_face_and_the_plate_and_the_review_says_so(monkeypatch):
    calls = _stand_ins(monkeypatch)
    job, snapshot = _job(monkeypatch, _binding(marks=[_mark()]))

    variant = _run(job, snapshot)

    receipt = variant[PROTECTED_TEXT_FIELD]
    assert receipt["verdict"] == "clear", receipt
    plate = next(t for t in receipt["targets"] if t["label"] == "plate")
    # The mark's provenance rides on the receipt (re-projection key for later edits).
    assert plate["provenance"]["media_id"] == "c0"
    assert plate["provenance"]["generation"] == "1"
    assert [t["requirement_ids"] for t in receipt["targets"]] == [["r8"], ["r8"]]
    # The tracker ran on the speaker clip over its whole kept span.
    assert calls and calls[0]["clip_path"] == "/tmp/c0.mp4"
    assert (calls[0]["start_s"], calls[0]["end_s"]) == (0.0, 10.0)
    # Measured independently: the stored title row is off the plate AND off the face.
    title = _title_row(variant)
    box = _measure_title_box(title, canvas=CANVAS)
    plate_canvas = source_box_to_canvas(
        PLATE, display_width=1080, display_height=1920, canvas=CANVAS
    )
    face_canvas = source_box_to_canvas(FACE, display_width=1080, display_height=1920, canvas=CANVAS)
    assert object_collision(box, plate_canvas) <= 0.05
    assert box.coverage_by(face_canvas) <= 0.05
    assert _r8(variant).status == "met"


def test_without_a_mark_the_plate_is_never_called_clear(monkeypatch):
    calls = _stand_ins(monkeypatch)
    job, snapshot = _job(monkeypatch, _binding(marks=None))

    variant = _run(job, snapshot)

    assert calls == []
    assert variant[PROTECTED_TEXT_FIELD]["verdict"] == "no_selection"
    receipt = _r8(variant)
    assert receipt.status == "partial"
    assert "Mark the plate" in (receipt.reason or "")


def test_a_mark_on_older_bytes_of_the_clip_is_not_reused(monkeypatch):
    calls = _stand_ins(monkeypatch)
    job, snapshot = _job(monkeypatch, _binding(marks=[_mark(generation="0")]))

    variant = _run(job, snapshot)

    assert calls == []
    assert variant[PROTECTED_TEXT_FIELD]["verdict"] == "invalidated"
    assert _r8(variant).status == "partial"


def test_the_creators_mark_alone_keeps_text_off_the_plate(monkeypatch):
    """Drawing the box is the ask: the stored T1 brief (no `keep_text_off` fact) plus the
    creator's mark still keeps the title off the plate."""
    calls = _stand_ins(monkeypatch)
    job, snapshot = _job(monkeypatch, _binding(marks=[_mark()], brief=_STORED))

    variant = _run(job, snapshot)

    assert calls
    assert variant[PROTECTED_TEXT_FIELD]["verdict"] == "clear"
    box = _measure_title_box(_title_row(variant), canvas=CANVAS)
    plate = source_box_to_canvas(PLATE, display_width=1080, display_height=1920, canvas=CANVAS)
    assert object_collision(box, plate) <= 0.05


def test_a_render_that_never_asked_is_unchanged(monkeypatch):
    """F18: no fact and no mark: no tracker, no new receipt, and the title placed exactly as
    before this change (the face-only path)."""
    calls = _stand_ins(monkeypatch)
    job, snapshot = _job(monkeypatch, _binding(marks=None, brief=_STORED))

    variant = _run(job, snapshot)

    assert calls == []
    assert PROTECTED_TEXT_FIELD not in variant
    placement = variant["opening_title_placement"]
    assert "objects" not in placement and "object_collision" not in placement
    # The face-only ladder's first clear row under her face, as on main.
    assert placement["status"] == "moved" and placement["chosen_y_frac"] == 0.5


# ── KRI-903: every speaker clip, every caption window, and the cards ────────────────────────────
#
# Failure modes (written before the code): the second speaker's captions read as clear because
# only the first clip was tracked (M1); the first clip's cut / crop is applied to the second
# clip's plate (M2); a speaker clip nobody marked reads as clear (M3); six face samples over a
# take leave most caption lines unsampled (F1); a Visuals card or a reaction beat lands on the
# marked object because grounding never hears of it (G1); a closing line over the last speaker
# is placed with no protection at all (C1).


from tests.tasks.test_phone_subtitled_narrated_dispatch import (  # noqa: E402
    _setup_talking_multi,
)

CAPTION_BAND_PLATE = NormalizedBox(0.10, 0.74, 0.90, 0.90)
MIDDLE_PLATE = NormalizedBox(0.30, 0.42, 0.70, 0.55)


def _box_list(box: NormalizedBox) -> list[float]:
    return [box.left, box.top, box.right, box.bottom]


def _plates_by_clip(
    monkeypatch, plates: dict[str, NormalizedBox], face_anchors: list | None = None
):
    calls: list[dict] = []

    def faces(_path, anchors, **_kw):
        if face_anchors is not None:
            face_anchors.append(list(anchors))
        return [ProtectedRegion(a, a, FACE, "face") for a in anchors], {"decoded": len(anchors)}

    def track(clip_path, **kw):
        calls.append({"clip_path": clip_path, **kw})
        box = plates[kw["media_id"]]
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

    monkeypatch.setattr(title_mod, "sample_face_regions", faces)
    monkeypatch.setattr(protected_mod, "track_object", track)
    return calls


def _multi_job(monkeypatch, marks: list[dict]):
    job, snapshot, _bindings = _setup_talking_multi(
        monkeypatch, clips=[("take1", 10.0, 20), ("take2", 8.0, 16)]
    )
    binding = _binding(marks=marks)
    snapshot["creator_brief_binding"] = binding
    job.assembly_plan["creator_brief_binding"] = binding
    return job


def _run_multi(job) -> dict:
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job.assembly_plan["variants"][0]


def test_the_second_speakers_captions_keep_off_the_plate_marked_on_that_clip(monkeypatch):
    calls = _plates_by_clip(monkeypatch, {"take1": MIDDLE_PLATE, "take2": CAPTION_BAND_PLATE})
    job = _multi_job(
        monkeypatch,
        [
            _mark(media_id="take1", box=_box_list(MIDDLE_PLATE)),
            _mark(media_id="take2", box=_box_list(CAPTION_BAND_PLATE)),
        ],
    )

    variant = _run_multi(job)

    assert sorted({c["media_id"] for c in calls}) == ["take1", "take2"]
    take2 = next(c for c in calls if c["media_id"] == "take2")
    assert (take2["start_s"], take2["end_s"]) == (0.0, 8.0), "its own clock, not take1's cut"
    receipt = variant[PROTECTED_TEXT_FIELD]
    plate = next(t for t in receipt["targets"] if t["label"] == "plate")
    assert plate["status"] == "clear", receipt
    y_frac = variant["caption_editor_style"]["position_y_frac"]
    assert not (0.74 <= y_frac <= 0.90), "the captions moved off the plate under take2"
    assert variant["phone_talking_multi_clip"]["speaker_media_ids"] == ["take1", "take2"]


def test_a_speaker_clip_without_a_mark_is_never_called_clear(monkeypatch):
    _plates_by_clip(monkeypatch, {"take1": MIDDLE_PLATE, "take2": CAPTION_BAND_PLATE})
    job = _multi_job(monkeypatch, [_mark(media_id="take1", box=_box_list(MIDDLE_PLATE))])

    variant = _run_multi(job)

    receipt = variant[PROTECTED_TEXT_FIELD]
    plate = next(t for t in receipt["targets"] if t["label"] == "plate")
    assert plate["status"] == "other_source", receipt
    assert receipt["verdict"] != "clear"
    assert _r8(variant).status == "partial"


def test_faces_are_sampled_inside_every_caption_line_not_six_times_over_the_take(monkeypatch):
    anchors: list[list[float]] = []
    _stand_ins(monkeypatch)
    _plates_by_clip(monkeypatch, {"c0": PLATE}, face_anchors=anchors)
    job, snapshot = _job(monkeypatch, _binding(marks=[_mark()]))

    variant = _run(job, snapshot)

    caption_samples = max(anchors, key=len)
    assert len(caption_samples) > 6
    for cue in variant["caption_cues"]:
        assert any(cue["start_s"] <= at < cue["end_s"] for at in caption_samples), cue


def test_grounding_is_told_where_the_marked_object_is_so_no_card_or_beat_lands_on_it(
    monkeypatch,
):
    import app.services.phone_overlay_grounding as overlay_mod
    import app.services.phone_reaction_grounding as beats_mod
    from tests.tasks.test_phone_subtitled_narrated_dispatch import (
        _basic_beat_receipt,
        _beat_dict,
        _beat_grounding_mock,
        _enable_beats,
        _grounding_mock,
    )

    _stand_ins(monkeypatch)
    cards = _grounding_mock([])
    beats = _beat_grounding_mock([], [], _basic_beat_receipt())
    monkeypatch.setattr(overlay_mod, "ground_phone_subtitled_overlays", cards)
    monkeypatch.setattr(beats_mod, "ground_phone_reaction_beats", beats)
    job, snapshot = _job(monkeypatch, _binding(marks=[_mark()]))
    _enable_beats(monkeypatch)
    job.all_candidates["creator_strategy"]["reaction_beats"] = [
        _beat_dict("b0", "Hello", visual_id="photo1")
    ]

    _run(job, snapshot)

    plate = source_box_to_canvas(PLATE, display_width=1080, display_height=1920, canvas=CANVAS)
    for grounding in (cards, beats):
        grounding.assert_called_once()
        regions = grounding.call_args.kwargs["protected_regions"]
        assert regions, "grounding was told where the marked object is"
        assert all(plate.coverage_by(r.box) > 0.99 for r in regions)
        assert min(r.start_s for r in regions) == 0.0
        assert max(r.end_s for r in regions) == pytest.approx(10.0)


def test_grounding_hears_nothing_when_the_creator_never_asked(monkeypatch):
    import app.services.phone_overlay_grounding as overlay_mod
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _grounding_mock

    _stand_ins(monkeypatch)
    cards = _grounding_mock([])
    monkeypatch.setattr(overlay_mod, "ground_phone_subtitled_overlays", cards)
    job, snapshot = _job(monkeypatch, _binding(marks=None, brief=_STORED))
    _enable_beats(monkeypatch)

    _run(job, snapshot)

    cards.assert_called_once()
    assert "protected_regions" not in cards.call_args.kwargs


# ── Review round 2: a row that STARTS on the first speaker and runs onto the next ────────────────
#
# Failure mode (written before the code): a text row's receipt was judged only by where it
# starts, so a spoken-word row, a list label or a held title that runs from the first speaker
# onto the second kept its first-clip-only "clear" while it sat over the second clip unmeasured.

from app.pipeline.phone_protected_text import (  # noqa: E402
    ProtectedGeometry,
    ProtectedPlan,
    SpeakerSlice,
    protected_text_receipt,
)

_MEASURED = {"status": "preset", "objects_verified": True, "object_collision": False}


def _two_speaker_protection() -> ProtectedPlan:
    from app.kria.protected_targets import ProtectedTarget

    target = ProtectedTarget("object", "plate", ("r1",))
    row = {
        "kind": "object",
        "label": "plate",
        "requirement_ids": ["r1"],
        "track_status": "tracked",
    }
    second = ProtectedPlan(targets=(target,), target_rows=(row,))
    return ProtectedPlan(
        targets=(target,),
        target_rows=(row,),
        geometry=ProtectedGeometry(((0.0, 10.0),), 1080, 1920, 1.0, 0.0),
        speakers=(SpeakerSlice("take2", 10.0, 20.0, second),),
    )


def test_a_row_that_runs_from_the_first_speaker_onto_the_next_is_not_called_clear():
    plan = _two_speaker_protection()

    straddling = gb._past_first_speaker(_MEASURED, 9.0, 11.0, plan, 10.0)

    assert straddling["objects_verified"] is False
    assert protected_text_receipt(plan, {"spoken:r1": straddling})["verdict"] == "unverified"


def test_a_row_inside_the_first_speaker_keeps_its_verdict():
    plan = _two_speaker_protection()

    assert gb._past_first_speaker(_MEASURED, 7.0, 10.0, plan, 10.0) == _MEASURED
    assert gb._past_first_speaker(_MEASURED, 7.0, 10.0, None, 10.0) == _MEASURED


def test_a_row_wholly_on_a_later_speaker_stays_unverified():
    plan = _two_speaker_protection()

    assert gb._past_first_speaker(_MEASURED, 12.0, 14.0, plan, 10.0)["objects_verified"] is False


def test_a_spoken_word_row_that_runs_onto_the_next_speaker_is_not_called_clear():
    plan = _two_speaker_protection()
    receipt = {
        "rows": [
            {
                "requirement_id": "r1",
                "status": "placed",
                "start_s": 9.0,
                "end_s": 11.0,
                "placement": _MEASURED,
            },
            {
                "requirement_id": "r2",
                "status": "placed",
                "start_s": 2.0,
                "end_s": 4.0,
                "placement": _MEASURED,
            },
        ]
    }

    placements = gb._spoken_protected_placements(receipt, plan, 10.0)

    assert placements["spoken:r1"]["objects_verified"] is False
    assert placements["spoken:r2"] == _MEASURED


def test_a_list_label_that_runs_onto_the_next_speaker_is_not_called_clear():
    plan = _two_speaker_protection()
    receipt = {
        "rows": [
            {
                "requirement_id": "l1",
                "status": "placed",
                "start_s": 8.5,
                "end_s": 11.5,
                "placement": _MEASURED,
            }
        ]
    }

    placements = gb._list_label_protected_placements(receipt, plan, 10.0)

    assert placements["list:l1-1"]["objects_verified"] is False


def test_a_title_held_past_a_short_first_clip_is_not_called_clear():
    plan = _two_speaker_protection()
    held = [{"id": "t", "start_s": 0.0, "end_s": 4.0}]

    assert gb._title_protected_placement(_MEASURED, held, plan, 3.0)["objects_verified"] is False
    assert gb._title_protected_placement(_MEASURED, held, plan, 10.0) == _MEASURED
    assert gb._title_protected_placement(_MEASURED, [], plan, 3.0) is None


# KRI-720 (Emir's review of #2251): with several takes, the first take can be cut while every
# later take plays whole. Face/object protection for take2 must start where take2 really plays
# (after the first take's KEPT seconds), never at the first take's full length.


@pytest.mark.parametrize(
    ("first_keep", "later_cut", "take2_start_s"),
    [
        ([(0.0, 10.0)], None, 10.0),  # nothing cut: unchanged
        ([(0.0, 2.0), (4.0, 10.0)], None, 8.0),  # first cut, later whole (the review case)
        ([(0.0, 2.0), (4.0, 10.0)], {"take2": [(1.0, 8.0)]}, 8.0),  # both cut
    ],
)
def test_later_take_protection_starts_after_the_first_takes_kept_seconds(
    monkeypatch, first_keep, later_cut, take2_start_s
):
    from tests.pipeline.test_phone_voiceover_montage_plan import _binding as _source

    _plates_by_clip(monkeypatch, {"take1": MIDDLE_PLATE, "take2": CAPTION_BAND_PLATE})
    marks = [
        _mark(media_id="take1", box=_box_list(MIDDLE_PLATE)),
        _mark(media_id="take2", box=_box_list(CAPTION_BAND_PLATE)),
    ]
    plan, _look, _receipt = gb._phone_protected_plan(
        {"creator_brief_binding": _binding(marks=marks)},
        binding=_source("take1", duration_s=10.0),
        clip_path="/tmp/take1.mp4",
        keep_segments=first_keep,
        landscape_fit="fill",
        position_x=0.0,
        cues=[{"text": "hello there", "start_s": 0.5, "end_s": 1.5}],
        caption_style="sentence",
        extra_speakers=((_source("take2", duration_s=8.0), "/tmp/take2.mp4"),),
        **({"extra_keep_segments": later_cut} if later_cut else {}),
    )

    (take2,) = [s for s in plan.speakers if s.media_id == "take2"]
    assert take2.start_s == pytest.approx(take2_start_s)
    later_s = sum(b - a for a, b in (later_cut or {}).get("take2", [(0.0, 8.0)]))
    assert take2.end_s == pytest.approx(take2_start_s + later_s)

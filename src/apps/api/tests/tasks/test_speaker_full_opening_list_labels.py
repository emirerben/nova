"""Qendresa item 16 x item 19 (S88): list labels keep off her face as it is drawn AT THEIR TIME.

Item 19 (#2116) places each numbered label off the speaker's face mapped into the corner
`speaker_frame` for the whole edit. Item 16 draws her FULL-FRAME before the switch
(`speaker_background.full_screen_windows`), so a label shown during the opening was kept off a
small corner face that is not on screen, and could sit on her real face (Emir's review of #2111,
a static integration finding).

Item 19's worker harness (Q2 walkthrough, 130 s, five recordings, her full-frame face from its
stub sampler at y 0.25-0.50), with the opening asked for 8.5 s (stated) and open (flag,
`speakerFrameWindows`, protocol 41). Kept off the captions the labels sit mid-screen (y ~0.46-0.54):
clear of her face in the bottom-right corner, but over its chin when she fills the screen.
"spec kit" is said at ~7.85 s, inside the opening.

Ways it could fail (written before the fix):
1. A label inside the opening is kept off the corner face only and covers her full-frame face.
2. A label crossing the switch is checked against one geometry for its whole time.
3. Labels that follow the recordings (item 17 on) start at their window, after the switch
   (delayed intro labels): they must keep the corner geometry, unchanged.
4. Without an opening nothing about label placement changes (item 19's own tests unchanged).
5. The spans are on another clock than the label rows (both are cut-timeline seconds).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.pipeline.phone_subtitled_plan import _STORY_CANVAS
from app.pipeline.phone_subtitled_title import _measure_title_box, frame_face_box
from app.pipeline.render_geometry import NormalizedBox
from app.tasks import generative_build as gb
from tests.tasks import test_qendresa_list_labels_worker as item19
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture: no Gemini, no face sampling, no trace DB
    _recipe,
)

FACE = item19.FULL_FRAME_FACE
OPENING_S = 8.5
CORNER = (0.42, 0.76, 0.80)  # small_bottom_right: width, x, y


def _run(monkeypatch, *, follows_talk: bool, opening: bool = True):
    item19._flag(monkeypatch, True)
    background = {**item19.SEQUENCE_BACKGROUND}
    if opening:
        background |= {"full_screen_opening": True, "full_screen_opening_s": OPENING_S}
    job = item19._label_job(monkeypatch, background=background, follows_talk=follows_talk)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "speakerFrameWindows"],
    )
    job.all_candidates["creator_client_protocol"] = 41
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", (job.status, job.error_detail)
    return job, job.assembly_plan["variants"][0]


def _face_at(at: float, spans) -> NormalizedBox:
    """Her face as the phone draws it at ``at``: full-frame inside the opening, else the corner."""
    if any(span.start <= at < span.end for span in spans):
        return FACE
    return frame_face_box(FACE, CORNER)


def _check(job, variant) -> list[tuple[str, float, float]]:
    spans = _recipe(job).speaker_background.full_screen_windows or []
    rows = item19._label_rows(variant)
    assert rows, "no label rows"
    bad = []
    for row in rows:
        box = _measure_title_box(row, canvas=_STORY_CANVAS)
        start, end = float(row["start_s"]), float(row["end_s"])
        # Every switch inside the row's time is a moment the face moves: judge both sides.
        moments = {start, *(s.end for s in spans if start < s.end < end)}
        for at in sorted(moments):
            overlap = box.intersection_area(_face_at(at, spans))
            if overlap > 0.0:
                bad.append((row["text"], at, round(overlap, 4)))
    return bad


@pytest.mark.parametrize("follows_talk", [False, True])
def test_labels_keep_off_her_face_as_drawn_at_their_time(monkeypatch, follows_talk):
    """1, 2, 3, 5: word-grounded labels (item 17 off) start inside the opening, the first one
    crossing the switch; recording-following labels (item 17 on) start after it."""
    job, variant = _run(monkeypatch, follows_talk=follows_talk)
    spans = _recipe(job).speaker_background.full_screen_windows or []
    assert len(spans) == 1 and spans[0].end == pytest.approx(OPENING_S, abs=0.6), spans
    first = item19._label_rows(variant)[0]
    if not follows_talk:
        assert first["start_s"] < spans[0].end < first["end_s"], (first, spans)
    assert _check(job, variant) == []


def test_without_the_opening_labels_keep_item_19s_corner_placement(monkeypatch):
    """4 (control): the same take with no opening is item 19's placement, off the corner face."""
    job, variant = _run(monkeypatch, follows_talk=False, opening=False)
    assert not _recipe(job).speaker_background.full_screen_windows
    assert _check(job, variant) == []

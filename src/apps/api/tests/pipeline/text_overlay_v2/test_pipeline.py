"""Unit tests for the A→B→C→D orchestrator.

Uses a fake OCR backend so the orchestrator's control flow (parallelism,
stage chaining, error propagation, backend selection) is exercised
without ffmpeg, without GCP creds, and without network. The chain's
correctness on the not_just_luck OCR ground truth is already pinned by
`tests/pipeline/text_overlay_v2/test_phrases.py::test_full_pipeline_on_not_just_luck_ocr_ground_truth`
— this file focuses on the orchestrator's plumbing rather than re-testing
the grouping/phrase logic.
"""

from __future__ import annotations

import json
import time

import pytest

from app.agents._schemas.text_overlay_ocr import FrameDetection, OcrPolygon
from app.pipeline.text_overlay_v2.pipeline import (
    DEFAULT_OCR_CONCURRENCY,
    _classified_phrases_to_output,
    _dump_stage,
    _normalize_overlay_text,
    run_phrase_pipeline_from_frames,
)
from app.services.video_frames import ExtractedFrame


def _make_polygon(x: float, y: float, w: float = 0.2, h: float = 0.05) -> OcrPolygon:
    half_w, half_h = w / 2.0, h / 2.0
    return OcrPolygon(
        points=[
            (x - half_w, y - half_h),
            (x + half_w, y - half_h),
            (x + half_w, y + half_h),
            (x - half_w, y + half_h),
        ]
    )


class _FakeOcrBackend:
    """In-memory OCR backend keyed by frame path.

    Records every detect() call so tests can assert on what was called
    and in what order (for sequential mode) without inspecting threads.
    """

    name = "fake"

    def __init__(self, detections_by_path: dict[str, list[FrameDetection]]) -> None:
        self._detections_by_path = detections_by_path
        self.calls: list[tuple[str, float]] = []
        self._sleep_s: float = 0.0

    def with_sleep(self, sleep_s: float) -> _FakeOcrBackend:
        self._sleep_s = sleep_s
        return self

    def detect(self, image_path: str, *, frame_t_s: float) -> list[FrameDetection]:
        self.calls.append((image_path, frame_t_s))
        if self._sleep_s:
            time.sleep(self._sleep_s)
        return list(self._detections_by_path.get(image_path, []))


def _det(t: float, text: str, x: float = 0.5, y: float = 0.5) -> FrameDetection:
    return FrameDetection(
        frame_t_s=t,
        text=text,
        polygon=_make_polygon(x, y),
        confidence=1.0,
    )


# ── Boundary cases ────────────────────────────────────────────────────────────


def test_empty_frame_list_returns_empty_phrases():
    backend = _FakeOcrBackend({})
    assert run_phrase_pipeline_from_frames([], backend=backend) == []
    assert backend.calls == []


def test_frames_with_no_detections_return_empty_phrases():
    frames = [ExtractedFrame(path="/f/1.jpg", t_s=0.0), ExtractedFrame(path="/f/2.jpg", t_s=0.5)]
    backend = _FakeOcrBackend({"/f/1.jpg": [], "/f/2.jpg": []})
    assert run_phrase_pipeline_from_frames(frames, backend=backend) == []


# ── Stage chaining: B → C → D ─────────────────────────────────────────────────


def test_single_caption_across_frames_yields_one_phrase():
    frames = [
        ExtractedFrame(path="/f/1.jpg", t_s=0.0),
        ExtractedFrame(path="/f/2.jpg", t_s=0.5),
        ExtractedFrame(path="/f/3.jpg", t_s=1.0),
    ]
    backend = _FakeOcrBackend(
        {
            "/f/1.jpg": [_det(0.0, "Hello")],
            "/f/2.jpg": [_det(0.5, "Hello")],
            "/f/3.jpg": [_det(1.0, "Hello")],
        }
    )
    phrases = run_phrase_pipeline_from_frames(frames, backend=backend)
    assert len(phrases) == 1
    assert phrases[0].sample_text == "Hello"
    assert phrases[0].start_t_s == 0.0
    assert phrases[0].end_t_s == 1.0


def test_phrase_reset_after_screen_clear_produces_two_phrases():
    # "Hi" appears, "Hi" disappears, "Bye" appears later. Backend returns
    # nothing for the gap frames — the pipeline should split.
    frames = [
        ExtractedFrame(path="/f/1.jpg", t_s=0.0),
        ExtractedFrame(path="/f/2.jpg", t_s=0.5),
        ExtractedFrame(path="/f/3.jpg", t_s=1.5),  # gap > jitter threshold (1.0s)
        ExtractedFrame(path="/f/4.jpg", t_s=3.5),
        ExtractedFrame(path="/f/5.jpg", t_s=4.0),
    ]
    backend = _FakeOcrBackend(
        {
            "/f/1.jpg": [_det(0.0, "Hi")],
            "/f/2.jpg": [_det(0.5, "Hi")],
            "/f/3.jpg": [],  # screen clear
            "/f/4.jpg": [_det(3.5, "Bye")],
            "/f/5.jpg": [_det(4.0, "Bye")],
        }
    )
    phrases = run_phrase_pipeline_from_frames(frames, backend=backend)
    assert len(phrases) == 2
    assert phrases[0].sample_text == "Hi"
    assert phrases[1].sample_text == "Bye"


def test_build_up_multi_line_phrase_assembles_in_y_order():
    # Three frames; each adds a new line below the previous. All three
    # lines should land in ONE phrase, Y-sorted.
    frames = [
        ExtractedFrame(path="/f/1.jpg", t_s=0.0),
        ExtractedFrame(path="/f/2.jpg", t_s=0.5),
        ExtractedFrame(path="/f/3.jpg", t_s=1.0),
    ]
    backend = _FakeOcrBackend(
        {
            "/f/1.jpg": [_det(0.0, "It's", y=0.45)],
            "/f/2.jpg": [_det(0.5, "It's", y=0.45), _det(0.5, "not", y=0.5)],
            "/f/3.jpg": [
                _det(1.0, "It's", y=0.45),
                _det(1.0, "not", y=0.5),
                _det(1.0, "just luck", y=0.55),
            ],
        }
    )
    phrases = run_phrase_pipeline_from_frames(frames, backend=backend)
    assert len(phrases) == 1
    assert phrases[0].lines == ["It's", "not", "just luck"]
    assert phrases[0].sample_text == "It's\nnot\njust luck"


# ── Concurrency contract ──────────────────────────────────────────────────────


def test_parallel_ocr_is_faster_than_sequential_for_slow_backend():
    # 10 frames × 50ms each. Sequential = 500ms; parallel ≈ 50-100ms.
    n = 10
    sleep_per_frame = 0.05
    frames = [ExtractedFrame(path=f"/f/{i}.jpg", t_s=i * 0.5) for i in range(n)]
    detections = {f.path: [_det(f.t_s, "x")] for f in frames}

    seq_backend = _FakeOcrBackend(detections).with_sleep(sleep_per_frame)
    t0 = time.monotonic()
    run_phrase_pipeline_from_frames(frames, backend=seq_backend, ocr_concurrency=1)
    seq_time = time.monotonic() - t0

    par_backend = _FakeOcrBackend(detections).with_sleep(sleep_per_frame)
    t0 = time.monotonic()
    run_phrase_pipeline_from_frames(frames, backend=par_backend, ocr_concurrency=10)
    par_time = time.monotonic() - t0

    # Sequential should be ~10× slower. Allow generous margin to avoid flakiness.
    assert par_time < seq_time / 2, f"parallel={par_time:.3f}s seq={seq_time:.3f}s"


def test_every_frame_is_OCRd_exactly_once():
    frames = [
        ExtractedFrame(path="/f/1.jpg", t_s=0.0),
        ExtractedFrame(path="/f/2.jpg", t_s=0.5),
        ExtractedFrame(path="/f/3.jpg", t_s=1.0),
    ]
    backend = _FakeOcrBackend({f.path: [] for f in frames})
    run_phrase_pipeline_from_frames(frames, backend=backend)

    paths_called = sorted(c[0] for c in backend.calls)
    assert paths_called == ["/f/1.jpg", "/f/2.jpg", "/f/3.jpg"]


def test_each_ocr_call_receives_corresponding_frame_t_s():
    frames = [
        ExtractedFrame(path="/f/A.jpg", t_s=2.5),
        ExtractedFrame(path="/f/B.jpg", t_s=4.0),
    ]
    backend = _FakeOcrBackend({f.path: [] for f in frames})
    run_phrase_pipeline_from_frames(frames, backend=backend, ocr_concurrency=1)

    call_map = dict(backend.calls)
    assert call_map["/f/A.jpg"] == 2.5
    assert call_map["/f/B.jpg"] == 4.0


# ── Threshold pass-through ────────────────────────────────────────────────────


def test_jitter_max_gap_s_is_forwarded_to_grouping():
    # 1.5s gap is within default 1.0s threshold? No — should split.
    # But with a custom 2.0s threshold, the same gap stays together.
    frames = [
        ExtractedFrame(path="/f/1.jpg", t_s=0.0),
        ExtractedFrame(path="/f/2.jpg", t_s=1.5),
    ]
    backend = _FakeOcrBackend(
        {
            "/f/1.jpg": [_det(0.0, "Hi")],
            "/f/2.jpg": [_det(1.5, "Hi")],
        }
    )
    # Default 1.0s threshold: 1.5 > 1.0 → splits into two events → two phrases
    default = run_phrase_pipeline_from_frames(frames, backend=backend)
    assert len(default) == 2

    # Looser 2.0s threshold: stays as one
    loose = run_phrase_pipeline_from_frames(frames, backend=backend, jitter_max_gap_s=2.0)
    assert len(loose) == 1


def test_x_band_threshold_is_forwarded_to_phrase_reconstruction():
    # Two simultaneous events far apart in X. Default x_band=0.15 keeps
    # them in separate phrases; loose 0.5 merges them.
    frames = [ExtractedFrame(path="/f/1.jpg", t_s=0.0)]
    backend = _FakeOcrBackend(
        {
            "/f/1.jpg": [_det(0.0, "Left", x=0.3), _det(0.0, "Right", x=0.7)],
        }
    )
    assert len(run_phrase_pipeline_from_frames(frames, backend=backend)) == 2
    assert len(run_phrase_pipeline_from_frames(frames, backend=backend, x_band_threshold=0.5)) == 1


# ── Module constants ──────────────────────────────────────────────────────────


def test_default_ocr_concurrency_is_10():
    # Pinning the design-doc value so a follow-up cap change is intentional.
    assert DEFAULT_OCR_CONCURRENCY == 10


# ── Error propagation ─────────────────────────────────────────────────────────


class _ExplodingBackend:
    name = "exploding"

    def detect(self, image_path: str, *, frame_t_s: float) -> list[FrameDetection]:
        raise RuntimeError(f"intentional failure on {image_path}")


def test_backend_error_propagates_through_orchestrator():
    frames = [ExtractedFrame(path="/f/1.jpg", t_s=0.0)]
    with pytest.raises(RuntimeError, match="intentional failure"):
        run_phrase_pipeline_from_frames(frames, backend=_ExplodingBackend())


# ── Stage-dump instrumentation ────────────────────────────────────────────────


def test_dump_stage_noop_when_dir_is_none(tmp_path):
    # Sanity: passing None must not touch the filesystem or raise.
    _dump_stage(None, "stage_a", [_det(0.0, "x")])
    assert list(tmp_path.iterdir()) == []


def test_dump_stage_writes_pydantic_list_as_json(tmp_path):
    detections = [_det(0.0, "Hi"), _det(0.5, "There")]
    _dump_stage(tmp_path, "stage_b", detections)
    payload = json.loads((tmp_path / "stage_b.json").read_text())
    assert isinstance(payload, list)
    assert len(payload) == 2
    assert payload[0]["text"] == "Hi"
    assert payload[1]["text"] == "There"


def test_dump_stage_writes_pydantic_model_as_json(tmp_path):
    from app.agents._schemas.template_text import TemplateTextOutput

    out = TemplateTextOutput(overlays=[])
    _dump_stage(tmp_path, "stage_g", out)
    payload = json.loads((tmp_path / "stage_g.json").read_text())
    assert payload == {"overlays": []}


def test_dump_stage_swallows_serialization_errors(tmp_path):
    # Instrumentation must never break the pipeline.
    class _BadDump:
        def model_dump(self):
            raise RuntimeError("boom")

    _dump_stage(tmp_path, "stage_x", _BadDump())
    # File is either absent or empty; what matters is no exception escaped.
    f = tmp_path / "stage_x.json"
    assert not f.exists() or f.read_text() in ("", "{}", "null")


# ── Stage G text normalization (regression: prod job 87b7292b) ────────────────


def test_normalize_overlay_text_collapses_embedded_newlines():
    # Phrase.sample_text joins lines with "\n". Layer-1 renderer was emitting
    # the literal "\n" as on-screen "/N" instead of an ASS line break.
    # Normalizer collapses to a single space.
    assert _normalize_overlay_text("if\nyou\nput in") == "if you put in"


def test_normalize_overlay_text_strips_truncation_marker():
    assert (
        _normalize_overlay_text("the work to get there[overlap_truncated]")
        == "the work to get there"
    )


def test_normalize_overlay_text_strips_literal_escape_sequences():
    # If alignment or upstream stages leaked a literal "\\N" or "\\n" string
    # into the text, strip it rather than echoing it to the renderer.
    assert _normalize_overlay_text("line1\\Nline2") == "line1 line2"
    assert _normalize_overlay_text("a\\nb") == "a b"


def test_normalize_overlay_text_collapses_internal_whitespace():
    assert _normalize_overlay_text("  the   quick   brown  ") == "the quick brown"


def test_normalize_overlay_text_passthrough_for_clean_input():
    assert _normalize_overlay_text("if") == "if"
    assert _normalize_overlay_text("hello world") == "hello world"


def test_normalize_overlay_text_handles_empty_and_whitespace_only():
    assert _normalize_overlay_text("") == ""
    assert _normalize_overlay_text("   ") == ""
    assert _normalize_overlay_text("\\N\\n") == ""


# ── Stage G end_s extension (regression: 10ms "luck" entries from post-v0.4.34.0) ─


def _make_classified_phrase(text: str, start_t_s: float, end_t_s: float):
    """Build a minimal ClassifiedPhrase for end_s extension tests."""
    from app.agents._schemas.text_classification import ClassifiedPhrase
    from app.agents._schemas.text_overlay_pipeline import Phrase

    return ClassifiedPhrase(
        phrase=Phrase(
            lines=[text],
            start_t_s=start_t_s,
            end_t_s=end_t_s,
            aabb=(0.3, 0.4, 0.7, 0.5),
            mean_confidence=0.9,
        ),
        effect="none",
        role="label",
        size_class="medium",
        font_color_hex="#FFFFFF",
    )


def test_stage_g_extends_single_frame_phrase_to_next_phrase_start():
    """A phrase whose end_t_s ≈ start_t_s (single-frame OCR detection) gets
    extended to the next phrase's start so the word stays visible. Regression
    for the post-v0.4.34.0 reanalyze where 'luck' rendered for 10ms.
    """
    classified = [
        _make_classified_phrase("It's", 0.0, 0.9),
        _make_classified_phrase("not", 1.0, 1.5),
        # Single-frame detection: end_t_s == start_t_s.
        _make_classified_phrase("luck", 7.5, 7.5),
        _make_classified_phrase("combination", 8.0, 8.5),
    ]
    out = _classified_phrases_to_output(classified, slot_boundaries_s=[(0.0, 10.0)])
    overlays_by_text = {o.sample_text: o for o in out.overlays}

    luck = overlays_by_text["luck"]
    # Without the extension, luck would be 7.5 → 7.51 (10ms). With it,
    # luck extends toward the next phrase's start at 8.0 — capped at the
    # _MIN_OVERLAY_DURATION_S * 4 ceiling (2.0s), so 8.0 wins here.
    assert luck.end_s == pytest.approx(8.0)
    assert luck.start_s == pytest.approx(7.5)


def test_stage_g_extends_last_phrase_to_min_duration_when_no_next():
    """The final phrase has no next phrase to extend toward. It gets a
    floor of _MIN_OVERLAY_DURATION_S (0.5s) so it doesn't end at 10ms.
    """
    classified = [
        _make_classified_phrase("It's", 0.0, 0.9),
        _make_classified_phrase("end", 9.0, 9.0),  # single-frame at the end
    ]
    out = _classified_phrases_to_output(classified, slot_boundaries_s=[(0.0, 10.0)])
    overlays_by_text = {o.sample_text: o for o in out.overlays}

    end = overlays_by_text["end"]
    assert end.end_s == pytest.approx(9.5)  # 9.0 + 0.5s minimum


def test_stage_g_does_not_extend_phrases_with_real_duration():
    """A phrase with end_t_s well past start_t_s keeps its original window.
    No greedy extension — only the degenerate single-frame case is touched.
    """
    classified = [
        _make_classified_phrase("It's", 0.0, 0.9),
        _make_classified_phrase("just luck", 2.0, 4.0),  # already 2 seconds
        _make_classified_phrase("end", 5.0, 5.5),
    ]
    out = _classified_phrases_to_output(classified, slot_boundaries_s=[(0.0, 10.0)])
    overlays_by_text = {o.sample_text: o for o in out.overlays}

    # Real-duration phrase is untouched.
    assert overlays_by_text["just luck"].end_s == pytest.approx(4.0)


def test_stage_g_caps_extension_at_max_window():
    """A short phrase followed by a long gap doesn't linger for the entire
    gap. Extension is capped at _MIN_OVERLAY_DURATION_S * 4 = 2.0s.
    """
    classified = [
        _make_classified_phrase("hook", 0.0, 0.0),  # single-frame at 0
        _make_classified_phrase("after_gap", 9.0, 9.5),  # 9-second gap
    ]
    out = _classified_phrases_to_output(classified, slot_boundaries_s=[(0.0, 10.0)])
    overlays_by_text = {o.sample_text: o for o in out.overlays}

    hook = overlays_by_text["hook"]
    # Without the cap, hook would extend to 9.0s. Capped at 0 + 2.0 = 2.0s.
    assert hook.end_s == pytest.approx(2.0)


# ── Stage G cumulative emit (LineGroup → progressive word reveal) ──────────


def _make_atomized_classified(word: str, start_s: float, x_min: float = 0.1):
    """ClassifiedPhrase with a single-word atomized phrase at a known left x."""
    from app.agents._schemas.text_classification import ClassifiedPhrase
    from app.agents._schemas.text_overlay_pipeline import Phrase

    return ClassifiedPhrase(
        phrase=Phrase(
            lines=[word],
            start_t_s=start_s,
            end_t_s=start_s + 0.5,
            aabb=(x_min, 0.7, x_min + 0.2, 0.85),
            mean_confidence=0.9,
        ),
        effect="pop-in",
        role="label",
        size_class="medium",
        font_color_hex="#FFFFFF",
    )


def test_stage_g_line_group_emits_n_cumulative_overlays():
    """A LineGroup of 3 atomized phrases emits 3 cumulative overlays where
    each carries the cumulative text up to and including its word. This is
    the core progressive-reveal behavior the user requested."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    classified = [
        _make_atomized_classified("good", 0.0),
        _make_atomized_classified("morning", 1.0),
        _make_atomized_classified("everyone", 2.0),
    ]
    lg = LineGroup(
        phrase_indices=[0, 1, 2],
        line_end_s=3.0,
        line_anchor_x_frac=0.1,
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=[0, 1, 2],
        word_start_s_list=[0.0, 1.0, 2.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 10.0)], line_groups=[lg]
    )
    assert len(out.overlays) == 3
    cumulative_texts = [o.sample_text for o in out.overlays]
    assert cumulative_texts == ["good", "good morning", "good morning everyone"]


def test_stage_g_line_group_overlays_are_left_anchored():
    """Cumulative overlays must carry text_anchor='left' so the rendered
    text grows rightward without re-centering as new words appear."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    classified = [
        _make_atomized_classified("hello", 0.0, x_min=0.15),
        _make_atomized_classified("world", 1.0, x_min=0.4),
    ]
    lg = LineGroup(
        phrase_indices=[0, 1],
        line_end_s=2.0,
        line_anchor_x_frac=0.15,
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=[0, 1],
        word_start_s_list=[0.0, 1.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 5.0)], line_groups=[lg]
    )
    assert all(o.text_anchor == "left" for o in out.overlays)
    # All cumulative overlays share the FIRST phrase's left anchor, regardless
    # of where individual words' bboxes were on screen. Left edge of "good"
    # stays put as "morning" enters to its right.
    assert all(o.bbox.x_norm == pytest.approx(0.15) for o in out.overlays)


def test_stage_g_pop_animated_suffix_on_pop_in_effect():
    """When the classified effect is pop-in, each cumulative stage carries
    pop_animated_suffix = newly added word so only that word pops on screen
    (prefix renders statically)."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    classified = [
        _make_atomized_classified("good", 0.0),
        _make_atomized_classified("morning", 1.0),
    ]
    lg = LineGroup(
        phrase_indices=[0, 1],
        line_end_s=2.0,
        line_anchor_x_frac=0.1,
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=[0, 1],
        word_start_s_list=[0.0, 1.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 5.0)], line_groups=[lg]
    )
    assert [o.pop_animated_suffix for o in out.overlays] == ["good", "morning"]


def test_stage_g_pop_animated_suffix_none_for_non_pop_effect():
    """When the classified effect is not pop-in, pop_animated_suffix stays
    None so the renderer doesn't try to animate a suffix on a static overlay."""
    from app.agents._schemas.text_classification import ClassifiedPhrase
    from app.agents._schemas.text_overlay_pipeline import Phrase
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    # Build classified with effect="none" instead of pop-in.
    def _none_atomized(w, s):
        return ClassifiedPhrase(
            phrase=Phrase(
                lines=[w],
                start_t_s=s,
                end_t_s=s + 0.3,
                aabb=(0.1, 0.7, 0.3, 0.85),
                mean_confidence=0.9,
            ),
            effect="none",
            role="label",
            size_class="medium",
            font_color_hex="#FFFFFF",
        )

    classified = [_none_atomized("a", 0.0), _none_atomized("b", 1.0)]
    lg = LineGroup(
        phrase_indices=[0, 1],
        line_end_s=2.0,
        line_anchor_x_frac=0.1,
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=[0, 1],
        word_start_s_list=[0.0, 1.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 5.0)], line_groups=[lg]
    )
    assert all(o.pop_animated_suffix is None for o in out.overlays)


def test_stage_g_ungrouped_phrases_passthrough_unchanged():
    """Phrases NOT in any LineGroup use the existing per-phrase emit path
    (one TemplateTextOverlay per phrase, center-anchored). This is the
    regression lock — visual-only labels and non-progressive captions
    must keep working."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    classified = [
        _make_atomized_classified("hello", 0.0),
        _make_atomized_classified("PERU", 5.0),  # not in any group
        _make_atomized_classified("world", 1.0),
    ]
    # Only phrases 0 and 2 are in the line group; phrase 1 ("PERU") stays
    # ungrouped and passes through.
    lg = LineGroup(
        phrase_indices=[0, 2],
        line_end_s=2.0,
        line_anchor_x_frac=0.1,
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=[0, 1],
        word_start_s_list=[0.0, 1.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 10.0)], line_groups=[lg]
    )
    # 2 cumulative + 1 passthrough = 3 overlays.
    assert len(out.overlays) == 3
    by_text = {o.sample_text: o for o in out.overlays}
    assert "PERU" in by_text
    # PERU stays center-anchored (the default), not left-anchored.
    assert by_text["PERU"].text_anchor == "center"
    # The two cumulative overlays are left-anchored.
    assert by_text["hello"].text_anchor == "left"
    assert by_text["hello world"].text_anchor == "left"


def test_stage_g_empty_line_groups_preserves_existing_behavior():
    """Passing line_groups=None or empty list must produce identical output
    to the pre-change behavior — every phrase goes through the per-phrase
    passthrough emit. This is the broad regression lock."""
    classified = [
        _make_atomized_classified("good", 0.0),
        _make_atomized_classified("morning", 1.0),
    ]
    out_none = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 5.0)], line_groups=None
    )
    out_empty = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 5.0)], line_groups=[]
    )
    assert len(out_none.overlays) == 2
    assert len(out_empty.overlays) == 2
    # Both are center-anchored (default), not left.
    assert all(o.text_anchor == "center" for o in out_none.overlays)


def test_stage_g_line_split_breaks_oversized_cumulative_line():
    """A LineGroup whose cumulative text would exceed 90% of canvas width
    splits at the overflow word into separate sub-groups, each with its
    own start time. No word is dropped."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    # 12 medium-size words: cumulative text grows past 90% canvas width
    # around word 6-8 depending on font. Forces at least one split.
    long_words = [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "theta",
        "iota",
        "kappa",
        "lambda",
        "mu",
    ]
    classified = [_make_atomized_classified(w, float(i)) for i, w in enumerate(long_words)]
    lg = LineGroup(
        phrase_indices=list(range(len(long_words))),
        line_end_s=12.0,
        line_anchor_x_frac=0.05,
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=list(range(len(long_words))),
        word_start_s_list=[float(i) for i in range(len(long_words))],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 15.0)], line_groups=[lg]
    )
    # Total overlays > len(words) because each sub-group emits N cumulative
    # stages for N words — and split sub-groups duplicate prefix words across
    # boundaries? No: split sub-groups RESET cumulative text, so total =
    # sum of sub-group sizes. With 12 words and at least one split, total
    # overlays equals 12 (no overlap, no drop).
    assert len(out.overlays) == len(long_words)
    # Reveal sequence: the first overlay is "alpha"; somewhere in the middle
    # a cumulative resets (text becomes a single word, not cumulative-prefixed).
    cumulative_resets = sum(
        1 for o in out.overlays if " " not in o.sample_text and o.sample_text in long_words[1:]
    )
    # At least one reset means line was split into >=2 sub-groups.
    assert cumulative_resets >= 1


def test_stage_g_cumulative_bbox_y_norm_comes_from_first_phrase():
    """Cumulative overlays must inherit the line group's y position, not
    hardcode canvas center. Regression for the P1 bug where every cumulative
    reveal rendered at y=0.5 regardless of where OCR detected the line."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    classified = [
        _make_atomized_classified("hello", 0.0),
        _make_atomized_classified("world", 1.0),
    ]
    lg = LineGroup(
        phrase_indices=[0, 1],
        line_end_s=2.0,
        line_anchor_x_frac=0.1,
        line_anchor_y_frac=0.8,  # bottom of screen, NOT 0.5
        line_height_frac=0.05,
        transcript_word_indices=[0, 1],
        word_start_s_list=[0.0, 1.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 5.0)], line_groups=[lg]
    )
    assert all(o.bbox.y_norm == pytest.approx(0.8) for o in out.overlays)


def test_stage_g_cumulative_bbox_x_norm_is_not_clamped():
    """Cumulative overlays at the canvas's left edge keep their detected x
    position. Regression for the prior bug where x_norm was clamped to
    >= 0.05, visibly shifting left-hugging text right by ~30px."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    classified = [
        _make_atomized_classified("hello", 0.0),
        _make_atomized_classified("world", 1.0),
    ]
    lg = LineGroup(
        phrase_indices=[0, 1],
        line_end_s=2.0,
        line_anchor_x_frac=0.02,  # would have been clamped to 0.05
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=[0, 1],
        word_start_s_list=[0.0, 1.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 5.0)], line_groups=[lg]
    )
    assert all(o.bbox.x_norm == pytest.approx(0.02) for o in out.overlays)


def test_stage_g_output_is_sorted_by_start_s():
    """Final overlay list interleaves cumulative + ungrouped by start time.
    FFmpeg's overlay filter layers later items on top of earlier ones —
    sorting by start_s keeps later-starting overlays visually on top
    regardless of which branch (cumulative vs passthrough) produced them."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    classified = [
        _make_atomized_classified("good", 5.0),
        _make_atomized_classified("morning", 6.0),
        _make_atomized_classified("PERU", 0.0),
        _make_atomized_classified("ROME", 8.0),
    ]
    lg = LineGroup(
        phrase_indices=[0, 1],
        line_end_s=7.0,
        line_anchor_x_frac=0.1,
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=[0, 1],
        word_start_s_list=[5.0, 6.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 10.0)], line_groups=[lg]
    )
    starts = [o.start_s for o in out.overlays]
    assert starts == sorted(starts), f"overlays out of time order: {starts}"
    assert out.overlays[0].sample_text == "PERU"
    assert out.overlays[-1].sample_text == "ROME"


def test_stage_g_no_word_is_lost_in_line_split():
    """When a LineGroup splits, every original word still appears in some
    overlay's sample_text. Critical leakage-prevention test."""
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    words = [
        "the",
        "quick",
        "brown",
        "fox",
        "jumps",
        "over",
        "the",
        "lazy",
        "dog",
        "running",
        "very",
        "fast",
    ]
    classified = [_make_atomized_classified(w, float(i)) for i, w in enumerate(words)]
    lg = LineGroup(
        phrase_indices=list(range(len(words))),
        line_end_s=float(len(words)),
        line_anchor_x_frac=0.05,
        line_anchor_y_frac=0.8,
        line_height_frac=0.05,
        transcript_word_indices=list(range(len(words))),
        word_start_s_list=[float(i) for i in range(len(words))],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 20.0)], line_groups=[lg]
    )
    all_words_in_overlays = set()
    for o in out.overlays:
        for w in o.sample_text.split():
            all_words_in_overlays.add(w)
    assert all_words_in_overlays == set(words)


def test_stage_g_suppresses_singleton_overlapping_cumulative():
    """A singleton phrase that overlaps a cumulative LineGroup in time AND y
    gets suppressed — the cumulative reveal is canonical for that band of
    screen at that time, and rendering the singleton on top causes a visual
    clash.

    Reproduces the prod 89cde014 shape: "The work to get" cumulative at
    y≈0.44 / 5.26-5.80s + "there" singleton at y≈0.44 / 5.50-6.17s.
    Pre-fix both rendered simultaneously, "there" appearing on top of the
    cumulative; post-fix the singleton is dropped.
    """
    from app.agents._schemas.text_classification import ClassifiedPhrase
    from app.agents._schemas.text_overlay_pipeline import Phrase
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    cumulative = [
        _make_atomized_classified("The", 4.547, x_min=0.05),
        _make_atomized_classified("work", 4.967, x_min=0.05),
        _make_atomized_classified("to", 5.257, x_min=0.05),
        _make_atomized_classified("get", 5.647, x_min=0.05),
    ]
    # The orphan singleton: at the same y, overlapping in time.
    orphan = ClassifiedPhrase(
        phrase=Phrase(
            lines=["there"],
            start_t_s=5.5,
            end_t_s=6.167,
            aabb=(0.4, 0.7, 0.5, 0.85),
            mean_confidence=0.9,
        ),
        effect="none",
        role="reaction",
        size_class="medium",
        font_color_hex="#FFFFFF",
    )
    classified = [*cumulative, orphan]  # orphan at index 4 — NOT in lg
    lg = LineGroup(
        phrase_indices=[0, 1, 2, 3],
        line_end_s=5.8,
        line_anchor_x_frac=0.05,
        line_anchor_y_frac=0.775,  # midpoint of aabb y=(0.7, 0.85)
        line_height_frac=0.15,
        transcript_word_indices=[0, 1, 2, 3],
        word_start_s_list=[4.547, 4.967, 5.257, 5.647],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 10.0)], line_groups=[lg]
    )
    texts = [o.sample_text for o in out.overlays]
    assert "there" not in texts, (
        "orphan singleton overlapping the cumulative group in y+time must "
        f"be suppressed; got overlays: {texts}"
    )
    # The cumulative stages must still all be there.
    assert any("The work to get" in t for t in texts)


def test_stage_g_keeps_singleton_outside_cumulative_y_band():
    """A singleton at a DIFFERENT y from the cumulative group survives.
    Suppression is scoped: same y_norm band + overlapping time triggers it,
    a singleton at a remote y doesn't.
    """
    from app.agents._schemas.text_classification import ClassifiedPhrase
    from app.agents._schemas.text_overlay_pipeline import Phrase
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    cumulative = [
        _make_atomized_classified("good", 0.0, x_min=0.05),
        _make_atomized_classified("morning", 1.0, x_min=0.05),
    ]
    # Singleton at the top of the screen (y≈0.15) while cumulative is at
    # the bottom (aabb y=0.7-0.85, midpoint 0.775). Δy = 0.62, well beyond
    # the 0.10 tolerance.
    far_singleton = ClassifiedPhrase(
        phrase=Phrase(
            lines=["heads-up"],
            start_t_s=0.5,
            end_t_s=1.5,
            aabb=(0.1, 0.1, 0.3, 0.2),
            mean_confidence=0.9,
        ),
        effect="none",
        role="reaction",
        size_class="medium",
        font_color_hex="#FFFFFF",
    )
    classified = [*cumulative, far_singleton]
    lg = LineGroup(
        phrase_indices=[0, 1],
        line_end_s=2.0,
        line_anchor_x_frac=0.05,
        line_anchor_y_frac=0.775,
        line_height_frac=0.15,
        transcript_word_indices=[0, 1],
        word_start_s_list=[0.0, 1.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 5.0)], line_groups=[lg]
    )
    texts = [o.sample_text for o in out.overlays]
    assert "heads-up" in texts, (
        "singleton at a remote y from the cumulative group must survive — "
        f"suppression should be y-scoped; got overlays: {texts}"
    )


def test_stage_g_keeps_singleton_outside_cumulative_time_window():
    """A singleton at the SAME y but OUTSIDE the cumulative group's time
    window survives. The suppression check requires BOTH y proximity AND
    time overlap.
    """
    from app.agents._schemas.text_classification import ClassifiedPhrase
    from app.agents._schemas.text_overlay_pipeline import Phrase
    from app.pipeline.text_overlay_v2.line_grouping import LineGroup

    cumulative = [
        _make_atomized_classified("good", 0.0, x_min=0.05),
        _make_atomized_classified("morning", 1.0, x_min=0.05),
    ]
    # Singleton at the same y but starting after the cumulative emit window.
    later = ClassifiedPhrase(
        phrase=Phrase(
            lines=["coffee"],
            start_t_s=5.0,
            end_t_s=6.0,
            aabb=(0.1, 0.7, 0.3, 0.85),
            mean_confidence=0.9,
        ),
        effect="none",
        role="reaction",
        size_class="medium",
        font_color_hex="#FFFFFF",
    )
    classified = [*cumulative, later]
    lg = LineGroup(
        phrase_indices=[0, 1],
        line_end_s=2.0,
        line_anchor_x_frac=0.05,
        line_anchor_y_frac=0.775,
        line_height_frac=0.15,
        transcript_word_indices=[0, 1],
        word_start_s_list=[0.0, 1.0],
    )
    out = _classified_phrases_to_output(
        classified, slot_boundaries_s=[(0.0, 10.0)], line_groups=[lg]
    )
    texts = [o.sample_text for o in out.overlays]
    assert "coffee" in texts

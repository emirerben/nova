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

"""Unit tests for the full A→B→C→D→E→F→G pipeline orchestrator (slice G).

These tests exercise `run_full_pipeline()` — the new entry point that wires
stages E (transcript alignment) and F (classification) into the orchestrator
and returns `TemplateTextOutput` matching the Layer-1 schema.

All tests mock out ffmpeg (stage A), the OCR backend (stages B-D), and the
LLM clients (stages E and F) so no network or filesystem access is needed.

Test philosophy: these tests pin the orchestrator's CONTROL FLOW (correct
stage chaining, early returns, error propagation, cleanup) rather than
re-testing the per-stage logic which is covered by the stage-specific tests.
"""

from __future__ import annotations

import shutil
import tempfile
from unittest.mock import patch

import pytest

from app.agents._runtime import TerminalError
from app.agents._schemas.template_text import TemplateTextOutput
from app.agents._schemas.text_classification import ClassifiedPhrase, TextClassificationOutput
from app.agents._schemas.text_overlay_ocr import FrameDetection, OcrPolygon
from app.agents._schemas.text_overlay_pipeline import Phrase
from app.pipeline.text_overlay_v2.pipeline import run_full_pipeline
from app.services.video_frames import ExtractedFrame

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_polygon(
    x: float = 0.5,
    y: float = 0.5,
    w: float = 0.3,
    h: float = 0.05,
) -> OcrPolygon:
    half_w, half_h = w / 2, h / 2
    return OcrPolygon(
        points=[
            (x - half_w, y - half_h),
            (x + half_w, y - half_h),
            (x + half_w, y + half_h),
            (x - half_w, y + half_h),
        ]
    )


def _make_phrase(
    text: str,
    start_t_s: float = 0.5,
    end_t_s: float = 2.0,
    x: float = 0.5,
    y: float = 0.5,
    w: float = 0.3,
    h: float = 0.05,
) -> Phrase:
    return Phrase(
        lines=[text],
        start_t_s=start_t_s,
        end_t_s=end_t_s,
        aabb=(x - w / 2, y - h / 2, x + w / 2, y + h / 2),
        mean_confidence=1.0,
    )


def _make_classified_phrase(
    text: str,
    start_t_s: float = 0.5,
    end_t_s: float = 2.0,
    effect: str = "none",
    role: str = "label",
    size_class: str = "medium",
    font_color_hex: str = "#FFFFFF",
) -> ClassifiedPhrase:
    phrase = _make_phrase(text, start_t_s, end_t_s)
    return ClassifiedPhrase(
        phrase=phrase,
        effect=effect,
        role=role,
        size_class=size_class,
        font_color_hex=font_color_hex,
    )


class _FakeOcrBackend:
    name = "fake"

    def __init__(self, detections_by_frame: dict | None = None) -> None:
        self._map = detections_by_frame or {}

    def detect(self, image_path: str, *, frame_t_s: float) -> list[FrameDetection]:
        return list(self._map.get(image_path, []))


def _det(text: str, t: float, x: float = 0.5, y: float = 0.5) -> FrameDetection:
    return FrameDetection(
        frame_t_s=t,
        text=text,
        polygon=_make_polygon(x, y),
        confidence=1.0,
    )


class _MockAlignmentAgent:
    """Always returns input phrases unchanged."""

    def run(self, input, *, ctx=None):  # noqa: A002
        from app.agents._schemas.text_alignment import TextAlignmentOutput  # noqa: PLC0415

        return TextAlignmentOutput(phrases=input.phrases, dropped_count=0)


class _MockClassificationAgent:
    """Classifies every phrase with default values."""

    def __init__(self, raise_on_run: Exception | None = None) -> None:
        self._raise = raise_on_run
        self.called = False

    def run(self, input, *, ctx=None):  # noqa: A002
        self.called = True
        if self._raise is not None:
            raise self._raise

        classified = [
            _make_classified_phrase(p.sample_text, p.start_t_s, p.end_t_s) for p in input.phrases
        ]
        return TextClassificationOutput(classified=classified)


class _MockAlignmentAgentRaises:
    """Always raises on run()."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def run(self, input, *, ctx=None):  # noqa: A002
        raise self._exc


# ── run_full_pipeline tests ───────────────────────────────────────────────────


def _run_with_mocks(
    phrases_from_d: list[Phrase],
    *,
    slot_boundaries_s: list[tuple[float, float]] | None = None,
    alignment_agent=None,
    classification_agent=None,
    out_dir: str | None = None,
) -> TemplateTextOutput:
    """Run `run_full_pipeline` with stage A-D mocked to produce `phrases_from_d`.

    Agents (E and F) are passed via the `alignment_client` / `classification_client`
    parameters but the actual instances are injected by patching the agent classes
    at their definition site — the lazy imports in `run_full_pipeline` import the
    *class*, so patching the class ensures our mock instance is constructed.
    """
    if alignment_agent is None:
        alignment_agent = _MockAlignmentAgent()
    if classification_agent is None:
        classification_agent = _MockClassificationAgent()

    frames = [ExtractedFrame(path="/f/1.jpg", t_s=0.0)]
    backend = _FakeOcrBackend({"/f/1.jpg": [_det("placeholder", 0.0)]})

    managed_dir = out_dir or tempfile.mkdtemp(prefix="nova-test-e2e-")

    with (
        patch(
            "app.pipeline.text_overlay_v2.pipeline.extract_frames",
            return_value=frames,
        ),
        patch(
            "app.pipeline.text_overlay_v2.pipeline.reconstruct_phrases",
            return_value=phrases_from_d,
        ),
        # Patch the agent classes at their source — the lazy imports in pipeline.py
        # import and instantiate these classes, so patching the class makes the
        # constructed instance become our mock.
        patch(
            "app.agents.text_alignment.TextAlignmentAgent",
            return_value=alignment_agent,
        ),
        patch(
            "app.agents.text_classification.TextClassificationAgent",
            return_value=classification_agent,
        ),
    ):
        return run_full_pipeline(
            "/fake/video.mp4",
            backend=backend,
            slot_boundaries_s=slot_boundaries_s or [],
            out_dir=managed_dir,
            ocr_concurrency=1,
        )


def test_run_pipeline_happy_path() -> None:
    """Full A→B→C→D→E→F→G run returns a non-empty TemplateTextOutput."""
    phrases = [
        _make_phrase("It's not just luck", start_t_s=0.5, end_t_s=2.0),
        _make_phrase("if you put in", start_t_s=2.5, end_t_s=4.0),
    ]
    slot_boundaries = [(0.0, 3.0), (3.0, 6.0)]

    result = _run_with_mocks(phrases, slot_boundaries_s=slot_boundaries)

    assert isinstance(result, TemplateTextOutput)
    assert len(result.overlays) == 2
    assert result.overlays[0].sample_text == "It's not just luck"
    assert result.overlays[1].sample_text == "if you put in"


def test_run_pipeline_dedups_duplicates_created_by_stage_e() -> None:
    """text_alignment can map several distinct OCR phrases to the same
    transcript word when the transcript word count is smaller than the OCR
    phrase count. The post-Stage-E dedup collapses those before classify.

    Reproduces the failure shape from prod template fdaf3bbc reanalyze on
    2026-05-21: alignment emitted "allow" 3× at 9.5-10.0, "anyone" 4× at
    9.5-10.5, and "combination" 4× at 6.5-8.0 from a CLEAN Stage-D input.
    """
    from app.agents._schemas.text_alignment import TextAlignmentOutput

    d_phrases = [
        _make_phrase("allow", 9.5, 10.0),
        _make_phrase("anyone", 9.5, 10.0),
        _make_phrase("diminish", 9.5, 10.0),
        _make_phrase("combination", 7.5, 7.5),
        _make_phrase("of", 7.5, 7.5),
    ]
    # The LLM, given a shorter transcript, repeats the same word for distinct
    # input phrases at overlapping timestamps.
    aligned_with_dupes = [
        _make_phrase("allow", 9.5, 10.0),
        _make_phrase("allow", 9.5, 10.0),
        _make_phrase("allow", 9.5, 10.0),
        _make_phrase("combination", 7.5, 7.5),
        _make_phrase("combination", 7.5, 7.5),
    ]

    class _DuplicatingAlignment:
        def run(self, input, *, ctx=None):  # noqa: A002
            return TextAlignmentOutput(phrases=aligned_with_dupes, dropped_count=0)

    result = _run_with_mocks(d_phrases, alignment_agent=_DuplicatingAlignment())

    sample_texts = sorted(o.sample_text for o in result.overlays)
    assert sample_texts == ["allow", "combination"], (
        f"post-Stage-E dedup should collapse aligned duplicates to one per word; got {sample_texts}"
    )


def test_run_pipeline_empty_phrases_skips_e_and_f() -> None:
    """When stage D produces no phrases, E and F are not called."""
    e_called = []
    f_called = []

    class _TrackingAlignmentAgent:
        def run(self, input, *, ctx=None):  # noqa: A002
            e_called.append(True)
            from app.agents._schemas.text_alignment import TextAlignmentOutput  # noqa: PLC0415

            return TextAlignmentOutput(phrases=[], dropped_count=0)

    class _TrackingClassificationAgent:
        def run(self, input, *, ctx=None):  # noqa: A002
            f_called.append(True)

            return TextClassificationOutput(classified=[])

    result = _run_with_mocks(
        [],  # D returns no phrases
        alignment_agent=_TrackingAlignmentAgent(),
        classification_agent=_TrackingClassificationAgent(),
    )

    assert isinstance(result, TemplateTextOutput)
    assert result.overlays == []
    # E and F must NOT be called when D returns nothing
    assert e_called == [], "E was called despite empty phrase list"
    assert f_called == [], "F was called despite empty phrase list"


def test_run_pipeline_alignment_failure_propagates() -> None:
    """Stage E raises → orchestrator wraps in TerminalError with stage context."""
    phrases = [_make_phrase("hello")]
    exc = RuntimeError("LLM exploded")

    with pytest.raises(TerminalError, match="stage=alignment"):
        _run_with_mocks(
            phrases,
            alignment_agent=_MockAlignmentAgentRaises(exc),
        )


def test_run_pipeline_classification_failure_propagates() -> None:
    """Stage F raises → orchestrator wraps in TerminalError with stage context."""
    phrases = [_make_phrase("hello")]

    class _ExplodingClassification:
        def run(self, input, *, ctx=None):  # noqa: A002
            raise ValueError("classification kaboom")

    with pytest.raises(TerminalError, match="stage=classification"):
        _run_with_mocks(
            phrases,
            classification_agent=_ExplodingClassification(),
        )


def test_run_pipeline_cleans_up_frames_on_success() -> None:
    """Scratch dir is removed after a successful run.

    We spy on shutil.rmtree to capture which path was cleaned up, rather than
    creating a real dir and checking its existence (which would require the
    pipeline's own tempfile.mkdtemp to return our known path).
    """
    phrases = [_make_phrase("hello")]
    removed_paths: list[str] = []

    original_rmtree = shutil.rmtree

    def _spy_rmtree(path, **kwargs):
        removed_paths.append(str(path))
        original_rmtree(path, **kwargs)

    with patch("app.pipeline.text_overlay_v2.pipeline.shutil.rmtree", side_effect=_spy_rmtree):
        _run_with_mocks_no_out_dir(phrases)

    # rmtree was called at least once (for the managed scratch dir).
    assert len(removed_paths) >= 1, "shutil.rmtree was never called — frames not cleaned up"
    # All paths cleaned up should look like nova-frames- tempdir paths.
    assert any("nova-frames-" in p for p in removed_paths), (
        f"Expected a nova-frames-* path to be cleaned up, got: {removed_paths}"
    )


def test_run_pipeline_cleans_up_frames_on_failure() -> None:
    """Scratch dir is removed even when stage F raises."""

    class _ExplodingF:
        def run(self, input, *, ctx=None):  # noqa: A002
            raise RuntimeError("F explodes for cleanup test")

    phrases = [_make_phrase("hello")]
    removed_paths: list[str] = []

    original_rmtree = shutil.rmtree

    def _spy_rmtree(path, **kwargs):
        removed_paths.append(str(path))
        original_rmtree(path, **kwargs)

    with patch("app.pipeline.text_overlay_v2.pipeline.shutil.rmtree", side_effect=_spy_rmtree):
        with pytest.raises(TerminalError, match="stage=classification"):
            _run_with_mocks_no_out_dir(phrases, classification_agent=_ExplodingF())

    assert len(removed_paths) >= 1, "shutil.rmtree was never called — frames leaked on failure"
    assert any("nova-frames-" in p for p in removed_paths), (
        f"Expected a nova-frames-* path to be cleaned up on failure, got: {removed_paths}"
    )


def _run_with_mocks_no_out_dir(
    phrases_from_d: list[Phrase],
    *,
    slot_boundaries_s: list[tuple[float, float]] | None = None,
    alignment_agent=None,
    classification_agent=None,
) -> TemplateTextOutput:
    """Same as _run_with_mocks but does NOT pass out_dir so the pipeline manages cleanup."""
    if alignment_agent is None:
        alignment_agent = _MockAlignmentAgent()
    if classification_agent is None:
        classification_agent = _MockClassificationAgent()

    frames = [ExtractedFrame(path="/f/1.jpg", t_s=0.0)]
    backend = _FakeOcrBackend({"/f/1.jpg": [_det("placeholder", 0.0)]})

    with (
        patch(
            "app.pipeline.text_overlay_v2.pipeline.extract_frames",
            return_value=frames,
        ),
        patch(
            "app.pipeline.text_overlay_v2.pipeline.reconstruct_phrases",
            return_value=phrases_from_d,
        ),
        patch(
            "app.agents.text_alignment.TextAlignmentAgent",
            return_value=alignment_agent,
        ),
        patch(
            "app.agents.text_classification.TextClassificationAgent",
            return_value=classification_agent,
        ),
    ):
        return run_full_pipeline(
            "/fake/video.mp4",
            backend=backend,
            slot_boundaries_s=slot_boundaries_s or [],
            # No out_dir — pipeline manages its own scratch dir.
            ocr_concurrency=1,
        )


def test_run_pipeline_slot_index_assignment() -> None:
    """Phrases at known timestamps get the correct slot_index based on slot_boundaries_s."""
    # Two slots: slot 1 = [0.0, 3.0), slot 2 = [3.0, 6.0)
    slot_boundaries = [(0.0, 3.0), (3.0, 6.0)]
    phrases = [
        _make_phrase("slot one phrase", start_t_s=1.0, end_t_s=2.5),  # → slot 1
        _make_phrase("slot two phrase", start_t_s=4.0, end_t_s=5.5),  # → slot 2
    ]

    result = _run_with_mocks(phrases, slot_boundaries_s=slot_boundaries)

    assert len(result.overlays) == 2
    slot_one = next(o for o in result.overlays if o.sample_text == "slot one phrase")
    slot_two = next(o for o in result.overlays if o.sample_text == "slot two phrase")
    assert slot_one.slot_index == 1
    assert slot_two.slot_index == 2

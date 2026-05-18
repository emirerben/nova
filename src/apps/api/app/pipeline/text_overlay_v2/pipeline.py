"""End-to-end stages A→B→C→D for the Layer-2 text-overlay pipeline.

`run_phrase_pipeline(video_path, ...)` returns the list of phrases visible
on screen, in temporal order, with line-stacking matching what a human reads.
This is the first slice of the pipeline that can be pointed at a real video.

Stages E (transcript alignment, fixes OCR character errors) and F
(classification, picks effect / role / size_class / font_color_hex per
phrase) come in the next slice. The integration with
`template_text_extraction._merge_overlays_into_slots` lands in the final
slice — gated by `settings.text_overlay_v2_enabled` (default OFF).

OCR is parallelized across frames via a bounded thread pool. The default
concurrency cap (10) matches the design doc's Cloud Vision quota budget;
local Apple Vision can go higher but the cap keeps prod safe by default.

Two public entry points:

- `run_phrase_pipeline(video_path, ...)`: ffmpeg-driven, runs the whole
  chain end to end. The production entry point.
- `run_phrase_pipeline_from_frames(frames, ...)`: takes pre-extracted
  frames and runs B→D. Used by unit tests so the orchestrator's
  control flow can be exercised without ffmpeg, and by eval harnesses
  that want to inspect intermediate state.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import structlog

from app.agents._schemas.text_overlay_ocr import FrameDetection
from app.agents._schemas.text_overlay_pipeline import Phrase
from app.pipeline.text_overlay_v2.grouping import (
    DEFAULT_IOU_MATCH,
    DEFAULT_JITTER_MAX_GAP_S,
    group_detections_into_events,
)
from app.pipeline.text_overlay_v2.phrases import (
    DEFAULT_X_BAND_THRESHOLD,
    reconstruct_phrases,
)
from app.services.text_overlay_ocr import OcrBackend, default_backend
from app.services.video_frames import (
    DEFAULT_FPS,
    ExtractedFrame,
    extract_frames,
    frames_workdir,
)

log = structlog.get_logger()

DEFAULT_OCR_CONCURRENCY = 10


def run_phrase_pipeline(
    video_path: str | Path,
    *,
    backend: OcrBackend | None = None,
    fps: float = DEFAULT_FPS,
    out_dir: str | Path | None = None,
    ocr_concurrency: int = DEFAULT_OCR_CONCURRENCY,
    iou_match_threshold: float = DEFAULT_IOU_MATCH,
    jitter_max_gap_s: float = DEFAULT_JITTER_MAX_GAP_S,
    x_band_threshold: float = DEFAULT_X_BAND_THRESHOLD,
) -> list[Phrase]:
    """Run stages A→B→C→D on a single video file.

    Steps:
      1. Extract frames at `fps` to `out_dir` (or a tempdir if None).
      2. Run OCR on each frame in parallel (capped at `ocr_concurrency`).
      3. Group consecutive same-text detections into events.
      4. Cluster events into phrases.

    If `backend` is None, `default_backend()` is used — Cloud Vision in
    prod (when `google-cloud-vision` is installed AND a GCS-style
    credential env var is set) or Apple Vision in local-dev macOS.

    `out_dir=None` cleans the frame JPEGs up on return. Pass an explicit
    path to keep them for debugging or downstream eval inspection.
    """
    if backend is None:
        backend = default_backend()
    log.info(
        "phrase_pipeline_start",
        video=str(video_path),
        backend=backend.name,
        fps=fps,
    )
    with frames_workdir(out_dir) as work_dir:
        frames = extract_frames(video_path, out_dir=work_dir, fps=fps)
        detections = _ocr_frames(frames, backend=backend, max_workers=ocr_concurrency)
        events = group_detections_into_events(
            detections,
            iou_match_threshold=iou_match_threshold,
            jitter_max_gap_s=jitter_max_gap_s,
        )
        phrases = reconstruct_phrases(events, x_band_threshold=x_band_threshold)
    log.info(
        "phrase_pipeline_done",
        video=str(video_path),
        n_frames=len(frames),
        n_detections=len(detections),
        n_events=len(events),
        n_phrases=len(phrases),
    )
    return phrases


def run_phrase_pipeline_from_frames(
    frames: list[ExtractedFrame],
    *,
    backend: OcrBackend,
    ocr_concurrency: int = DEFAULT_OCR_CONCURRENCY,
    iou_match_threshold: float = DEFAULT_IOU_MATCH,
    jitter_max_gap_s: float = DEFAULT_JITTER_MAX_GAP_S,
    x_band_threshold: float = DEFAULT_X_BAND_THRESHOLD,
) -> list[Phrase]:
    """Run stages B→C→D on already-extracted frames.

    Splitting this from `run_phrase_pipeline()` lets unit tests exercise
    the orchestrator's control flow (concurrency, error handling, stage
    chaining) without depending on ffmpeg being present. Also useful in
    eval harnesses that pre-extract frames once and want to A/B different
    OCR backends or threshold values cheaply.

    `backend` is required here — `default_backend()` is only the default
    for the full pipeline, not the from-frames helper.
    """
    detections = _ocr_frames(frames, backend=backend, max_workers=ocr_concurrency)
    events = group_detections_into_events(
        detections,
        iou_match_threshold=iou_match_threshold,
        jitter_max_gap_s=jitter_max_gap_s,
    )
    return reconstruct_phrases(events, x_band_threshold=x_band_threshold)


def _ocr_frames(
    frames: list[ExtractedFrame],
    *,
    backend: OcrBackend,
    max_workers: int,
) -> list[FrameDetection]:
    """OCR every frame in parallel, flatten into a single detection list.

    Frame-level order does not matter to grouping — each detection carries
    its own `frame_t_s` and grouping sorts internally. Using
    `as_completed` lets the slowest frames not block faster ones.

    `max_workers=1` falls back to sequential to keep deterministic order
    in tests that mock the backend.
    """
    if not frames:
        return []
    if max_workers <= 1:
        return [det for f in frames for det in backend.detect(f.path, frame_t_s=f.t_s)]

    detections: list[FrameDetection] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(backend.detect, f.path, frame_t_s=f.t_s) for f in frames]
        for fut in concurrent.futures.as_completed(futures):
            detections.extend(fut.result())
    return detections

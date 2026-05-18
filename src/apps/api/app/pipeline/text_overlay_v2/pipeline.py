"""End-to-end Layer-2 text-overlay pipeline: stages A→B→C→D (phrase extraction)
and (slice G) A→B→C→D→E→F→G (full pipeline returning `TemplateTextOutput`).

Slice G wires stages E (transcript alignment) and F (classification) into the
orchestrator and adds `run_full_pipeline()` which returns a `TemplateTextOutput`
matching the Layer-1 schema — enabling flag-gated routing in
`TemplateTextAgent.run()` without any downstream schema changes.

OCR is parallelized across frames via a bounded thread pool. The default
concurrency cap (10) matches the design doc's Cloud Vision quota budget;
local Apple Vision can go higher but the cap keeps prod safe by default.

Public entry points:

- `run_full_pipeline(video_path, ...)`: A→B→C→D→E→F→G. Returns
  `TemplateTextOutput`. The Layer-2 production entry point called by
  `TemplateTextAgent._run_layer2()` when `text_overlay_v2_enabled=True`.
- `run_phrase_pipeline(video_path, ...)`: A→B→C→D only. Returns
  `list[Phrase]`. Used by eval harnesses that inspect intermediate state.
- `run_phrase_pipeline_from_frames(frames, ...)`: B→C→D on pre-extracted
  frames. Used by unit tests to exercise orchestrator control flow without
  ffmpeg.

Frame JPEG lifecycle:
  - `run_full_pipeline` creates a scratch dir for the duration of the run.
  - Frames are preserved through stage F (TextClassificationAgent reads them
    as inline JPEG Parts for multi-image classification).
  - The scratch dir is removed in a try/finally block — on both success and
    failure — so no frames are ever leaked.
  - Callers can override this by passing `out_dir` to keep frames for debug.
"""

from __future__ import annotations

import concurrent.futures
import json
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from app.agents._runtime import TerminalError
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

if TYPE_CHECKING:
    from app.agents._schemas.template_text import TemplateTextOutput
    from app.agents._schemas.text_alignment import TranscriptWord

log = structlog.get_logger()

DEFAULT_OCR_CONCURRENCY = 10


# ── Full A→B→C→D→E→F→G pipeline (slice G) ───────────────────────────────────


def run_full_pipeline(
    video_path: str | Path,
    *,
    transcript_words: list[TranscriptWord] | None = None,
    slot_boundaries_s: list[tuple[float, float]] | None = None,
    backend: OcrBackend | None = None,
    fps: float = DEFAULT_FPS,
    out_dir: str | Path | None = None,
    ocr_concurrency: int = DEFAULT_OCR_CONCURRENCY,
    iou_match_threshold: float = DEFAULT_IOU_MATCH,
    jitter_max_gap_s: float = DEFAULT_JITTER_MAX_GAP_S,
    x_band_threshold: float = DEFAULT_X_BAND_THRESHOLD,
    template_id: str | None = None,
    job_id: str | None = None,
    alignment_client=None,
    classification_client=None,
    dump_stages_dir: str | Path | None = None,
) -> TemplateTextOutput:
    """Run the full A→B→C→D→E→F→G pipeline on a local video file.

    Stages:
      A: extract frames at `fps` into a scratch dir
      B: OCR each frame in parallel (capped at `ocr_concurrency`)
      C: group detections into temporal text events
      D: cluster events into phrases
      E: align OCR text to the audio transcript (corrects character errors)
      F: classify each phrase (effect / role / size_class / font_color_hex)
      G: convert classified phrases to `TemplateTextOutput` (Layer-1 schema)

    Frame JPEG lifecycle: frames are kept in the scratch dir until after F
    so the classification agent can read them as inline image Parts. The
    scratch dir is removed in a try/finally block regardless of success or
    failure — no frames leak.

    Pass `out_dir` to override the auto-cleanup scratch dir (useful for
    debugging and eval harnesses that want to inspect frames post-run).

    `transcript_words` is the per-word transcript for the video. When empty
    or None, stage E skips the LLM call and returns phrases unchanged (the
    `TextAlignmentAgent` short-circuits on empty transcript input). No
    transcript is required for the pipeline to produce output; the alignment
    stage degrades gracefully.

    `slot_boundaries_s` is a list of (start_s, end_s) tuples derived from
    the template recipe. Each phrase is assigned to the slot whose window
    contains its `start_t_s`. Phrases before the first slot or after the
    last slot are assigned to slot 1 or the last slot respectively.

    `dump_stages_dir` is a debugging hook. When set, the per-stage output of
    A/B/C/D/E/F/G is written as JSON into that directory (`stage_a.json` …
    `stage_g.json`). Stage E also writes `stage_e_dropped.txt` with the
    alignment drop count. Used by `scripts/debug_layer2.py` to attribute
    overlay errors to a specific stage; safe to leave None in production.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RunContext  # noqa: PLC0415
    from app.agents._schemas.template_text import TemplateTextOutput  # noqa: PLC0415
    from app.agents._schemas.text_alignment import TextAlignmentInput  # noqa: PLC0415
    from app.agents._schemas.text_classification import TextClassificationInput  # noqa: PLC0415
    from app.agents.text_alignment import TextAlignmentAgent  # noqa: PLC0415
    from app.agents.text_classification import TextClassificationAgent  # noqa: PLC0415

    if backend is None:
        backend = default_backend()
    if transcript_words is None:
        transcript_words = []
    if slot_boundaries_s is None:
        slot_boundaries_s = []

    ctx = RunContext(job_id=job_id)
    alignment_agent = TextAlignmentAgent(alignment_client or default_client())
    classification_agent = TextClassificationAgent(classification_client or default_client())

    dump_dir: Path | None = None
    if dump_stages_dir is not None:
        dump_dir = Path(dump_stages_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)

    log.info(
        "full_pipeline_start",
        video=str(video_path),
        backend=backend.name,
        fps=fps,
        n_transcript_words=len(transcript_words),
        n_slots=len(slot_boundaries_s),
        job_id=job_id,
        template_id=template_id,
    )

    # ── Scratch dir: kept for E→F, removed in finally ──────────────────────
    _managed_dir: str | None = None
    if out_dir is None:
        _managed_dir = tempfile.mkdtemp(prefix="nova-frames-")
        work_dir = Path(_managed_dir)
    else:
        work_dir = Path(out_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── A: Frame extraction ────────────────────────────────────────────
        log.info("full_pipeline_stage_a_start", job_id=job_id)
        frames = extract_frames(video_path, out_dir=work_dir, fps=fps)
        log.info("full_pipeline_stage_a_done", n_frames=len(frames), job_id=job_id)
        _dump_stage(dump_dir, "stage_a", [{"path": str(f.path), "t_s": f.t_s} for f in frames])

        # ── B: Per-frame OCR ──────────────────────────────────────────────
        log.info("full_pipeline_stage_b_start", n_frames=len(frames), job_id=job_id)
        detections = _ocr_frames(frames, backend=backend, max_workers=ocr_concurrency)
        log.info("full_pipeline_stage_b_done", n_detections=len(detections), job_id=job_id)
        _dump_stage(dump_dir, "stage_b", detections)

        # ── C: Temporal grouping ──────────────────────────────────────────
        log.info("full_pipeline_stage_c_start", job_id=job_id)
        events = group_detections_into_events(
            detections,
            iou_match_threshold=iou_match_threshold,
            jitter_max_gap_s=jitter_max_gap_s,
        )
        log.info("full_pipeline_stage_c_done", n_events=len(events), job_id=job_id)
        _dump_stage(dump_dir, "stage_c", events)

        # ── D: Phrase reconstruction ──────────────────────────────────────
        log.info("full_pipeline_stage_d_start", job_id=job_id)
        phrases = reconstruct_phrases(events, x_band_threshold=x_band_threshold)
        log.info("full_pipeline_stage_d_done", n_phrases=len(phrases), job_id=job_id)
        _dump_stage(dump_dir, "stage_d", phrases)

        if not phrases:
            log.info(
                "full_pipeline_no_phrases_early_return",
                job_id=job_id,
                template_id=template_id,
            )
            return TemplateTextOutput(overlays=[])

        # ── Build frame_paths mapping for stage F ─────────────────────────
        # Map each phrase to the closest extracted frame path (by timestamp).
        frame_paths = _build_frame_paths(phrases, frames)

        # ── E: Transcript alignment ───────────────────────────────────────
        log.info(
            "full_pipeline_stage_e_start",
            n_phrases=len(phrases),
            n_transcript_words=len(transcript_words),
            job_id=job_id,
        )
        try:
            alignment_input = TextAlignmentInput(
                phrases=phrases,
                transcript_words=transcript_words,
                template_id=template_id,
            )
            alignment_output = alignment_agent.run(alignment_input, ctx=ctx)
        except Exception as exc:
            raise TerminalError(f"stage=alignment: {exc}") from exc
        aligned_phrases = alignment_output.phrases
        log.info(
            "full_pipeline_stage_e_done",
            n_aligned=len(aligned_phrases),
            n_dropped=alignment_output.dropped_count,
            job_id=job_id,
        )
        _dump_stage(dump_dir, "stage_e", aligned_phrases)
        if dump_dir is not None:
            (dump_dir / "stage_e_dropped.txt").write_text(
                f"dropped_count={alignment_output.dropped_count}\n"
            )

        if not aligned_phrases:
            log.info(
                "full_pipeline_no_aligned_phrases_early_return",
                job_id=job_id,
                template_id=template_id,
            )
            return TemplateTextOutput(overlays=[])

        # Rebuild frame_paths after E may have dropped phrases (indices shift).
        # Re-derive by position: aligned phrase i corresponds to the i-th
        # surviving phrase; carry forward the frame path from the original.
        # Since TextAlignmentAgent preserves phrase ordering and only drops
        # (never reorders), we can match by (start_t_s, sample_text).
        aligned_frame_paths = _remap_frame_paths(aligned_phrases, phrases, frame_paths)

        # ── F: Classification ────────────────────────────────────────────
        log.info(
            "full_pipeline_stage_f_start",
            n_phrases=len(aligned_phrases),
            job_id=job_id,
        )
        try:
            classification_input = TextClassificationInput(
                phrases=aligned_phrases,
                frame_paths=aligned_frame_paths,
                template_id=template_id,
            )
            classification_output = classification_agent.run(classification_input, ctx=ctx)
        except Exception as exc:
            raise TerminalError(f"stage=classification: {exc}") from exc
        log.info(
            "full_pipeline_stage_f_done",
            n_classified=len(classification_output.classified),
            job_id=job_id,
        )
        _dump_stage(dump_dir, "stage_f", classification_output.classified)

        # ── G: Convert to TemplateTextOutput ─────────────────────────────
        log.info("full_pipeline_stage_g_start", job_id=job_id)
        output = _classified_phrases_to_output(
            classification_output.classified,
            slot_boundaries_s=slot_boundaries_s,
        )
        log.info(
            "full_pipeline_stage_g_done",
            n_overlays=len(output.overlays),
            job_id=job_id,
            template_id=template_id,
        )
        _dump_stage(dump_dir, "stage_g", output)
        return output

    finally:
        if _managed_dir is not None:
            try:
                shutil.rmtree(_managed_dir, ignore_errors=True)
                log.debug("full_pipeline_frames_cleaned", job_id=job_id)
            except Exception:  # noqa: BLE001
                pass


# ── Phrase-only A→B→C→D pipeline (original) ─────────────────────────────────


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


# ── Internal helpers ──────────────────────────────────────────────────────────


def _dump_stage(dir_path: Path | None, stage_name: str, payload: Any) -> None:
    """Write one stage's output to `<dir>/<stage_name>.json`.

    Pydantic models (and lists of them) are serialised via `model_dump()`;
    dataclasses fall back to `__dict__`; everything else goes through
    `default=str`. Dump failures are swallowed — instrumentation must never
    break the pipeline.
    """
    if dir_path is None:
        return

    def _coerce(obj: Any) -> Any:
        if isinstance(obj, list):
            return [_coerce(x) for x in obj]
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return obj

    try:
        with (dir_path / f"{stage_name}.json").open("w") as fh:
            json.dump(_coerce(payload), fh, indent=2, default=str)
    except Exception as exc:  # noqa: BLE001
        log.warning("dump_stage_failed", stage=stage_name, error=str(exc))


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


def _build_frame_paths(
    phrases: list[Phrase],
    frames: list[ExtractedFrame],
) -> dict[int, Path]:
    """Map each phrase index to the nearest extracted frame by timestamp.

    For each phrase, finds the frame whose `t_s` is closest to the phrase's
    `start_t_s`. Returns a dict of {phrase_index: Path}.

    When `frames` is empty, returns an empty dict (classification agent
    handles missing frame paths gracefully by falling back to text-only).
    """
    if not frames:
        return {}
    sorted_frames = sorted(frames, key=lambda f: f.t_s)
    result: dict[int, Path] = {}
    for i, phrase in enumerate(phrases):
        # Binary-search-style: find nearest frame by start_t_s.
        best = min(sorted_frames, key=lambda f, t=phrase.start_t_s: abs(f.t_s - t))
        result[i] = Path(best.path)
    return result


def _remap_frame_paths(
    aligned: list[Phrase],
    original: list[Phrase],
    original_frame_paths: dict[int, Path],
) -> dict[int, Path]:
    """Rebuild a phrase_index → frame_path mapping after stage E may have dropped phrases.

    TextAlignmentAgent preserves phrase ordering and only omits phrases
    (never reorders them). We match each surviving aligned phrase to its
    original index by identity (start_t_s + sample_text), then carry
    forward the frame path under the new 0-based index.

    Falls back to an empty dict if matching fails (classification agent
    degrades gracefully without frame paths).
    """
    # Build a lookup: (start_t_s, sample_text) → original index
    original_lookup: dict[tuple[float, str], int] = {}
    for orig_idx, orig_phrase in enumerate(original):
        key = (orig_phrase.start_t_s, orig_phrase.sample_text)
        original_lookup[key] = orig_idx

    result: dict[int, Path] = {}
    for new_idx, phrase in enumerate(aligned):
        key = (phrase.start_t_s, phrase.sample_text)
        orig_idx = original_lookup.get(key)
        if orig_idx is not None:
            frame_path = original_frame_paths.get(orig_idx)
            if frame_path is not None:
                result[new_idx] = frame_path
    return result


def _assign_slot_index(
    phrase_start_s: float,
    slot_boundaries_s: list[tuple[float, float]],
) -> int:
    """Return the 1-indexed slot whose window contains `phrase_start_s`.

    Assignment rule: a phrase belongs to the slot whose start_s ≤
    phrase_start_s < end_s. If the phrase starts before the first slot,
    it is assigned to slot 1. If it starts at or after the last slot's
    end, it is assigned to the last slot.

    When `slot_boundaries_s` is empty, returns 1.
    """
    if not slot_boundaries_s:
        return 1
    # Find the last slot whose start is ≤ phrase_start_s.
    best_slot = 1
    for i, (start_s, end_s) in enumerate(slot_boundaries_s, start=1):
        if phrase_start_s >= start_s:
            best_slot = i
        else:
            break
    return best_slot


def _classified_phrases_to_output(
    classified: list,
    *,
    slot_boundaries_s: list[tuple[float, float]],
) -> TemplateTextOutput:
    """Stage G: convert `ClassifiedPhrase` list to `TemplateTextOutput`.

    Each `ClassifiedPhrase` is mapped to a `TemplateTextOverlay` with:
      - `slot_index` derived from `phrase.start_t_s` + `slot_boundaries_s`
      - `sample_text` = phrase.sample_text (newline-joined lines)
      - `start_s` / `end_s` from phrase timing
      - `bbox` from phrase aabb (converted: aabb is (x_min, y_min, x_max, y_max)
        in normalised coords; TextBBox wants center + extent)
      - `font_color_hex`, `effect`, `role`, `size_class` from classification
      - `bbox.sample_frame_t` = phrase.start_t_s (closest frame we have)

    Invalid overlays (e.g. zero-area bbox) are skipped with a warning.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    from app.agents._schemas.template_text import (  # noqa: PLC0415
        TemplateTextOutput,
        TemplateTextOverlay,
        TextBBox,
    )

    overlays: list[TemplateTextOverlay] = []
    for i, cp in enumerate(classified):
        phrase = cp.phrase
        x_min, y_min, x_max, y_max = phrase.aabb

        # Convert axis-aligned bbox to center + extent.
        x_norm = (x_min + x_max) / 2.0
        y_norm = (y_min + y_max) / 2.0
        w_norm = x_max - x_min
        h_norm = y_max - y_min

        # Guard: degenerate bboxes (zero area) would fail TextBBox validation.
        if w_norm <= 0.0 or h_norm <= 0.0:
            log.warning(
                "full_pipeline_degenerate_bbox_skipped",
                phrase_index=i,
                sample_text=phrase.sample_text[:60],
                aabb=phrase.aabb,
            )
            continue

        # Clamp to valid range (OCR polygons can occasionally exceed unit square).
        w_norm = min(w_norm, 1.0)
        h_norm = min(h_norm, 1.0)
        x_norm = max(w_norm / 2.0, min(1.0 - w_norm / 2.0, x_norm))
        y_norm = max(h_norm / 2.0, min(1.0 - h_norm / 2.0, y_norm))

        # end_s must be > start_s for TemplateTextOverlay validation.
        start_s = phrase.start_t_s
        end_s = max(phrase.end_t_s, start_s + 0.01)

        slot_index = _assign_slot_index(start_s, slot_boundaries_s)

        try:
            overlay = TemplateTextOverlay(
                slot_index=slot_index,
                sample_text=phrase.sample_text,
                start_s=start_s,
                end_s=end_s,
                bbox=TextBBox(
                    x_norm=x_norm,
                    y_norm=y_norm,
                    w_norm=w_norm,
                    h_norm=h_norm,
                    sample_frame_t=start_s,
                ),
                font_color_hex=cp.font_color_hex,
                effect=cp.effect,
                role=cp.role,
                size_class=cp.size_class,
            )
        except (ValidationError, ValueError) as exc:
            log.warning(
                "full_pipeline_overlay_validation_failed",
                phrase_index=i,
                sample_text=phrase.sample_text[:60],
                error=str(exc),
            )
            continue

        overlays.append(overlay)

    return TemplateTextOutput(overlays=overlays)

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
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from app.agents._runtime import TerminalError
from app.agents._schemas.text_overlay_ocr import FrameDetection
from app.agents._schemas.text_overlay_pipeline import Phrase
from app.pipeline.text_overlay_v2.constants import (
    LAYER2_RENDER_TEXT_SIZE,
    LAYER2_RENDER_TEXT_SIZE_PX,
)
from app.pipeline.text_overlay_v2.grouping import (
    DEFAULT_IOU_MATCH,
    DEFAULT_JITTER_MAX_GAP_S,
    group_detections_into_events,
)
from app.pipeline.text_overlay_v2.line_grouping import (
    LineGroup,
    build_line_groups,
)
from app.pipeline.text_overlay_v2.phrases import (
    ATOMIZED_DEDUP_GAP_THRESHOLD_S,
    DEFAULT_X_BAND_THRESHOLD,
    dedup_overlapping_atomized_phrases,
    reconstruct_phrases,
)
from app.pipeline.text_overlay_v2.tokenize import tokenize_detections_into_words
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
    atomize_words: bool = True,
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

    `atomize_words=True` (default) tokenizes each OCR detection into per-word
    sub-detections at stage B's output, then forces stage D into
    `atomize_per_event` mode so each word becomes its own overlay with its
    own first-seen `start_s`. This matches viral short-form templates whose
    text builds up word-by-word with individual pop-in animations
    (canary: `not just luck` / template `fdaf3bbc-…`). Set `False` to fall
    back to phrase-level overlays (one overlay per visually-coherent
    multi-line block) — useful for templates with a single stable headline
    rather than a word-by-word reveal.
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

        # ── B.5: Word tokenization (when atomize_words=True) ──────────────
        # Splits each multi-word OCR detection into per-word sub-detections so
        # stage C groups same-word detections across frames instead of treating
        # "if" and "if you" as different events that stage D then stacks as
        # redundant lines. See tokenize.py docstring for the full rationale.
        if atomize_words:
            log.info("full_pipeline_stage_b5_start", n_detections=len(detections), job_id=job_id)
            detections = tokenize_detections_into_words(detections)
            log.info(
                "full_pipeline_stage_b5_done",
                n_word_detections=len(detections),
                job_id=job_id,
            )
            _dump_stage(dump_dir, "stage_b5_words", detections)

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
        # When atomize_words=True, each event is already a single word; stage D
        # MUST NOT re-cluster same-line words into multi-line phrases, so we
        # force atomize_per_event so each event becomes its own one-line phrase.
        log.info("full_pipeline_stage_d_start", job_id=job_id)
        phrases = reconstruct_phrases(
            events,
            x_band_threshold=x_band_threshold,
            atomize_per_event=atomize_words,
        )
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
                atomize_mode=atomize_words,
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

        # Stage-E creates duplicates that Stage-D dedup never sees: when the
        # transcript word count is smaller than the OCR phrase count, the LLM
        # maps multiple distinct OCR phrases onto the same transcript word —
        # producing N copies of one word at overlapping timestamps. The same
        # function used at the tail of `reconstruct_phrases` collapses these
        # post-alignment. Only runs in atomized mode because phrase-mode
        # alignment never re-labels (it just rewrites characters within a
        # phrase). Evidence: prod template fdaf3bbc 2026-05-21 reanalyze
        # emitted "allow" 3×, "anyone" 4×, "combination" 4× from a CLEAN
        # 31-phrase Stage-D output paired with a 15-word transcript.
        if atomize_words and len(aligned_phrases) > 1:
            pre_dedup_count = len(aligned_phrases)
            aligned_phrases = dedup_overlapping_atomized_phrases(
                aligned_phrases, gap_threshold_s=ATOMIZED_DEDUP_GAP_THRESHOLD_S
            )
            if len(aligned_phrases) != pre_dedup_count:
                log.info(
                    "full_pipeline_stage_e_post_dedup",
                    pre_dedup=pre_dedup_count,
                    post_dedup=len(aligned_phrases),
                    collapsed=pre_dedup_count - len(aligned_phrases),
                    job_id=job_id,
                )
                _dump_stage(dump_dir, "stage_e_post_dedup", aligned_phrases)

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
        # Build progressive-reveal line groups from aligned phrases + transcript.
        # Atomized phrases whose transcript matches form contiguous sentences
        # become LineGroups; Stage G emits them as cumulative reveal overlays
        # (text_anchor="left", N stages per N words). Phrases not in any
        # group fall through to the existing single-overlay emit unchanged.
        line_groups = build_line_groups(aligned_phrases, transcript_words)
        log.info(
            "full_pipeline_line_groups_built",
            n_groups=len(line_groups),
            n_grouped_phrases=sum(len(g.phrase_indices) for g in line_groups),
            job_id=job_id,
        )
        log.info("full_pipeline_stage_g_start", job_id=job_id)
        output = _classified_phrases_to_output(
            classification_output.classified,
            slot_boundaries_s=slot_boundaries_s,
            line_groups=line_groups,
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


_OVERLAY_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_overlay_text(text: str) -> str:
    """Collapse internal whitespace and strip literal escape sequences.

    `Phrase.sample_text` joins lines with `\\n`. When that string reaches the
    overlay renderer (drawtext/ASS) the literal `\\n` was rendering as on-screen
    text (`/N`) instead of a line break, because the downstream escaping path
    treats it as a control sequence in the font's glyph table. Layer-2's target
    is viral-template overlays where each phrase is one visible block — joining
    lines with a single space gives the right rendered output for the 99% case
    (single-word atomized phrases get unchanged single-word output).

    Also strips known debug-marker suffixes that occasionally leak from
    upstream OCR snippets (`[overlap_truncated]`, stray `\\N`).
    """
    if not text:
        return text
    cleaned = text.replace("[overlap_truncated]", "")
    cleaned = cleaned.replace("\\N", " ").replace("\\n", " ")
    cleaned = _OVERLAY_WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


# Threshold for the Stage G line-split pass. Cumulative text whose measured
# width would exceed this fraction of the canvas gets split into a new
# LineGroup that starts at the overflowing word's transcript time. Matches
# the renderer's `_TEXT_MAX_LINE_W` so a stage emitted by Stage G doesn't
# trigger the renderer's shrink-to-fit safety net.
_CUMULATIVE_LINE_MAX_W_FRAC = 0.90
_CANVAS_W_PX = 1080
_CANVAS_H_PX = 1920

# Layer-2 ships every overlay at the same large size — the classifier's
# per-phrase `size_class` is overridden downstream by the uniform bridge,
# so measuring Pass-1 widths against the (often "small") classifier output
# would under-count rendered width and skip the line-overflow split.
# Measure against the actual rendered size so Pass-1 splits accurately.
# `LAYER2_RENDER_TEXT_SIZE_PX` is imported at module top from the shared
# `constants` module so the bridge and the line-split path cannot drift
# apart again. History: prod template 89cde014 ran with `size_class="small"`
# (36 px) — a 9-word cumulative measured at 36 px fit comfortably under
# 90% canvas, never split, and emitted a single "combination of and good
# timing don't to allow anyone" overlay that physically overflowed the
# 1080-px canvas at the rendered 120 px.

# Line-step renderer constant — match `text_overlay._TEXT_LINE_SPACING`.
# Used to stack split sub-groups vertically inside one LineGroup so two
# sub-groups don't land at the same `line_anchor_y_frac`.
_LINE_SPACING_MULT = 1.15

# Floor on the emitted cumulative-stage duration. `text_reveal.MIN_RENDERABLE_S`
# (0.05 s) is fine for *butted* lyric reveals where a single frame between
# stages is invisible — but Layer-2 emits each stage as an independent
# overlay, so a 50-ms emit becomes a one-frame flash on screen. Bump the
# floor passed into `build_cumulative_stages` so middle stages narrower than
# 0.2 s drop out (their text still appears in the next surviving stage's
# cumulative) and the terminal stage's defensive bridge is wider too.
_CUMULATIVE_MIN_RENDERABLE_S = 0.2

# Minimum on-screen time each word gets before the next word in the same
# cumulative reveal appears. OCR first-seen timestamps cluster badly — a
# coarse frame-sample rate means a frame holding "good timing" stamps BOTH
# words at the same t (prod 89cde014: idx 13-16 "just"/"luck"/"is"/"a" all
# at t=6.50, idx 27-29 "your"/"hard"/"work." all at t=10.00). A cumulative
# reveal driven by clustered times drops the sub-renderable intermediate
# stages, so words pop in 2-4 at a time ("It's not" → "It's not just luck")
# instead of one-by-one — the "jumping" the reveal is supposed to avoid.
# De-spacing pushes each word to at least this gap after the previous reveal,
# preserving naturally well-spaced words and only fixing the clusters. Larger
# than `_CUMULATIVE_MIN_RENDERABLE_S` so every de-spaced stage clears the
# render floor with margin.
_MIN_WORD_REVEAL_STEP_S = 0.30


def _despace_word_starts(
    starts: list[float],
    line_end_s: float,
    *,
    min_step_s: float = _MIN_WORD_REVEAL_STEP_S,
) -> tuple[list[float], float]:
    """Enforce a minimum gap between consecutive cumulative word reveals.

    Walk left-to-right; push each word to at least `min_step_s` after the
    previous (already-placed) reveal. Naturally well-spaced words keep their
    timing; clustered words get spread so each lands its own reveal beat.
    Returns the de-spaced starts plus a `line_end_s` extended so the final
    word still has a full `min_step_s` of screen time before the line clears.

    Monotonic and order-preserving. Empty input returns unchanged.
    """
    if not starts:
        return starts, line_end_s
    spaced = [float(starts[0])]
    for t in starts[1:]:
        spaced.append(max(float(t), spaced[-1] + min_step_s))
    new_line_end = max(line_end_s, spaced[-1] + min_step_s)
    return spaced, new_line_end


def _compute_line_step_norm() -> float:
    """Return per-sub-group vertical offset in canvas-height fraction.

    Lifts `text_overlay`'s ascent+descent line-step calculation out of the
    renderer so the two paths cannot drift. When sub-groups stack, they
    must use the SAME spacing the renderer uses for wrapped lines or the
    sub-group separation will look different from a natural multi-line
    block.

    Falls back to a conservative 0.08 (~150 px / 1920 — roughly correct for
    Playfair Display at 120 px) when font metrics can't be read in test
    environments without Pillow fonts.
    """
    try:
        from app.pipeline.text_overlay import _load_styled_font  # noqa: PLC0415

        font = _load_styled_font(
            "display",
            LAYER2_RENDER_TEXT_SIZE,
            text_size_px=LAYER2_RENDER_TEXT_SIZE_PX,
        )
        if font is None:
            return 0.08
        ascent, descent = font.getmetrics()
        line_step_px = (ascent + descent) * _LINE_SPACING_MULT
        return line_step_px / _CANVAS_H_PX
    except Exception:  # noqa: BLE001
        return 0.08


def _emit_cumulative_line_overlays(
    lg: LineGroup,
    *,
    classified: list,
    slot_boundaries_s: list[tuple[float, float]],
    drops: dict[str, int],
) -> list:
    """Emit N cumulative reveal overlays for a single LineGroup.

    Two passes:
      1. Line-split: walk words left-to-right, measure cumulative width with
         the classified font for THIS group, and split into sub-groups at
         each overflow point. A single oversized word records to
         `drops["single_word_overflow"]` and is emitted as a solo cumulative
         (the renderer's shrink-to-fit will handle it).
      2. Cumulative emit: for each sub-group, call
         `text_reveal.build_cumulative_stages` and emit one
         `TemplateTextOverlay` per stage with `text_anchor="left"`,
         `bbox.x_norm = lg.line_anchor_x_frac`, and the classified font /
         color / role / size_class from the FIRST phrase in the group.

    Empty groups, validation failures, and degenerate inputs are recorded in
    `drops` and skipped silently.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    from app.agents._schemas.template_text import (  # noqa: PLC0415
        TemplateTextOverlay,
        TextBBox,
    )
    from app.pipeline.text_overlay import measure_text_width  # noqa: PLC0415
    from app.pipeline.text_reveal import Word, build_cumulative_stages  # noqa: PLC0415

    if not lg.phrase_indices:
        drops["empty_group"] += 1
        return []

    # Pull the texts and per-word start_s from the line group. Each phrase is
    # atomized (one word). The size_class / effect / font_color come from the
    # FIRST phrase's classification — cumulative stages share one visual style.
    first_cp = classified[lg.phrase_indices[0]]
    size_class = first_cp.size_class
    effect = first_cp.effect
    font_color_hex = first_cp.font_color_hex
    role = first_cp.role

    word_texts: list[str] = []
    for idx in lg.phrase_indices:
        cp = classified[idx]
        text = (cp.phrase.lines[0] if cp.phrase.lines else "").strip()
        if not text:
            text = ""
        word_texts.append(text)

    # Pass 1: line-split by measured cumulative width.
    # Width measurement uses the UNIFORM Layer-2 render size (large/120 px)
    # — not `first_cp.size_class` — because the bridge in
    # `tasks/template_text_extraction._overlay_to_recipe_dict` overrides
    # every Layer-2 overlay's render size to that constant. Measuring at
    # the classifier's `size_class` (often "small"=36 px) under-counts
    # rendered width by ~3.3× and lets long lines escape the split.
    max_w_px = int(_CANVAS_W_PX * _CUMULATIVE_LINE_MAX_W_FRAC)
    sub_groups: list[tuple[list[int], float]] = []  # (local indices into word_texts, line_end_s)
    current: list[int] = []
    for j in range(len(word_texts)):
        candidate = current + [j]
        cumulative_text = " ".join(word_texts[k] for k in candidate if word_texts[k])
        width = measure_text_width(
            cumulative_text,
            text_size=LAYER2_RENDER_TEXT_SIZE,
            text_size_px=LAYER2_RENDER_TEXT_SIZE_PX,
        )
        if width > max_w_px and current:
            # Adding word j overflows. Close current sub-group at j-1, open a
            # new sub-group starting at j. Sub-group's line_end_s is the next
            # word's start_s minus a frame so the prior line clears before
            # the next opens (butted edge).
            line_end_s = lg.word_start_s_list[j]
            sub_groups.append((list(current), line_end_s))
            current = [j]
            # Check if the new singleton ALREADY overflows (a single word
            # wider than 90% canvas). If so, flag it and let it through —
            # the renderer's shrink-to-fit will handle the actual rendering.
            singleton_text = word_texts[j]
            if (
                singleton_text
                and measure_text_width(
                    singleton_text,
                    text_size=LAYER2_RENDER_TEXT_SIZE,
                    text_size_px=LAYER2_RENDER_TEXT_SIZE_PX,
                )
                > max_w_px
            ):
                drops["single_word_overflow"] += 1
        elif width > max_w_px and not current:
            # Very first word of the group is already too wide. Same fallback.
            drops["single_word_overflow"] += 1
            current = [j]
        else:
            current = candidate

    # Close the final sub-group with the group's overall line_end_s.
    if current:
        sub_groups.append((list(current), lg.line_end_s))

    if len(sub_groups) > 1:
        drops["line_overflow"] += len(sub_groups) - 1

    # Per-sub-group vertical step so split sub-groups DON'T render at the
    # same y. Pre-fix, every sub-group used `lg.line_anchor_y_frac` and
    # stacked on top of each other (prod 89cde014 had "THE work to get" and
    # "there just" at identical y=0.44). Reading the renderer's intrinsic
    # ascent+descent keeps the spacing consistent with the natural
    # multi-line wrapping the renderer applies inside a single overlay.
    line_step_norm = _compute_line_step_norm()

    # Pass 2: cumulative emit per sub-group.
    out: list = []
    for sub_group_idx, (sub_indices, sub_line_end_s) in enumerate(sub_groups):
        # De-cluster word reveal times BEFORE building stages so clustered
        # OCR timestamps don't collapse into multi-word pops. Preserves order
        # and well-spaced words; only spreads the clusters.
        sub_texts = [word_texts[k] for k in sub_indices if word_texts[k]]
        sub_starts = [
            float(lg.word_start_s_list[k]) for k in sub_indices if word_texts[k]
        ]
        if not sub_texts:
            drops["empty_group"] += 1
            continue
        spaced_starts, sub_line_end_s = _despace_word_starts(sub_starts, sub_line_end_s)
        words = [
            Word(text=t, start_s=s, end_s=s)  # end_s informational only
            for t, s in zip(sub_texts, spaced_starts, strict=True)
        ]
        if not words:
            drops["empty_group"] += 1
            continue
        # Use a higher floor than `text_reveal.MIN_RENDERABLE_S` because
        # Layer-2 emits each stage as an INDEPENDENT overlay (vs the
        # music-lyric injector where stages are butted into a single ASS
        # block). A 50-ms emit becomes a one-frame flash on screen — drop
        # those middle stages so the cumulative reveal shows readable beats
        # only. The dropped word still appears in the next surviving
        # stage's cumulative text.
        stages = build_cumulative_stages(
            words,
            line_end_s=sub_line_end_s,
            min_renderable_s=_CUMULATIVE_MIN_RENDERABLE_S,
        )
        for stage in stages:
            slot_index = _assign_slot_index(stage.start_s, slot_boundaries_s)
            # Read positional metadata from the LineGroup so the cumulative
            # overlay's bbox reflects where the OCR actually detected the
            # text on screen — NOT a hardcoded canvas-center placeholder.
            # `_bbox_to_named_position(y_norm)` downstream will bucket this
            # to top/center/bottom correctly; without the real y the
            # renderer would default every reveal to canvas center.
            anchor_x = max(0.0, min(1.0, lg.line_anchor_x_frac))
            # Stack sub-groups vertically. The split happens BECAUSE the
            # cumulative text exceeded 90% canvas width — render-time the
            # sub-groups *cannot* share a y or they will visually overlap.
            anchor_y_raw = lg.line_anchor_y_frac + sub_group_idx * line_step_norm
            anchor_y = max(0.0, min(1.0, anchor_y_raw))
            # Tiny w_norm so the TextBBox extent stays in-frame for anchors
            # near 0.0 or 1.0. Layout for left-anchored cumulative overlays
            # is driven by position_x_frac + the renderer's measured text
            # width, NOT by bbox extent.
            w_norm_placeholder = 0.01
            h_norm = min(max(lg.line_height_frac, 0.01), 1.0)
            try:
                # Pop animation only meaningful for pop-in effect.
                pop_suffix = stage.pop_animated_suffix if effect == "pop-in" else None
                overlay = TemplateTextOverlay(
                    slot_index=slot_index,
                    sample_text=stage.text,
                    start_s=stage.start_s,
                    end_s=stage.end_s,
                    bbox=TextBBox(
                        x_norm=anchor_x,
                        y_norm=anchor_y,
                        w_norm=w_norm_placeholder,
                        h_norm=h_norm,
                        sample_frame_t=stage.start_s,
                    ),
                    font_color_hex=font_color_hex,
                    effect=effect,
                    role=role,
                    size_class=size_class,
                    text_anchor="left",
                    pop_animated_suffix=pop_suffix,
                )
            except (ValidationError, ValueError) as exc:
                log.warning(
                    "stage_g_cumulative_validation_failed",
                    error=str(exc),
                    text=stage.text[:60],
                )
                drops["validation_failed"] += 1
                continue
            out.append(overlay)
    return out


def _classified_phrases_to_output(
    classified: list,
    *,
    slot_boundaries_s: list[tuple[float, float]],
    line_groups: list[LineGroup] | None = None,
) -> TemplateTextOutput:
    """Stage G: convert `ClassifiedPhrase` list to `TemplateTextOutput`.

    Each `ClassifiedPhrase` is mapped to a `TemplateTextOverlay` with:
      - `slot_index` derived from `phrase.start_t_s` + `slot_boundaries_s`
      - `sample_text` = phrase.sample_text, with embedded newlines + known
        debug markers normalized to a single-line form via
        `_normalize_overlay_text` (literal `\\n` was rendering as `/N` on
        screen in prod job 87b7292b)
      - `start_s` / `end_s` from phrase timing
      - `bbox` from phrase aabb (converted: aabb is (x_min, y_min, x_max, y_max)
        in normalised coords; TextBBox wants center + extent)
      - `font_color_hex`, `effect`, `role`, `size_class` from classification
      - `bbox.sample_frame_t` = phrase.start_t_s (closest frame we have)

    Invalid overlays (e.g. zero-area bbox, empty text post-normalize) are
    skipped with a warning.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    from app.agents._schemas.template_text import (  # noqa: PLC0415
        TemplateTextOutput,
        TemplateTextOverlay,
        TextBBox,
    )
    from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

    drops: dict[str, int] = {
        "degenerate_bbox": 0,
        "empty_text_after_normalize": 0,
        "validation_failed": 0,
        "empty_group": 0,
        "single_word_overflow": 0,
        "line_overflow": 0,
        "cumulative_overlap_suppressed": 0,
    }
    overlays: list[TemplateTextOverlay] = []

    # ── Cumulative line-group emit (progressive word reveal) ───────────────
    # Build the set of phrase indices covered by any LineGroup, then route
    # those through the cumulative-reveal path. Indices are into the
    # ORIGINAL `classified` list (pre-sort). Grouped phrases are skipped in
    # the per-phrase loop below.
    line_groups = line_groups or []
    grouped_indices: set[int] = set()
    for lg in line_groups:
        grouped_indices.update(lg.phrase_indices)

    # Track (y_anchor, time_start, time_end) regions covered by emitted
    # cumulative overlays. The ungrouped passthrough below uses this to
    # suppress singleton phrases that overlap in time + y with a cumulative
    # group — those singletons are visually redundant (the cumulative line
    # already occupies that vertical band at that time) and previously
    # rendered on top of the cumulative reveal, causing the "there clashes
    # with 'The work to get'" pattern on template 89cde014. Y tolerance
    # 0.10 (~192 px on a 1920-tall canvas) covers ~1.5 lines at 120 px so
    # an ungrouped phrase rendered slightly below the cumulative anchor
    # still gets suppressed.
    cumulative_coverage: list[tuple[float, float, float]] = []  # (y, start_s, end_s)
    _CUMULATIVE_Y_TOLERANCE = 0.10

    for lg in line_groups:
        cum_overlays = _emit_cumulative_line_overlays(
            lg,
            classified=classified,
            slot_boundaries_s=slot_boundaries_s,
            drops=drops,
        )
        overlays.extend(cum_overlays)
        # Record one coverage tuple per LineGroup. Use lg.line_anchor_y_frac
        # as the y, and the union span of the emitted overlays as the time
        # window. Empty emission (validation failures) records nothing.
        if cum_overlays:
            cov_start = min(ov.start_s for ov in cum_overlays)
            cov_end = max(ov.end_s for ov in cum_overlays)
            cumulative_coverage.append((lg.line_anchor_y_frac, cov_start, cov_end))

    # ── Per-phrase passthrough (ungrouped only) ────────────────────────────
    # Sort classified phrases by start_t_s so we can extend each phrase's
    # end_s up to (but not past) the next phrase's start. Single-frame OCR
    # detections produce phrases where end_t_s == start_t_s; without this
    # extension Stage G would emit zero-duration overlays clamped to 10ms,
    # which render for a single frame (prod evidence: "luck" 7.50-7.51s in
    # the post-v0.4.34.0 reanalyze of template fdaf3bbc).
    # We filter by `grouped_indices` against the ORIGINAL position so
    # line-group phrases don't double-emit.
    ungrouped_with_orig_idx = [
        (orig_idx, cp) for orig_idx, cp in enumerate(classified) if orig_idx not in grouped_indices
    ]
    ungrouped_sorted = sorted(ungrouped_with_orig_idx, key=lambda pair: pair[1].phrase.start_t_s)
    classified_sorted = [cp for _, cp in ungrouped_sorted]
    next_start_by_index = {
        id(classified_sorted[i]): classified_sorted[i + 1].phrase.start_t_s
        for i in range(len(classified_sorted) - 1)
    }
    _MIN_OVERLAY_DURATION_S = 0.5
    _MAX_OVERLAY_EXTENSION_S = 2.0

    for i, cp in enumerate(classified_sorted):
        phrase = cp.phrase
        # Suppress singletons that overlap a cumulative group in y + time.
        # Without this, a phrase like "there" at y=0.44 / 5.5-6.17s renders
        # on top of the "The work to get" cumulative reveal at y=0.44 /
        # 5.26-5.80s, producing the visual clash reported on template
        # 89cde014. The cumulative reveal is canonical; the singleton is
        # redundant for that band of screen at that time.
        if cumulative_coverage:
            y_center = (phrase.aabb[1] + phrase.aabb[3]) / 2.0
            p_start = float(phrase.start_t_s)
            p_end = float(max(phrase.end_t_s, p_start + 0.01))
            overlaps_cumulative = any(
                abs(y_center - cov_y) <= _CUMULATIVE_Y_TOLERANCE
                and p_start < cov_end
                and p_end > cov_start
                for cov_y, cov_start, cov_end in cumulative_coverage
            )
            if overlaps_cumulative:
                log.info(
                    "full_pipeline_singleton_suppressed_by_cumulative",
                    phrase_index=i,
                    sample_text=(phrase.sample_text or "")[:40],
                    y_norm=y_center,
                    phrase_window=(p_start, p_end),
                )
                drops["cumulative_overlap_suppressed"] += 1
                continue
        normalized_text = _normalize_overlay_text(phrase.sample_text)
        if not normalized_text:
            log.warning(
                "full_pipeline_empty_text_after_normalize_skipped",
                phrase_index=i,
                raw_sample_text=phrase.sample_text[:60],
            )
            drops["empty_text_after_normalize"] += 1
            continue
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
                sample_text=normalized_text[:60],
                aabb=phrase.aabb,
            )
            drops["degenerate_bbox"] += 1
            continue

        # Clamp to valid range (OCR polygons can occasionally exceed unit square).
        w_norm = min(w_norm, 1.0)
        h_norm = min(h_norm, 1.0)
        x_norm = max(w_norm / 2.0, min(1.0 - w_norm / 2.0, x_norm))
        y_norm = max(h_norm / 2.0, min(1.0 - h_norm / 2.0, y_norm))

        # end_s must be > start_s for TemplateTextOverlay validation.
        # Stretch single-frame detections (end_t_s ~= start_t_s) up to the
        # next phrase's start so the word stays visible long enough to read.
        # Cap the extension at _MAX_OVERLAY_EXTENSION_S so a phrase followed
        # by a long gap doesn't linger for the entire gap.
        start_s = phrase.start_t_s
        raw_end_s = max(phrase.end_t_s, start_s + 0.01)
        if raw_end_s - start_s < _MIN_OVERLAY_DURATION_S:
            next_start = next_start_by_index.get(id(cp))
            if next_start is not None and next_start > start_s:
                extended_cap = min(next_start, start_s + _MAX_OVERLAY_EXTENSION_S)
                end_s = max(raw_end_s, min(extended_cap, start_s + _MIN_OVERLAY_DURATION_S * 4))
            else:
                end_s = max(raw_end_s, start_s + _MIN_OVERLAY_DURATION_S)
        else:
            end_s = raw_end_s

        slot_index = _assign_slot_index(start_s, slot_boundaries_s)

        try:
            overlay = TemplateTextOverlay(
                slot_index=slot_index,
                sample_text=normalized_text,
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
                sample_text=normalized_text[:60],
                error=str(exc),
            )
            drops["validation_failed"] += 1
            continue

        overlays.append(overlay)

    # Sort the final list by start_s so cumulative-reveal overlays and
    # ungrouped passthrough overlays interleave chronologically. Without
    # this, every cumulative overlay would precede every ungrouped overlay
    # in iteration order — and FFmpeg's overlay filter chain layers later
    # items ON TOP of earlier ones. A visual-only label that overlaps in
    # time with a cumulative reveal would obscure it. Sorting by start_s
    # keeps later-starting overlays on top regardless of their group origin.
    overlays.sort(key=lambda o: o.start_s)

    # ── Stage G summary: aggregate counts for /admin/jobs Debug tab ────────
    # The drops dict captures every silent-skip reason. Sum of drops equals
    # input_phrases - emitted_overlays - cumulative_extra (each LineGroup
    # emits N overlays for N words, expanding overlay count beyond phrase
    # count). Leakage audit reads this event.
    try:
        record_pipeline_event(
            "overlay",
            "stage_g_summary",
            {
                "phrases_in": len(classified),
                "line_groups_in": len(line_groups),
                "grouped_phrases_in": len(grouped_indices),
                "overlays_out": len(overlays),
                "drops": dict(drops),
            },
        )
    except Exception:  # noqa: BLE001
        # Pipeline-trace failures must never break the build. Best-effort.
        pass

    return TemplateTextOutput(overlays=overlays)

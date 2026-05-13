"""[Stage 4] Score 9 clip candidates → return top 3 + hold 6 for re-roll.

Scoring formula (from plan):
  combined = HOOK_WEIGHT × hook_score + ENGAGEMENT_WEIGHT × engagement_score

Hook score:   Gemini analyzes the actual video segment (0-10). Adjusted by scene
              cut proximity. Falls back to heuristic on Gemini failure.
Engagement:   cut density + speech density + audio RMS z-score → normalized 0-10.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import structlog

from app.config import settings
from app.pipeline.probe import VideoProbe
from app.pipeline.scene_detect import SceneCut
from app.pipeline.transcribe import Transcript

log = structlog.get_logger()

CANDIDATE_COUNT = 9
TOP_N = 3
SCENE_CUT_PROXIMITY_S = 5.0  # +1 hook bonus if segment starts within 5s of a cut
FILLER_WORDS = {"um", "uh", "like", "you know", "so", "basically", "literally"}

# Target clip duration from plan
CLIP_DURATION_S = 50.0  # midpoint of 45-59s range


@dataclass
class ClipCandidate:
    start_s: float
    end_s: float
    hook_text: str  # first sentence of transcript in this segment
    hook_score: float  # 0-10
    engagement_score: float  # 0-10
    combined_score: float  # weighted
    rank: int = 0  # 1-9 after sorting


def select_candidates(
    probe: VideoProbe,
    transcript: Transcript,
    scene_cuts: list[SceneCut],
    source_ref: Any = None,
    *,
    job_id: str | None = None,
) -> list[ClipCandidate]:
    """Score CANDIDATE_COUNT clip segments and return all 9 sorted by combined_score.

    source_ref: Gemini File API reference for the uploaded source video.
                If provided, each segment is analyzed with Gemini (parallel calls).
                Falls back to heuristic hook scoring if source_ref is None.

    `job_id` threads into the Gemini agent's `RunContext` for Langfuse session
    clustering. Defaults to None for back-compat.

    Caller uses [:TOP_N] for rendering and [TOP_N:] for re-roll storage.
    """
    segments = _generate_segments(probe.duration_s)
    if len(segments) < 1:
        log.warning("too_few_segments", duration_s=probe.duration_s)

    # Cap at CANDIDATE_COUNT
    segments = segments[:CANDIDATE_COUNT]

    # Extract hook text per segment from transcript (first sentence of transcript in window)
    hook_texts = [_first_sentence(transcript, start_s, end_s) for start_s, end_s in segments]

    # Hook scoring: Gemini video analysis (parallel) or heuristic fallback
    if source_ref is not None:
        raw_hook_scores, gemini_hook_texts = _score_hooks_gemini(
            source_ref, segments, job_id=job_id
        )
        # Use Gemini-extracted hook texts where available
        hook_texts = [
            gemini_hook_texts[i] or hook_texts[i]
            for i in range(len(segments))
        ]
    elif transcript.low_confidence:
        log.info("asr_fallback_mode_engagement_only")
        raw_hook_scores = [0.0] * len(segments)
    else:
        # Heuristic fallback: use transcript words as proxy for hook quality
        raw_hook_scores = _score_hooks_heuristic(hook_texts)

    # Adjust hook scores for scene cut proximity and filler words
    cut_timestamps = {c.timestamp_s for c in scene_cuts}
    hook_scores = [
        _adjust_hook_score(raw_hook_scores[i], hook_texts[i], segments[i][0], cut_timestamps)
        for i in range(len(segments))
    ]

    # Engagement scores (heuristic, no LLM)
    engagement_scores = _compute_engagement_scores(segments, transcript, scene_cuts, probe)

    candidates: list[ClipCandidate] = []
    for i, (start_s, end_s) in enumerate(segments):
        hook = hook_scores[i]
        eng = engagement_scores[i]
        if source_ref is None and transcript.low_confidence:
            combined = eng  # engagement-only in ASR fallback
        else:
            combined = settings.hook_weight * hook + settings.engagement_weight * eng

        candidates.append(ClipCandidate(
            start_s=start_s,
            end_s=end_s,
            hook_text=hook_texts[i],
            hook_score=hook,
            engagement_score=eng,
            combined_score=combined,
        ))

    candidates.sort(key=lambda c: c.combined_score, reverse=True)
    for i, c in enumerate(candidates):
        c.rank = i + 1

    log.info(
        "candidates_scored",
        count=len(candidates),
        top_score=candidates[0].combined_score if candidates else None,
        gemini_analysis=source_ref is not None,
    )
    return candidates


def _score_hooks_gemini(
    source_ref: Any,
    segments: list[tuple[float, float]],
    *,
    job_id: str | None = None,
) -> tuple[list[float], list[str]]:
    """Analyze each segment via Gemini in parallel. Returns (hook_scores, hook_texts).

    Per-segment failures fall back to score=5.0 and empty hook_text — never raises.
    """
    from app.pipeline.agents.gemini_analyzer import (  # noqa: PLC0415
        GeminiAnalysisError,
        GeminiRefusalError,
        analyze_clip,
    )

    def _analyze_one(idx_segment: tuple[int, tuple[float, float]]) -> tuple[int, float, str]:
        idx, (start_s, end_s) = idx_segment
        try:
            meta = analyze_clip(source_ref, start_s=start_s, end_s=end_s, job_id=job_id)
            return idx, meta.hook_score, meta.hook_text
        except (GeminiRefusalError, GeminiAnalysisError, Exception) as exc:
            log.warning("gemini_hook_score_failed", segment_idx=idx, error=str(exc))
            return idx, 5.0, ""  # neutral fallback

    scores = [5.0] * len(segments)
    texts = [""] * len(segments)

    max_workers = min(len(segments), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_analyze_one, (i, seg)): i
            for i, seg in enumerate(segments)
        }
        for future in as_completed(futures):
            idx, score, text = future.result()
            scores[idx] = score
            texts[idx] = text

    return scores, texts


def _score_hooks_heuristic(hook_texts: list[str]) -> list[float]:
    """Simple heuristic hook scoring when Gemini is unavailable.

    Scores based on word count and punctuation (questions/exclamations score higher).
    """
    scores = []
    for text in hook_texts:
        if not text:
            scores.append(3.0)
            continue
        words = text.split()
        base = min(5.0 + len(words) * 0.1, 7.0)
        if text.endswith("?"):
            base += 1.5
        elif text.endswith("!"):
            base += 1.0
        scores.append(min(base, 9.0))
    return scores


def _generate_segments(duration_s: float) -> list[tuple[float, float]]:
    """Generate up to CANDIDATE_COUNT non-overlapping segment start/end pairs.

    Distributes segments evenly across the video.
    """
    if duration_s <= CLIP_DURATION_S:
        return [(0.0, min(duration_s, settings.output_max_duration_s))]

    # Slide window with even spacing
    step = (duration_s - CLIP_DURATION_S) / max(CANDIDATE_COUNT - 1, 1)
    segments = []
    for i in range(CANDIDATE_COUNT):
        start = i * step
        end = start + CLIP_DURATION_S
        if end > duration_s:
            end = duration_s
            start = max(0.0, end - CLIP_DURATION_S)
        segments.append((round(start, 2), round(end, 2)))
    return segments


def _first_sentence(transcript: Transcript, start_s: float, end_s: float) -> str:
    """Extract the first sentence of transcript words within [start_s, end_s]."""
    words_in_window = [w for w in transcript.words if start_s <= w.start_s < end_s]
    if not words_in_window:
        return ""
    text = " ".join(w.text.strip() for w in words_in_window[:30])  # first ~30 words
    # Find first sentence boundary
    for sep in (".", "!", "?"):
        idx = text.find(sep)
        if idx != -1:
            return text[: idx + 1].strip()
    return text.strip()


def _adjust_hook_score(
    raw: float,
    hook_text: str,
    start_s: float,
    cut_timestamps: set[float],
) -> float:
    score = raw
    # +1 if segment starts near a scene cut
    if any(abs(start_s - t) <= SCENE_CUT_PROXIMITY_S for t in cut_timestamps):
        score += 1.0
    # -1 if starts with filler word
    lower = hook_text.lower().strip()
    if any(lower.startswith(fw) for fw in FILLER_WORDS):
        score -= 1.0
    return max(0.0, min(10.0, score))


def _compute_engagement_scores(
    segments: list[tuple[float, float]],
    transcript: Transcript,
    scene_cuts: list[SceneCut],
    probe: VideoProbe,
) -> list[float]:
    """Heuristic engagement scores based on cut density, speech density, audio RMS."""
    cut_timestamps = [c.timestamp_s for c in scene_cuts]
    total_duration = probe.duration_s or 1.0

    # Source-level baselines
    source_cuts_per_30s = (len(cut_timestamps) / total_duration) * 30.0 if cut_timestamps else 1.0
    total_words = len(transcript.words)
    source_wps = total_words / total_duration if total_duration > 0 else 1.0

    raw_scores = []
    for start_s, end_s in segments:
        seg_dur = end_s - start_s or 1.0

        cuts_in_seg = sum(1 for t in cut_timestamps if start_s <= t < end_s)
        seg_cuts_per_30s = (cuts_in_seg / seg_dur) * 30.0
        cut_ratio = seg_cuts_per_30s / (source_cuts_per_30s or 1.0)

        words_in_seg = [w for w in transcript.words if start_s <= w.start_s < end_s]
        seg_wps = len(words_in_seg) / seg_dur
        speech_ratio = seg_wps / (source_wps or 1.0)

        raw_scores.append(cut_ratio + speech_ratio)

    return _normalize_to_10(raw_scores)


def _normalize_to_10(values: list[float]) -> list[float]:
    """Min-max normalize a list of floats to [0, 10]."""
    if not values:
        return []
    min_v = min(values)
    max_v = max(values)
    if max_v == min_v:
        return [5.0] * len(values)
    return [10.0 * (v - min_v) / (max_v - min_v) for v in values]

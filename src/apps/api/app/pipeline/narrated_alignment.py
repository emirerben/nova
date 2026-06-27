"""Script-to-voiceover alignment for narrated walkthrough edits."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

from app.pipeline.lyrics_alignment import align


class WordLike(Protocol):
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True, slots=True)
class StepScript:
    step_id: str
    text: str


@dataclass(frozen=True, slots=True)
class StepTiming:
    step_id: str
    start_s: float
    end_s: float
    confidence: float


_LOW_STEP_CONFIDENCE = 0.5


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[\w']+", text, flags=re.UNICODE) if any(c.isalnum() for c in t)]


def _norm(token: str) -> str:
    return re.sub(r"[^a-z0-9]", "", token.lower())


def _similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if min(len(a), len(b)) <= 2:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _ordered_match_ratio(
    script_text: str,
    words: list[WordLike],
    start_s: float,
    end_s: float,
) -> float:
    expected = _tokens(script_text)
    if not expected:
        return 0.0
    window = [w for w in words if w.end_s > start_s and w.start_s < end_s]
    cursor = 0
    matched = 0
    for token in expected:
        target = _norm(token)
        best_idx: int | None = None
        best_score = 0.0
        for idx in range(cursor, min(len(window), cursor + 5)):
            score = _similarity(target, _norm(window[idx].text))
            if score > best_score:
                best_score = score
                best_idx = idx
        if best_idx is not None and best_score >= 0.65:
            matched += 1
            cursor = best_idx + 1
    return matched / len(expected)


def _even_split(script_steps: list[StepScript], total_duration_s: float) -> list[StepTiming]:
    if not script_steps:
        return []
    step_s = max(total_duration_s, 0.001) / len(script_steps)
    return [
        StepTiming(
            step_id=step.step_id,
            start_s=round(idx * step_s, 3),
            end_s=round((idx + 1) * step_s, 3),
            confidence=0.0,
        )
        for idx, step in enumerate(script_steps)
    ]


def contiguous_step_timings(
    speech_starts: list[float],
    timeline_end: float,
) -> list[StepTiming]:
    """Tile ``[0, timeline_end]`` with contiguous segments, one per phrase bucket.

    ``speech_starts`` is each bucket's speech-onset time (voiceover-absolute,
    ascending). Segment 0 starts at 0 (absorbing any leading silence); every
    other segment starts at its bucket's speech onset; each segment ends where
    the NEXT bucket's speech begins (inter-phrase gaps absorbed into the
    preceding segment); the last segment runs to ``timeline_end``.

    This is what keeps the narrated_ready captions in lockstep with the voice:
    the assembled visual concatenates these durations from 0, so when segments
    tile the FULL voiceover the assembled timeline equals voiceover-absolute
    time and burned captions (rebased onto it) land exactly on the spoken word.
    Starting segments at the speech onset instead — leaving leading silence and
    gaps out — compresses the visual and the captions lead the audio.
    """
    n = len(speech_starts)
    timings: list[StepTiming] = []
    for i in range(n):
        start = 0.0 if i == 0 else float(speech_starts[i])
        end = float(speech_starts[i + 1]) if i + 1 < n else float(timeline_end)
        end = max(end, start + 0.001)
        timings.append(
            StepTiming(
                step_id=f"seg_{i}",
                start_s=round(start, 3),
                end_s=round(end, 3),
                confidence=1.0,
            )
        )
    return timings


def align_script_to_voiceover(
    script_steps: list[StepScript],
    whisper_words: list[WordLike],
) -> list[StepTiming]:
    """Purely align step scripts to precomputed word timestamps.

    Low-confidence or missing steps fall back to their index's even split across
    the voiceover duration. The wrapper below owns GCS/OpenAI side effects.
    """
    steps = [s for s in script_steps if s.text.strip()]
    words = sorted(list(whisper_words), key=lambda w: (float(w.start_s), float(w.end_s)))
    if not steps or not words:
        return _even_split(steps, 0.001)

    total_duration_s = max(float(w.end_s) for w in words)
    even = _even_split(steps, total_duration_s)
    result = align([s.text for s in steps], words)
    if not result.lines:
        return even

    by_text: dict[str, list] = {}
    for line in result.lines:
        by_text.setdefault(line.text, []).append(line)

    aligned_lines: list = []
    for step in steps:
        matches = by_text.get(step.text) or []
        aligned_lines.append(matches.pop(0) if matches else None)

    line_starts = [float(line.start_s) if line is not None else None for line in aligned_lines]
    timings: list[StepTiming] = []
    for idx, step in enumerate(steps):
        line = aligned_lines[idx]
        if line is None:
            timings.append(even[idx])
            continue

        next_start = next((s for s in line_starts[idx + 1 :] if s is not None), None)
        start_s = 0.0 if idx == 0 else float(line.start_s)
        end_s = float(next_start) if next_start is not None else total_duration_s
        if end_s <= start_s:
            start_s = float(line.start_s)
            end_s = max(float(line.end_s), start_s + 0.001)

        confidence = _ordered_match_ratio(step.text, words, start_s, end_s)
        confidence = min(confidence, float(result.confidence))
        timing = StepTiming(
            step_id=step.step_id,
            start_s=round(start_s, 3),
            end_s=round(end_s, 3),
            confidence=round(confidence, 3),
        )
        timings.append(even[idx] if timing.confidence < _LOW_STEP_CONFIDENCE else timing)

    return timings


def align_script_gcs_to_voiceover(
    script_steps: list[StepScript],
    voiceover_gcs_path: str,
    tmpdir: str,
) -> list[StepTiming]:
    """Download and transcribe a voiceover, then call the pure aligner."""
    from app.pipeline.transcribe import _transcribe_openai  # noqa: PLC0415
    from app.storage import download_to_file  # noqa: PLC0415

    os.makedirs(tmpdir, exist_ok=True)
    voiceover_local = os.path.join(tmpdir, "narrated_voiceover")
    download_to_file(voiceover_gcs_path, voiceover_local)
    transcript = _transcribe_openai(voiceover_local)
    return align_script_to_voiceover(script_steps, transcript.words)

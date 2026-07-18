"""Readable, word-timed caption grammar for Smart talking-head edits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.smart_edit.presets import CaptionPolicy

_TOKEN_RE = re.compile(r"\S+")
_STRONG_END_RE = re.compile(r"[.!?…][\"')\]]*$")


@dataclass(frozen=True, slots=True)
class _TimedToken:
    text: str
    start_s: float
    end_s: float
    timing_quality: str


def _tokens_for_cue(cue: dict[str, Any]) -> list[_TimedToken]:
    display_tokens = _TOKEN_RE.findall(str(cue.get("text") or "").strip())
    if not display_tokens:
        return []
    raw_words = cue.get("words")
    if isinstance(raw_words, list) and len(raw_words) == len(display_tokens):
        aligned: list[_TimedToken] = []
        try:
            for display, raw in zip(display_tokens, raw_words):
                if not isinstance(raw, dict):
                    raise ValueError
                start = max(0.0, float(raw.get("start_s", 0.0)))
                end = max(start + 0.01, float(raw.get("end_s", start + 0.01)))
                aligned.append(_TimedToken(display, start, end, "aligned"))
            return aligned
        except (TypeError, ValueError):
            pass

    start = max(0.0, float(cue.get("start_s", 0.0) or 0.0))
    end = max(start + 0.01, float(cue.get("end_s", start + 0.01) or start + 0.01))
    step = (end - start) / len(display_tokens)
    return [
        _TimedToken(token, start + index * step, start + (index + 1) * step, "segment_estimate")
        for index, token in enumerate(display_tokens)
    ]


def _should_close(
    current: list[_TimedToken],
    next_token: _TimedToken | None,
    policy: CaptionPolicy,
) -> bool:
    if len(current) >= policy.max_words:
        return True
    if len(" ".join(token.text for token in current)) >= policy.max_chars:
        return True
    if len(current) < policy.min_words:
        return False
    if _STRONG_END_RE.search(current[-1].text):
        return True
    if next_token is not None and next_token.start_s - current[-1].end_s >= 0.34:
        return True
    return False


def build_smart_caption_cues(
    cues: list[dict[str, Any]],
    policy: CaptionPolicy,
) -> list[dict[str, Any]]:
    """Re-chunk corrected cues into short readable phrases.

    The legacy captioner remains untouched. Smart Captions flattens the final
    corrected word timeline, then closes phrases on punctuation, real pauses,
    the preset word limit, or the preset character limit. Every output cue
    carries its word timings so highlighting, authored-text claims, and SFX
    anchors all share one clock.
    """

    tokens = [token for cue in cues for token in _tokens_for_cue(cue)]
    if not tokens:
        return []

    chunks: list[list[_TimedToken]] = []
    current: list[_TimedToken] = []
    for index, token in enumerate(tokens):
        current.append(token)
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if _should_close(current, following, policy):
            chunks.append(current)
            current = []
    if current:
        if (
            chunks
            and len(current) < policy.min_words
            and len(chunks[-1]) + len(current) <= policy.max_words
        ):
            chunks[-1].extend(current)
        else:
            chunks.append(current)

    result: list[dict[str, Any]] = []
    for chunk in chunks:
        if not chunk:
            continue
        result.append(
            {
                "text": " ".join(token.text for token in chunk),
                "start_s": round(chunk[0].start_s, 3),
                "end_s": round(chunk[-1].end_s, 3),
                "words": [
                    {
                        "text": token.text,
                        "start_s": round(token.start_s, 3),
                        "end_s": round(token.end_s, 3),
                        "timing_quality": token.timing_quality,
                    }
                    for token in chunk
                ],
            }
        )
    return result

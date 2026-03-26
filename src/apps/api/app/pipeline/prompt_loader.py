"""Lazy-loading prompt template manager.

Loads .txt prompt files from the prompts/ directory using string.Template
($variable syntax). Falls back to inline defaults if files are missing.

Uses lazy initialization (same pattern as _get_client() in gemini_analyzer.py)
to avoid import-time filesystem access.
"""

import os
from pathlib import Path
from string import Template

import structlog

log = structlog.get_logger()

# Prompt files live alongside the app package
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# Module-level cache — populated lazily on first access
_cache: dict[str, str] = {}

# Inline defaults — source of truth. Files are optional overrides.
_INLINE_DEFAULTS: dict[str, str] = {
    "analyze_template_pass1": (
        "Watch this TikTok/short-form video template. Describe its editing "
        "style in one detailed paragraph — cover: pacing and rhythm, transition "
        "types between shots, text overlay animation style, color grading and "
        "visual treatment, speed ramping or slow motion usage, and how visual "
        "cuts relate to the audio/music beats. Write as if you're giving "
        "instructions to a video editor who needs to recreate this exact style "
        "with different footage."
    ),
    "analyze_clip": (
        "$segment_instruction\n\n"
        "Return a JSON object with these exact fields:\n"
        '- "transcript": string — full spoken text in the segment\n'
        '- "hook_text": string — the first compelling sentence that creates curiosity\n'
        '- "hook_score": float 0–10 — how strongly the opening hooks the viewer\n'
        '- "best_moments": list of 3–6 objects with '
        '{"start_s": float, "end_s": float, "energy": float 0–10, "description": string} '
        "covering a VARIETY of durations — include some short moments (3–5s) AND some "
        "medium moments (8–12s) AND at least one longer moment (13–20s) so this clip can "
        "fill both short and long template slots\n\n"
        "Return ONLY valid JSON, no markdown."
    ),
    "transcribe": (
        "Transcribe all speech in this video.\n\n"
        "Return a JSON object with these exact fields:\n"
        '- "full_text": string — complete transcript\n'
        '- "words": list of {"text": string, "start_s": float, "end_s": float}\n'
        '- "low_confidence": boolean — true if transcription quality is poor\n\n'
        "Return ONLY valid JSON, no markdown."
    ),
}


def load_prompt(name: str, **variables: str) -> str:
    """Load a prompt template by name and substitute variables.

    Looks for prompts/<name>.txt first. Falls back to inline default.
    Variables are substituted using string.Template ($variable syntax).
    Missing variables are left as-is (safe_substitute).

    Args:
        name: Prompt file name without .txt extension.
        **variables: Template variables to substitute.

    Returns:
        The rendered prompt string.
    """
    raw = _get_raw(name)
    return Template(raw).safe_substitute(**variables)


def _get_raw(name: str) -> str:
    """Get raw prompt text (cached). File > inline default."""
    if name in _cache:
        return _cache[name]

    # Try loading from file
    file_path = _PROMPTS_DIR / f"{name}.txt"
    if file_path.exists():
        try:
            text = file_path.read_text(encoding="utf-8")
            _cache[name] = text
            return text
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("prompt_file_load_failed", name=name, error=str(exc))

    # Fall back to inline default
    if name in _INLINE_DEFAULTS:
        text = _INLINE_DEFAULTS[name]
        _cache[name] = text
        return text

    # No file and no inline default — return empty string with warning
    log.warning("prompt_not_found", name=name)
    _cache[name] = ""
    return ""


def clear_cache() -> None:
    """Clear the prompt cache. Useful for testing."""
    _cache.clear()

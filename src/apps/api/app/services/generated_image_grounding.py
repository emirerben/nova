"""Words a creator-generated image carries so Visuals autoplace can match it to speech.

A generated image is never analysed (Kria wrote it), so before this module its stored
analysis was `{width, height, source, model}` and the overlay placement agent saw an empty
description beside a one-word title (prod 2026-11-03: "Beer" vs the spoken Turkish "alkol").

The text here is the creator's OWN prompt and title, kept on their own asset. It fills only
`subject` / `description`, which ground matching and the creator-facing label. It never fills
`on_screen_text` or `brands`, and nothing here writes overlay text, so the invariant that
Gemini metadata never becomes on-screen text is untouched. The prompt is never logged and
never echoed in an operation response.
"""

from __future__ import annotations

from typing import Any

from app.services.clip_understanding import GENERATED_SOURCE

GROUNDING_PROMPT_MAX_CHARS = 300
_SUBJECT_MAX_CHARS = 80
_LABEL_MAX_CHARS = 60
FALLBACK_LABEL = "Generated image"
IMAGE_PROMPT_KEY = "image_prompt"


def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def prompt_metadata(capability: str, prompt: str) -> dict[str, str]:
    """The prompt text kept on the operation. Only a fresh image has a description of itself;
    an edit prompt ("make the hat red") describes a change, not the picture."""
    if capability != "image":
        return {}
    text = _one_line(prompt, GROUNDING_PROMPT_MAX_CHARS)
    return {IMAGE_PROMPT_KEY: text} if text else {}


def generated_image_analysis(
    *, width: int, height: int, model: str | None, prompt: str, title: str
) -> dict[str, Any]:
    analysis: dict[str, Any] = {
        "width": width,
        "height": height,
        "source": GENERATED_SOURCE,
        "model": model,
    }
    clean_prompt = _one_line(prompt, GROUNDING_PROMPT_MAX_CHARS)
    clean_title = _one_line(title, _SUBJECT_MAX_CHARS)
    subject = clean_title or clean_prompt[:_SUBJECT_MAX_CHARS].strip()
    description = clean_prompt or clean_title
    if subject:
        analysis["subject"] = subject
    if description:
        analysis["description"] = description
    return analysis


def is_generated(analysis: object) -> bool:
    return isinstance(analysis, dict) and analysis.get("source") == GENERATED_SOURCE


def with_generated_grounding(analysis: dict | None, user_context: str | None) -> dict:
    """The analysis a matcher should read. Rows admitted before the prompt was kept carry
    only a title (as `user_context`), so their text is derived on read; nothing is rewritten.
    Anything that is not a generated image is returned untouched."""
    if not isinstance(analysis, dict):
        return {}
    if not is_generated(analysis):
        return analysis
    title = _one_line(user_context, _SUBJECT_MAX_CHARS)
    fill = {
        key: title
        for key in ("subject", "description")
        if title and not str(analysis.get(key) or "").strip()
    }
    return {**analysis, **fill} if fill else analysis


def grounding_label(user_context: str | None, analysis: dict | None) -> str:
    """What the creator calls this generated image: never the stored `generated.png`."""
    subject = analysis.get("subject") if isinstance(analysis, dict) else ""
    return _one_line(user_context or subject or FALLBACK_LABEL, _LABEL_MAX_CHARS)

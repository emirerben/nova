"""Words a creator-generated image carries so Visuals autoplace can match it to speech.

A generated image is never analysed (Kria wrote it), so before this module its stored
analysis was `{width, height, source, model}` and the overlay placement agent saw an empty
description beside a one-word title.

Two kinds of text, kept apart on purpose:

- `subject` / `description` hold the creator's TITLE only. Every reader of those fields (the
  no-AI keyword matcher, the wishlist filter, the pool response the client reads, the edit
  proposal) is built for short topical text, and the title is already visible to the creator.
- `generation_prompt` holds the trimmed prompt the image model was given. In the Visuals panel
  that prompt is written by `OverlayGenerationAgent` (a model) and then reviewed by the creator,
  so it is model text, not the creator's own words, and it is image-making text ("photorealistic,
  soft light") that says little about speech. Only the LLM placement input reads it, as a hint
  for what the picture shows. It is stored on the creator's own asset row and the operation row
  (`request_metadata.image_prompt`), is redacted from the AgentRun / Langfuse copy of the
  placement input (`_runtime._SENSITIVE_INPUT_KEYS`), and is never echoed in an operation
  response.

Nothing here fills `on_screen_text` or `brands`, and nothing here writes overlay text, so the
invariant that Gemini metadata never becomes on-screen text is untouched.
"""

from __future__ import annotations

from typing import Any

from app.services.clip_understanding import GENERATED_SOURCE

GROUNDING_PROMPT_MAX_CHARS = 300
_SUBJECT_MAX_CHARS = 80
_LABEL_MAX_CHARS = 60
FALLBACK_LABEL = "Generated image"
IMAGE_PROMPT_KEY = "image_prompt"
GENERATION_PROMPT_KEY = "generation_prompt"


def _one_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def prompt_metadata(capability: str, prompt: str) -> dict[str, str]:
    """The prompt text kept on the operation (admission copies it to `generation_prompt`). Only a fresh image has a description of itself;
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
    clean_title = _one_line(title, _SUBJECT_MAX_CHARS)
    if clean_title:
        analysis["subject"] = analysis["description"] = clean_title
    clean_prompt = _one_line(prompt, GROUNDING_PROMPT_MAX_CHARS)
    if clean_prompt:
        analysis[GENERATION_PROMPT_KEY] = clean_prompt
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
    stored = analysis if isinstance(analysis, dict) else {}
    named = user_context or stored.get("subject") or stored.get(GENERATION_PROMPT_KEY)
    return _one_line(named or FALLBACK_LABEL, _LABEL_MAX_CHARS)

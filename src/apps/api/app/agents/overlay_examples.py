"""Versioned few-shot example library for the generative-edit text agents.

The library is a JSON fixture (`prompts/overlay_examples.json`) — versioned in-repo,
not a DB table (plan Decision 4). It is embedded into BOTH the format-matcher prompt
(for selection) and the intro-writer prompt (top-K exemplars for style steering), so
it is part of those prompts: bump the agents' `prompt_version` when this file changes.

`load_overlay_examples()` is cached after first read. The file is required for the
generative path; a missing/malformed file raises at load time rather than silently
matching against an empty library.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

_EXAMPLES_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "overlay_examples.json"


class OverlayExample(BaseModel):
    id: str = Field(min_length=1)
    content_profile: str = Field(min_length=1)
    effect: str = Field(min_length=1)
    text: str = Field(min_length=1)
    highlight_word: str | None = None
    position: str = "center"
    size_class: str = "jumbo"
    text_color: str = "#FFFFFF"
    highlight_color: str = "#FFD24A"
    # "linear" (one centered block) or "cluster" (editorial word-cluster — multiple
    # positioned blocks with mixed sizes; see app/pipeline/intro_cluster.py).
    layout: str = "linear"
    # Optional provenance from the weekly market-research agent. Hand-written
    # entries omit these; mined entries tag their niche + attribution. Style
    # reference only — the intro writer composes new text, never verbatim.
    niche: str | None = None
    source: str | None = None


@lru_cache(maxsize=1)
def load_overlay_examples() -> tuple[OverlayExample, ...]:
    """Return the example library as an immutable tuple (cached). Raises on bad file."""
    with open(_EXAMPLES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("examples")
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"overlay_examples.json: 'examples' must be a non-empty list ({_EXAMPLES_PATH})"
        )
    return tuple(OverlayExample(**e) for e in raw)


def examples_by_id() -> dict[str, OverlayExample]:
    return {e.id: e for e in load_overlay_examples()}


def library_version() -> str:
    try:
        with open(_EXAMPLES_PATH, encoding="utf-8") as f:
            return str(json.load(f).get("version", "unknown"))
    except Exception:
        return "unknown"

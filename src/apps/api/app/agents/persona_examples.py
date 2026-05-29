"""Versioned few-shot banks for the plan agents (persona + content plan).

Mirrors app/agents/overlay_examples.py exactly: JSON fixtures versioned in-repo
(prompts/persona_archetypes.json, prompts/content_ideas.json), cached after
first read, required (a missing/malformed file raises rather than silently
steering off an empty bank). The weekly /research-tiktok agent grows these
files; bumping either file's `version` requires bumping the matching agent
prompt_version (PERSONA_PROMPT_VERSION / CONTENT_PLAN_PROMPT_VERSION) — see the
coupling guard test.

The bank content is injected into generate_persona.txt / generate_content_plan.txt
as DATA (style reference), never as instructions.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.agents._schemas.market_research import ContentIdea, PersonaArchetype

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_ARCHETYPES_PATH = _PROMPTS_DIR / "persona_archetypes.json"
_IDEAS_PATH = _PROMPTS_DIR / "content_ideas.json"


@lru_cache(maxsize=1)
def load_persona_archetypes() -> tuple[PersonaArchetype, ...]:
    """Return the persona-archetype pool (cached). Raises on a bad file."""
    with open(_ARCHETYPES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("archetypes")
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"persona_archetypes.json: 'archetypes' must be a non-empty list ({_ARCHETYPES_PATH})"
        )
    return tuple(PersonaArchetype(**a) for a in raw)


@lru_cache(maxsize=1)
def load_content_ideas() -> tuple[ContentIdea, ...]:
    """Return the content-idea bank (cached). Raises on a bad file."""
    with open(_IDEAS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("ideas")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"content_ideas.json: 'ideas' must be a non-empty list ({_IDEAS_PATH})")
    return tuple(ContentIdea(**i) for i in raw)


def archetypes_version() -> str:
    try:
        with open(_ARCHETYPES_PATH, encoding="utf-8") as f:
            return str(json.load(f).get("version", "unknown"))
    except Exception:
        return "unknown"


def content_ideas_version() -> str:
    try:
        with open(_IDEAS_PATH, encoding="utf-8") as f:
            return str(json.load(f).get("version", "unknown"))
    except Exception:
        return "unknown"


def _overlaps(a: str, terms: list[str]) -> int:
    al = a.lower()
    return sum(1 for t in terms if t and t.lower() in al)


def format_archetypes(limit: int = 6) -> str:
    """Render the archetype pool as compact prompt lines. The persona generator
    shows these as style references so its output lands in a recognizable lane."""
    lines: list[str] = []
    for a in load_persona_archetypes()[:limit]:
        hooks = "; ".join(a.sample_hooks[:3])
        line = (
            f"- [{a.niche}] {a.summary} | voice: {a.tone} | pillars: {', '.join(a.content_pillars)}"
        )
        if hooks:
            line += f" | hooks: {hooks}"
        lines.append(line)
    return "\n".join(lines) or "(none)"


def format_ideas_for_pillars(pillars: list[str], limit: int = 12) -> str:
    """Render the idea bank, ranked by overlap with the creator's pillars, as
    prompt lines for the content-plan generator. Falls back to top-N when no
    pillar matches so the bank always contributes some signal."""
    ideas = load_content_ideas()
    ranked = sorted(
        ideas, key=lambda i: _overlaps(f"{i.niche} {i.pillar} {i.idea}", pillars), reverse=True
    )
    lines = [f"- [{i.niche}/{i.pillar}] {i.idea} (hook: {i.hook_pattern})" for i in ranked[:limit]]
    return "\n".join(lines) or "(none)"

"""Versioned few-shot banks for the plan agents (persona + content plan + intro).

Mirrors app/agents/overlay_examples.py exactly: JSON fixtures versioned in-repo
(prompts/persona_archetypes.json, content_ideas.json, tiktok_success_factors.json),
cached after first read, required (a missing/malformed file raises rather than
silently steering off an empty bank). The weekly /research-tiktok agent grows
these files; bumping any file's `version` requires bumping the matching agent
prompt_version (PERSONA_PROMPT_VERSION / CONTENT_PLAN_PROMPT_VERSION /
IntroTextWriterAgent.spec.prompt_version) — see the coupling guard test.

The bank content is injected into the plan/intro prompts as DATA (style /
strategy reference), never as instructions.

Ranking: ideas and archetypes are ordered by (pillar overlap, performance) —
pillar fit stays primary (an off-pillar viral idea is useless to this creator),
and the mined PerformanceSignal breaks ties so a proven idea beats a guessed one
at equal fit. Entries with no performance signal sort last within their fit tier.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from app.agents._schemas.market_research import ContentIdea, PersonaArchetype, SuccessFactor

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_ARCHETYPES_PATH = _PROMPTS_DIR / "persona_archetypes.json"
_IDEAS_PATH = _PROMPTS_DIR / "content_ideas.json"
_SUCCESS_FACTORS_PATH = _PROMPTS_DIR / "tiktok_success_factors.json"


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


@lru_cache(maxsize=1)
def load_success_factors() -> tuple[SuccessFactor, ...]:
    """Return the TikTok success-factor bank (cached). Raises on a bad file."""
    with open(_SUCCESS_FACTORS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("factors")
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            f"tiktok_success_factors.json: 'factors' must be a non-empty list "
            f"({_SUCCESS_FACTORS_PATH})"
        )
    return tuple(SuccessFactor(**f) for f in raw)


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


def success_factors_version() -> str:
    try:
        with open(_SUCCESS_FACTORS_PATH, encoding="utf-8") as f:
            return str(json.load(f).get("version", "unknown"))
    except Exception:
        return "unknown"


def _overlaps(a: str, terms: list[str]) -> int:
    al = a.lower()
    return sum(1 for t in terms if t and t.lower() in al)


def _performance_score(perf) -> float:
    """A single scalar for tie-breaking by real performance. Prefer view_index
    (outperformance vs the source account's own baseline), fall back to
    engagement_rate, else 0 so unmeasured entries sort last within their tier."""
    if perf is None:
        return 0.0
    if perf.view_index is not None:
        return float(perf.view_index)
    if perf.engagement_rate is not None:
        return float(perf.engagement_rate)
    return 0.0


def format_archetypes(limit: int = 6) -> str:
    """Render the archetype pool as compact prompt lines, best-performing lanes
    first. The persona generator shows these as style references so its output
    lands in a recognizable, proven lane."""
    ranked = sorted(
        load_persona_archetypes(), key=lambda a: _performance_score(a.performance), reverse=True
    )
    lines: list[str] = []
    for a in ranked[:limit]:
        hooks = "; ".join(a.sample_hooks[:3])
        line = (
            f"- [{a.niche}] {a.summary} | voice: {a.tone} | pillars: {', '.join(a.content_pillars)}"
        )
        if hooks:
            line += f" | hooks: {hooks}"
        lines.append(line)
    return "\n".join(lines) or "(none)"


def format_ideas_for_pillars(pillars: list[str], limit: int = 12) -> str:
    """Render the idea bank, ranked by (pillar overlap, mined performance), as
    prompt lines for the content-plan generator. Pillar fit is primary so the
    bank stays relevant to THIS creator; performance breaks ties so a proven idea
    beats a guessed one. Falls back to top-N when no pillar matches so the bank
    always contributes some signal."""
    ideas = load_content_ideas()
    ranked = sorted(
        ideas,
        key=lambda i: (
            _overlaps(f"{i.niche} {i.pillar} {i.idea}", pillars),
            _performance_score(i.performance),
        ),
        reverse=True,
    )
    lines = [f"- [{i.niche}/{i.pillar}] {i.idea} (hook: {i.hook_pattern})" for i in ranked[:limit]]
    return "\n".join(lines) or "(none)"


def format_success_factors(applies_to: str, limit: int = 8) -> str:
    """Render success factors relevant to a stage ("persona"/"plan"/"hook") as
    compact prompt lines, each labeled by provenance so the model (and any human
    reading the prompt) never confuses "observed in our data" with "TikTok says".
    Factors tagged "all" always apply. Corpus-observed factors lead (they're
    grounded in our own engagement data); public-doc factors follow."""
    factors = [
        f for f in load_success_factors() if applies_to in f.applies_to or "all" in f.applies_to
    ]
    # Corpus first (our own evidence), then public — stable within each group.
    factors.sort(key=lambda f: 0 if f.provenance == "corpus" else 1)
    label = {"corpus": "observed", "public": "TikTok docs"}
    lines = [f"- [{label[f.provenance]}] {f.factor} — {f.why}" for f in factors[:limit]]
    return "\n".join(lines) or "(none)"

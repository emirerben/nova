"""Agent registry: name → class mapping.

This is intentionally a Python dict, not a DB table. The agent layer doesn't need
runtime registration; agents are imported at module load time. The registry exists
to (a) give callers a uniform `get_agent(name)` for orchestration logic that
already knows the agent name as a string, and (b) make the catalog discoverable
from a single file.

To add a new agent: define the class in its own module under `app/agents/`, then
add a row to `_REGISTRATIONS` below. The lazy `_load()` import keeps startup time
fast (~zero cost until first lookup).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents._runtime import Agent


# (agent_name, module_path, class_name)
_REGISTRATIONS: tuple[tuple[str, str, str], ...] = (
    # Migrated agents (existing LLM call sites consolidated under runtime)
    ("nova.video.clip_metadata", "app.agents.clip_metadata", "ClipMetadataAgent"),
    ("nova.compose.template_recipe", "app.agents.template_recipe", "TemplateRecipeAgent"),
    ("nova.compose.template_text", "app.agents.template_text", "TemplateTextAgent"),
    ("nova.audio.transcript", "app.agents.transcript", "TranscriptAgent"),
    ("nova.audio.template_recipe", "app.agents.audio_template", "AudioTemplateAgent"),
    ("nova.audio.song_classifier", "app.agents.song_classifier", "SongClassifierAgent"),
    ("nova.audio.song_sections", "app.agents.song_sections", "SongSectionsAgent"),
    ("nova.audio.music_matcher", "app.agents.music_matcher", "MusicMatcherAgent"),
    ("nova.compose.platform_copy", "app.agents.platform_copy", "PlatformCopyAgent"),
    ("nova.compose.creative_direction", "app.agents.creative_direction", "CreativeDirectionAgent"),
    # New agents (built on the runtime; no platform plumbing)
    ("nova.layout.text_designer", "app.agents.text_designer", "TextDesignerAgent"),
    ("nova.video.shot_ranker", "app.agents.shot_ranker", "ShotRankerAgent"),
    ("nova.layout.transition_picker", "app.agents.transition_picker", "TransitionPickerAgent"),
    ("nova.audio.beat_aligner", "app.agents.beat_aligner", "BeatAlignerAgent"),
    ("nova.video.clip_router", "app.agents.clip_router", "ClipRouterAgent"),
    ("nova.qa.output_validator", "app.agents.output_validator", "OutputValidatorAgent"),
)


def AGENTS() -> dict[str, type[Agent]]:  # noqa: N802 — exposed as constant-ish public name
    """Return the full {name: class} map. Lazy: only imports modules that exist."""
    out: dict[str, type[Agent]] = {}
    for name, mod_path, cls_name in _REGISTRATIONS:
        cls = _try_load(mod_path, cls_name)
        if cls is not None:
            out[name] = cls
    return out


def get_agent(name: str) -> type[Agent]:
    """Look up an agent class by namespaced name. Raises KeyError if not registered."""
    for reg_name, mod_path, cls_name in _REGISTRATIONS:
        if reg_name == name:
            cls = _try_load(mod_path, cls_name)
            if cls is None:
                raise KeyError(f"agent {name!r} registered but module not yet implemented")
            return cls
    raise KeyError(f"unknown agent: {name!r}")


def _try_load(mod_path: str, cls_name: str) -> type[Agent] | None:
    import importlib

    try:
        mod = importlib.import_module(mod_path)
    except ImportError:
        return None
    return getattr(mod, cls_name, None)

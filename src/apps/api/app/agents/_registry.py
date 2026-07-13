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
    (
        "nova.compose.agentic_style_selector",
        "app.agents.agentic_style_selector",
        "AgenticStyleSelectorAgent",
    ),
    ("nova.audio.transcript", "app.agents.transcript", "TranscriptAgent"),
    ("nova.audio.lyrics", "app.agents.lyrics", "LyricsExtractionAgent"),
    ("nova.audio.template_recipe", "app.agents.audio_template", "AudioTemplateAgent"),
    ("nova.audio.song_classifier", "app.agents.song_classifier", "SongClassifierAgent"),
    ("nova.audio.song_sections", "app.agents.song_sections", "SongSectionsAgent"),
    ("nova.audio.music_matcher", "app.agents.music_matcher", "MusicMatcherAgent"),
    # plans/010: abandoned-take spans merged into the silence-cut CutPlan — wired
    # into generative_build._silence_cut_retake_spans behind RETAKE_CUT_ENABLED,
    # failure-isolated (detector failure ⇒ zero retake cuts, never a failed job).
    ("nova.audio.retake_detector", "app.agents.retake_detector", "RetakeDetectorAgent"),
    (
        "nova.audio.lyric_style_selector",
        "app.agents.lyric_style_selector",
        "LyricStyleSelectorAgent",
    ),
    # Generative-edit text agents (no reference template; AI-authored overlay text)
    (
        "nova.compose.overlay_format_matcher",
        "app.agents.overlay_format_matcher",
        "OverlayFormatMatcherAgent",
    ),
    ("nova.compose.intro_writer", "app.agents.intro_writer", "IntroTextWriterAgent"),
    ("nova.compose.sequence_emphasis", "app.agents.sequence_emphasis", "SequenceEmphasisAgent"),
    (
        "nova.compose.sequence_quote",
        "app.agents.sequence_quote_writer",
        "SequenceQuoteWriterAgent",
    ),
    ("nova.compose.platform_copy", "app.agents.platform_copy", "PlatformCopyAgent"),
    ("nova.compose.creative_direction", "app.agents.creative_direction", "CreativeDirectionAgent"),
    # New agents (built on the runtime; no platform plumbing)
    ("nova.layout.text_designer", "app.agents.text_designer", "TextDesignerAgent"),
    ("nova.video.shot_ranker", "app.agents.shot_ranker", "ShotRankerAgent"),
    ("nova.layout.transition_picker", "app.agents.transition_picker", "TransitionPickerAgent"),
    ("nova.audio.beat_aligner", "app.agents.beat_aligner", "BeatAlignerAgent"),
    ("nova.video.clip_router", "app.agents.clip_router", "ClipRouterAgent"),
    ("nova.qa.output_validator", "app.agents.output_validator", "OutputValidatorAgent"),
    # Layer-2 text-overlay pipeline agents (slices E + F)
    ("nova.compose.text_alignment", "app.agents.text_alignment", "TextAlignmentAgent"),
    (
        "nova.compose.text_classification",
        "app.agents.text_classification",
        "TextClassificationAgent",
    ),
    # Content-plan agents (no Job; off-job AgentRun path with job_id=None)
    ("nova.plan.persona_generator", "app.agents.persona_generator", "PersonaGeneratorAgent"),
    (
        "nova.plan.content_plan_generator",
        "app.agents.content_plan_generator",
        "ContentPlanGeneratorAgent",
    ),
    ("nova.plan.clip_plan_matcher", "app.agents.clip_plan_matcher", "ClipPlanMatcherAgent"),
    # Creator Agent M1: derive per-user style from persona + TikTok analysis.
    ("nova.plan.style_derivation", "app.agents.style_derivation", "StyleDerivationAgent"),
    # Creator Agent M2: parse a free-text style utterance into a typed intent.
    ("nova.plan.style_intent", "app.agents.style_intent", "StyleIntentAgent"),
    # Edit Copilot v1: parse full-editor chat turns into draft edit ops.
    ("nova.edit.copilot", "app.agents.edit_copilot", "EditCopilotAgent"),
    # Creator Agent M4: conformance verdict at clip-attach time (best-effort, display-only).
    (
        "nova.plan.conformance_feedback",
        "app.agents.conformance_feedback",
        "ConformanceFeedbackAgent",
    ),
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

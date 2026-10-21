"""KRI-786 guard: every agent prompt knows the creator's language, or says why it needn't.

Map O01-O18: only a handful of the ~74 agents are told which language to write in, so
interview questions, plan items, captions and review notes come back in English for a
Turkish creator (or in whatever language the model guesses).

An agent (a class with ``render_prompt``) has a language input when either:

- ``render_prompt`` or a same-module helper it calls by name uses an identifier containing
  ``language`` (``prompt_language_line(...)``, ``input.language``, ``caption_language=``), or
- a prompt file it names (``prompts/<name>.txt``, via ``load_prompt("<name>")`` or the
  spec's ``prompt_id``) has a ``{...language...}`` placeholder.

Otherwise it must be in ``LANGUAGE_NEUTRAL`` (with a one-line reason: it writes nothing a
creator reads) or in ``LANGUAGE_GAPS``, the shrinking baseline lane D of the Turkish parity
project is fixing. A gap that gets a language input should leave ``LANGUAGE_GAPS``; the
test prints a hint but does not fail, so parallel lanes don't block each other.
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

_API = Path(__file__).resolve().parents[2]
_APP = _API / "app"
_PROMPTS = _API / "prompts"
_PLACEHOLDER = re.compile(r"\{\w*language\w*\}", re.IGNORECASE)

# Agents whose output no creator reads as prose: ids, scores, timings, styles, or text
# copied verbatim from the media. Keep the reason to one line.
LANGUAGE_NEUTRAL: dict[str, str] = {
    "agents/_runtime.py::Agent": "abstract base; subclasses are checked",
    "agents/agentic_style_selector.py::AgenticStyleSelectorAgent": "picks a style id",
    "agents/audio_template.py::AudioTemplateAgent": "song structure analysis",
    "agents/beat_aligner.py::BeatAlignerAgent": "beat timing",
    "agents/clip_metadata.py::ClipMetadataAgent": "machine metadata, never on-screen text",
    "agents/clip_plan_matcher.py::ClipPlanMatcherAgent": "matches clip ids to plan items",
    "agents/clip_router.py::ClipRouterAgent": "assigns clip ids to slots",
    "agents/creative_direction.py::CreativeDirectionAgent": "template analysis",
    "agents/detect_plan_relevance.py::DetectPlanRelevanceAgent": "relevance verdict",
    "agents/image_metadata.py::ImageMetadataAgent": "machine metadata, never on-screen text",
    "agents/lyric_style_selector.py::LyricStyleSelectorAgent": "picks a style id",
    "agents/lyrics.py::LyricsExtractionAgent": "transcribes lyrics in the song's language",
    "agents/narration_focus.py::NarrationFocusAgent": "subject counts and frame ids",
    "agents/output_validator.py::OutputValidatorAgent": "pre-publish technical gate",
    "agents/overlay_placement.py::OverlayPlacementAgent": "overlay positions",
    "agents/phone_typography.py::PhoneTypographyAgent": "font and size choice",
    "agents/sfx_placement.py::SfxPlacementAgent": "sound-effect timing",
    "agents/shot_ranker.py::ShotRankerAgent": "ranks moments",
    "agents/song_classifier.py::SongClassifierAgent": "labels from a fixed vocabulary",
    "agents/song_sections.py::SongSectionsAgent": "song section timing",
    "agents/style_observation.py::StyleObservationAgent": "semantic style vocabulary",
    "agents/template_recipe.py::TemplateRecipeAgent": "template analysis",
    "agents/template_text.py::TemplateTextAgent": "reads template text as written",
    "agents/text_alignment.py::TextAlignmentAgent": "times given text to a transcript",
    "agents/text_classification.py::TextClassificationAgent": "classifies overlay roles",
    "agents/text_designer.py::TextDesignerAgent": "label styling",
    "agents/tiktok_analyzer.py::TikTokAnalyzerAgent": "internal analysis fed to prompts",
    "agents/transcript.py::TranscriptAgent": "transcribes speech as spoken",
    "agents/transition_picker.py::TransitionPickerAgent": "picks transition ids",
}

# Agents that write creator-facing prose with no language input (parity map O02-O17).
# Shrink-only: remove an entry once the agent gets prompt_language_line or a language var.
LANGUAGE_GAPS: frozenset[str] = frozenset(
    {
        "agents/brief_extractor.py::BriefExtractorAgent",
        "agents/clip_question.py::ClipQuestionAgent",
        "agents/conformance_feedback.py::ConformanceFeedbackAgent",
        "agents/content_plan_generator.py::ContentPlanGeneratorAgent",
        "agents/creator_memory_extractor.py::CreatorMemoryExtractorAgent",
        "agents/edit_director.py::EditDirectorAgent",
        "agents/edit_guide.py::EditGuideAgent",
        "agents/edit_proposal.py::EditProposalAgent",
        "agents/idea_expander.py::IdeaExpanderAgent",
        "agents/interviewer_agent.py::InterviewerAgent",
        "agents/montage_reviewer.py::MontageReviewAgent",
        "agents/narration_annotations.py::NarrationAnnotationAgent",
        "agents/persona_generator.py::PersonaGeneratorAgent",
        "agents/plan_item_advisor.py::PlanItemAdvisorAgent",
        "agents/platform_copy.py::PlatformCopyAgent",
        "agents/semantic_edit_proposal.py::SemanticEditProposalAgent",
        "agents/shot_list_writer.py::ShotListWriterAgent",
        "agents/slide_post_composer.py::SlidePostComposerAgent",
        "agents/speech_excerpt_planner.py::SpeechExcerptPlannerAgent",
        "agents/style_derivation.py::StyleDerivationAgent",
        "agents/style_intent.py::StyleIntentAgent",
        "agents/visual_treatment_planner.py::VisualTreatmentPlannerAgent",
        "agents/voiceover_interviewer.py::VoiceoverInterviewerAgent",
        "agents/voiceover_script_writer.py::VoiceoverScriptWriterAgent",
        "services/creation_text_composition.py::CreationTextComposer",
        "services/researched_text.py::GroundedResearchAgent",
        "services/researched_text.py::ResearchCopyAgent",
        "services/researched_text.py::ResearchReviewAgent",
    }
)


def _render_prompt_units() -> dict[str, bool]:
    """``"<module>::<Class>" -> has a language input`` for every class with render_prompt."""
    stems = {p.stem for p in _PROMPTS.glob("*.txt")}
    units: dict[str, bool] = {}
    for path in sorted(_APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "def render_prompt" not in source:
            continue
        tree = ast.parse(source)
        helpers = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            render = next(
                (
                    m
                    for m in cls.body
                    if isinstance(m, ast.FunctionDef) and m.name == "render_prompt"
                ),
                None,
            )
            if render is None:
                continue
            scanned: list[ast.AST] = [render]
            scanned += [
                helpers[n.func.id]
                for n in ast.walk(render)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in helpers
            ]
            idents: set[str] = set()
            for node in scanned:
                for n in ast.walk(node):
                    if isinstance(n, ast.Name):
                        idents.add(n.id)
                    elif isinstance(n, ast.Attribute):
                        idents.add(n.attr)
                    elif isinstance(n, ast.keyword | ast.arg) and n.arg:
                        idents.add(n.arg)
            prompt_files = {
                n.value
                for root in (cls, *scanned)
                for n in ast.walk(root)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in stems
            }
            has_language = any("language" in i.lower() for i in idents) or any(
                _PLACEHOLDER.search((_PROMPTS / f"{stem}.txt").read_text(encoding="utf-8"))
                for stem in prompt_files
            )
            units[f"{path.relative_to(_APP)}::{cls.name}"] = has_language
    return units


def test_every_agent_prompt_has_a_language_input_or_a_reason():
    units = _render_prompt_units()
    unexplained = sorted(
        unit
        for unit, has_language in units.items()
        if not has_language and unit not in LANGUAGE_NEUTRAL and unit not in LANGUAGE_GAPS
    )
    fixed = sorted(unit for unit in LANGUAGE_GAPS if units.get(unit))
    if fixed:
        warnings.warn(
            f"KRI-786: these agents now have a language input; drop them from LANGUAGE_GAPS: "
            f"{fixed}",
            stacklevel=1,
        )
    assert not unexplained, (
        f"KRI-786: agents with no language input: {unexplained}. Pass the creator's language "
        "(app.kria.reply_language.prompt_language_line, or a `{language}` placeholder fed "
        "from the job/thread), or, if the agent writes nothing a creator reads, add it to "
        "LANGUAGE_NEUTRAL with a one-line reason. See agents/DECISIONS.md 'Language policy'."
    )


def test_allowlists_name_real_agents():
    units = _render_prompt_units()
    stale = sorted((set(LANGUAGE_NEUTRAL) | LANGUAGE_GAPS) - set(units))
    assert not stale, f"no such agent (renamed or removed?): {stale}"
    assert not set(LANGUAGE_NEUTRAL) & LANGUAGE_GAPS
    assert all(reason.strip() for reason in LANGUAGE_NEUTRAL.values())

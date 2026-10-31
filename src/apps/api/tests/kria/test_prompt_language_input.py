"""KRI-786 guard: every agent prompt knows the creator's language, or says why it needn't.

Map O01-O18: only a handful of the ~74 agents are told which language to write in, so
interview questions, plan items, captions and review notes come back in English for a
Turkish creator (or in whatever language the model guesses).

An agent (a class with ``render_prompt``) has a language input when either:

- ``render_prompt`` or a same-module helper it calls by name uses an identifier containing
  ``language`` (``prompt_language_line(...)``, ``input.language``, ``caption_language=``)
  outside a ``load_prompt(...)`` call, or
- a prompt file it names (``prompts/<name>.txt``, via ``load_prompt("<name>")`` or the
  spec's ``prompt_id``) has a ``$language`` / ``${language_hint}`` placeholder.
  ``load_prompt`` fills prompts with ``string.Template``, so only ``$name`` is substituted;
  a ``{language}`` brace placeholder reaches the model as literal text and does not count
  (``test_load_prompt_variables_are_template_placeholders`` fails on one).

A ``language`` field with a concrete default on the agent's input (``language_hint: str =
"en"``) only counts when every construction of that input in ``app/`` passes it by keyword;
otherwise a Turkish creator silently runs as English (map O-rows, SequenceEmphasisAgent).
``test_inputs_with_a_default_language_are_always_given_one`` keeps omissions from growing.

Otherwise it must be in ``LANGUAGE_NEUTRAL`` (with a one-line reason: it writes nothing a
creator reads) or in ``LANGUAGE_GAPS``, the shrinking baseline lane D of the Turkish parity
project is fixing. A gap that gets a language input should leave ``LANGUAGE_GAPS``; the
test prints a hint but does not fail, so parallel lanes don't block each other.
"""

from __future__ import annotations

import ast
import re
import warnings
from functools import cache
from pathlib import Path

_API = Path(__file__).resolve().parents[2]
_APP = _API / "app"
_PROMPTS = _API / "prompts"
# ``string.Template`` placeholders (``$language``, ``${language_hint}``): what load_prompt fills.
_PLACEHOLDER = re.compile(r"\$\{?\w*language\w*\}?", re.IGNORECASE)

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
    "agents/media_moment_relevance.py::MomentRelevanceAgent": "returns moment ids",
    "agents/narration_focus.py::NarrationFocusAgent": "subject counts and frame ids",
    "agents/output_validator.py::OutputValidatorAgent": "pre-publish technical gate",
    "agents/overlay_placement.py::OverlayPlacementAgent": "overlay positions",
    "agents/phone_typography.py::PhoneTypographyAgent": "font and size choice",
    "agents/sfx_placement.py::SfxPlacementAgent": "sound-effect timing",
    "agents/shot_ranker.py::ShotRankerAgent": "ranks moments",
    "agents/song_classifier.py::SongClassifierAgent": "labels from a fixed vocabulary",
    "agents/song_sections.py::SongSectionsAgent": "song section timing",
    "agents/source_moment_location.py::SourceMomentLocationAgent": (
        "source timestamps and internal evidence; creator labels copy request words"
    ),
    "agents/style_observation.py::StyleObservationAgent": "semantic style vocabulary",
    "agents/template_recipe.py::TemplateRecipeAgent": "template analysis",
    "agents/template_text.py::TemplateTextAgent": "reads template text as written",
    "agents/text_alignment.py::TextAlignmentAgent": "times given text to a transcript",
    "agents/text_classification.py::TextClassificationAgent": "classifies overlay roles",
    "agents/text_designer.py::TextDesignerAgent": "label styling",
    "agents/tiktok_analyzer.py::TikTokAnalyzerAgent": "internal analysis fed to prompts",
    "agents/transcript.py::TranscriptAgent": "transcribes speech as spoken",
    "agents/transition_picker.py::TransitionPickerAgent": "picks transition ids",
    "services/story_idea_research.py::StoryResearchReviewAgent": "approval verdict on ideas",
    "services/story_idea_research.py::StorySourceReviewAgent": "approval verdict on ideas",
}

# Agents that write creator-facing prose with no language input (parity map O02-O17).
# Shrink-only: remove an entry once the agent gets prompt_language_line or a language var.
LANGUAGE_GAPS: frozenset[str] = frozenset(
    {
        "agents/brief_extractor.py::BriefExtractorAgent",
        # #1685 (camera-roll ideas): writes title/hook/payoff a creator reads, no language.
        "agents/camera_roll_stories.py::CameraRollStoriesAgent",
        "agents/creator_memory_extractor.py::CreatorMemoryExtractorAgent",
        "agents/edit_director.py::EditDirectorAgent",
        "agents/edit_guide.py::EditGuideAgent",
        "agents/montage_reviewer.py::MontageReviewAgent",
        "agents/narration_annotations.py::NarrationAnnotationAgent",
        # Its input defaults language_hint="en" and generative_build never passes it.
        "agents/sequence_emphasis.py::SequenceEmphasisAgent",
        # Passes language=, but its prompt writes `{language}` (brace): load_prompt never fills
        # it, so the model reads the literal text. See KNOWN_UNFILLED_PLACEHOLDERS.
        "agents/smart_edit_planner.py::SmartEditPlannerAgent",
        "agents/speech_excerpt_planner.py::SpeechExcerptPlannerAgent",
        "agents/style_derivation.py::StyleDerivationAgent",
        "agents/visual_treatment_planner.py::VisualTreatmentPlannerAgent",
        "services/creation_text_composition.py::CreationTextComposer",
        "services/researched_text.py::GroundedResearchAgent",
        "services/researched_text.py::ResearchCopyAgent",
        "services/researched_text.py::ResearchReviewAgent",
    }
)

# ``load_prompt("<stem>", name=...)`` keywords whose prompt file has ``{name}`` but no
# ``$name``: string.Template never fills them, so the model sees the literal braces.
# Shrink-only; fix the prompt to ``$name`` (and bump the agent's prompt_version).
KNOWN_UNFILLED_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "smart_edit_planner:assets_json",
        "smart_edit_planner:candidates_json",
        "smart_edit_planner:language",
        "smart_edit_planner:preset_id",
        "smart_edit_planner:preset_version",
        "smart_edit_planner:words_json",
    }
)

# Constructions of an agent ``*Input`` that leave a defaulted language field at its English
# default (``"<module>:<Input>.<field>"`` -> count). Shrink-only.
DEFAULT_LANGUAGE_OMISSIONS: dict[str, int] = {
    "tasks/generative_build.py:SequenceEmphasisInput.language_hint": 1,
}


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


@cache  # parsed once per session: every test here walks all of app/
def _app_trees() -> tuple[tuple[str, ast.Module], ...]:
    return tuple(
        (str(path.relative_to(_APP)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_APP.rglob("*.py"))
    )


def _defaulted_language_fields(trees) -> dict[str, tuple[str, ...]]:
    """``*Input`` class -> its language fields that default to a concrete language code."""
    fields: dict[str, list[str]] = {}
    for _, tree in trees:
        for cls in ast.walk(tree):
            if not (isinstance(cls, ast.ClassDef) and cls.name.endswith("Input")):
                continue
            for stmt in cls.body:
                if (
                    isinstance(stmt, ast.AnnAssign)
                    and isinstance(stmt.target, ast.Name)
                    and "language" in stmt.target.id.lower()
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                    and stmt.value.value
                ):
                    fields.setdefault(cls.name, []).append(stmt.target.id)
    return {name: tuple(f) for name, f in fields.items()}


def _default_language_omissions(trees) -> dict[str, int]:
    defaulted = _defaulted_language_fields(trees)
    found: dict[str, int] = {}
    for rel, tree in trees:
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or _callee(call) not in defaulted:
                continue
            given = {k.arg for k in call.keywords}
            if None in given:  # **kwargs: cannot tell statically
                continue
            for field in defaulted[_callee(call)]:
                if field not in given:
                    key = f"{rel}:{_callee(call)}.{field}"
                    found[key] = found.get(key, 0) + 1
    return found


def _load_prompt_calls(tree: ast.AST):
    for call in ast.walk(tree):
        if (
            isinstance(call, ast.Call)
            and _callee(call) == "load_prompt"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ):
            yield call


def _prompt_text(stem: str) -> str | None:
    path = _PROMPTS / f"{stem}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else None


def _template_has(text: str, name: str) -> bool:
    return bool(re.search(rf"\$(?:{name}\b|\{{{name}\}})", text))


@cache
def _render_prompt_units() -> dict[str, bool]:
    """``"<module>::<Class>" -> has a language input`` for every class with render_prompt."""
    stems = {p.stem for p in _PROMPTS.glob("*.txt")}
    trees = _app_trees()
    omitted_inputs = {
        key.split(":", 1)[1].split(".")[0] for key in _default_language_omissions(trees)
    }
    units: dict[str, bool] = {}
    for rel, tree in trees:
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
            # Identifiers inside load_prompt(...) only count if the prompt really has $name.
            inside_load = {
                id(n) for root in scanned for c in _load_prompt_calls(root) for n in ast.walk(c)
            }
            idents: set[str] = set()
            for node in scanned:
                for n in ast.walk(node):
                    if id(n) in inside_load:
                        continue
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
            input_cls = next(
                (
                    stmt.value.id
                    for stmt in cls.body
                    if isinstance(stmt, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "Input" for t in stmt.targets)
                    and isinstance(stmt.value, ast.Name)
                ),
                None,
            )
            if input_cls in omitted_inputs:
                has_language = False
            units[f"{rel}::{cls.name}"] = has_language
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
        "(app.kria.reply_language.prompt_language_line, or a `$language` placeholder fed "
        "from the job/thread; load_prompt is string.Template, so `{language}` is never "
        "filled), or, if the agent writes nothing a creator reads, add it to "
        "LANGUAGE_NEUTRAL with a one-line reason. See agents/DECISIONS.md 'Language policy'."
    )


def test_allowlists_name_real_agents():
    units = _render_prompt_units()
    stale = sorted((set(LANGUAGE_NEUTRAL) | LANGUAGE_GAPS) - set(units))
    assert not stale, f"no such agent (renamed or removed?): {stale}"
    assert not set(LANGUAGE_NEUTRAL) & LANGUAGE_GAPS
    assert all(reason.strip() for reason in LANGUAGE_NEUTRAL.values())


def test_load_prompt_variables_are_template_placeholders():
    """``load_prompt`` is ``string.Template.safe_substitute``: a variable the prompt spells
    ``{name}`` instead of ``$name`` is silently never filled."""
    unfilled = set()
    for _, tree in _app_trees():
        for call in _load_prompt_calls(tree):
            stem = call.args[0].value
            text = _prompt_text(stem)
            if text is None:
                continue
            for kw in call.keywords:
                if kw.arg and not _template_has(text, kw.arg) and f"{{{kw.arg}}}" in text:
                    unfilled.add(f"{stem}:{kw.arg}")
    new = sorted(unfilled - KNOWN_UNFILLED_PLACEHOLDERS)
    fixed = sorted(KNOWN_UNFILLED_PLACEHOLDERS - unfilled)
    if fixed:
        warnings.warn(f"KRI-786: drop from KNOWN_UNFILLED_PLACEHOLDERS: {fixed}", stacklevel=1)
    assert not new, (
        f"KRI-786: load_prompt variables written as `{{name}}` in the prompt: {new}. "
        "load_prompt uses string.Template, so write `$name` (or `${name}`)."
    )


def test_inputs_with_a_default_language_are_always_given_one():
    """An agent input whose language field defaults to ``"en"`` must be given the creator's
    language at every construction, or a Turkish creator's run is silently English."""
    found = _default_language_omissions(_app_trees())
    grew = {
        key: (n, DEFAULT_LANGUAGE_OMISSIONS.get(key, 0))
        for key, n in found.items()
        if n > DEFAULT_LANGUAGE_OMISSIONS.get(key, 0)
    }
    fixed = sorted(k for k, base in DEFAULT_LANGUAGE_OMISSIONS.items() if found.get(k, 0) < base)
    if fixed:
        warnings.warn(f"KRI-786: lower DEFAULT_LANGUAGE_OMISSIONS: {fixed}", stacklevel=1)
    assert not grew, (
        f"KRI-786: agent inputs built without their language field (now, baseline): {grew}. "
        "Pass the creator's language (thread/job language) by keyword."
    )

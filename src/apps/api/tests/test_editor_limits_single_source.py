"""Guard: an editor limit is written once, in `app/services/editor_limits.py` (KRI-1377).

The per-word text floor was copied into five files and the prompts; a fix to the server copy left
the refusal in place. Ways a limit can be copied again, and the assertion that catches each:
* a Python module retypes the playback-rate bounds in a `Field(ge=, le=)` or a range compare
  (`test_python_does_not_retype_the_playback_rate`);
* a Swift file retypes a range or the minimum clip length instead of reading `EditorLimits` /
  `TextAnimationPhases.speedRange` (`test_swift_does_not_retype_editor_limits`);
* a prompt types a number the prompt renderer already knows
  (`test_prompts_render_limits_instead_of_typing_them`);
* a rendered prompt keeps an unresolved `{{TOKEN}}` / `$variable`, which the model would read
  literally (`test_edit_copilot_prompt_renders_every_limit`);
* the generated Swift drifts from Python (`test_native_text_contract.py --check`, already in CI).
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.services import editor_limits

API_ROOT = Path(__file__).resolve().parents[1]
IOS_ROOT = API_ROOT.parents[1] / "apps" / "ios"

# Different limits that happen to share a value. A file here is exempt for the stated reason.
_PYTHON_PLAYBACK_ALLOWED = {
    "app/agents/editor_ops_v2/text.py": "text `size_scale`, a size multiplier",
    "app/kria/portable_text.py": "text motion speed, its own range",
    "app/agents/_schemas/text_element.py": "text motion speed, its own range",
    "app/pipeline/reframe.py": "ffmpeg speed_factor of a template slot, not an editor clip",
    "app/pipeline/agents/gemini_analyzer.py": "template-recipe slot speed_factor clamp",
    "app/agents/template_recipe.py": "template-recipe slot speed_factor clamp",
    "app/pipeline/custom_effects.py": "custom effect `speed` parameter",
    "app/pipeline/text_motion_v2.py": "text motion speed, its own range",
}
_PYTHON_PLAYBACK = re.compile(r"\bge=0\.25\b|\b0\.25 <= \w+ <= 4\b|\b0\.25, 4(?:\.0)?\)")

_SWIFT_ALLOWED = {
    "Kria/Core/NativeEditorRenderCompiler.swift": ('number("speed", "speed", 1, 0.25, 4)',),
    "Packages/KriaMediaEngine/Sources/KriaMediaEngine/TextMotionTiming.swift": (
        "(speed, 0.25...4)",
    ),
}
_SWIFT_LIMIT_LITERALS = re.compile(
    r"0\.25\.\.\.[34]\b|\bmax\(rate, 0\.25\)|"
    r"minimum(?:Clip)?Duration\b[^\n=]*=\s*0\.1\s*$|minimum: TimeInterval = 0\.1\b",
    re.M,
)

_PROMPT_TYPED_LIMITS = (
    (re.compile(r"seconds, 0\.1 or more"), "the minimum clip length"),
    (re.compile(r"less than 0\.1 seconds"), "the minimum clip length"),
    (re.compile(r'"playback_rate" \(0\.25'), "the playback-rate range"),
    (re.compile(r"between 0\.25 and 4"), "the playback-rate range"),
    (re.compile(r"\bspeed (?:is )?0\.25-3"), "the animation-speed range"),
    (re.compile(r"\(1/30 s\)"), "the output frame length"),
)


def _python_sources() -> list[Path]:
    return sorted((API_ROOT / "app").rglob("*.py"))


def test_python_does_not_retype_the_playback_rate() -> None:
    found = [
        f"{path.relative_to(API_ROOT)}:{n}"
        for path in _python_sources()
        if path.relative_to(API_ROOT).as_posix() not in _PYTHON_PLAYBACK_ALLOWED
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _PYTHON_PLAYBACK.search(line)
    ]
    assert not found, (
        "Import PLAYBACK_RATE_MIN / PLAYBACK_RATE_MAX from app.services.editor_limits:\n"
        + "\n".join(found)
    )


def test_swift_does_not_retype_editor_limits() -> None:
    found: list[str] = []
    for root in (IOS_ROOT / "Kria", IOS_ROOT / "Packages/KriaMediaEngine/Sources"):
        for path in sorted(root.rglob("*.swift")):
            if ".generated." in path.name:
                continue
            rel = path.relative_to(IOS_ROOT).as_posix()
            exempt = _SWIFT_ALLOWED.get(rel, ())
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _SWIFT_LIMIT_LITERALS.search(line) and not any(e in line for e in exempt):
                    found.append(f"{rel}:{n}: {line.strip()}")
    assert not found, (
        "Read EditorLimits.* / TextAnimationPhases.speedRange (generated from "
        "app/services/editor_limits.py by scripts/generate_native_text_contract.py):\n"
        + "\n".join(found)
    )


def test_prompts_render_limits_instead_of_typing_them() -> None:
    found = [
        f"{path.relative_to(API_ROOT)}:{n} types {what}"
        for path in sorted((API_ROOT / "prompts").rglob("*.txt"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        for pattern, what in _PROMPT_TYPED_LIMITS
        if pattern.search(line)
    ]
    assert not found, (
        "Use a {{TOKEN}} (app/agents/prompt_vocab.py) or a load_prompt variable:\n"
        + "\n".join(found)
    )


def test_edit_copilot_prompt_renders_every_limit() -> None:
    snapshot = {
        "editor_ops_version": 2,
        "allowed_op_families": ["clip", "text"],
        "timeline_patch_capabilities": {"playback_rate": True},
    }
    prompt = EditCopilotAgent(MagicMock()).render_prompt(
        EditCopilotInput(utterance="make it faster", variant_snapshot=snapshot)
    )
    assert "{{" not in prompt and "$min_clip_s" not in prompt
    minimum = f"{editor_limits.EDITOR_MIN_CLIP_S:g}"
    assert f"less than {minimum} seconds of content" in prompt

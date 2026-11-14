"""Guard: an editor limit is written once, in `app/services/editor_limits.py` (KRI-1377).

The per-word text floor was copied into five files and the prompts; a fix to the server copy left
the refusal in place. Ways a limit can be copied again, and the assertion that catches each:
* a Python module retypes the playback-rate bounds in a `Field(ge=, le=)`, a range compare or a
  `*_RATE = 0.25` constant (`test_python_does_not_retype_the_playback_rate`);
* an editor-op, Save or split-screen module retypes the 0.1 s clip minimum or the 1/30 s frame
  (`test_editor_modules_do_not_retype_the_clip_minimum_or_frame`);
* a planner defines its own `*FPS = 30` or a Python module retypes the 2e-6 text-piece tolerance
  or a `0.25, 4` rate pair (`test_planners_do_not_define_their_own_frame_rate_constant`,
  `test_python_does_not_retype_the_text_piece_tolerance_or_a_rate_pair`);
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
    "app/services/editor_limits.py": "the single source",
    "app/agents/editor_ops_v2/text.py": "text `size_scale`, a size multiplier",
    "app/kria/portable_text.py": "text motion speed, its own range",
    "app/agents/_schemas/text_element.py": "text motion speed, its own range",
    "app/pipeline/agents/gemini_analyzer.py": "template-recipe slot speed_factor clamp",
    "app/agents/template_recipe.py": "template-recipe slot speed_factor clamp",
    "app/pipeline/custom_effects.py": "custom effect `speed` parameter",
    "app/pipeline/text_motion_v2.py": "text motion speed, its own range",
}
_PYTHON_PLAYBACK = re.compile(
    r"\bge=0\.25\b|\b0\.25 <= \w+ <= 4\b|\b0\.25, 4(?:\.0)?\)"
    r"|\b\w*(?:PLAYBACK_RATE|SPEED_RATE|(?:MIN|MAX)_RATE)\w*(?:: [^=]+)? = (?:0\.25|4(?:\.0)?)\s*$"
    r"|\b\w*rate\w*(?:\.\w+)* ?[<>]=? ?(?:0\.25|4(?:\.0)?)\b(?![0-9.])"
    r"|\b(?:0\.25|4(?:\.0)?) ?[<>]=? ?\w*rate\b"
    r"|\b(?:max|min)\(\w*rate\w*, (?:0\.25|4(?:\.0)?)\)"
)

# A module-level `*MIN*CLIP* = 0.1` is a second copy of `EDITOR_MIN_CLIP_S`, wherever it lives.
_MIN_CLIP_CONSTANT = re.compile(
    r"^_?\w*MIN\w*(?:CLIP|DURATION|SLOT)\w*(?:: [^=]+)? = (?:0\.1|1(?:\.0)? ?/ ?10)\s*(?:#.*)?$",
    re.M,
)
_MIN_CLIP_CONSTANT_ALLOWED = {
    "app/agents/_schemas/drawing.py": "a drawing stroke's own minimum duration",
    "app/agents/_schemas/visual_block.py": "a visual block's own minimum media duration",
}

# A frame-rate constant in the editor planners is a copy of `MOTION_FPS`. Renderer modules under
# app/pipeline and the proxy-transcode ceiling keep their own clocks (listed in the PR).
_FPS_CONSTANT = re.compile(
    r"^\s*_?\w*FPS\w*(?:: [^=]+)? = 30(?:\.0)?\s*(?:#.*)?$|^\s*fps = 30(?:\.0)?\s*$", re.M
)
_FPS_SCOPES = ("app/kria", "app/services", "app/agents")
_FPS_ALLOWED = {
    "app/services/image_to_video.py": "the image-to-video renderer clock",
    "app/services/visual_talking_takes.py": "the proxy transcode fps ceiling",
}
_TOLERANCE_LITERAL = re.compile(r"\b0\.000002\b|\b2e-0?6\b|\b1 ?/ ?500000\b")
_RATE_PAIR = re.compile(r"=\s*\(?0\.25, ?4(?:\.0)?\)?\s*$")

# Modules where an editor clip window is validated, saved or cut. A bare `0.1` / `1/30` here is a
# copy of `EDITOR_MIN_CLIP_S` / `FRAME_S`. Planner and renderer modules elsewhere keep their own
# floors for other reasons (export safety margins, transition overlaps, 30 fps renderer clocks)
# and are listed in the PR as follow-ups, not exempted here.
_EDITOR_MODULES = (
    "app/kria/split_full_screen.py",
    "app/agents/editor_ops_v2/split_screen.py",
    "app/agents/editor_ops_v2/timeline.py",
    "app/agents/_schemas/split_screen.py",
    "app/kria/clip_source_clock.py",
    "app/kria/split_screen.py",
    "app/services/initial_split_screen.py",
    "app/services/kria_editor_timeline.py",
    "app/services/phone_voiceover_cut_editor.py",
    "app/services/authored_editor.py",
    "app/services/kria_editor_ops_text.py",
    "app/services/kria_editor_ops.py",
    "app/agents/edit_copilot.py",
)
# A transition overlap floor is its own limit (0.1 s of dissolve), not a clip minimum.
_EDITOR_LINE_EXEMPT = "overlap"
# Lines in a listed module that carry a 0.1 s for another purpose.
_EDITOR_LINE_ALLOWED = {
    "app/kria/split_full_screen.py": ("and allowed >= 0.1",),  # transition overlap floor
    "app/agents/edit_copilot.py": (
        "total_s - 0.1",  # a sound effect may sit at most 0.1 s before the end
        'out["transition"] != "cut" and max_duration < 0.1',  # transition length floor
        "max(0.1, float(out.get(",  # transition length floor
    ),
}
_EDITOR_LITERALS = re.compile(
    r"\bge=0\.1\b|[<>]=? ?0\.1\b(?![0-9])|\b0\.1 ?[<>]=? |max\(0\.1,|\belse 0\.1\b"
    r"|- 0\.1\b(?![0-9])|(?:least|than) 0\.1\b|\b0\.1 seconds"
    r"|\b1(?:\.0)? ?/ ?30(?:\.0)?\b|\* ?30\) ?/ ?30\b|\b1(?:\.0)? ?/ ?10(?:\.0)?\b"
)

_SWIFT_ALLOWED = {
    "Kria/Core/NativeEditorRenderCompiler.swift": ('number("speed", "speed", 1, 0.25, 4)',),
    "Packages/KriaMediaEngine/Sources/KriaMediaEngine/TextMotionTiming.swift": (
        "(speed, 0.25...4)",
    ),
}
_SWIFT_LIMIT_LITERALS = re.compile(
    r"0\.25\.\.\.[34]\b|\bmax\(rate, 0\.25\)|"
    r"(?:uration|ength|pan|indow)\w*(?:\.\w+)* ?[<>]=? ?0\.1\b(?![0-9])|"
    r"\bminimum\w*\b[^\n=]*=\s*0\.1\s*$|minimum: TimeInterval = 0\.1\b|"
    r"\bmax\(0\.1, \w+\.end - \w+\.start\)|\(duration \* 30\)|"
    r"\b\w*[rR]ate\w*(?:\.\w+)* ?[<>]=? ?(?:0\.25|4(?:\.0)?)\b(?![0-9.])|"
    r"[<>]=? ?1 ?/ ?10\b|min\(3, max\(0\.25",
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


def test_no_module_defines_its_own_minimum_clip_constant() -> None:
    found = [
        f"{path.relative_to(API_ROOT)}:{n}: {match.group(0).strip()}"
        for path in _python_sources()
        if path.relative_to(API_ROOT).as_posix()
        not in {"app/services/editor_limits.py", *_MIN_CLIP_CONSTANT_ALLOWED}
        for text in [path.read_text(encoding="utf-8")]
        for match in _MIN_CLIP_CONSTANT.finditer(text)
        for n in [text.count("\n", 0, match.start()) + 1]
    ]
    assert not found, (
        "Import EDITOR_MIN_CLIP_S from app.services.editor_limits instead of redefining it:\n"
        + "\n".join(found)
    )


def test_planners_do_not_define_their_own_frame_rate_constant() -> None:
    found = [
        f"{rel}:{n}: {line.strip()}"
        for path in _python_sources()
        for rel in [path.relative_to(API_ROOT).as_posix()]
        if rel.startswith(_FPS_SCOPES) and rel not in _FPS_ALLOWED
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _FPS_CONSTANT.search(line)
    ]
    assert not found, "Import MOTION_FPS from app.services.editor_limits:\n" + "\n".join(found)


def test_python_does_not_retype_the_text_piece_tolerance_or_a_rate_pair() -> None:
    found = [
        f"{rel}:{n}: {line.strip()}"
        for path in _python_sources()
        for rel in [path.relative_to(API_ROOT).as_posix()]
        if rel != "app/services/editor_limits.py" and rel not in _PYTHON_PLAYBACK_ALLOWED
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _TOLERANCE_LITERAL.search(line) or _RATE_PAIR.search(line)
    ]
    assert not found, (
        "Import TEXT_PIECE_TOLERANCE_S / PLAYBACK_RATE_MIN / PLAYBACK_RATE_MAX from "
        "app.services.editor_limits:\n" + "\n".join(found)
    )


def test_editor_modules_do_not_retype_the_clip_minimum_or_frame() -> None:
    found = [
        f"{rel}:{n}: {line.strip()}"
        for rel in _EDITOR_MODULES
        for n, line in enumerate((API_ROOT / rel).read_text(encoding="utf-8").splitlines(), 1)
        if _EDITOR_LITERALS.search(line)
        and _EDITOR_LINE_EXEMPT not in line
        and not any(a in line for a in _EDITOR_LINE_ALLOWED.get(rel, ()))
        and not line.lstrip().startswith("#")
    ]
    assert not found, (
        "Import EDITOR_MIN_CLIP_S / FRAME_S from app.services.editor_limits and compare with "
        "below_editor_minimum (app.schemas.editor_window):\n" + "\n".join(found)
    )


def test_inline_prompts_render_the_frame_length() -> None:
    from app.services import creation_text_composition

    rendered = creation_text_composition._INSTRUCTIONS
    assert "{{" not in rendered
    assert f"(1/{editor_limits.MOTION_FPS} s)" in rendered


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


# The copilot's prompt_version is hand-numbered (tests, goldens and recorded threads pin it), so it
# does not move when a limit it renders changes. Pinning the limits here makes a limit change fail
# this test, which is where the version bump is demanded.
_COPILOT_RENDERED_LIMITS = (0.1, 0.25, 4.0, 0.25, 3, 30)


def test_copilot_limits_change_only_with_a_prompt_version_bump() -> None:
    current = (
        editor_limits.EDITOR_MIN_CLIP_S,
        editor_limits.PLAYBACK_RATE_MIN,
        editor_limits.PLAYBACK_RATE_MAX,
        editor_limits.ANIMATION_SPEED_MIN,
        editor_limits.ANIMATION_SPEED_MAX,
        editor_limits.MOTION_FPS,
    )
    assert current == _COPILOT_RENDERED_LIMITS, (
        "An editor limit the edit copilot prompt renders changed: bump "
        "EDIT_COPILOT_PROMPT_VERSION (app/agents/edit_copilot.py) in the same change, then "
        "update _COPILOT_RENDERED_LIMITS here."
    )

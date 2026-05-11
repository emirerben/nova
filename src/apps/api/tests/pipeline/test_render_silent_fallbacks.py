"""Regression guards against silent-fallback patterns in the render pipeline.

A silent fallback is when an exception is caught, logged with log.warning or
log.error, and execution continues (or returns early) without re-raising.
For render-pipeline code, this produces broken output that ships without any
visible failure surface — exactly how the slot-N curtain-close regression
(PR #103) went undetected for a full deploy cycle.

This test enforces a structural rule: any except handler around the listed
render-pipeline call sites must contain a `raise` statement. AST-based so
the test survives line-number drift and code reformatting.

Each guarded site is a recipe-defined visual element. Silent skip = the
output is missing something the recipe declared, with no error to the user.

Curtain-close is covered separately by test_curtain_close_timeout.py:
test_curtain_close_failure_is_not_swallowed.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _find_try_blocks_around(tree: ast.AST, *target_function_names: str) -> list[ast.Try]:
    """Return all ast.Try nodes whose body contains a call to any of the named
    functions. Walks the whole tree (does not constrain to a specific function
    scope) so the test stays resilient to refactors that move the call."""
    found: list[ast.Try] = []
    targets = set(target_function_names)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                name = None
                if isinstance(fn, ast.Name):
                    name = fn.id
                elif isinstance(fn, ast.Attribute):
                    name = fn.attr
                if name in targets:
                    found.append(node)
                    break
    return found


def _every_handler_raises(try_node: ast.Try) -> bool:
    if not try_node.handlers:
        return False
    for h in try_node.handlers:
        if not any(isinstance(n, ast.Raise) for n in ast.walk(h)):
            return False
    return True


def _orch_tree() -> ast.AST:
    path = (
        Path(__file__).resolve().parents[2]
        / "app" / "tasks" / "template_orchestrate.py"
    )
    return ast.parse(path.read_text())


def _text_overlay_tree() -> ast.AST:
    path = (
        Path(__file__).resolve().parents[2]
        / "app" / "pipeline" / "text_overlay.py"
    )
    return ast.parse(path.read_text())


def test_interstitial_render_failure_is_not_swallowed() -> None:
    """The except handler around `render_color_hold` (the interstitial render
    call site in template_orchestrate.py) must re-raise. A silent skip drops
    recipe-defined interstitials (curtain-hold, fade-to-black, flash) from
    the final video without surfacing the failure."""
    tries = _find_try_blocks_around(_orch_tree(), "render_color_hold")
    assert tries, (
        "no try/except wraps render_color_hold — either the call was moved or "
        "removed. If intentional, update this test."
    )
    for t in tries:
        assert _every_handler_raises(t), (
            "render_color_hold's except handler does not raise. Silent skip "
            "drops the recipe-defined interstitial from the final video. "
            "Re-raise (after structured logging) so the job fails loudly."
        )


def test_animated_overlay_failure_is_not_swallowed() -> None:
    """The except handler around `_write_animated_ass` in text_overlay.py must
    re-raise. A silent skip drops recipe-defined animated text (the BRAZIL
    font-cycle, hook titles, etc.) from the final video. Same class of bug
    as the curtain-close silent-fallback."""
    tries = _find_try_blocks_around(_text_overlay_tree(), "_write_animated_ass")
    assert tries, (
        "no try/except wraps _write_animated_ass — either the call was moved "
        "or removed. If intentional, update this test."
    )
    for t in tries:
        assert _every_handler_raises(t), (
            "_write_animated_ass's except handler does not raise. Silent skip "
            "drops recipe-defined animated text. Re-raise so the job fails "
            "loudly instead of producing partial output."
        )


def test_clip_probe_failure_is_not_swallowed() -> None:
    """The except handler around `probe_video` inside `_probe_clips` (the
    user-clip probe loop) must re-raise. Silent fallback to fabricated
    VideoProbe defaults (1920×1080/30fps/30s) makes downstream FFmpeg
    operate on lies — wrong trim points, wrong reframe geometry. ffprobe
    is mature; failure means corrupted/missing file, which the user needs
    to know about.

    Other probe_video call sites (template-reference fallback, stream-copy
    concat verification) are intentionally tolerant — those swallow probe
    errors to fall through to slow-path re-encode, which is correct."""
    tree = _orch_tree()
    # Find the _probe_clips function body specifically — narrower than
    # "all probe_video call sites" to avoid flagging legitimate fallbacks
    # elsewhere in the orchestrator.
    probe_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_probe_clips":
            probe_fn = node
            break
    assert probe_fn is not None, (
        "_probe_clips function not found in template_orchestrate.py — if "
        "renamed or removed, update this test."
    )
    tries = _find_try_blocks_around(probe_fn, "probe_video")
    assert tries, (
        "no try/except wraps probe_video inside _probe_clips — either the "
        "call was moved or removed. If intentional, update this test."
    )
    for t in tries:
        assert _every_handler_raises(t), (
            "_probe_clips's except handler around probe_video does not "
            "raise. Fabricating probe defaults silently produces wrong "
            "output downstream (wrong trim points, wrong reframe geometry). "
            "Re-raise so the job fails with a real diagnostic."
        )

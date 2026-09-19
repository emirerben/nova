"""Proves `check_edit_copilot_compiles` (runners/structural.py) actually catches
a plumbing regression that the parser-level `check_edit_copilot` floor cannot.

Regression under test: PR #1093 fixed `compile_editor_ops`'s `remove_text`
handling, which used to pop `text_elements` IN PLACE while iterating a bundle
of `remove_text` ops. Every `bar_index` in a bundle addresses the ORIGINAL
snapshot the model saw, not the shrinking list — popping in place walks the
indices off a moving target, deleting the wrong bars and then rejecting a
still-valid later index as out of range. This test reproduces that exact
pre-fix behavior as a monkeypatched stand-in for `compile_editor_ops` and
asserts the eval gate turns red — before #1093/#1100 shipped, nothing in the
eval suite would have caught this, because the parser's `ops` were already
schema-valid; only replaying them through the real compiler exposes the bug.
"""

from __future__ import annotations

from .runners import structural
from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "edit_copilot"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


def _golden(name: str):
    path = next(path for path in FIXTURE_PATHS if path.stem == name)
    return load_fixture(path)


def _pop_in_place_compile_editor_ops(job, variant, ops):
    """Pre-#1093 `remove_text`: pops `text_elements` in place while iterating,
    so `bar_index` walks off the shrinking list instead of the original one.
    """
    from app.routes.generative_jobs import EditorCommitRequest, variant_render_baseline
    from app.services.kria_editor_ops import CompiledEditorDraft, KriaEditorOpError

    text = [dict(row) for row in variant.get("text_elements") or [] if isinstance(row, dict)]
    for op in ops:
        if op.get("op") != "remove_text":
            raise KriaEditorOpError(f"{op.get('op')} is not portable to Kria yet")
        index = op.get("bar_index")
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(text):
            raise KriaEditorOpError("Text changed before this edit could be drafted")
        text.pop(index)  # <-- the bug: shrinks the list ops keep indexing into
    return CompiledEditorDraft(
        payload=EditorCommitRequest(
            text_elements=text, base_generation=variant_render_baseline(variant)
        ),
        changes=["Remove text"],
    )


def test_structural_check_catches_the_pre_1093_pop_in_place_regression(monkeypatch):
    """`story_remove_all_thought_bars` compiles fine against the real (fixed)
    `compile_editor_ops`; swapping in the pre-fix pop-in-place behavior must
    turn `check_edit_copilot_compiles` red on that exact golden.
    """
    fixture = _golden("story_remove_all_thought_bars")

    # Sanity: the real compiler passes this golden today (the fix is live).
    baseline = run_eval(fixture)
    assert baseline.passed, baseline.structural_failures

    monkeypatch.setattr(
        "app.services.kria_editor_ops.compile_editor_ops",
        _pop_in_place_compile_editor_ops,
    )

    regressed = run_eval(fixture)

    assert not regressed.passed
    assert len(regressed.structural_failures) == 1
    failure = regressed.structural_failures[0]
    assert fixture.fixture_id in failure
    assert "compile_editor_ops rejected" in failure
    assert "remove_text" in failure


def test_structural_check_reports_op_bundle_and_exception_text(monkeypatch):
    """Failures must name the golden, the rejected op(s), and the exception —
    not just "structural check failed"."""

    def _always_raise(job, variant, ops):
        from app.services.kria_editor_ops import KriaEditorOpError

        raise KriaEditorOpError("synthetic failure for op-bundle reporting")

    monkeypatch.setattr(
        "app.services.kria_editor_ops.compile_editor_ops",
        _always_raise,
    )

    fixture = _golden("story_remove_all_thought_bars")
    result = run_eval(fixture)

    assert not result.passed
    (failure,) = result.structural_failures
    assert failure.startswith(fixture.fixture_id)
    assert "remove_text" in failure
    assert "synthetic failure for op-bundle reporting" in failure


def test_allowlisted_golden_skips_the_compile_replay(monkeypatch):
    """An allowlisted stem must short-circuit before the (possibly still
    broken) compiler is ever invoked — allowlisting is a documented skip, not
    a swallowed failure."""

    def _boom(*_args, **_kwargs):
        raise AssertionError("compile_editor_ops must not run for an allowlisted golden")

    monkeypatch.setattr("app.services.kria_editor_ops.compile_editor_ops", _boom)
    monkeypatch.setitem(
        structural._EDITOR_OPS_ALLOWLIST,
        "story_remove_all_thought_bars",
        "test-only allowlist entry",
    )

    fixture = _golden("story_remove_all_thought_bars")
    result = run_eval(fixture)

    assert result.passed

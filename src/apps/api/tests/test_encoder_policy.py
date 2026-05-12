"""Encoder preset policy regression gate.

Locks the libx264 preset at every call site of
``app.pipeline.reframe._encoding_args`` so the BRAZIL pixelation /
blue-canopy banding bug stops resurfacing.

Background — read this before changing the allow-lists below
============================================================
libx264 ``preset=ultrafast`` disables ``mb-tree``, ``psy-rd``,
B-frames and trellis quant. On smooth gradients (sky, dark canopy)
those losses are visible as 16x16 macroblocking. CRF does NOT save
you — CRF controls the rate target; preset controls which encoder
features run.

Why a policy and not a single constant
--------------------------------------
The pipeline re-encodes each slot 3-4 times before output. Intermediates
get re-encoded downstream so ``ultrafast`` is fine; the bytes the user
actually sees come from the FINAL pass and must use ``fast`` or
stricter or banding becomes visible.

Three encode call sites in ``template_orchestrate.py`` were silently
left on ``ultrafast`` after PR #102 and PR #105 fixed the same bug for
curtain-close. This test prevents that drift from happening again.

How this test works
-------------------
* Walks the AST of every file in ``FILES_TO_AUDIT``.
* Finds every ``_encoding_args(...)`` call.
* Reads the ``preset=`` keyword (or the second positional arg if
  someone refactored the signature).
* Cross-references the call site (file + function name containing
  the call) against ``INTERMEDIATE_ALLOWLIST`` and
  ``FINAL_OUTPUT_REQUIRED``.
* Fails if a FINAL_OUTPUT site uses ``ultrafast`` (regression).
* Fails if a brand-new call site appears that isn't in either list
  (forces a conscious decision on the new site's quality budget).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Allow-lists. If you're updating these, read the file docstring first AND
# update the audit list in reframe._encoding_args.__doc__.
# ---------------------------------------------------------------------------

# (file_relative_to_apps_api, enclosing_function_name): preset is OK at "ultrafast"
# because the output is re-encoded by a downstream step before reaching the user.
INTERMEDIATE_ALLOWLIST: set[tuple[str, str]] = {
    ("app/pipeline/reframe.py", "reframe_and_export"),
    ("app/pipeline/reframe.py", "_build_overlay_cmd"),
}

# (file_relative_to_apps_api, enclosing_function_name): preset MUST be one of
# PRESETS_AT_OR_BETTER_THAN_FAST. These produce the bytes that ship to users.
FINAL_OUTPUT_REQUIRED: set[tuple[str, str]] = {
    ("app/tasks/template_orchestrate.py", "_concat_demuxer"),
    ("app/tasks/template_orchestrate.py", "_pre_burn_curtain_slot_text"),
    ("app/tasks/template_orchestrate.py", "_burn_text_overlays"),
}

# libx264 presets ordered from fastest to slowest. Anything at or stricter
# (slower) than "fast" keeps mb-tree + psy-rd enabled. "veryfast" / "superfast"
# / "ultrafast" all drop the features that protect smooth gradients.
PRESETS_FAST_OR_STRICTER: set[str] = {
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
    "placebo",
}

# Files under src/apps/api/ that may contain _encoding_args calls. If
# someone adds a new caller in a new file, add the file here AND tag each
# new call site in one of the allow-lists above.
FILES_TO_AUDIT: list[str] = [
    "app/pipeline/reframe.py",
    "app/tasks/template_orchestrate.py",
]


def _apps_api_root() -> Path:
    """Return src/apps/api/ regardless of pytest cwd."""
    here = Path(__file__).resolve()
    # tests/test_encoder_policy.py  -> tests/ -> apps/api/
    return here.parent.parent


def _find_enclosing_function(
    tree: ast.AST,
    target_line: int,
) -> str | None:
    """Return the innermost FunctionDef/AsyncFunctionDef name containing
    the given line, or None if at module scope.
    """
    best: tuple[int, str] | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            if start <= target_line <= end:
                # Pick the function whose start line is closest to (but
                # not after) target_line — that's the innermost match.
                if best is None or start > best[0]:
                    best = (start, node.name)
    return best[1] if best else None


def _extract_preset(call: ast.Call) -> str | None:
    """Pull the ``preset=`` value from an _encoding_args call literal.
    Returns None if not a string literal (e.g. variable, expression).
    """
    # Keyword first.
    for kw in call.keywords:
        if kw.arg == "preset" and isinstance(kw.value, ast.Constant):
            v = kw.value.value
            return v if isinstance(v, str) else None
    # Positional second arg (signature: _encoding_args(path, preset, crf)).
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        v = call.args[1].value
        return v if isinstance(v, str) else None
    return None


def _is_encoding_args_call(node: ast.AST) -> bool:
    """True for ``_encoding_args(...)``, including ``*_encoding_args(...)``
    and ``app.pipeline.reframe._encoding_args(...)`` style calls.
    """
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    if isinstance(fn, ast.Name) and fn.id == "_encoding_args":
        return True
    if isinstance(fn, ast.Attribute) and fn.attr == "_encoding_args":
        return True
    return False


def _collect_calls() -> list[tuple[str, int, str, str | None]]:
    """Walk every file in FILES_TO_AUDIT and return one tuple per
    _encoding_args call: (file_rel, lineno, enclosing_fn, preset_literal).
    """
    root = _apps_api_root()
    found: list[tuple[str, int, str, str | None]] = []
    for rel in FILES_TO_AUDIT:
        path = root / rel
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))
        # Find every Call node and check for _encoding_args.
        for node in ast.walk(tree):
            if _is_encoding_args_call(node):
                fn_name = _find_enclosing_function(tree, node.lineno) or "<module>"
                preset = _extract_preset(node)
                found.append((rel, node.lineno, fn_name, preset))
    return found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_ultrafast_in_final_output_path() -> None:
    """Every final-output _encoding_args site must use preset=fast or stricter.

    Regression for the BRAZIL pixelation / blue-canopy banding bug: PR #102
    and PR #105 fixed curtain-close by switching to preset=fast, but the
    same flip wasn't propagated to _concat_demuxer, _pre_burn_curtain_slot_text,
    or _burn_text_overlays in template_orchestrate.py. This test locks all
    three.
    """
    calls = _collect_calls()
    offending = [
        (rel, ln, fn, preset)
        for (rel, ln, fn, preset) in calls
        if (rel, fn) in FINAL_OUTPUT_REQUIRED
        and (preset is None or preset not in PRESETS_FAST_OR_STRICTER)
    ]
    assert not offending, (
        "FINAL-OUTPUT _encoding_args call(s) must use preset in "
        f"{sorted(PRESETS_FAST_OR_STRICTER)}.\n"
        "ultrafast/superfast/veryfast disable mb-tree+psy-rd and produce visible\n"
        "macroblocking on smooth gradients. See "
        "app/pipeline/reframe.py:_encoding_args docstring for the policy.\n"
        "Offending calls:\n  "
        + "\n  ".join(
            f"{rel}:{ln} in {fn}() — preset={preset!r}" for (rel, ln, fn, preset) in offending
        )
    )


def test_all_callsites_are_tagged() -> None:
    """Every _encoding_args call site must be classified as either
    INTERMEDIATE or FINAL_OUTPUT.

    Prevents a future PR from adding a new call site that's accidentally
    left on ultrafast without a conscious quality-budget decision.
    """
    calls = _collect_calls()
    untagged = [
        (rel, ln, fn, preset)
        for (rel, ln, fn, preset) in calls
        if (rel, fn) not in INTERMEDIATE_ALLOWLIST and (rel, fn) not in FINAL_OUTPUT_REQUIRED
    ]
    assert not untagged, (
        "Found _encoding_args call site(s) not tagged in either "
        "INTERMEDIATE_ALLOWLIST or FINAL_OUTPUT_REQUIRED. Add an entry "
        "to the appropriate set in this file and update the audit list "
        "in app/pipeline/reframe.py:_encoding_args.__doc__.\n"
        "Untagged calls:\n  "
        + "\n  ".join(
            f"{rel}:{ln} in {fn}() — preset={preset!r}" for (rel, ln, fn, preset) in untagged
        )
    )


def test_required_callsites_still_exist() -> None:
    """Catches a refactor that renames or removes one of the locked
    final-output sites — without this guard, _collect_calls() would return
    zero matches and test_no_ultrafast_in_final_output_path would pass
    vacuously.
    """
    calls = _collect_calls()
    seen = {(rel, fn) for (rel, _, fn, _) in calls}
    missing = FINAL_OUTPUT_REQUIRED - seen
    assert not missing, (
        f"Expected _encoding_args call inside these functions but found none: "
        f"{sorted(missing)}. If you renamed or removed the function, update "
        f"FINAL_OUTPUT_REQUIRED in this test."
    )


# Tune=film safety net for the curtain-close path (different module, but
# the same banding-on-gradients root cause).
def test_curtain_close_preset_is_fast_with_film_tune() -> None:
    """The curtain-close re-encode uses CURTAIN_CLOSE_X264_PRESET and
    CURTAIN_CLOSE_X264_TUNE — both must be set to values that keep mb-tree
    and deblocking active. PR #102 / PR #105 history.
    """
    from app.pipeline.interstitials import (  # noqa: PLC0415
        CURTAIN_CLOSE_X264_PRESET,
        CURTAIN_CLOSE_X264_TUNE,
    )

    assert CURTAIN_CLOSE_X264_PRESET in PRESETS_FAST_OR_STRICTER, (
        f"CURTAIN_CLOSE_X264_PRESET={CURTAIN_CLOSE_X264_PRESET!r} — must be "
        f"in {sorted(PRESETS_FAST_OR_STRICTER)} to keep mb-tree + psy-rd on."
    )
    assert CURTAIN_CLOSE_X264_TUNE == "film", (
        "tune=film tightens deblocking for the curtain-close motion — PR #102 history."
    )


@pytest.mark.parametrize(
    "callsite",
    sorted(FINAL_OUTPUT_REQUIRED),
    ids=lambda c: f"{c[0]}::{c[1]}",
)
def test_each_final_output_site_individually(
    callsite: tuple[str, str],
) -> None:
    """One sub-test per final-output site, so a failure pinpoints which one
    drifted. Belt-and-suspenders alongside test_no_ultrafast_in_final_output_path.
    """
    calls = _collect_calls()
    matches = [(rel, ln, fn, preset) for (rel, ln, fn, preset) in calls if (rel, fn) == callsite]
    assert matches, f"No _encoding_args call found in {callsite}"
    bad = [m for m in matches if m[3] not in PRESETS_FAST_OR_STRICTER]
    assert not bad, (
        f"{callsite[0]}::{callsite[1]} uses preset(s) "
        f"{[m[3] for m in bad]} — must be in {sorted(PRESETS_FAST_OR_STRICTER)}"
    )

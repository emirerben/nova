"""Strict-isolation contract guard for Line lyric code.

This test FAILS the build whenever anyone edits the frozen `_inject_line`
range in `app/pipeline/lyric_injector.py` (lines 797..1299, which covers
the line-style injector AND the dynamic-crossfade post-pass `§1c`/`§1g`
reconciliation). It does NOT prevent the edit — it forces the editor to
acknowledge they're touching Line code and update the locked SHA below
deliberately, after confirming Line's invariant suite
(`test_lyric_injector_no_stacking.py`) still passes.

This is intentionally crude. The real defense is the invariant test suite
under `tests/pipeline/test_lyric_injector_no_stacking.py` which proves
Line still satisfies its contract. The SHA guard is the FIRST line of
defense — a tripwire that says "are you sure you meant to touch this?"
before the change ships and we discover the regression in prod.

How to legitimately update the SHA
----------------------------------
1. Edit the Line code intentionally.
2. Run the FULL Line invariant suite locally:
     pytest tests/pipeline/test_lyric_injector_no_stacking.py -v
   It must all pass.
3. Recompute the new SHA:
     sed -n '797,1299p' app/pipeline/lyric_injector.py | shasum -a 256
4. Replace _LINE_FROZEN_RANGE_SHA256 below with the new value.
5. In the PR description, name the invariant you reverified and link to
   the test run output.

Doing this WITHOUT step 2 is the failure mode this guard exists to catch.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_LYRIC_INJECTOR_PATH = (
    Path(__file__).resolve().parents[2] / "app" / "pipeline" / "lyric_injector.py"
)

# Inclusive line range covering `_inject_line` and the dynamic-crossfade
# post-pass. Line 797 starts with `def _inject_line(`; line 1299 is the
# last line before `def _finalize_lyric_audible_window(` (the next public
# function). Range moved through 644..1146 → 677..1179 (PR Beauty-And-A-Beat,
# 2026-05-27) → 679..1181 (insertion of _INJECTOR_ALLOWED_SOURCES) → 720..1222
# in PR Instant-Crush (2026-05-27): a new constants block above `_inject_line`
# (the trailing-line drop thresholds) shifted the function by +41 lines but
# the content of `_inject_line` + post-pass is byte-identical (same SHA) →
# 797..1299 after post-merge helper growth above `_inject_line` (PR #414) →
# 814..1316 after per-word-pop helper growth above `_inject_line` →
# 822..1325 after karaoke finalization metadata growth above `_inject_line`;
# verified with `test_lyric_injector_no_stacking.py` (66 tests) on 2026-06-04.
# Same range, new SHA on 2026-06-05 after the audible-word rule string was
# updated for tail-started lyric preview words; invariant suite still passes.
# If the file structure changes such that this range no longer captures the
# right scope, update BOTH endpoints below AND the SHA.
_LINE_FROZEN_RANGE_START: int = 822
_LINE_FROZEN_RANGE_END: int = 1325

# Locked SHA256 of the frozen range. DO NOT update this constant casually.
# Read the module docstring above for the legitimate update procedure.
_LINE_FROZEN_RANGE_SHA256: str = "ebe3028ba09e466f7da1f635af2ccd4af7a91ab49cb8b1d83dab3479957264b8"


def _compute_range_sha256() -> str:
    """Read the frozen range and return its SHA256 hex digest.

    Reads using utf-8 + newline=None (Python text mode) which normalizes
    line endings to '\\n', so a CRLF check-in won't mysteriously flip the
    hash on Windows clones.
    """
    with _LYRIC_INJECTOR_PATH.open("r", encoding="utf-8", newline=None) as f:
        lines = f.readlines()
    # Use 1-based inclusive indices (matches `sed -n 'A,Bp'` semantics so
    # the update procedure in the docstring matches what this code reads).
    snippet = "".join(lines[_LINE_FROZEN_RANGE_START - 1 : _LINE_FROZEN_RANGE_END])
    return hashlib.sha256(snippet.encode("utf-8")).hexdigest()


def test_line_frozen_range_sha_matches() -> None:
    """The Line code range is byte-identical to the locked snapshot.

    Failure procedure:
      - If you did NOT mean to touch Line code, revert the change in the
        frozen range. The Line invariant tests in
        `tests/pipeline/test_lyric_injector_no_stacking.py` are what your
        change would have broken in subtle ways at runtime.
      - If you DID mean to touch Line code, follow the update procedure
        in this module's docstring. The new SHA goes in
        `_LINE_FROZEN_RANGE_SHA256` only AFTER you have confirmed the
        full Line invariant suite still passes against the change.
    """
    actual = _compute_range_sha256()
    assert actual == _LINE_FROZEN_RANGE_SHA256, (
        "Line code range SHA changed.\n"
        f"  expected: {_LINE_FROZEN_RANGE_SHA256}\n"
        f"  actual:   {actual}\n"
        f"  range:    {_LYRIC_INJECTOR_PATH.name}:"
        f"{_LINE_FROZEN_RANGE_START}-{_LINE_FROZEN_RANGE_END}\n"
        "Read the docstring at the top of this test module for the "
        "legitimate-update procedure. Bypassing this without running the "
        "Line invariant tests is the bug this guard exists to prevent."
    )


def test_line_frozen_range_still_contains_inject_line_def() -> None:
    """Sanity check: if `_inject_line` ever moves out of the locked range
    (e.g. someone refactors it earlier or later in the file), the SHA
    guard above would still pass for a stale range and silently miss real
    Line edits elsewhere. Verify the range still covers what we claim.
    """
    with _LYRIC_INJECTOR_PATH.open("r", encoding="utf-8", newline=None) as f:
        lines = f.readlines()
    range_text = "".join(lines[_LINE_FROZEN_RANGE_START - 1 : _LINE_FROZEN_RANGE_END])
    assert "def _inject_line(" in range_text, (
        f"Frozen range {_LINE_FROZEN_RANGE_START}..{_LINE_FROZEN_RANGE_END} "
        "no longer contains 'def _inject_line(' — the function may have moved. "
        "Update both _LINE_FROZEN_RANGE_START / _END above to cover the new "
        "location, then recompute and update the SHA."
    )

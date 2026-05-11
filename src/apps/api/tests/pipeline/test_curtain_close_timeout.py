"""Regression guard for the curtain-close timeout + silent-fallback bug.

PR #102 changed ``apply_curtain_close_tail`` from ``preset=ultrafast`` to
``preset=medium tune=film`` to fix banding on dark gradients, but kept the
180-second subprocess timeout. On Fly's worker (2 shared CPUs,
``--concurrency=2``) the per-pixel single-threaded geq filter under the
slower preset took 3-5x longer than the local-dev measurement and blew
the budget on the first job after deploy. The orchestrator caught the
``TimeoutExpired`` and only logged a warning, leaving the slot in the
output sequence without curtain bars while the configured post-slot
color-hold interstitial got inserted afterwards — so the final video
looked like "title plays full duration -> hard cut to black -> next slot"
with no visible failure to the user. Two days of iteration chased the
symptom because the failure was silent.

Three tests below guard the regression surface:

1. **Timeout floor.** The constant must stay >=300s. Anyone tightening it
   below that is reintroducing the same silent-regression risk.

2. **Call-site uses the constant.** Stub ``subprocess.run`` and verify
   the ``timeout=`` kwarg is the module-level constant value, not a
   hard-coded literal. Catches a future caller bypassing the constant.

3. **Loud failure structure.** AST-parse template_orchestrate.py and
   verify the ``except`` handler around ``apply_curtain_close_tail`` raises
   (does not swallow). Catches anyone re-adding the silent log.warning-only
   fallback. AST-level so it survives line-number drift.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from unittest import mock

from app.pipeline import interstitials
from app.pipeline.interstitials import apply_curtain_close_tail


def test_curtain_close_timeout_floor() -> None:
    """Timeout must stay generous. <300s reintroduces the PR #102 regression
    surface (silent fallback under load on the 2-shared-CPU Fly worker)."""
    assert interstitials.CURTAIN_CLOSE_SUBPROCESS_TIMEOUT_S >= 300, (
        f"timeout {interstitials.CURTAIN_CLOSE_SUBPROCESS_TIMEOUT_S}s is "
        "too tight; preset=medium geq on Fly's 2-shared-CPU worker observed "
        "~540s wall-clock on the slot-5 title (5.2s @ 1080x1920). Setting "
        "<300s reintroduces the silent-fallback risk fixed by this PR."
    )


def test_curtain_close_subprocess_uses_pinned_timeout(tmp_path: Path) -> None:
    """The ffmpeg invocation must pass the module-level constant as its
    timeout, not a hard-coded literal. Catches a future caller that bypasses
    the constant and reintroduces the 180s budget."""
    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"

    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:r=30:d=1.5",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100:d=1.5",
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-preset",
            "ultrafast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-shortest",
            str(src),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )

    real_run = subprocess.run
    captured_timeouts: list[float | None] = []

    def capturing_run(cmd, *args, **kwargs):
        # PNG-overlay path: ffmpeg -filter_complex "...overlay=..."
        if (
            isinstance(cmd, list)
            and cmd
            and cmd[0] == "ffmpeg"
            and "-filter_complex" in cmd
        ):
            fc_idx = cmd.index("-filter_complex")
            if fc_idx + 1 < len(cmd) and "overlay=" in cmd[fc_idx + 1]:
                captured_timeouts.append(kwargs.get("timeout"))
        return real_run(cmd, *args, **kwargs)

    with mock.patch.object(subprocess, "run", side_effect=capturing_run):
        apply_curtain_close_tail(str(src), str(out), animate_s=0.5)

    assert captured_timeouts, (
        "no overlay ffmpeg call observed — function never reached the "
        "re-encode step (or reverted to a -vf geq path)"
    )
    assert captured_timeouts[0] == interstitials.CURTAIN_CLOSE_SUBPROCESS_TIMEOUT_S, (
        f"timeout drift: ffmpeg called with timeout={captured_timeouts[0]} "
        f"but constant is {interstitials.CURTAIN_CLOSE_SUBPROCESS_TIMEOUT_S}"
    )


def test_curtain_close_failure_is_not_swallowed() -> None:
    """The orchestrator's except handler around apply_curtain_close_tail
    must re-raise, not log-and-continue. A silent fallback here leaves the
    final video without curtain bars while still inserting the post-slot
    color-hold interstitial — partial output that masks the failure. This
    test parses the source AST so it survives line-number drift; it asserts
    *some* try/except wrapping apply_curtain_close_tail has a raise in its
    handler."""
    orch_path = Path(__file__).resolve().parents[2] / "app" / "tasks" / "template_orchestrate.py"
    source = orch_path.read_text()
    tree = ast.parse(source)

    def is_curtain_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "apply_curtain_close_tail"
        )

    def call_present(stmts: list[ast.stmt]) -> bool:
        for s in stmts:
            for sub in ast.walk(s):
                if is_curtain_call(sub):
                    return True
        return False

    def handler_raises(handler: ast.ExceptHandler) -> bool:
        for sub in ast.walk(handler):
            if isinstance(sub, ast.Raise):
                return True
        return False

    found_protected = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and call_present(node.body):
            found_protected = True
            assert node.handlers, (
                "apply_curtain_close_tail is wrapped in a try with no except — "
                "the failure mode would propagate as an unstructured exception"
            )
            for handler in node.handlers:
                assert handler_raises(handler), (
                    "apply_curtain_close_tail's except handler does not raise. "
                    "A silent fallback here masks curtain-close failures and "
                    "ships partial output without the hero animation. Re-raise "
                    "(after structured logging) so the job fails loudly."
                )

    assert found_protected, (
        "no try/except wraps apply_curtain_close_tail in template_orchestrate.py "
        "— the call is either gone or unprotected. If intentional, update this test."
    )

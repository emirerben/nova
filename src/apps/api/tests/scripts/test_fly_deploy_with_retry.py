"""Unit tests for scripts/fly-deploy-with-retry.sh (issue #834).

The script wraps `flyctl deploy` with a single, signature-gated retry for the
intermittent release-machine startup kill (exit 143 + "has state: destroyed").
These tests run the real script via subprocess against a stub `flyctl` on
PATH, so they exercise the actual bash control flow (including the
`run_deploy ...; exit_code=$?` pattern that must never fall through an `if`
with no `else` and silently reset `$?` to 0) rather than re-implementing its
logic in Python.

Acceptance criteria under test (from the plan / issue #834):
  - a startup-killed release command (143-signature) no longer fails the
    deploy outright — it retries once and can go green
  - a genuine migration failure (any other exit code) still fails on the
    FIRST attempt, visibly, with no retry — this is the #812 negative case
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

# Repo root: src/apps/api/tests/scripts/this_file.py -> parents[5]
REPO_ROOT = Path(__file__).resolve().parents[5]
SCRIPT = REPO_ROOT / "scripts" / "fly-deploy-with-retry.sh"

_STUB_FLYCTL = """#!/usr/bin/env bash
set -euo pipefail

count=0
if [[ -f "$STUB_COUNT_FILE" ]]; then
  count=$(cat "$STUB_COUNT_FILE")
fi

line=$(sed -n "$((count + 1))p" "$STUB_PLAN_FILE")
IFS='|' read -r exit_code mode <<< "$line"

echo $((count + 1)) > "$STUB_COUNT_FILE"
echo "$@" > "$STUB_COUNT_FILE.args.$((count + 1))"

echo "==> Verifying app config"
echo "--> Deploying nova-video"

case "$mode" in
  ok)
    echo "1 desired, 1 placed, 1 healthy, 0 unhealthy"
    echo "--> v42 deployed successfully"
    ;;
  signature)
    echo "Release command machine 1234567890ab has state: destroyed"
    echo "Error: release_command failed running on machine 1234567890ab with exit code 143"
    ;;
  signature_padded)
    # Signature lines followed by a large trailing tail — regression shape
    # for the SIGPIPE/pipefail footgun (grep -q short-circuiting on an early
    # match while the producer is still writing; see the script's file-based
    # grep and scripts/build-admin-extension-zip.sh for the documented case).
    echo "Release command machine 1234567890ab has state: destroyed"
    echo "Error: release_command failed running on machine 1234567890ab with exit code 143"
    yes "trailing flyctl output padding line" | head -n 20000
    ;;
  signature_ansi)
    # Real flyctl colorizes output — signature lines wrapped in SGR escapes
    # must still match after the script's ANSI strip.
    printf '\\033[33mRelease command machine 1234567890ab has state: \\033[31mdestroyed\\033[0m\\n'
    printf '\\033[31mError: release_command failed running on machine 1234567890ab'
    printf ' with exit code 143\\033[0m\\n'
    ;;
  destroyed_only)
    echo "Release command machine 1234567890ab has state: destroyed"
    echo "Error: release_command failed running on machine 1234567890ab with exit code 137"
    ;;
  code143_only)
    echo "Error: release_command failed running on machine 1234567890ab with exit code 143"
    ;;
  generic)
    echo "Error: release_command failed running on machine 1234567890ab with exit code 1"
    echo "alembic.util.exc.CommandError: Can't locate revision identified by 'deadbeef'"
    echo "  raise CommandError(...)"
    ;;
esac

exit "$exit_code"
"""


def _write_stub_flyctl(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub_path = bin_dir / "flyctl"
    stub_path.write_text(_STUB_FLYCTL)
    stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_script(
    tmp_path: Path,
    plan_lines: list[str],
    github_step_summary: Path | None = "unset",
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    _write_stub_flyctl(bin_dir)

    plan_file = tmp_path / "plan.txt"
    plan_file.write_text("\n".join(plan_lines) + "\n")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["FLY_DEPLOY_RETRY_DELAY_SECONDS"] = "0"
    env["STUB_COUNT_FILE"] = str(tmp_path / "count")
    env["STUB_PLAN_FILE"] = str(plan_file)

    if github_step_summary in ("unset", None):
        env.pop("GITHUB_STEP_SUMMARY", None)
    else:
        env["GITHUB_STEP_SUMMARY"] = str(github_step_summary)

    # Invoke via `bash` like the workflow effectively does (it chmods first,
    # but running through bash removes the test's dependency on the checked-in
    # exec bit entirely).
    return subprocess.run(
        ["bash", str(SCRIPT), *(args if args is not None else ["--remote-only"])],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _invocation_count(tmp_path: Path) -> int:
    count_file = tmp_path / "count"
    if not count_file.exists():
        return 0
    return int(count_file.read_text().strip())


def test_success_first_attempt_single_invocation(tmp_path: Path) -> None:
    result = _run_script(tmp_path, ["0|ok"])

    assert result.returncode == 0
    assert _invocation_count(tmp_path) == 1


def test_signature_then_success_retries_once_and_warns(tmp_path: Path) -> None:
    # flyctl itself exits 1 on a release_command failure (observed in run
    # 31972609339: log says "exit code 143", step says "Process completed
    # with exit code 1") — the 143 only ever appears in the LOG. Exit 1 +
    # signature lines is therefore the realistic production shape, and it
    # MUST retry: the gate is log-based by design. Do not "fix" the script
    # to require exit code 143 — that would disable the retry forever.
    result = _run_script(tmp_path, ["1|signature", "0|ok"])

    assert result.returncode == 0
    assert _invocation_count(tmp_path) == 2
    assert "::warning" in result.stderr
    assert "issue #834" in result.stderr


def test_signature_with_exit_143_also_retries(tmp_path: Path) -> None:
    # Robustness either way: if a future flyctl propagates the release
    # command's 143 as its own exit code, the log-based gate still fires.
    result = _run_script(tmp_path, ["143|signature", "0|ok"])

    assert result.returncode == 0
    assert _invocation_count(tmp_path) == 2


def test_generic_failure_does_not_retry(tmp_path: Path) -> None:
    result = _run_script(tmp_path, ["1|generic"])

    assert result.returncode == 1
    assert _invocation_count(tmp_path) == 1
    assert "::warning" not in result.stderr


def test_signature_twice_fails_after_one_retry(tmp_path: Path) -> None:
    # Second attempt's exit code is preserved verbatim (1 in the realistic
    # shape — see test_signature_then_success_retries_once_and_warns).
    result = _run_script(tmp_path, ["1|signature", "1|signature"])

    assert result.returncode == 1
    assert _invocation_count(tmp_path) == 2


def test_missing_github_step_summary_does_not_crash(tmp_path: Path) -> None:
    result = _run_script(tmp_path, ["143|signature", "0|ok"], github_step_summary=None)

    assert result.returncode == 0
    assert _invocation_count(tmp_path) == 2


def test_github_step_summary_gets_retry_block(tmp_path: Path) -> None:
    summary_file = tmp_path / "step_summary.md"
    summary_file.write_text("")

    result = _run_script(tmp_path, ["143|signature", "0|ok"], github_step_summary=summary_file)

    assert result.returncode == 0
    summary_content = summary_file.read_text()
    assert "#834" in summary_content
    assert "exit code 143" in summary_content


def test_destroyed_line_without_exit_143_does_not_retry(tmp_path: Path) -> None:
    # Only "has state: destroyed" present (paired with a different exit code)
    # — the AND requirement must reject a partial signature match.
    result = _run_script(tmp_path, ["137|destroyed_only"])

    assert result.returncode == 137
    assert _invocation_count(tmp_path) == 1
    assert "::warning" not in result.stderr


def test_exit_143_line_without_destroyed_line_does_not_retry(tmp_path: Path) -> None:
    # Only the exit-143 line, no "has state: destroyed" — same AND requirement.
    result = _run_script(tmp_path, ["143|code143_only"])

    assert result.returncode == 143
    assert _invocation_count(tmp_path) == 1
    assert "::warning" not in result.stderr


def test_ansi_colorized_signature_still_retries(tmp_path: Path) -> None:
    # Real flyctl output is colorized; the ANSI strip must not be load-bearing
    # on GNU-only sed escapes (regression guard for the $'\x1b' quoting).
    result = _run_script(tmp_path, ["1|signature_ansi", "0|ok"])

    assert result.returncode == 0
    assert _invocation_count(tmp_path) == 2


def test_signature_with_large_trailing_output_still_retries(tmp_path: Path) -> None:
    # Regression guard for the SIGPIPE/pipefail footgun: grep -q used to read
    # from a live pipe and could short-circuit on an early match while the
    # producer was still writing, turning a genuine match into exit 141.
    # flyctl's real output does not end right after the signature lines.
    result = _run_script(tmp_path, ["1|signature_padded", "0|ok"])

    assert result.returncode == 0
    assert _invocation_count(tmp_path) == 2
    assert "::warning" in result.stderr


def test_retry_preserves_second_attempt_exit_code(tmp_path: Path) -> None:
    # A signature match followed by a DIFFERENT failure (e.g. a real migration
    # error surfacing once the platform race clears) must exit with attempt
    # two's own code, unmodified.
    result = _run_script(tmp_path, ["1|signature", "1|generic"])

    assert result.returncode == 1
    assert _invocation_count(tmp_path) == 2


def test_args_forwarded_verbatim_on_both_attempts(tmp_path: Path) -> None:
    result = _run_script(
        tmp_path,
        ["1|signature", "0|ok"],
        args=["--remote-only", "--strategy", "bluegreen"],
    )

    assert result.returncode == 0
    for attempt in (1, 2):
        recorded = (tmp_path / f"count.args.{attempt}").read_text().strip()
        # "deploy" is the subcommand the script itself supplies to flyctl.
        assert recorded == "deploy --remote-only --strategy bluegreen"

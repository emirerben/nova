from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
CHECK_API = REPO_ROOT / "scripts" / "check-api.sh"


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo with spaces"
    script = repo / "scripts" / "check-api.sh"
    python = repo / "src" / "apps" / "api" / ".venv" / "bin" / "python"
    script.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    shutil.copy2(CHECK_API, script)
    python.write_text(
        "#!/usr/bin/env bash\n"
        'printf "cwd=%s\\n" "$PWD"\n'
        'printf "pythonpath=%s\\n" "${PYTHONPATH:-}"\n'
        'printf "argv_item=%s\\n" "$@"\n'
        'exit "${FAKE_PYTHON_STATUS:-0}"\n',
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    return repo, script


@pytest.mark.parametrize(
    ("arguments", "expected_argv"),
    [
        (["test", "-q", "tests/cli"], ["-m", "pytest", "-q", "tests/cli"]),
        (
            ["lint", "check", "app", "--select", "F"],
            ["-m", "ruff", "check", "app", "--select", "F"],
        ),
        (["lint", "format", "--check", "app"], ["-m", "ruff", "format", "--check", "app"]),
        (["profile", "--help"], ["-m", "app.cli.runtime_profile", "--help"]),
        (
            ["profile", "literal value with spaces", "*.py"],
            ["-m", "app.cli.runtime_profile", "literal value with spaces", "*.py"],
        ),
    ],
)
def test_wrapper_uses_its_own_api_directory_and_preserves_arguments(
    tmp_path: Path, arguments: list[str], expected_argv: list[str]
) -> None:
    repo, script = _make_repo(tmp_path)
    nested_cwd = tmp_path / "unrelated" / "nested"
    nested_cwd.mkdir(parents=True)

    result = subprocess.run(
        [str(script), *arguments], cwd=nested_cwd, text=True, capture_output=True, check=False
    )

    assert result.returncode == 0
    assert "pythonpath=\n" in result.stdout
    assert f"cwd={repo / 'src/apps/api'}" in result.stdout
    assert [
        line.removeprefix("argv_item=")
        for line in result.stdout.splitlines()
        if line.startswith("argv_item=")
    ] == expected_argv


def test_wrapper_drops_an_inherited_pythonpath(tmp_path: Path) -> None:
    _, script = _make_repo(tmp_path)
    result = subprocess.run(
        [str(script), "profile"],
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": "/another/worktree/src/apps/api"},
        check=False,
    )

    assert result.returncode == 0
    assert "pythonpath=\n" in result.stdout


def test_wrapper_preserves_child_exit_status(tmp_path: Path) -> None:
    _, script = _make_repo(tmp_path)

    result = subprocess.run(
        [str(script), "profile"],
        text=True,
        capture_output=True,
        env={**os.environ, "FAKE_PYTHON_STATUS": "23"},
        check=False,
    )

    assert result.returncode == 23


def test_wrapper_reports_missing_worktree_venv(tmp_path: Path) -> None:
    repo, script = _make_repo(tmp_path)
    (repo / "src/apps/api/.venv/bin/python").unlink()

    result = subprocess.run([str(script), "profile"], text=True, capture_output=True, check=False)

    assert result.returncode == 127
    assert "API virtual environment is missing" in result.stderr
    assert "scripts/worktree-setup.sh" in result.stderr


def test_wrapper_resolves_a_symlinked_entrypoint(tmp_path: Path) -> None:
    repo, script = _make_repo(tmp_path)
    link = tmp_path / "check-api"
    link.symlink_to(script)

    result = subprocess.run([str(link), "profile"], text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert f"cwd={repo / 'src/apps/api'}" in result.stdout

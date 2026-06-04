"""Wiring guards for the autonomous dev-loop launchd scheduler.

These do NOT exercise the loop end-to-end (that needs prod + a headless agent).
They lock the cheap, high-value invariants: the plist is valid and points at the
wrapper, the new shell scripts are syntactically sound, and the wrapper fails
CLOSED on misconfiguration instead of silently no-op'ing every scheduled tick.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "scripts" / "cron" / "gate_runner.sh").exists():
            return parent
    raise RuntimeError("repo root not found from test location")


REPO_ROOT = _repo_root()
SCRIPTS_DIR = REPO_ROOT / "scripts" / "cron"
WRAPPER = SCRIPTS_DIR / "dev_loop_tick.sh"
INSTALLER = SCRIPTS_DIR / "install-dev-loop.sh"
PLIST = REPO_ROOT / "infra" / "launchd" / "com.nova.dev-loop.plist"

bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


def test_new_files_exist() -> None:
    for path in (WRAPPER, INSTALLER, PLIST):
        assert path.is_file(), f"missing {path}"


def test_plist_parses_and_points_at_wrapper() -> None:
    with PLIST.open("rb") as fh:
        data = plistlib.load(fh)
    assert data["Label"] == "com.nova.dev-loop"
    # RunAtLoad must be false: enabling the timer is a deliberate manual step.
    assert data.get("RunAtLoad") is False
    assert isinstance(data["StartInterval"], int) and data["StartInterval"] > 0
    args = data["ProgramArguments"]
    # Template placeholder the installer rewrites to the wrapper's absolute path.
    assert any("__DEV_LOOP_TICK__" in a for a in args), args
    assert args[-1] == "both", "timer must run the combined builder->gate tick"


def test_installer_substitutes_the_plist_placeholder() -> None:
    # The installer must rewrite __DEV_LOOP_TICK__ -> the wrapper, else launchd
    # would exec a literal placeholder path.
    text = INSTALLER.read_text()
    assert "__DEV_LOOP_TICK__" in text and "dev_loop_tick.sh" in text


def test_installer_strips_prod_key_from_seeded_env() -> None:
    # .env.example documents ADMIN_PROD_API_KEY (empty); copying it verbatim into
    # the checkout's .env trips assert_no_prod_key_in_env_file (it matches ANY
    # occurrence), so the installer MUST strip that line after the copy.
    text = INSTALLER.read_text()
    assert ".env.example" in text, "installer should seed .env from .env.example"
    assert "sed" in text and "ADMIN_PROD_API_KEY" in text, (
        "installer must strip ADMIN_PROD_API_KEY from the seeded .env"
    )


@bash
@pytest.mark.parametrize("script", ["dev_loop_tick.sh", "install-dev-loop.sh"])
def test_shell_script_passes_bash_n(script: str) -> None:
    proc = subprocess.run(
        ["bash", "-n", str(SCRIPTS_DIR / script)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


LIB = SCRIPTS_DIR / "_dev_loop_lib.sh"


@pytest.mark.parametrize("script", ["dev_loop_tick.sh", "gate_runner.sh", "_dev_loop_lib.sh"])
def test_no_script_invokes_flock(script: str) -> None:
    # flock is util-linux and absent on stock macOS; invoking it silently no-ops
    # every tick. The locks must stay on the portable mkdir helper. (We check the
    # code only — comments may legitimately mention flock to explain its absence.)
    code = "\n".join(
        line.split("#", 1)[0] for line in (SCRIPTS_DIR / script).read_text().splitlines()
    )
    assert "flock" not in code, f"{script} invokes flock (breaks macOS)"


@bash
def test_mkdir_lock_is_mutually_exclusive(tmp_path: Path) -> None:
    # acquire_lock holds; a second acquire on the same dir fails; release frees it.
    lock = tmp_path / "lock.d"
    script = f"""
        set -u
        source '{LIB}'
        acquire_lock '{lock}' || {{ echo FAIL_FIRST; exit 10; }}
        if ( acquire_lock '{lock}' ); then echo FAIL_SECOND; exit 11; fi
        release_lock '{lock}'
        acquire_lock '{lock}' || {{ echo FAIL_THIRD; exit 12; }}
        release_lock '{lock}'
        echo OK
    """
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "OK" in proc.stdout, proc.stdout


@bash
def test_mkdir_lock_reclaims_stale(tmp_path: Path) -> None:
    # A lock older than DEV_LOOP_LOCK_STALE_S is reclaimed (a crashed tick can't
    # wedge the loop forever).
    lock = tmp_path / "stale.d"
    lock.mkdir()
    script = f"""
        set -u
        export DEV_LOOP_LOCK_STALE_S=0
        source '{LIB}'
        acquire_lock '{lock}' || {{ echo FAIL_RECLAIM; exit 13; }}
        echo OK
    """
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "OK" in proc.stdout, proc.stdout


@bash
def test_wrapper_rejects_unknown_mode() -> None:
    proc = subprocess.run(
        ["bash", str(WRAPPER), "bogus-mode"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "unknown mode" in proc.stderr.lower()


@bash
def test_wrapper_fails_closed_on_missing_env_file(tmp_path: Path) -> None:
    # A missing secrets file must exit 1, never silently no-op a scheduled tick.
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env["NOVA_DEV_LOOP_ENV"] = str(tmp_path / "does-not-exist.env")
    env["NOVA_DEV_LOOP_REPO"] = str(tmp_path / "no-such-checkout")
    proc = subprocess.run(
        ["bash", str(WRAPPER), "builder"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1, (proc.returncode, proc.stdout, proc.stderr)

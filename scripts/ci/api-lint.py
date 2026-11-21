#!/usr/bin/env python3
"""Lint and format-check the API with the ruff version CI installs.

    python3 scripts/ci/api-lint.py                 # the whole API tree
    python3 scripts/ci/api-lint.py app/a.py ...    # paths under src/apps/api, written
                                                   # from there or from the repo root
    python3 scripts/ci/api-lint.py --ruff-path     # print the pinned ruff, for hooks

`lint-suites` in CI and `scripts/preship-check.sh` both run this, so a clean
local run means a clean CI run. It refuses to run with any ruff other than the
one `src/apps/api/uv.lock` pins: two versions format differently. Both checks
always run, so one push shows every problem. Guard: scripts/ci/test_api_lint.py.
"""

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src/apps/api"


def locked_version(lock: str) -> str:
    match = re.search(r'^name = "ruff"\nversion = "([^"]+)"$', lock, re.M)
    if not match:
        raise SystemExit("src/apps/api/uv.lock does not pin ruff")
    return match.group(1)


def ruff_candidates() -> list[str]:
    """Every ruff worth asking for its version: this checkout's environment, the
    primary checkout's (a worktree made with `git worktree add` has none of its
    own), then PATH."""
    found = [API / ".venv/bin/ruff"]
    common = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if common:
        found.append(Path(common).parent / "src/apps/api/.venv/bin/ruff")
    on_path = shutil.which("ruff")
    if on_path:
        found.append(Path(on_path))
    return list(dict.fromkeys(str(path) for path in found if path.exists()))


def installed_version(ruff: str, run=subprocess.run) -> str:
    return run([ruff, "--version"], capture_output=True, text=True).stdout.split()[-1]


def api_paths(argv, exists) -> tuple[list[str], list[str]]:
    """Arguments as paths relative to src/apps/api, and the ones that are not API files."""
    paths, refused = [], []
    for arg in argv:
        path = arg.removeprefix("src/apps/api/")
        outside = path.startswith(("/", "src/")) or ".." in Path(path).parts
        (refused if outside or not exists(path) else paths).append(arg if outside else path)
    return paths, refused


def main(
    argv,
    *,
    run=subprocess.run,
    ruff=None,
    lock=None,
    say=print,
    exists=lambda path: (API / path).exists(),
) -> int:
    lock = (API / "uv.lock").read_text() if lock is None else lock
    want = locked_version(lock)
    candidates = ruff_candidates() if ruff is None else ruff
    versions = {path: installed_version(path, run) for path in candidates}
    ruff = next((path for path, version in versions.items() if version == want), None)
    if ruff is None:
        for path, version in versions.items():
            say(f"api-lint: {path} is ruff {version}")
        say(
            f"api-lint: CI uses ruff {want} and no such ruff is installed here. "
            f"Install it: src/apps/api/.venv/bin/python -m pip install ruff=={want}"
        )
        return 2
    if argv == ["--ruff-path"]:
        say(ruff)
        return 0
    targets, refused = api_paths(argv, exists)
    if refused:
        say(f"api-lint: not files under src/apps/api: {' '.join(refused)}")
        return 2
    targets = targets or ["."]
    failed = []
    for name, command in (("check", ["check"]), ("format", ["format", "--check"])):
        started = time.monotonic()
        code = run([ruff, *command, *targets], cwd=API).returncode
        say(f"api-lint: ruff {' '.join(command)}: {time.monotonic() - started:.2f} s")
        if code:
            failed.append(name)
    if "format" in failed:
        say(f"api-lint: fix formatting with: cd src/apps/api && {ruff} format <the files listed>")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

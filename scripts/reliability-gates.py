#!/usr/bin/env python3
"""Reliability gates (KRI-577): select, run and red-control the incident regression gates.

Stdlib only, so it runs with any Python 3 from the repo root (CI, preship, a bare shell).
The ledger is ``src/apps/api/tests/gates/reliability-gates.json``; its schema is checked by
``tests/gates/test_reliability_gates.py``.

    python3 scripts/reliability-gates.py list                 # every gate, one line each
    python3 scripts/reliability-gates.py select [--base REF]  # gates whose paths changed vs REF
    python3 scripts/reliability-gates.py tests  [--all|--changed|ID ...]   # pytest targets
    python3 scripts/reliability-gates.py run    [--all|--changed|ID ...]   # run them
    python3 scripts/reliability-gates.py journey              # scripts/ci journey block (JSON)
    python3 scripts/reliability-gates.py red-control ID [...] # fix's parent must FAIL the gate

``run`` separates environment skips (Postgres unreachable and similar) from real failures,
so an acceptance result stays usable on a laptop without the test database.
``red-control`` checks the acceptance rule "the incident fixture fails with the fix
disabled": it checks the fix commit's parent out into a temporary worktree, copies the
gate's regression tests from HEAD over it, runs them there, and expects a failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API = REPO / "src/apps/api"
MANIFEST = API / "tests/gates/reliability-gates.json"
JOURNEY_ID = "kri-577-reliability-gates"
ENV_SKIP_RE = re.compile(
    r"Postgres not reachable|role \"postgres\" does not exist|Connection refused|"
    r"could not connect to server|connection to server at",
)


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def merged(gates: list[dict]) -> list[dict]:
    return [g for g in gates if g.get("status", "merged") == "merged"]


def ci_union(gates: list[dict]) -> tuple[list[str], list[str]]:
    paths: set[str] = set()
    tests: set[str] = set()
    for gate in merged(gates):
        if gate.get("journey"):
            continue
        paths.update(gate.get("paths", []))
        tests.update(gate.get("regression_tests", []))
    return sorted(paths), sorted(tests)


def git(*args: str, cwd: Path = REPO) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=cwd, text=True, stderr=subprocess.PIPE
    )


def changed_paths(base: str) -> list[str]:
    try:
        merge_base = git("merge-base", base, "HEAD").strip()
    except subprocess.CalledProcessError:
        merge_base = base
    out = set()
    for args in (
        ("diff", "--name-only", merge_base, "HEAD"),
        ("diff", "--name-only", "HEAD"),
    ):
        out.update(line for line in git(*args).splitlines() if line)
    out.update(
        line for line in git("ls-files", "--others", "--exclude-standard").splitlines()
    )
    return sorted(out)


def select_gates(gates: list[dict], paths: list[str]) -> list[dict]:
    hits = []
    for gate in merged(gates):
        if any(p in gate.get("paths", []) for p in paths):
            hits.append(gate)
    return hits


def by_ids(gates: list[dict], ids: list[str]) -> list[dict]:
    known = {g["id"]: g for g in gates}
    missing = [i for i in ids if i not in known]
    if missing:
        raise SystemExit(f"unknown gate id(s): {', '.join(missing)}")
    return [known[i] for i in ids]


def python_bin() -> str:
    venv = API / ".venv/bin/python"
    return str(venv) if venv.exists() else sys.executable


def tests_for(gates: list[dict]) -> list[str]:
    return sorted({t for g in gates for t in g.get("regression_tests", [])})


def resolve(args, gates):
    if args.all:
        return [g for g in merged(gates) if not g.get("journey")]
    if args.changed:
        return select_gates(gates, changed_paths(args.base))
    if args.ids:
        return by_ids(gates, args.ids)
    raise SystemExit("name gate ids, or pass --all / --changed")


def cmd_list(args, gates):
    for g in gates:
        status = g.get("status", "merged")
        flag = f" flag={g['kill_switch']}" if g.get("kill_switch") else ""
        prs = ",".join(f"#{p}" for p in g.get("fix_prs", []))
        print(
            f"{g['id']:<38} {g['boundary']:<12} {status:<8} {g['harness']:<16} "
            f"{prs:<22} red={g.get('red_control', 'pending')}{flag}"
        )


def cmd_select(args, gates):
    paths = changed_paths(args.base)
    hits = select_gates(gates, paths)
    if not hits:
        print("no reliability gate maps to the changed paths")
        return 0
    for g in hits:
        touched = [p for p in paths if p in g["paths"]]
        print(f"{g['id']}  <- {', '.join(touched)}")
    print("\nrun:  python3 scripts/reliability-gates.py run --changed")
    return 0


def cmd_tests(args, gates):
    for t in tests_for(resolve(args, gates)):
        print(t)
    return 0


def run_pytest(
    tests: list[str], cwd: Path, python: str, extra: list[str]
) -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [python, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider", "-rs", *extra],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def summarize(output: str) -> dict:
    counts = {}
    for key in ("passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed"):
        m = re.search(rf"\b(\d+) {key}\b", output)
        if m:
            counts[key] = int(m.group(1))
    env_skips = len(
        [
            line
            for line in output.splitlines()
            if "SKIPPED" in line and ENV_SKIP_RE.search(line)
        ]
    )
    env_errors = len(ENV_SKIP_RE.findall(output)) - env_skips
    counts["environment_skips"] = env_skips
    counts["environment_errors"] = max(env_errors, 0)
    return counts


def cmd_run(args, gates):
    chosen = resolve(args, gates)
    tests = tests_for(chosen)
    if not tests:
        print("nothing to run")
        return 0
    print(f"reliability gates: {', '.join(g['id'] for g in chosen)}")
    print(f"tests ({len(tests)}): {' '.join(tests)}")
    code, out = run_pytest(tests, API, args.python or python_bin(), args.pytest_args)
    counts = summarize(out)
    tail = "\n".join(out.strip().splitlines()[-25:])
    print(tail)
    print("\nsummary:", json.dumps(counts))
    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        Path(args.log).write_text(out, encoding="utf-8")
    if counts.get("environment_skips") or counts.get("environment_errors"):
        print(
            "environment: some DB-backed gates could not run here "
            "(see scripts/dev/test-db.sh); they are not regressions."
        )
    return code


def cmd_journey(args, gates):
    paths, tests = ci_union(gates)
    block = {
        "id": JOURNEY_ID,
        "api_tests": " ".join(tests),
        "paths": paths,
    }
    print(json.dumps(block, indent=2))
    return 0


def cmd_red_control(args, gates):
    chosen = by_ids(gates, args.ids)
    python = args.python or python_bin()
    failures = 0
    for gate in chosen:
        if gate.get("status", "merged") != "merged" or not gate.get("fix_commit"):
            print(f"{gate['id']}: not applicable (no merged fix commit)")
            continue
        parent = f"{gate['fix_commit']}^"
        tests = gate.get("regression_tests", [])
        with tempfile.TemporaryDirectory(prefix="nova-red-control-") as tmp:
            wt = Path(tmp) / "wt"
            git("worktree", "add", "--detach", str(wt), parent)
            try:
                for test in tests:
                    rel = test.split("::")[0]
                    src = API / rel
                    dst = wt / "src/apps/api" / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    # Sibling conftest/helpers the HEAD test imports may be newer too.
                    for helper in src.parent.glob("*.py"):
                        if helper.name.startswith("test_"):
                            continue
                        shutil.copy2(helper, dst.parent / helper.name)
                code, out = run_pytest(
                    tests, wt / "src/apps/api", python, ["-x", "--no-header"]
                )
            finally:
                git("worktree", "remove", "--force", str(wt))
        counts = summarize(out)
        collection = "error" in out.lower() and counts.get("failed", 0) == 0
        if code == 0:
            verdict = "FAIL: the gate still passes without the fix (it does not pin the incident)"
            failures += 1
        elif collection:
            verdict = "weak: fails only at collection/import on the pre-fix tree"
        else:
            verdict = "verified: the pre-fix tree fails the gate"
        print(f"{gate['id']} @ {parent}: {verdict} {json.dumps(counts)}")
        if args.verbose:
            print("\n".join(out.strip().splitlines()[-15:]))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("select")
    p.add_argument("--base", default="origin/main")
    for name in ("tests", "run"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="*")
        p.add_argument("--all", action="store_true")
        p.add_argument("--changed", action="store_true")
        p.add_argument("--base", default="origin/main")
        p.add_argument("--python")
        p.add_argument("--log")
    sub.add_parser("journey")
    p = sub.add_parser("red-control")
    p.add_argument("ids", nargs="+")
    p.add_argument("--python")
    p.add_argument("-v", "--verbose", action="store_true")
    args, pytest_args = parser.parse_known_args()
    args.pytest_args = [a for a in pytest_args if a != "--"]
    gates = load()["gates"]
    return {
        "list": cmd_list,
        "select": cmd_select,
        "tests": cmd_tests,
        "run": cmd_run,
        "journey": cmd_journey,
        "red-control": cmd_red_control,
    }[args.cmd](args, gates) or 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"git failed: {error.stderr}") from error

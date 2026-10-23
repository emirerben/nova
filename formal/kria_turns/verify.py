#!/usr/bin/env python3
"""Check the Kria chat-turn TLA+ model against the code it describes.

Steps (all must pass):
  1. drift   - the covered functions still hash to what the model was last synced to
  2. model   - KriaTurns.tla (the code now) keeps every rule, at 2 and 3 messages
  3. history - KriaTurnsBefore.tla (origin/main before the fix) breaks exactly the
               two rules TLC found
  4. teeth   - each rule fails on a copy of KriaTurns.tla with its guard removed
  5. replay  - the Postgres replays in tests/kria/test_turn_follow_up_postgres.py
               (skipped with --no-replay, or when no *_test database is configured)

Usage: python3 formal/kria_turns/verify.py [--no-replay] [--resync]
Env:   TLA2TOOLS (path to tla2tools.jar v1.8.0), JAVA (java 11+ binary)
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
API = REPO / "src/apps/api"
OUT = REPO / ".dev/formal-kria-turns"
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"
TLC_SHA256 = "dae639a1f51a9e13ce60c369b15f32124cf1a78017aca366b6de0ae2b5169439"
RULES = ["OneAtATime", "NoStaleCommit", "NoStuckFollowUp", "AnswersOnlyUpToItsMessage"]
BEFORE_BREAKS = {"NoStuckFollowUp", "AnswersOnlyUpToItsMessage"}

# Each mutation removes one guard from KriaTurns.tla; TLC must then break its rule.
MUTATIONS = {
    "NoStaleCommit": [("  /\\ epoch[t] = w[x].epoch\n", "  /\\ TRUE\n"),
                      ("  /\\ live[t]\n\n", "  /\\ TRUE\n\n")],
    "NoStuckFollowUp": [(" \\/ Reconcile(t) \\/ ReleaseStranded(t)", " \\/ Reconcile(t)")],
    "AnswersOnlyUpToItsMessage": [
        ("UpTo(IF sent < w[x].turn THEN sent ELSE w[x].turn)", "UpTo(sent)")],
    "OneAtATime": [("IF Queued = {} \\/ RunningTurn THEN s", "IF Queued = {} THEN s")],
}


def tlc_jar() -> Path:
    jar = Path(os.environ["TLA2TOOLS"]) if os.environ.get("TLA2TOOLS") else OUT / "tla2tools.jar"
    if not jar.exists():
        jar.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {TLC_URL}")
        urllib.request.urlretrieve(TLC_URL, jar)  # noqa: S310 - pinned URL + sha256 below
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    if digest != TLC_SHA256:
        sys.exit(f"FAIL: {jar} sha256 {digest} is not TLC v1.8.0 ({TLC_SHA256})")
    return jar


def java() -> str:
    found = os.environ.get("JAVA") or shutil.which("java")
    if not found or subprocess.run([found, "-version"], capture_output=True).returncode:
        sys.exit("FAIL: no Java runtime. Install one (brew install openjdk@21) and set JAVA.")
    return found


def tlc(spec: Path, msgs: int, rules: list[str]) -> set[str]:
    """Run TLC once per rule; return the rules it breaks."""
    broken = set()
    for rule in rules:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "run.cfg"
            cfg.write_text(
                "SPECIFICATION Spec\nCONSTANTS\n"
                f"  Msgs = {msgs}\n  Workers = {{1, 2}}\n  MaxEpoch = 3\n"
                f"INVARIANT {rule}\n"
            )
            shutil.copy(spec, tmp)
            run = subprocess.run(
                [JAVA, "-XX:+UseParallelGC", "-cp", str(JAR), "tlc2.TLC", "-workers", "auto",
                 "-deadlock", "-metadir", str(Path(tmp) / "states"), "-config", str(cfg),
                 spec.name],
                cwd=tmp, capture_output=True, text=True, timeout=600,
            )
            log = OUT / f"{spec.stem}-{msgs}-{rule}.log"
            log.write_text(run.stdout + run.stderr)
            if f"Invariant {rule} is violated" in run.stdout:
                broken.add(rule)
            elif "No error has been found" not in run.stdout:
                sys.exit(f"FAIL: TLC did not finish on {spec.name} {rule}; see {log}")
    return broken


def function_hashes() -> dict[str, str]:
    coverage = json.loads((HERE / "coverage.json").read_text())
    out = {}
    for rel, names in coverage["functions"].items():
        tree = ast.parse((REPO / rel).read_text())
        source = (REPO / rel).read_text().splitlines()
        found = {
            n.name: "\n".join(source[n.lineno - 1 : n.end_lineno])
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in names:
            if name not in found:
                sys.exit(f"DRIFT: {rel}:{name} no longer exists; update the model and coverage.json")
            out[f"{rel}:{name}"] = hashlib.sha256(found[name].encode()).hexdigest()[:16]
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {}

    hashes = function_hashes()
    coverage = json.loads((HERE / "coverage.json").read_text())
    if "--resync" in sys.argv:
        coverage["hashes"] = hashes
        coverage["sync_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
        ).stdout.strip()
        (HERE / "coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")
        print("resynced coverage.json; commit it only after re-reading the model against the code")
    drift = sorted(k for k, v in hashes.items() if coverage.get("hashes", {}).get(k) != v)
    report["drift"] = drift
    if drift:
        print("DRIFT (review, not a bug): these covered functions changed since "
              f"{coverage.get('sync_commit')}:\n  " + "\n  ".join(drift))

    for msgs in (2, 3):
        broken = tlc(HERE / "KriaTurns.tla", msgs, RULES)
        report[f"model_msgs_{msgs}"] = sorted(broken)
        print(f"model, {msgs} messages: " + ("all rules hold" if not broken else f"BROKEN {broken}"))
        if broken:
            sys.exit(f"FAIL: the code model breaks {sorted(broken)}; logs in {OUT}")

    broken = tlc(HERE / "KriaTurnsBefore.tla", 2, RULES)
    report["before_fix"] = sorted(broken)
    print(f"before the fix: breaks {sorted(broken)}")
    if broken != BEFORE_BREAKS:
        sys.exit(f"FAIL: KriaTurnsBefore.tla should break exactly {sorted(BEFORE_BREAKS)}")

    teeth = {}
    for rule, edits in MUTATIONS.items():
        text = (HERE / "KriaTurns.tla").read_text()
        for old, new in edits:
            if text.count(old) != 1:
                sys.exit(f"FAIL: mutation for {rule} no longer matches KriaTurns.tla: {old!r}")
            text = text.replace(old, new)
        mutant = OUT / "mutant" / "KriaTurns.tla"
        mutant.parent.mkdir(exist_ok=True)
        mutant.write_text(text)
        caught = rule in tlc(mutant, 3 if rule == "OneAtATime" else 2, [rule])
        teeth[rule] = caught
        print(f"teeth, {rule} without its guard: " + ("caught" if caught else "NOT CAUGHT"))
        if not caught:
            sys.exit(f"FAIL: {rule} cannot fail, so it proves nothing")
    report["teeth"] = teeth

    if "--no-replay" not in sys.argv:
        url = os.environ.get("DATABASE_URL", "")
        url_file = REPO / ".dev/test-db-url"
        if not url and url_file.exists():
            url = url_file.read_text().strip()
        if not re.search(r"/[A-Za-z0-9_]+_test$", url):
            print("replay: skipped (no *_test DATABASE_URL; run bash scripts/dev/test-db.sh)")
            report["replay"] = "skipped"
        else:
            python = API / ".venv/bin/python"
            run = subprocess.run(
                [str(python), "-m", "pytest", "tests/kria/test_turn_follow_up_postgres.py", "-q",
                 "-p", "no:randomly"],
                cwd=API, env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True,
            )
            (OUT / "replay.log").write_text(run.stdout + run.stderr)
            report["replay"] = "passed" if run.returncode == 0 else "failed"
            print(f"replay: {report['replay']} ({run.stdout.strip().splitlines()[-1]})")
            if run.returncode:
                sys.exit(f"FAIL: Postgres replays failed; see {OUT / 'replay.log'}")

    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print("PASS" + (" with DRIFT to review" if drift else ""))
    sys.exit(3 if drift else 0)


if __name__ == "__main__":
    JAR = tlc_jar()
    JAVA = java()
    main()
